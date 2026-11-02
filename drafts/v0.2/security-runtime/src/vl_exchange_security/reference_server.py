"""Reference online grant issuer for VL Exchange; not a production service.

The issuer public key and grant-server private key are provisioned separately.
The default HTTP server binds to loopback only. A production deployment needs
TLS, durable revocation storage, access controls, monitoring and review.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import ssl
import threading
import time
from typing import Callable

from nacl.signing import SigningKey, VerifyKey

from .protection import (
    AUDIENCE, GRANT_DOMAIN, GRANT_FORMAT, LICENSE_FORMAT, MAX_METADATA,
    REQUEST_DOMAIN, REQUEST_FORMAT, VERSION, AuthorizationError, _decode,
    _encode, _fail, _hex, _json_bytes, _json_object, _keys, _positive_time,
    _read_license, _sha, _sign, _verify,
)

REQUEST_KEYS = {"format", "version", "audience", "license_sha256", "license_id", "installation_id",
                "package_sha256", "recipient_key_sha256", "nonce", "issued_at"}


class ReferenceLicenseService:
    """Issue a short-lived grant only to a holder of the installation key."""

    def __init__(self, *, issuer_verify_key: VerifyKey, server_signing_key: SigningKey,
                 revoked: Callable[[str], bool] | None = None, clock: Callable[[], int] | None = None,
                 grant_ttl: int = 60, max_clock_skew: int = 30):
        if type(grant_ttl) is not int or not 1 <= grant_ttl <= 300:
            raise ValueError("grant_ttl must be 1..300 seconds")
        if type(max_clock_skew) is not int or not 0 <= max_clock_skew <= 120:
            raise ValueError("max_clock_skew must be 0..120 seconds")
        self.issuer_verify_key = issuer_verify_key
        self._server_signing_key = server_signing_key
        self.revoked = revoked or (lambda _: False)
        self.clock = clock or (lambda: int(time.time()))
        self.grant_ttl = grant_ttl
        self.max_clock_skew = max_clock_skew
        self._seen: dict[str, int] = {}
        self._lock = threading.Lock()

    def authorize(self, raw_body: bytes) -> bytes:
        body = _json_object(raw_body)
        _keys(body, {"license", "license_signature", "request", "request_signature"}, "online request body")
        raw_license = _decode(body["license"])
        license_signature = _decode(body["license_signature"], size=64)
        raw_request = _decode(body["request"])
        request_signature = _decode(body["request_signature"], size=64)
        now = int(self.clock())
        claims = _read_license(raw_license, license_signature, self.issuer_verify_key, now, expected_mode="online")
        if self.revoked(claims["license_id"]):
            _fail("revoked", "License is revoked")
        # The issuer-signed license binds the public request verification key.
        request_key = VerifyKey(_decode(claims["request_verify_key"], size=32))
        _verify(request_key, REQUEST_DOMAIN, raw_request, request_signature)
        request = _json_object(raw_request)
        _keys(request, REQUEST_KEYS, "signed online request")
        if request["format"] != REQUEST_FORMAT or request["version"] != VERSION or request["audience"] != AUDIENCE:
            _fail("invalid_request", "Unsupported request format, version or audience")
        if request["license_sha256"] != _sha(raw_license):
            _fail("invalid_request", "License digest does not match the request")
        for key in ("license_id", "installation_id", "recipient_key_sha256", "package_sha256"):
            if request[key] != claims[key]:
                _fail("invalid_request", "Request is bound to another installation or package")
        _hex(request["nonce"])
        _positive_time(request["issued_at"])
        if abs(now - request["issued_at"]) > self.max_clock_skew:
            _fail("invalid_request", "Signed request is stale or ahead of the trusted server clock")
        replay_key = hashlib.sha256((claims["license_id"] + ":" + request["nonce"]).encode()).hexdigest()
        with self._lock:
            self._seen = {key: when for key, when in self._seen.items() if now - when <= 2 * self.max_clock_skew + 1}
            if replay_key in self._seen:
                _fail("replay", "This signed challenge was already used")
            if len(self._seen) >= 100_000:
                _fail("overloaded", "Reference replay cache is full")
            self._seen[replay_key] = now
        expiry = min(now + self.grant_ttl, claims["expires_at"])
        if expiry <= now:
            _fail("license_expired", "License expired before a grant could be issued")
        grant = {
            "format": GRANT_FORMAT, "version": VERSION, "audience": AUDIENCE,
            "license_sha256": _sha(raw_license), "license_id": claims["license_id"],
            "installation_id": claims["installation_id"], "recipient_key_sha256": claims["recipient_key_sha256"],
            "package_sha256": claims["package_sha256"], "nonce": request["nonce"],
            "issued_at": now, "expires_at": expiry, "permissions": ["execute"],
        }
        raw_grant = _json_bytes(grant)
        return _json_bytes({"grant": _encode(raw_grant),
                            "signature": _encode(_sign(self._server_signing_key, GRANT_DOMAIN, raw_grant))})


def make_server(service: ReferenceLicenseService, host: str = "127.0.0.1", port: int = 0,
                *, ssl_context: ssl.SSLContext | None = None) -> ThreadingHTTPServer:
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback and ssl_context is None:
        raise ValueError("Non-loopback listener requires a server TLS context")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            # Requests contain entitlements; do not log bodies or credentials.
            return

        def do_POST(self):
            if self.path != "/v1/grant":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0 or length > MAX_METADATA:
                    raise AuthorizationError("invalid_request", "Invalid request size")
                reply = service.authorize(self.rfile.read(length))
            except (AuthorizationError, ValueError, OSError):
                reply = _json_bytes({"error": "authorization_denied"})
                self.send_response(403)
            except Exception:
                # Revocation storage and unexpected infrastructure errors fail closed.
                reply = _json_bytes({"error": "authorization_unavailable"})
                self.send_response(503)
            else:
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(reply)

    server = ThreadingHTTPServer((host, port), Handler)
    if ssl_context is not None:
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    return server


def _revocations(path: Path | None):
    def is_revoked(license_id: str) -> bool:
        if path is None:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("Revocations must be a JSON array of license IDs")
        return license_id in value
    return is_revoked


def main():
    parser = argparse.ArgumentParser(description="VL Exchange reference online license service")
    parser.add_argument("--issuer-public-key", type=Path, required=True, help="32 raw bytes; trusted publisher verification key")
    parser.add_argument("--server-private-key", type=Path, required=True, help="32 raw bytes; grant signing seed, kept private")
    parser.add_argument("--revocations", type=Path, help="Trusted JSON list of revoked license IDs, read on each request")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    args = parser.parse_args()
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("Both --tls-cert and --tls-key are required")
    context = None
    if args.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
    service = ReferenceLicenseService(issuer_verify_key=VerifyKey(args.issuer_public_key.read_bytes()),
                                      server_signing_key=SigningKey(args.server_private_key.read_bytes()),
                                      revoked=_revocations(args.revocations))
    server = make_server(service, args.host, args.port, ssl_context=context)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
