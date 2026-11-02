"""Verified-before-use recipient encryption and offline/online entitlements.

The issuer and online-service trust keys are supplied by the operator, not by
the package. Python's immutable plaintext bytes cannot be securely erased;
run this in a trusted process with a private tmpfs where that matters.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import ssl
import stat
import tempfile
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from nacl.exceptions import BadSignatureError, CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.signing import SigningKey, VerifyKey

LICENSE_DOMAIN = b"VL-EXCHANGE-LICENSE-V1\0"
REQUEST_DOMAIN = b"VL-EXCHANGE-REQUEST-V1\0"
GRANT_DOMAIN = b"VL-EXCHANGE-GRANT-V1\0"
ENVELOPE_FORMAT = "vl.exchange.protected"
LICENSE_FORMAT = "vl.exchange.license"
REQUEST_FORMAT = "vl.exchange.online-request"
GRANT_FORMAT = "vl.exchange.online-grant"
VERSION = "0.1.0"
AUDIENCE = "vl.exchange.runtime"
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_ENTRIES = 1024
MAX_METADATA = 1024 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class AuthorizationError(Exception):
    """Fail-closed protection error. Never include secrets in messages."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _fail(code: str, message: str):
    raise AuthorizationError(code, message)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decode(value: object, *, size: int | None = None) -> bytes:
    if not isinstance(value, str) or len(value) > 2 * MAX_METADATA:
        _fail("invalid_encoding", "Invalid base64 field")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise AuthorizationError("invalid_encoding", "Invalid base64 field") from exc
    if size is not None and len(data) != size:
        _fail("invalid_encoding", "Unexpected decoded length")
    return data


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)).encode("utf-8")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json_object(raw: bytes) -> dict:
    if len(raw) > MAX_METADATA:
        _fail("invalid_json", "Metadata exceeds the reference implementation limit")
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise AuthorizationError("invalid_json", "Invalid strict JSON metadata") from exc
    if not isinstance(obj, dict):
        _fail("invalid_json", "Expected a JSON object")
    return obj


def _keys(obj: dict, required: set[str], where: str):
    if set(obj) != required:
        _fail("invalid_fields", f"Unexpected or missing {where} fields")


def _id(value: object):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail("invalid_fields", "Invalid identifier")


def _hex(value: object):
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        _fail("invalid_fields", "Expected lowercase SHA-256")


def _positive_time(value: object):
    if type(value) is not int or value < 0 or value > 253402300799:
        _fail("invalid_fields", "Expected a valid UTC Unix timestamp")


def _sign(key: SigningKey, domain: bytes, raw: bytes) -> bytes:
    return key.sign(domain + raw).signature


def _verify(key: VerifyKey, domain: bytes, raw: bytes, signature: bytes):
    try:
        key.verify(domain + raw, signature)
    except (BadSignatureError, ValueError) as exc:
        raise AuthorizationError("invalid_signature", "Signature does not match the configured trust key") from exc


def _manifest_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        _fail("unsafe_archive", "Manifest path must be relative")
    path = PurePosixPath(value)
    if any(part in ("", ".", "..") for part in value.split("/")) or str(path) != value:
        _fail("unsafe_archive", "Invalid manifest path")
    return value


def _archive_files(archive: bytes) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]:
    if not archive or len(archive) > MAX_ARCHIVE:
        _fail("archive_limit", "Encrypted package exceeds the reference archive limit")
    try:
        zf = zipfile.ZipFile(io.BytesIO(archive))
    except (zipfile.BadZipFile, OSError) as exc:
        raise AuthorizationError("unsafe_archive", "Invalid package ZIP") from exc
    files = {}
    seen = set()
    total = 0
    try:
        entries = zf.infolist()
        if len(entries) > MAX_ENTRIES:
            _fail("archive_limit", "Too many package entries")
        for item in entries:
            name = item.filename[:-1] if item.is_dir() and item.filename.endswith("/") else item.filename
            _manifest_path(name)
            # On case-insensitive filesystems, names must not overwrite each other.
            canonical = name.casefold()
            if canonical in seen:
                _fail("unsafe_archive", "Duplicate or case-colliding ZIP entry")
            seen.add(canonical)
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode) or item.flag_bits & 1:
                _fail("unsafe_archive", "Symlink and encrypted ZIP entries are unsupported")
            if not item.is_dir():
                total += item.file_size
                if total > MAX_EXPANDED:
                    _fail("archive_limit", "Expanded package exceeds the reference implementation limit")
                files[name] = item
        return zf, files
    except Exception:
        zf.close()
        raise


