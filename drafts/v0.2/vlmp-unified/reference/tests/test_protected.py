"""Protected VLMP reference lifecycle: real signed envelopes and real ORT."""
from functools import partial
import importlib.util
import io
from pathlib import Path
import sys

import numpy as np
import pytest
from nacl.public import PrivateKey
from nacl.signing import SigningKey

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT.parents[2]
sys.path.insert(0, str(REPO / 'drafts/v0.2/security-runtime/src'))
from vl_exchange_security import AuthorizationError, authorized_package, seal_package

spec = importlib.util.spec_from_file_location('reference_protected_test', ROOT / 'reference/runtime.py')
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


def protected(tmp_path, profile):
    issuer, recipient, installation = SigningKey.generate(), PrivateKey.generate(), SigningKey.generate()
    envelope = tmp_path / 'envelope'
    now = [1700000000]
    zip_path = ROOT / 'examples' / (profile + '.zip')
    seal_package(zip_path, envelope, issuer_signing_key=issuer, recipient_public_key=recipient.public_key,
        installation_verify_key=installation.verify_key, issuer_id='fixture', license_id='fixture',
        installation_id='fixture', mode='offline', issued_at=now[0], not_before=now[0],
        expires_at=now[0]+3600, manifest_path='model-package.json')
    verifier = partial(authorized_package, issuer_verify_key=issuer.verify_key,
        recipient_private_key=recipient, policy='offline', now=lambda: now[0])
    return envelope, verifier, now, zip_path


@pytest.mark.parametrize('profile', ['classification', 'detection'])
def test_authorized_model_matches_golden_and_cleans_decrypted_files(tmp_path, profile):
    envelope, verifier, now, source = protected(tmp_path, profile)
    model = reference.load_protected(envelope, verifier=verifier)
    root = model.snapshot.path
    assert root.is_dir() and model.zip_sha256 == reference.digest(source.read_bytes())
    fixture = model.manifest['golden']['fixtures'][0]
    image = np.load(io.BytesIO(model.snapshot.content(fixture['input'])), allow_pickle=False)
    result = model.predict(image)
    assert result == pytest.approx(fixture['expected']) if profile == 'classification' else result == fixture['expected']
    assert model.inference_calls == 1
    model.close()
    assert not root.exists()
    model.close()
    with pytest.raises(reference.PackageError):
        model.predict(image)


def test_expiry_after_real_inference_prevents_result_and_cleans_up(tmp_path):
    envelope, verifier, now, _ = protected(tmp_path, 'classification')
    model = reference.load_protected(envelope, verifier=verifier)
    root = model.snapshot.path
    actual = model._model._session
    class AdvanceAfterInference:
        def run(self, *args, **kwargs):
            value = actual.run(*args, **kwargs)
            now[0] += 3601
            return value
    model._model._session = AdvanceAfterInference()
    try:
        with pytest.raises(AuthorizationError):
            model.predict(np.zeros((2, 2, 3), dtype=np.uint8))
        assert model.inference_calls == 1
    finally:
        model.close()
    assert not root.exists()


def test_expired_license_and_corrupt_ciphertext_rejected_before_onnx_initialization(tmp_path, monkeypatch):
    envelope, verifier, now, _ = protected(tmp_path, 'classification')
    def forbidden(*args, **kwargs):
        pytest.fail('ONNX must not initialize for an unauthorized package')
    monkeypatch.setattr(reference.ort, 'InferenceSession', forbidden)
    now[0] += 3601
    with pytest.raises(AuthorizationError):
        reference.load_protected(envelope, verifier=verifier)
    now[0] -= 3601
    ciphertext = envelope / 'package.sealed'
    ciphertext.write_bytes(ciphertext.read_bytes() + b'corrupt')
    with pytest.raises(AuthorizationError):
        reference.load_protected(envelope, verifier=verifier)


def test_mutating_decrypted_payload_is_rejected_and_cleaned(tmp_path):
    envelope, verifier, now, _ = protected(tmp_path, 'classification')
    model = reference.load_protected(envelope, verifier=verifier)
    root = model.snapshot.path
    weights = root / model.manifest['backend']['weights']
    weights.write_bytes(weights.read_bytes() + b'corrupt')
    try:
        with pytest.raises(reference.PackageError, match='changed'):
            model.predict(np.zeros((2, 2, 3), dtype=np.uint8))
        with pytest.raises(reference.PackageError, match='changed'):
            model.close()
    finally:
        assert not root.exists()


