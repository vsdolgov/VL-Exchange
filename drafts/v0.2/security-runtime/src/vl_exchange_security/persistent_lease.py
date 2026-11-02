"""Durable, operation-bound execution grants for trusted asynchronous hosts.

The persisted value contains signed license/grant *public* bytes, never any
private key or bearer token. It is signed again by the installation's private
Ed25519 key with an application-specific domain to bind it to one operation.
Trust anchors are resolved exclusively from the already-authorized host, not
from the persisted record. A grant renewal never extends an old snapshot.

Storage integrity, clock rollback protection, process isolation and atomic
binding of the exact snapshot to the job/attempt are host responsibilities.
"""
from __future__ import annotations

from nacl.signing import SigningKey, VerifyKey

from .protection import (
    AuthorizationError, AuthorizedPackage, _decode, _encode, _fail, _json_bytes,
    _json_object, _keys, _positive_time, _read_license, _sign, _verify, _verify_grant,
)

SNAPSHOT_FORMAT = "vl.exchange.execution-lease"
SNAPSHOT_VERSION = "0.1.0"
SNAPSHOT_DOMAIN = b"VL-EXCHANGE-PERSISTENT-LEASE-V1\0"
MAX_SNAPSHOT_BYTES = 256 * 1024
FIELDS = {"format", "version", "binding", "mode", "license", "license_signature", "grant", "signature"}
GRANT_FIELDS = {"raw", "signature", "nonce"}


def _binding(value: object) -> str:
    if type(value) is not str or not value or len(value) > 512:
        _fail("invalid_binding", "A bounded nonempty operation binding is required")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise AuthorizationError("invalid_binding", "Operation binding must contain Unicode scalar values") from exc
    return value


def _copy_snapshot(snapshot: object) -> dict:
    if type(snapshot) is not dict:
        _fail("invalid_lease", "Persistent lease must be a JSON object")
    try:
        raw = _json_bytes(snapshot)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise AuthorizationError("invalid_lease", "Invalid persistent lease JSON") from exc
    if len(raw) > MAX_SNAPSHOT_BYTES:
        _fail("invalid_lease", "Persistent lease exceeds size limit")
    # Parsing the detached canonical bytes also excludes custom Python types.
    return _json_object(raw)


def capture_persistent_lease(authorized: AuthorizedPackage, binding: str,
                             installation_signing_key: SigningKey) -> dict:
    """Obtain a fresh lease and sign its immutable payload for this operation."""
    if not isinstance(authorized, AuthorizedPackage):
        _fail("invalid_lease", "An active authorized package is required")
    binding = _binding(binding)
    if not isinstance(installation_signing_key, SigningKey):
        _fail("installation_mismatch", "Host installation signing key is required")
    lease = authorized.before_process()
    claims = _read_license(lease._license_raw, lease._license_signature, lease._issuer_key,
                           int(lease._clock()), expected_mode=lease._mode)
    if bytes(installation_signing_key.verify_key) != _decode(claims["request_verify_key"], size=32):
        _fail("installation_mismatch", "Signing key is not bound to this licensed installation")
    grant = None
    if lease._mode == "online":
        if lease._grant is None:
            _fail("online_denied", "An online grant is required")
        raw, signature, nonce = lease._grant
        grant = {"raw": _encode(raw), "signature": _encode(signature), "nonce": nonce}
    payload = {"format": SNAPSHOT_FORMAT, "version": SNAPSHOT_VERSION, "binding": binding,
               "mode": lease._mode, "license": _encode(lease._license_raw),
               "license_signature": _encode(lease._license_signature), "grant": grant}
    snapshot = {**payload, "signature": _encode(_sign(installation_signing_key, SNAPSHOT_DOMAIN,
                                                       _json_bytes(payload)))}
    # Expiry or invalidation during signing must not produce an accepted lease.
    lease.assert_current()
    assert_persistent_lease(authorized, snapshot, binding)
    return snapshot


def assert_persistent_lease(authorized: AuthorizedPackage, snapshot: dict, binding: str) -> None:
    """Verify stored bytes with host-configured keys and the original validity.

    The caller must provide the operation binding reconstructed from trusted
    job/attempt identity, not one taken from this snapshot. The active owner
    supplies the configured publisher/server keys and recipient/license binding.
    """
    _verify_persistent_lease(authorized, snapshot, binding, historical=False)


def assert_persistent_lease_integrity(authorized: AuthorizedPackage, snapshot: dict, binding: str) -> None:
    """Verify historical signed provenance; never use it to authorize a call."""
    _verify_persistent_lease(authorized, snapshot, binding, historical=True)


def _verify_persistent_lease(authorized: AuthorizedPackage, snapshot: dict, binding: str, *, historical: bool) -> None:
    if not isinstance(authorized, AuthorizedPackage):
        _fail("invalid_lease", "An active authorized package is required")
    binding = _binding(binding)
    record = _copy_snapshot(snapshot)
    _keys(record, FIELDS, "persistent lease")
    if (record["format"] != SNAPSHOT_FORMAT or record["version"] != SNAPSHOT_VERSION
            or record["binding"] != binding):
        _fail("invalid_lease", "Persistent lease format or operation binding mismatch")
    if record["mode"] not in ("offline", "online") or record["mode"] != authorized.claims.get("mode"):
        _fail("policy_mismatch", "Persistent lease mode differs from host policy")
    # Current trusted owner also enforces closure and fail-closed renewal/revocation
    # policy. It may hold a newer grant; the original one is checked below.
    authorized.assert_current()
    license_raw = _decode(record["license"])
    license_signature = _decode(record["license_signature"], size=64)
    claims = _read_license(license_raw, license_signature, authorized._issuer_key,
                           int(authorized._clock()), expected_mode=record["mode"])
    if (license_raw != authorized._license_raw or license_signature != authorized._license_signature
            or claims != authorized.claims or claims["package_sha256"] != authorized.package_sha256):
        _fail("invalid_lease", "Persistent lease is bound to a different licensed package or installation")
    payload = {k: v for k, v in record.items() if k != "signature"}
    signing_key = VerifyKey(_decode(claims["request_verify_key"], size=32))
    _verify(signing_key, SNAPSHOT_DOMAIN, _json_bytes(payload), _decode(record["signature"], size=64))
    if record["mode"] == "offline":
        if record["grant"] is not None:
            _fail("invalid_lease", "Offline lease must not contain an online grant")
        return
    grant = record["grant"]
    if type(grant) is not dict or authorized._authorizer is None or authorized._grant is None:
        _fail("online_denied", "Original online grant and configured server trust key are required")
    _keys(grant, GRANT_FIELDS, "persistent online grant")
    nonce = grant["nonce"]
    if type(nonce) is not str or len(nonce) != 64 or any(c not in "0123456789abcdef" for c in nonce):
        _fail("invalid_lease", "Invalid original grant nonce")
    raw = _decode(grant["raw"])
    current_time = int(authorized._clock())
    verification_time = current_time
    if historical:
        # Verify the original server-signed grant at its issued-at boundary,
        # not at current time. This demonstrates grant structure/signatures,
        # but does not by itself attest when the host signed the snapshot.
        verification_time = _json_object(raw).get("issued_at")
        _positive_time(verification_time)
        if verification_time > current_time:
            _fail("invalid_lease", "Historical grant cannot be issued in the future")
        _read_license(license_raw, license_signature, authorized._issuer_key,
                      verification_time, expected_mode=record["mode"])
    _verify_grant(raw, _decode(grant["signature"], size=64),
                  authorized._authorizer.server_verify_key, license_raw, claims, nonce, verification_time)