def _check_archive(archive: bytes, manifest_path: str, manifest_sha256: str | None = None,
                   package_format: str | None = None) -> tuple[str, str]:
    manifest_path = _manifest_path(manifest_path)
    zf, files = _archive_files(archive)
    try:
        if manifest_path not in files or files[manifest_path].file_size > MAX_METADATA:
            _fail("unsafe_archive", "The signed package manifest is missing or too large")
        raw = zf.read(files[manifest_path])
        digest = _sha(raw)
        if manifest_sha256 is not None and digest != manifest_sha256:
            _fail("package_mismatch", "Package manifest digest differs from the license")
        manifest = _json_object(raw)
        actual_format = manifest.get("format")
        if not isinstance(actual_format, str) or not re.fullmatch(r"vl\.[a-z][a-z0-9.-]{0,80}", actual_format):
            _fail("invalid_fields", "Manifest has no supported VL format identifier")
        if package_format is not None and actual_format != package_format:
            _fail("package_mismatch", "Package format differs from the license")
        return digest, actual_format
    finally:
        zf.close()


LICENSE_KEYS = {"format", "version", "audience", "issuer_id", "license_id", "installation_id",
                "recipient_key_sha256", "request_verify_key", "package_sha256", "manifest_path",
                "manifest_sha256", "package_format", "mode", "issued_at", "not_before", "expires_at", "permissions"}


def _read_license(raw: bytes, signature: bytes, issuer_key: VerifyKey, current: int,
                  *, expected_mode: str | None = None) -> dict:
    _verify(issuer_key, LICENSE_DOMAIN, raw, signature)
    claims = _json_object(raw)
    _keys(claims, LICENSE_KEYS, "license")
    if claims["format"] != LICENSE_FORMAT or claims["version"] != VERSION or claims["audience"] != AUDIENCE:
        _fail("invalid_license", "Unsupported license format, version or audience")
    for key in ("issuer_id", "license_id", "installation_id"):
        _id(claims[key])
    for key in ("recipient_key_sha256", "package_sha256", "manifest_sha256"):
        _hex(claims[key])
    _decode(claims["request_verify_key"], size=32)
    _manifest_path(claims["manifest_path"])
    if not isinstance(claims["package_format"], str) or not re.fullmatch(r"vl\.[a-z][a-z0-9.-]{0,80}", claims["package_format"]):
        _fail("invalid_license", "Unsupported payload format identifier")
    if claims["mode"] not in ("offline", "online") or (expected_mode is not None and claims["mode"] != expected_mode):
        _fail("policy_mismatch", "The signed license does not authorize the requested mode")
    if claims["permissions"] != ["execute"]:
        _fail("unauthorized", "License does not authorize execution")
    for key in ("issued_at", "not_before", "expires_at"):
        _positive_time(claims[key])
    if not claims["issued_at"] <= claims["not_before"] < claims["expires_at"]:
        _fail("invalid_license", "Invalid license validity interval")
    if current < claims["issued_at"] or current < claims["not_before"] or current >= claims["expires_at"]:
        _fail("license_expired", "License is not currently valid")
    return claims