def test_expiry_during_open_closes_model_and_decrypted_directory(tmp_path, monkeypatch):
    from contextlib import contextmanager
    envelope, verifier, now, _ = protected(tmp_path, 'classification')
    roots, models = [], []
    @contextmanager
    def observe(path):
        with verifier(path) as authorization:
            roots.append(authorization.root)
            yield authorization
    load = reference._load_snapshot
    def expire_after_model_load(snapshot):
        model = load(snapshot)
        models.append(model)
        now[0] += 3601
        return model
    monkeypatch.setattr(reference, '_load_snapshot', expire_after_model_load)
    with pytest.raises(AuthorizationError):
        reference.load_protected(envelope, verifier=observe)
    assert len(roots) == len(models) == 1
    assert not roots[0].exists()
    assert models[0]._session is None


@pytest.mark.parametrize('relative,shape', [
    ('model/reports/vlmp-public-channel-probe.zip', (2, 2, 3)),
    ('drafts/v0.2/model/examples/vl-color-detector.zip', (2, 4, 3)),
])
def test_protected_legacy_packages_keep_original_digest_and_run(tmp_path, relative, shape):
    issuer, recipient, installation = SigningKey.generate(), PrivateKey.generate(), SigningKey.generate()
    source = REPO / relative
    envelope = tmp_path / 'legacy'
    now = 1700000000
    seal_package(source, envelope, issuer_signing_key=issuer, recipient_public_key=recipient.public_key,
        installation_verify_key=installation.verify_key, issuer_id='fixture', license_id='fixture',
        installation_id='fixture', mode='offline', issued_at=now, not_before=now,
        expires_at=now+3600, manifest_path='model-package.json')
    verifier = partial(authorized_package, issuer_verify_key=issuer.verify_key,
        recipient_private_key=recipient, policy='offline', now=lambda: now)
    with reference.load_protected(envelope, verifier=verifier) as model:
        assert model.zip_sha256 == reference.digest(source.read_bytes())
        assert type(model.predict(np.zeros(shape, dtype=np.uint8))) is dict


def test_signed_scenario_is_rejected_as_model_before_loading_onnx(tmp_path, monkeypatch):
    issuer, recipient, installation = SigningKey.generate(), PrivateKey.generate(), SigningKey.generate()
    source = REPO / 'drafts/v0.2/scenario/examples/vlsp-color-cascade.zip'
    envelope = tmp_path / 'scenario'
    now = 1700000000
    seal_package(source, envelope, issuer_signing_key=issuer, recipient_public_key=recipient.public_key,
        installation_verify_key=installation.verify_key, issuer_id='fixture', license_id='fixture',
        installation_id='fixture', mode='offline', issued_at=now, not_before=now,
        expires_at=now+3600, manifest_path='scenario-package.json')
    verifier = partial(authorized_package, issuer_verify_key=issuer.verify_key,
        recipient_private_key=recipient, policy='offline', now=lambda: now)
    monkeypatch.setattr(reference.ort, 'InferenceSession', lambda *args, **kwargs: pytest.fail('unexpected ONNX initialization'))
    with pytest.raises(reference.PackageError, match='not a VLMP'):
        reference.load_protected(envelope, verifier=verifier)


def test_verifier_stub_cannot_claim_protected_execution(tmp_path):
    from contextlib import contextmanager
    closed = []
    @contextmanager
    def fake(_path):
        try:
            yield object()
        finally:
            closed.append(True)
    with pytest.raises(reference.PackageError, match='AuthorizedPackage'):
        reference.load_protected(tmp_path, verifier=fake)
    assert closed == [True]


def test_invalid_lease_rejected_and_authorization_closed(tmp_path):
    from contextlib import contextmanager
    envelope, verifier, now, _ = protected(tmp_path, 'classification')
    roots = []
    @contextmanager
    def without_real_lease(path):
        with verifier(path) as auth:
            roots.append(auth.root)
            auth.before_process = lambda: object()
            yield auth
    with pytest.raises(reference.PackageError, match='execution lease'):
        reference.load_protected(envelope, verifier=without_real_lease)
    assert len(roots) == 1 and not roots[0].exists()
