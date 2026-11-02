"""Meaningful tests of the reusable VL Exchange protection profile."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import io
import json
from pathlib import Path
import ssl
import threading
import zipfile

from nacl.public import PrivateKey
from nacl.signing import SigningKey
import pytest

from vl_exchange_security import AuthorizationError, OnlineAuthorizer, authorized_package, seal_package
from vl_exchange_security.protection import (
    AUDIENCE, GRANT_DOMAIN, REQUEST_DOMAIN, REQUEST_FORMAT, VERSION, _encode, _json_bytes, _sha, _sign,
)
from vl_exchange_security.reference_server import ReferenceLicenseService, make_server


class Clock:
    def __init__(self):
        self.value = 1_800_000_000

    def __call__(self):
        return self.value


@pytest.fixture
def keys():
    return {"issuer": SigningKey.generate(), "server": SigningKey.generate(),
            "recipient": PrivateKey.generate(), "installation": SigningKey.generate()}


@pytest.fixture
def package(tmp_path):
    path = tmp_path / "input.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("scenario-package.json", json.dumps({"format": "vl.scenario-package", "spec_version": "0.1.0"}))
        z.writestr("code/scenario.py", "# immutable test fixture; never executed\n")
    return path


def issue(tmp_path, package, keys, clock, mode="offline", name="sealed"):
    output = tmp_path / name
    metadata = seal_package(package, output, issuer_signing_key=keys["issuer"],
        recipient_public_key=keys["recipient"].public_key,
        installation_verify_key=keys["installation"].verify_key,
        issuer_id="test-issuer", license_id=f"license-{mode}", installation_id="test-installation",
        mode=mode, issued_at=clock(), not_before=clock(), expires_at=clock() + 1000,
        manifest_path="scenario-package.json")
    return output, metadata


@contextmanager
def serve(service):
    server = make_server(service, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/grant"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def client(url, keys, server_key=None):
    return OnlineAuthorizer(url, server_key or keys["server"].verify_key,
                            keys["installation"], allow_loopback_http=True, timeout=2)


def test_offline_decrypt_integrity_expiry_and_cleanup(tmp_path, package, keys):
    clock = Clock()
    sealed, metadata = issue(tmp_path, package, keys, clock)
    with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                            recipient_private_key=keys["recipient"], policy="offline", now=clock) as auth:
        extracted = auth.root
        assert (extracted / "code/scenario.py").read_text().startswith("# immutable")
        assert auth.package_sha256 == _sha(package.read_bytes()) == metadata["package_sha256"]
        auth.assert_current()
        clock.value += 1000
        with pytest.raises(AuthorizationError, match="not currently valid") as err:
            auth.assert_current()
        assert err.value.code == "license_expired"
    assert not extracted.exists()
    with pytest.raises(AuthorizationError) as err:
        auth.assert_current()
    assert err.value.code == "authorization_closed"


def test_wrong_recipient_untrusted_issuer_tamper_and_mode_rejected(tmp_path, package, keys):
    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock)
    with pytest.raises(AuthorizationError) as err:
        with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                recipient_private_key=PrivateKey.generate(), policy="offline", now=clock):
            pass
    assert err.value.code == "installation_mismatch"
    with pytest.raises(AuthorizationError) as err:
        with authorized_package(sealed, issuer_verify_key=SigningKey.generate().verify_key,
                                recipient_private_key=keys["recipient"], policy="offline", now=clock):
            pass
    assert err.value.code == "invalid_signature"
    with pytest.raises(AuthorizationError) as err:
        with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                recipient_private_key=keys["recipient"], policy="online", now=clock):
            pass
    assert err.value.code == "policy_mismatch"
    blob = sealed / "package.sealed"
    data = bytearray(blob.read_bytes())
    data[-1] ^= 1
    blob.write_bytes(data)
    with pytest.raises(AuthorizationError) as err:
        with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                recipient_private_key=keys["recipient"], policy="offline", now=clock):
            pass
    assert err.value.code == "ciphertext_mismatch"


def test_license_cannot_be_extended_by_rewriting_unsigned_metadata(tmp_path, package, keys):
    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock)
    path = sealed / "envelope.json"
    envelope = json.loads(path.read_text())
    claims = json.loads(base64.b64decode(envelope["license"]))
    claims["expires_at"] += 86400
    envelope["license"] = _encode(_json_bytes(claims))  # original signature is not recomputed
    path.write_text(json.dumps(envelope))
    with pytest.raises(AuthorizationError) as err:
        with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                recipient_private_key=keys["recipient"], policy="offline", now=clock):
            pass
    assert err.value.code == "invalid_signature"


def test_online_grant_possession_expiry_renewal_and_revocation(tmp_path, package, keys):
    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")
    revoked = set()
    service = ReferenceLicenseService(issuer_verify_key=keys["issuer"].verify_key,
                                      server_signing_key=keys["server"], revoked=revoked.__contains__,
                                      clock=clock, grant_ttl=20)
    with serve(service) as url:
        online = client(url, keys)
        with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                recipient_private_key=keys["recipient"], policy="online",
                                online_authorizer=online, now=clock) as auth:
            auth.assert_current()
            clock.value += 19
            auth.assert_current()
            clock.value += 1
            with pytest.raises(AuthorizationError) as err:
                auth.assert_current()
            assert err.value.code == "grant_expired"
            auth.renew()
            auth.assert_current()
            revoked.add("license-online")
            with pytest.raises(AuthorizationError):
                auth.renew()
            with pytest.raises(AuthorizationError):
                auth.assert_current()  # failed renewal invalidates even an unexpired grant


def test_online_grant_valid_when_second_changes_during_request(tmp_path, package, keys):
    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")

    class TickBeforeIssue(ReferenceLicenseService):
        def authorize(self, body):
            clock.value += 1
            return super().authorize(body)

    service = TickBeforeIssue(issuer_verify_key=keys["issuer"].verify_key,
                              server_signing_key=keys["server"], clock=clock, grant_ttl=20)
    class LegacyAuthorizer(OnlineAuthorizer):
        # Existing subclasses only accepting the original keywords remain valid.
        def authorize(self, *, license_raw, license_signature, claims, now):
            return super().authorize(license_raw=license_raw, license_signature=license_signature,
                                     claims=claims, now=now)

    with serve(service) as url:
        online = LegacyAuthorizer(url, keys["server"].verify_key, keys["installation"], allow_loopback_http=True)
        with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                recipient_private_key=keys["recipient"], policy="online",
                                online_authorizer=online, now=clock) as auth:
            auth.assert_current()
            first_grant = auth._grant
            auth.renew()  # The same boundary is crossed during renewal.
            assert auth._grant != first_grant
            auth.assert_current()


def test_direct_authorize_with_historical_fixed_now(tmp_path, package, keys):
    from vl_exchange_security.protection import _read_license

    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")
    envelope = json.loads((sealed / "envelope.json").read_text())
    raw, signature = base64.b64decode(envelope["license"]), base64.b64decode(envelope["license_signature"])
    claims = _read_license(raw, signature, keys["issuer"].verify_key, clock(), expected_mode="online")
    service = ReferenceLicenseService(issuer_verify_key=keys["issuer"].verify_key,
                                      server_signing_key=keys["server"], clock=clock)
    with serve(service) as url:
        grant = client(url, keys).authorize(license_raw=raw, license_signature=signature,
                                            claims=claims, now=clock())
        assert len(grant) == 3


def test_direct_authorize_can_use_explicit_live_clock(tmp_path, package, keys):
    from vl_exchange_security.protection import _read_license

    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")
    envelope = json.loads((sealed / "envelope.json").read_text())
    raw, signature = base64.b64decode(envelope["license"]), base64.b64decode(envelope["license_signature"])
    claims = _read_license(raw, signature, keys["issuer"].verify_key, clock(), expected_mode="online")

    class TickBeforeIssue(ReferenceLicenseService):
        def authorize(self, body):
            clock.value += 1
            return super().authorize(body)

    service = TickBeforeIssue(issuer_verify_key=keys["issuer"].verify_key,
                              server_signing_key=keys["server"], clock=clock)
    with serve(service) as url:
        started = clock()
        grant = client(url, keys).authorize(license_raw=raw, license_signature=signature,
                                            claims=claims, now=started, clock=clock)
        assert len(grant) == 3
        assert clock() == started + 1


def test_custom_authorizer_type_error_propagates_unchanged(tmp_path, package, keys):
    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")

    class BrokenAuthorizer(OnlineAuthorizer):
        def authorize(self, *, license_raw, license_signature, claims, now):
            raise TypeError("custom authorizer failed")

    online = BrokenAuthorizer("http://127.0.0.1:1/v1/grant", keys["server"].verify_key,
                              keys["installation"], allow_loopback_http=True)
    with pytest.raises(TypeError, match="custom authorizer failed"):
        with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                recipient_private_key=keys["recipient"], policy="online",
                                online_authorizer=online, now=clock):
            pytest.fail("Broken authorizer must not authorize access")


def test_clock_rollback_after_response_rejected(tmp_path, package, keys, monkeypatch):
    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")

    class TickBeforeIssue(ReferenceLicenseService):
        def authorize(self, body):
            clock.value += 1
            return super().authorize(body)

    service = TickBeforeIssue(issuer_verify_key=keys["issuer"].verify_key,
                              server_signing_key=keys["server"], clock=clock)
    with serve(service) as url:
        online = client(url, keys)
        original = online.authorize

        def roll_back_after_response(**kwargs):
            grant = original(**kwargs)
            clock.value -= 1
            return grant

        monkeypatch.setattr(online, "authorize", roll_back_after_response)
        with pytest.raises(AuthorizationError) as err:
            with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                    recipient_private_key=keys["recipient"], policy="online",
                                    online_authorizer=online, now=clock):
                pytest.fail("Backward trusted clock must not authorize access")
        assert err.value.code == "invalid_grant"


def test_clock_rollback_during_decrypt_rejected_before_open(tmp_path, package, keys, monkeypatch):
    from vl_exchange_security import protection

    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock)
    ciphertext = (sealed / "package.sealed").read_bytes()
    real_sha, real_decrypt = protection._sha, protection.SealedBox.decrypt

    def hash_with_time(data):
        if data == ciphertext:
            clock.value += 1
        return real_sha(data)

    def rollback_while_decrypting(box, *args, **kwargs):
        result = real_decrypt(box, *args, **kwargs)
        clock.value -= 1
        return result

    monkeypatch.setattr(protection, "_sha", hash_with_time)
    monkeypatch.setattr(protection.SealedBox, "decrypt", rollback_while_decrypting)
    with pytest.raises(AuthorizationError) as err:
        with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                recipient_private_key=keys["recipient"], policy="offline", now=clock):
            pytest.fail("Rollback during decryption must not yield an authorized package")
    assert err.value.code == "invalid_grant"


@pytest.mark.parametrize("mode, advance, error_code", [("offline", 1000, "license_expired"),
                                                        ("online", 20, "grant_expired")])
def test_expiry_during_ciphertext_hash_rejected_before_decrypt(tmp_path, package, keys, monkeypatch,
                                                               mode, advance, error_code):
    from vl_exchange_security import protection

    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, mode)
    ciphertext = (sealed / "package.sealed").read_bytes()
    real_sha = protection._sha
    decrypted = []
    real_decrypt = protection.SealedBox.decrypt

    def slow_hash(data):
        if data == ciphertext:
            clock.value += advance
        return real_sha(data)

    def record_decrypt(box, *args, **kwargs):
        decrypted.append(True)
        return real_decrypt(box, *args, **kwargs)

    monkeypatch.setattr(protection, "_sha", slow_hash)
    monkeypatch.setattr(protection.SealedBox, "decrypt", record_decrypt)
    service = ReferenceLicenseService(issuer_verify_key=keys["issuer"].verify_key,
                                      server_signing_key=keys["server"], clock=clock, grant_ttl=20)
    with serve(service) as url:
        with pytest.raises(AuthorizationError) as err:
            with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                    recipient_private_key=keys["recipient"], policy=mode,
                                    online_authorizer=client(url, keys) if mode == "online" else None,
                                    now=clock):
                pytest.fail("Expired authorization must not allow decryption")
        assert err.value.code == error_code
        assert not decrypted


def test_online_grant_expiring_during_request_is_not_decrypted(tmp_path, package, keys, monkeypatch):
    from vl_exchange_security import protection

    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")

    class TickAfterIssue(ReferenceLicenseService):
        def authorize(self, body):
            response = super().authorize(body)
            clock.value += 20  # Exactly the grant TTL, while the response is in flight.
            return response

    service = TickAfterIssue(issuer_verify_key=keys["issuer"].verify_key,
                             server_signing_key=keys["server"], clock=clock, grant_ttl=20)
    decrypted = []
    real_decrypt = protection.SealedBox.decrypt

    def record_decrypt(box, *args, **kwargs):
        decrypted.append(True)
        return real_decrypt(box, *args, **kwargs)

    monkeypatch.setattr(protection.SealedBox, "decrypt", record_decrypt)
    with serve(service) as url:
        with pytest.raises(AuthorizationError) as err:
            with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                    recipient_private_key=keys["recipient"], policy="online",
                                    online_authorizer=client(url, keys), now=clock):
                pytest.fail("Expired grant must not authorize access")
        assert err.value.code == "grant_expired"
        assert not decrypted


def test_online_license_expiring_after_grant_is_not_decrypted(tmp_path, package, keys, monkeypatch):
    from vl_exchange_security import protection

    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")
    service = ReferenceLicenseService(issuer_verify_key=keys["issuer"].verify_key,
                                      server_signing_key=keys["server"], clock=clock)
    decrypted = []
    real_decrypt = protection.SealedBox.decrypt

    def record_decrypt(box, *args, **kwargs):
        decrypted.append(True)
        return real_decrypt(box, *args, **kwargs)

    monkeypatch.setattr(protection.SealedBox, "decrypt", record_decrypt)
    with serve(service) as url:
        online = client(url, keys)
        original = online.authorize

        def expire_before_decryption(**kwargs):
            grant = original(**kwargs)
            clock.value += 1000  # License expires after authorize, before the trust boundary.
            return grant

        monkeypatch.setattr(online, "authorize", expire_before_decryption)
        with pytest.raises(AuthorizationError) as err:
            with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                    recipient_private_key=keys["recipient"], policy="online",
                                    online_authorizer=online, now=clock):
                pytest.fail("Expired license must not authorize access")
        assert err.value.code == "license_expired"
        assert not decrypted


@pytest.mark.parametrize("advance, error_code", [(20, "grant_expired"), (1000, "license_expired")])
def test_renew_invalidates_grant_if_expired_before_commit(tmp_path, package, keys, monkeypatch, advance, error_code):
    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")
    service = ReferenceLicenseService(issuer_verify_key=keys["issuer"].verify_key,
                                      server_signing_key=keys["server"], clock=clock, grant_ttl=20)
    with serve(service) as url:
        online = client(url, keys)
        with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                recipient_private_key=keys["recipient"], policy="online",
                                online_authorizer=online, now=clock) as auth:
            original = online.authorize

            def expire_after_validation(**kwargs):
                grant = original(**kwargs)
                clock.value += advance
                return grant

            monkeypatch.setattr(online, "authorize", expire_after_validation)
            with pytest.raises(AuthorizationError) as err:
                auth.renew()
            assert err.value.code == error_code
            assert auth._grant is None
            with pytest.raises(AuthorizationError):
                auth.assert_current()


def test_renew_invalidates_previous_grant_when_license_already_expired(tmp_path, package, keys):
    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")
    service = ReferenceLicenseService(issuer_verify_key=keys["issuer"].verify_key,
                                      server_signing_key=keys["server"], clock=clock, grant_ttl=20)
    with serve(service) as url:
        with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                recipient_private_key=keys["recipient"], policy="online",
                                online_authorizer=client(url, keys), now=clock) as auth:
            assert auth._grant is not None
            clock.value += 1000
            with pytest.raises(AuthorizationError) as err:
                auth.renew()
            assert err.value.code == "license_expired"
            assert auth._grant is None


def test_online_rejects_wrong_server_key_wrong_installation_and_unreachable(tmp_path, package, keys):
    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")
    service = ReferenceLicenseService(issuer_verify_key=keys["issuer"].verify_key,
                                      server_signing_key=keys["server"], clock=clock)
    with serve(service) as url:
        with pytest.raises(AuthorizationError) as err:
            with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                    recipient_private_key=keys["recipient"], policy="online", now=clock,
                                    online_authorizer=client(url, keys, SigningKey.generate().verify_key)):
                pass
        assert err.value.code == "invalid_signature"
        wrong = OnlineAuthorizer(url, keys["server"].verify_key, SigningKey.generate(), allow_loopback_http=True)
        with pytest.raises(AuthorizationError) as err:
            with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                    recipient_private_key=keys["recipient"], policy="online", now=clock,
                                    online_authorizer=wrong):
                pass
        assert err.value.code == "installation_mismatch"
    # The server is now shut down; online must never silently become offline.
    with pytest.raises(AuthorizationError) as err:
        with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                recipient_private_key=keys["recipient"], policy="online", now=clock,
                                online_authorizer=client(url, keys)):
            pass
    assert err.value.code == "online_unavailable"


def test_auto_renew_pre_call_but_no_renewal_after_inflight_expiry(tmp_path, package, keys):
    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")
    revoked = set()
    service = ReferenceLicenseService(issuer_verify_key=keys["issuer"].verify_key,
                                      server_signing_key=keys["server"], revoked=revoked.__contains__,
                                      clock=clock, grant_ttl=20)
    with serve(service) as url:
        with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                recipient_private_key=keys["recipient"], policy="online", now=clock,
                                online_authorizer=client(url, keys)) as auth:
            initial = auth._grant
            clock.value += 11
            auth.before_process()
            assert auth._grant != initial  # a fresh, signed, nonce-bound grant
            clock.value += 20  # simulated long-running scenario: post-call guard must fail
            with pytest.raises(AuthorizationError) as err:
                auth.assert_current()
            assert err.value.code == "grant_expired"
            auth.before_process()  # next call can obtain a fresh grant
            revoked.add("license-online")
            clock.value += 11
            with pytest.raises(AuthorizationError):
                auth.before_process()
            with pytest.raises(AuthorizationError):
                auth.assert_current()  # no fallback to the prior grant


def test_online_request_proof_of_possession_and_replay(tmp_path, package, keys):
    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")
    envelope = json.loads((sealed / "envelope.json").read_text())
    raw_license = base64.b64decode(envelope["license"])
    claims = json.loads(raw_license)
    raw_request = _json_bytes({"format": REQUEST_FORMAT, "version": VERSION, "audience": AUDIENCE,
        "license_sha256": _sha(raw_license), "license_id": claims["license_id"],
        "installation_id": claims["installation_id"], "package_sha256": claims["package_sha256"],
        "recipient_key_sha256": claims["recipient_key_sha256"], "nonce": "42" * 32, "issued_at": clock()})
    body = {"license": envelope["license"], "license_signature": envelope["license_signature"],
            "request": _encode(raw_request),
            "request_signature": _encode(_sign(keys["installation"], REQUEST_DOMAIN, raw_request))}
    service = ReferenceLicenseService(issuer_verify_key=keys["issuer"].verify_key,
                                      server_signing_key=keys["server"], clock=clock)
    forged = dict(body, request_signature=_encode(_sign(SigningKey.generate(), REQUEST_DOMAIN, raw_request)))
    with pytest.raises(AuthorizationError) as err:
        service.authorize(_json_bytes(forged))
    assert err.value.code == "invalid_signature"
    assert json.loads(service.authorize(_json_bytes(body)))["grant"]
    with pytest.raises(AuthorizationError) as err:
        service.authorize(_json_bytes(body))
    assert err.value.code == "replay"


def test_correctly_signed_grant_with_wrong_nonce_is_rejected(tmp_path, package, keys):
    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")

    class WrongNonceServer(ReferenceLicenseService):
        def authorize(self, raw_body):
            response = json.loads(super().authorize(raw_body))
            grant = json.loads(base64.b64decode(response["grant"]))
            grant["nonce"] = "ff" * 32
            altered = _json_bytes(grant)
            return _json_bytes({"grant": _encode(altered), "signature": _encode(_sign(keys["server"], GRANT_DOMAIN, altered))})

    service = WrongNonceServer(issuer_verify_key=keys["issuer"].verify_key,
                               server_signing_key=keys["server"], clock=clock)
    with serve(service) as url:
        with pytest.raises(AuthorizationError) as err:
            with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                    recipient_private_key=keys["recipient"], policy="online", now=clock,
                                    online_authorizer=client(url, keys)):
                pass
    assert err.value.code == "invalid_grant"


@pytest.mark.parametrize("malicious", ["../outside", "/absolute", "code/../../escape", "code\\scenario.py"])
def test_unsafe_archives_cannot_be_issued(tmp_path, keys, malicious):
    clock = Clock()
    path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("scenario-package.json", '{"format":"vl.scenario-package"}')
        z.writestr(malicious, "forbidden")
    with pytest.raises(AuthorizationError) as err:
        issue(tmp_path, path, keys, clock)
    assert err.value.code == "unsafe_archive"


def test_http_requires_explicit_loopback_test_mode(tmp_path, package, keys):
    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")
    online = OnlineAuthorizer("http://example.com/v1/grant", keys["server"].verify_key,
                              keys["installation"], allow_loopback_http=True)
    with pytest.raises(AuthorizationError) as err:
        with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                recipient_private_key=keys["recipient"], policy="online", now=clock,
                                online_authorizer=online):
            pass
    assert err.value.code == "invalid_endpoint"


def test_tls_client_cannot_disable_certificate_or_hostname_verification(tmp_path, package, keys):
    clock = Clock()
    sealed, _ = issue(tmp_path, package, keys, clock, "online")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    online = OnlineAuthorizer("https://example.com/v1/grant", keys["server"].verify_key,
                              keys["installation"], ssl_context=context)
    with pytest.raises(AuthorizationError) as err:
        with authorized_package(sealed, issuer_verify_key=keys["issuer"].verify_key,
                                recipient_private_key=keys["recipient"], policy="online", now=clock,
                                online_authorizer=online):
            pass
    assert err.value.code == "invalid_endpoint"