def seal_package(package_zip: Path, output_dir: Path, *, issuer_signing_key: SigningKey,
                 recipient_public_key: PublicKey, installation_verify_key: VerifyKey,
                 issuer_id: str, license_id: str, installation_id: str, mode: str,
                 issued_at: int, not_before: int, expires_at: int,
                 manifest_path: str = "block-package.json") -> dict:
    """Issue one signed entitlement and encrypt the exact ZIP for one recipient."""
    package_zip, output_dir = Path(package_zip), Path(output_dir)
    with package_zip.open("rb") as stream:
        archive = stream.read(MAX_ARCHIVE + 1)
    manifest_sha, payload_format = _check_archive(archive, manifest_path)
    claims = {
        "format": LICENSE_FORMAT, "version": VERSION, "audience": AUDIENCE,
        "issuer_id": issuer_id, "license_id": license_id, "installation_id": installation_id,
        "recipient_key_sha256": _sha(bytes(recipient_public_key)),
        "request_verify_key": _encode(bytes(installation_verify_key)),
        "package_sha256": _sha(archive), "manifest_path": manifest_path,
        "manifest_sha256": manifest_sha, "package_format": payload_format, "mode": mode,
        "issued_at": issued_at, "not_before": not_before, "expires_at": expires_at,
        "permissions": ["execute"],
    }
    raw = _json_bytes(claims)
    signature = _sign(issuer_signing_key, LICENSE_DOMAIN, raw)
    _read_license(raw, signature, issuer_signing_key.verify_key, not_before, expected_mode=mode)
    ciphertext = SealedBox(recipient_public_key).encrypt(archive)
    envelope = {"format": ENVELOPE_FORMAT, "version": VERSION, "license": _encode(raw),
                "license_signature": _encode(signature), "ciphertext_file": "package.sealed",
                "ciphertext_sha256": _sha(ciphertext)}
    # Never replace an existing license or recipient ciphertext accidentally.
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    try:
        (output_dir / "package.sealed").write_bytes(ciphertext)
        (output_dir / "envelope.json").write_bytes(_json_bytes(envelope) + b"\n")
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    return {"format": ENVELOPE_FORMAT, "mode": mode, "license_id": license_id,
            "package_sha256": claims["package_sha256"], "manifest_sha256": manifest_sha,
            "ciphertext_sha256": envelope["ciphertext_sha256"]}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _fail("online_denied", "License-server redirects are forbidden")


def _check_endpoint(url: str, allow_loopback_http: bool):
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
        _fail("invalid_endpoint", "Invalid operator-configured license-server URL")
    if parsed.scheme == "https":
        return
    is_loopback = parsed.hostname == "localhost"
    try:
        is_loopback = is_loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme == "http" and allow_loopback_http and is_loopback:
        return
    _fail("invalid_endpoint", "HTTPS is required except explicitly enabled loopback tests")


GRANT_KEYS = {"format", "version", "audience", "license_sha256", "license_id", "installation_id",
              "recipient_key_sha256", "package_sha256", "nonce", "issued_at", "expires_at", "permissions"}


def _verify_grant(raw: bytes, signature: bytes, server_key: VerifyKey, license_raw: bytes,
                  claims: dict, nonce: str, now: int) -> dict:
    _verify(server_key, GRANT_DOMAIN, raw, signature)
    grant = _json_object(raw)
    _keys(grant, GRANT_KEYS, "online grant")
    if grant["format"] != GRANT_FORMAT or grant["version"] != VERSION or grant["audience"] != AUDIENCE:
        _fail("invalid_grant", "Unsupported grant format, version or audience")
    for key in ("license_id", "installation_id", "recipient_key_sha256", "package_sha256"):
        if grant[key] != claims[key]:
            _fail("invalid_grant", "Grant is bound to another license, installation or package")
    if grant["license_sha256"] != _sha(license_raw) or grant["nonce"] != nonce or grant["permissions"] != ["execute"]:
        _fail("invalid_grant", "Grant has incorrect license digest, challenge or permissions")
    _positive_time(grant["issued_at"])
    _positive_time(grant["expires_at"])
    if not grant["issued_at"] <= now < grant["expires_at"] <= claims["expires_at"]:
        _fail("grant_expired", "Online grant is not currently valid")
    if grant["expires_at"] - grant["issued_at"] > 300:
        _fail("invalid_grant", "Online grant exceeds the maximum lifetime")
    return grant


