"""Protected VLMP host regression checks with real signatures and CPU ORT."""
from contextlib import contextmanager
from functools import partial
from pathlib import Path
import hashlib

import numpy as np
import pytest
from nacl.public import PrivateKey
from nacl.signing import SigningKey

from independent.host import PackageError, load_protected
from vl_exchange_security import AuthorizationError, authorized_package, seal_package

ROOT = Path(__file__).resolve().parents[2]
NOW = 1800000000


def fixture(tmp_path, name):
    source = ROOT / 'examples' / (name + '.zip')
    issuer, recipient, installation = SigningKey.generate(), PrivateKey.generate(), SigningKey.generate()
    clock = [NOW]
    envelope = tmp_path / 'sealed'
    seal_package(source, envelope, issuer_signing_key=issuer,
        recipient_public_key=recipient.public_key, installation_verify_key=installation.verify_key,
        issuer_id='test-issuer', license_id='test-license', installation_id='test-host',
        mode='offline', issued_at=NOW, not_before=NOW, expires_at=NOW+3600,
        manifest_path='model-package.json')
    base = partial(authorized_package, issuer_verify_key=issuer.verify_key,
        recipient_private_key=recipient, policy='offline', now=lambda: clock[0])
    roots = []
    @contextmanager
    def verifier(path):
        with base(path) as auth:
            roots.append(auth.root)
            yield auth
    return source, envelope, clock, roots, verifier


@pytest.mark.parametrize('name', ['classification', 'detection'])
def test_protected_package_real_onnx_and_cleanup(tmp_path, name):
    source, envelope, _, roots, verifier = fixture(tmp_path, name)
    expected_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with load_protected(envelope, verifier=verifier) as model:
        assert model.zip_sha256 == expected_digest
        row = model.manifest['golden']['fixtures'][0]
        image = np.load(roots[0] / row['input'], allow_pickle=False)
        result = model.predict(image)
        if name == 'classification':
            assert result['label'] == row['expected']['label']
            assert result['score'] == pytest.approx(row['expected']['score'], abs=1e-6)
        else:
            assert result == row['expected']
        assert model.inference_calls == 1
        assert roots[0].exists()
    assert not roots[0].exists()
    model.close()  # Close remains idempotent after successful protected use.
    with pytest.raises(PackageError):
        model.predict(image)


@pytest.mark.parametrize('name', ['classification', 'detection'])
def test_expiry_after_actual_onnx_never_returns_result(tmp_path, name):
    _, envelope, clock, roots, verifier = fixture(tmp_path, name)
    with load_protected(envelope, verifier=verifier) as model:
        row = model.manifest['golden']['fixtures'][0]
        image = np.load(roots[0] / row['input'], allow_pickle=False)
        session = model._session
        class ExpireAfterRealInference:
            def run(self, output_names, feed):
                result = session.run(output_names, feed)
                clock[0] += 3601
                return result
        model._session = ExpireAfterRealInference()
        with pytest.raises(AuthorizationError) as failure:
            model.predict(image)
        assert failure.value.code == 'license_expired'
        assert model.inference_calls == 1
    assert not roots[0].exists()


def test_tampered_ciphertext_rejected_before_model_initialization(tmp_path, monkeypatch):
    _, envelope, _, roots, verifier = fixture(tmp_path, 'classification')
    file = envelope / 'package.sealed'
    data = bytearray(file.read_bytes())
    data[-1] ^= 1
    file.write_bytes(data)
    def forbidden(*args, **kwargs):
        pytest.fail('ONNX session initialized before authorization')
    monkeypatch.setattr('independent.host.ort.InferenceSession', forbidden)
    with pytest.raises(AuthorizationError) as failure:
        load_protected(envelope, verifier=verifier)
    assert failure.value.code == 'ciphertext_mismatch'
    assert roots == []


def test_modified_plaintext_rejected_before_inference_and_removed(tmp_path):
    _, envelope, _, roots, verifier = fixture(tmp_path, 'classification')
    model = load_protected(envelope, verifier=verifier)
    row = model.manifest['golden']['fixtures'][0]
    image = np.load(roots[0] / row['input'], allow_pickle=False)
    weights = roots[0] / model.manifest['backend']['weights']
    weights.write_bytes(weights.read_bytes() + b'changed')
    with pytest.raises(PackageError):
        model.predict(image)
    assert model.inference_calls == 0
    with pytest.raises(PackageError):
        model.close()
    assert not roots[0].exists()
    model.close()  # Even a close that reported tampering fully released resources.


def test_wrong_signed_package_format_fails_and_removes_plaintext(tmp_path):
    source = ROOT.parent / 'vlsp-unified-integration/fixtures/scenario.zip'
    issuer, recipient, installation = SigningKey.generate(), PrivateKey.generate(), SigningKey.generate()
    envelope = tmp_path / 'sealed-scenario'
    seal_package(source, envelope, issuer_signing_key=issuer,
        recipient_public_key=recipient.public_key, installation_verify_key=installation.verify_key,
        issuer_id='test-issuer', license_id='test-scenario', installation_id='test-host',
        mode='offline', issued_at=NOW, not_before=NOW, expires_at=NOW+3600,
        manifest_path='scenario-package.json')
    roots = []
    @contextmanager
    def verifier(path):
        with authorized_package(path, issuer_verify_key=issuer.verify_key,
                recipient_private_key=recipient, policy='offline', now=lambda: NOW) as auth:
            roots.append(auth.root)
            yield auth
    with pytest.raises(PackageError, match='not bound to a VLMP package'):
        load_protected(envelope, verifier=verifier)
    assert len(roots) == 1 and not roots[0].exists()


@pytest.mark.parametrize('relative', [
    'model/reports/vlmp-public-channel-probe.zip',
    'drafts/v0.2/model/examples/vl-color-detector.zip',
])
def test_published_legacy_zip_can_use_protection(tmp_path, relative):
    """Same signed original ZIP and same output as the legacy unprotected path."""
    from independent.host import load_package
    repository = ROOT.parents[2]
    source = repository / relative
    issuer, recipient, installation = SigningKey.generate(), PrivateKey.generate(), SigningKey.generate()
    envelope = tmp_path / 'sealed-legacy'
    seal_package(source, envelope, issuer_signing_key=issuer,
        recipient_public_key=recipient.public_key, installation_verify_key=installation.verify_key,
        issuer_id='test-issuer', license_id='test-legacy', installation_id='test-host',
        mode='offline', issued_at=NOW, not_before=NOW, expires_at=NOW+3600,
        manifest_path='model-package.json')
    verifier = partial(authorized_package, issuer_verify_key=issuer.verify_key,
        recipient_private_key=recipient, policy='offline', now=lambda: NOW)
    plain = load_package(source)
    protected = load_protected(envelope, verifier=verifier)
    root = protected._root
    try:
        golden = plain.manifest['golden']
        fixture = golden['fixtures'][0] if isinstance(golden, dict) else golden[0]
        image = np.load(plain._root / fixture['input'], allow_pickle=False)
        assert protected.zip_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
        assert protected.predict(image) == plain.predict(image)
        assert protected.inference_calls == 1
    finally:
        plain.close()
        protected.close()
    assert not root.exists()