@dataclass
class OnlineAuthorizer:
    endpoint: str
    server_verify_key: VerifyKey
    installation_signing_key: SigningKey = field(repr=False)
    allow_loopback_http: bool = False
    timeout: float = 5.0
    ssl_context: ssl.SSLContext | None = None
    auto_renew: bool = True
    renew_before_seconds: int = 10

    def authorize(self, *, license_raw: bytes, license_signature: bytes, claims: dict,
                  now: int) -> tuple[bytes, bytes, str]:
        _check_endpoint(self.endpoint, self.allow_loopback_http)
        if bytes(self.installation_signing_key.verify_key) != _decode(claims["request_verify_key"], size=32):
            _fail("installation_mismatch", "Request key does not match the licensed installation")
        nonce = secrets.token_hex(32)
        request_payload = _json_bytes({"format": REQUEST_FORMAT, "version": VERSION, "audience": AUDIENCE,
             "license_sha256": _sha(license_raw), "license_id": claims["license_id"],
             "installation_id": claims["installation_id"], "package_sha256": claims["package_sha256"],
             "recipient_key_sha256": claims["recipient_key_sha256"], "nonce": nonce, "issued_at": now})
        request_body = _json_bytes({"license": _encode(license_raw), "license_signature": _encode(license_signature),
                                    "request": _encode(request_payload),
                                    "request_signature": _encode(_sign(self.installation_signing_key, REQUEST_DOMAIN, request_payload))})
        handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]
        if urllib.parse.urlsplit(self.endpoint).scheme == "https":
            context = self.ssl_context or ssl.create_default_context()
            if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
                _fail("invalid_endpoint", "TLS must verify the certificate and server hostname")
            handlers.append(urllib.request.HTTPSHandler(context=context))
        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(self.endpoint, data=request_body, method="POST",
                                         headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    _fail("online_denied", "License server denied the request")
                body = response.read(MAX_METADATA + 1)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise AuthorizationError("online_unavailable", "License server unavailable or denied authorization") from exc
        data = _json_object(body)
        _keys(data, {"grant", "signature"}, "online response")
        raw, signature = _decode(data["grant"]), _decode(data["signature"], size=64)
        _verify_grant(raw, signature, self.server_verify_key, license_raw, claims, nonce, now)
        return raw, signature, nonce


@dataclass
class AuthorizedPackage:
    root: Path
    claims: dict
    package_sha256: str
    _license_raw: bytes = field(repr=False)
    _license_signature: bytes = field(repr=False)
    _issuer_key: VerifyKey = field(repr=False)
    _clock: Callable[[], int] = field(repr=False)
    _authorizer: OnlineAuthorizer | None = field(repr=False, default=None)
    _grant: tuple[bytes, bytes, str] | None = field(repr=False, default=None)
    _active: bool = field(default=True, repr=False)

    def capture_persistent_lease(self, binding: str, installation_signing_key: SigningKey) -> dict:
        """Sign an immutable per-operation lease for a trusted durable queue.

        Only signed public authorization data is serialized; the installation
        private key remains with the host. A new lease must be captured at
        dispatch, not only when a potentially long-waiting job is enqueued.
        """
        from .persistent_lease import capture_persistent_lease
        return capture_persistent_lease(self, binding, installation_signing_key)

    def assert_persistent_lease(self, snapshot: dict, binding: str) -> None:
        """Verify that the original signed grant and operation binding remain valid."""
        from .persistent_lease import assert_persistent_lease
        assert_persistent_lease(self, snapshot, binding)

    def assert_persistent_lease_integrity(self, snapshot: dict, binding: str) -> None:
        """Check a historical snapshot's signatures, without authorizing its use.

        A queued job may outlive its enqueue grant. This method validates its
        saved binding and signed history, not permission to execute: dispatch
        and result commit still require their own *currently valid* lease.
        """
        from .persistent_lease import assert_persistent_lease_integrity
        assert_persistent_lease_integrity(self, snapshot, binding)

    def assert_current(self):
        if not self._active:
            _fail("authorization_closed", "Authorized package is already closed")
        now = int(self._clock())
        expected = _read_license(self._license_raw, self._license_signature, self._issuer_key, now,
                                 expected_mode=self.claims["mode"])
        if expected != self.claims:
            _fail("invalid_license", "Authorized license claims were modified")
        if expected["mode"] == "online":
            if self._authorizer is None or self._grant is None:
                _fail("online_denied", "A valid online grant is required")
            raw, signature, nonce = self._grant
            _verify_grant(raw, signature, self._authorizer.server_verify_key, self._license_raw, expected, nonce, now)

    def renew(self):
        if not self._active:
            _fail("authorization_closed", "Authorized package is already closed")
        now = int(self._clock())
        claims = _read_license(self._license_raw, self._license_signature, self._issuer_key, now,
                               expected_mode="online")
        if self._authorizer is None:
            _fail("online_denied", "Online authorizer is not configured")
        # A failed renewal invalidates the previous grant: no silent fallback.
        self._grant = None
        self._grant = self._authorizer.authorize(license_raw=self._license_raw,
                                                 license_signature=self._license_signature,
                                                 claims=claims, now=now)
        self.assert_current()

    def before_process(self):
        """Refresh a nearly expired online grant before, never after, a call."""
        if not self._active:
            _fail("authorization_closed", "Authorized package is already closed")
        if self.claims["mode"] == "online" and self._authorizer is not None and self._authorizer.auto_renew:
            margin = self._authorizer.renew_before_seconds
            if type(margin) is not int or not 0 <= margin <= 300:
                _fail("invalid_grant", "Invalid online renewal margin")
            if self._grant is None:
                self.renew()
            else:
                grant = _json_object(self._grant[0])
                expiry = grant.get("expires_at")
                _positive_time(expiry)
                if int(self._clock()) >= expiry - margin:
                    self.renew()
        # The post-call guard must use assert_current(), without renewal: an
        # expired in-flight call is never committed using a later grant.
        self.assert_current()
        lease = ExecutionLease(self, self._license_raw, self._license_signature,
                               self._issuer_key, self._clock, self.claims["mode"],
                               self._authorizer.server_verify_key if self._authorizer else None,
                               self._grant)
        lease.assert_current()
        return lease


@dataclass(frozen=True)
class ExecutionLease:
    """Signed authorization snapshot for one call, even across renewal."""
    _owner: AuthorizedPackage = field(repr=False)
    _license_raw: bytes = field(repr=False)
    _license_signature: bytes = field(repr=False)
    _issuer_key: VerifyKey = field(repr=False)
    _clock: Callable[[], int] = field(repr=False)
    _mode: str
    _server_key: VerifyKey | None = field(repr=False)
    _grant: tuple[bytes, bytes, str] | None = field(repr=False)

    def assert_current(self):
        if not self._owner._active:
            _fail("authorization_closed", "Authorized package is already closed")
        now = int(self._clock())
        claims = _read_license(self._license_raw, self._license_signature, self._issuer_key, now,
                               expected_mode=self._mode)
        if claims != self._owner.claims:
            _fail("invalid_license", "Authorized license claims were modified")
        if self._mode == "online":
            if self._owner._grant is None:
                _fail("online_denied", "Online authorization was invalidated during the call")
            if self._server_key is None or self._grant is None:
                _fail("online_denied", "A valid online grant is required")
            raw, signature, nonce = self._grant
            _verify_grant(raw, signature, self._server_key, self._license_raw, claims, nonce, now)


@contextmanager
def authorized_package(envelope_path: Path, *, issuer_verify_key: VerifyKey,
                       recipient_private_key: PrivateKey, policy: str,
                       online_authorizer: OnlineAuthorizer | None = None,
                       now: Callable[[], int] | None = None):
    """Authorize then decrypt into a private temporary directory, with cleanup."""
    root = Path(envelope_path)
    if root.is_symlink() or not root.is_dir():
        _fail("invalid_envelope", "Protected envelope directory is invalid")
    descriptor = root / "envelope.json"
    encrypted = root / "package.sealed"
    if descriptor.is_symlink() or encrypted.is_symlink() or not descriptor.is_file() or not encrypted.is_file():
        _fail("invalid_envelope", "Required envelope files are missing or symlinks")
    if descriptor.stat().st_size > MAX_METADATA or encrypted.stat().st_size > MAX_ARCHIVE + 48:
        _fail("archive_limit", "Protected envelope exceeds reference implementation limits")
    with descriptor.open("rb") as stream:
        envelope = _json_object(stream.read(MAX_METADATA + 1))
    _keys(envelope, {"format", "version", "license", "license_signature", "ciphertext_file", "ciphertext_sha256"}, "envelope")
    if envelope["format"] != ENVELOPE_FORMAT or envelope["version"] != VERSION or envelope["ciphertext_file"] != "package.sealed":
        _fail("invalid_envelope", "Unsupported protected envelope")
    if policy not in ("offline", "online"):
        _fail("policy_mismatch", "Operator must explicitly select offline or online policy")
    raw = _decode(envelope["license"])
    signature = _decode(envelope["license_signature"], size=64)
    clock = now or (lambda: int(time.time()))
    current = int(clock())
    claims = _read_license(raw, signature, issuer_verify_key, current, expected_mode=policy)
    if _sha(bytes(recipient_private_key.public_key)) != claims["recipient_key_sha256"]:
        _fail("installation_mismatch", "The recipient key is not licensed for this installation")
    grant = None
    if policy == "online":
        if online_authorizer is None:
            _fail("online_denied", "Online authorization is required")
        grant = online_authorizer.authorize(license_raw=raw, license_signature=signature, claims=claims, now=current)
        grant_raw, grant_signature, grant_nonce = grant
        # Verify again at the trust boundary, before decrypting any source code.
        _verify_grant(grant_raw, grant_signature, online_authorizer.server_verify_key,
                      raw, claims, grant_nonce, current)
    _hex(envelope["ciphertext_sha256"])
    with encrypted.open("rb") as stream:
        ciphertext = stream.read(MAX_ARCHIVE + 49)
    if len(ciphertext) > MAX_ARCHIVE + 48:
        _fail("archive_limit", "Protected payload exceeds the reference implementation limit")
    if _sha(ciphertext) != envelope["ciphertext_sha256"]:
        _fail("ciphertext_mismatch", "Encrypted payload digest mismatch")
    try:
        archive = SealedBox(recipient_private_key).decrypt(ciphertext)
    except CryptoError as exc:
        raise AuthorizationError("decrypt_failed", "Cannot decrypt protected package for this installation") from exc
    if _sha(archive) != claims["package_sha256"]:
        _fail("package_mismatch", "Decrypted ZIP digest differs from the signed license")
    _check_archive(archive, claims["manifest_path"], claims["manifest_sha256"], claims["package_format"])
    zf, files = _archive_files(archive)
    temporary = tempfile.TemporaryDirectory(prefix="vl-exchange-protected-")
    authorized = None
    try:
        target = Path(temporary.name)
        os.chmod(target, 0o700)
        for name, info in files.items():
            dest = target.joinpath(*PurePosixPath(name).parts)
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with dest.open("xb") as stream, zf.open(info) as source:
                shutil.copyfileobj(source, stream, length=1024 * 1024)
            os.chmod(dest, 0o600)
        zf.close()
        authorized = AuthorizedPackage(target, claims, claims["package_sha256"], raw, signature,
                                       issuer_verify_key, clock, online_authorizer, grant)
        authorized.assert_current()
        yield authorized
    finally:
        if authorized is not None:
            authorized._active = False
        zf.close()
        temporary.cleanup()
