"""Exercise both VLMP hosts with the same protected model archives.

Uses genuine signed/encrypted envelopes and an ephemeral signed loopback issuer.
No keys or plaintext artifacts are written to the report. Test-only fault
injection wraps the actual ONNX Runtime session.run and moves a trusted clock.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
from functools import partial
import hashlib
import io
import json
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from unittest.mock import patch

if not __debug__:
    raise RuntimeError('Assertions are required; do not run with -O')
ROOT = Path(__file__).resolve().parents[4]
BASE = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / 'drafts/v0.2/vlsp-unified-integration'
for path in (BASE, BASE / 'tools', ROOT / 'drafts/v0.2/security-runtime/src',
             ROOT / 'drafts/v0.2/model/src', ROOT / 'model/src'):
    sys.path.insert(0, str(path))
import numpy as np
import onnxruntime as ort
from nacl.public import PrivateKey
from nacl.signing import SigningKey
from vl_exchange_security import OnlineAuthorizer, AuthorizationError, authorized_package, seal_package
from vl_exchange_security.reference_server import ReferenceLicenseService, make_server
import compare_hosts
compare_hosts.np = np
from compare_hosts import fixture_set, matches
from reference.runtime import load_protected as reference_load
from independent.host import load_protected as independent_load

ALIASES = ('detector', 'classifier_a', 'classifier_b')
HOSTS = {'reference': reference_load, 'independent': independent_load}
PINS = json.loads((INTEGRATION / 'fixtures/pins.json').read_text())

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def sources():
    paths = []
    for relative in ('drafts/v0.2/vlmp-unified/reference', 'drafts/v0.2/vlmp-unified/independent',
                     'drafts/v0.2/vlmp-unified/spec', 'drafts/v0.2/vlmp-unified/protected',
                     'drafts/v0.2/security-runtime/src/vl_exchange_security'):
        paths.extend(p for p in (ROOT / relative).rglob('*') if p.is_file() and p.suffix in ('.py', '.json')
                     and '__pycache__' not in p.parts and 'reports' not in p.parts)
    paths += [INTEGRATION / 'fixtures' / (alias + '.zip') for alias in ALIASES]
    paths.append(INTEGRATION / 'fixtures/pins.json')
    return {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in sorted(set(paths))}

class ExpireAfterRun:
    """Call actual ORT, then expire only the license or only the online grant."""
    def __init__(self, original, clock, advance):
        self.original, self.clock, self.advance, self.calls = original, clock, advance, 0
    def __getattr__(self, name):
        return getattr(self.original, name)
    def run(self, *args, **kwargs):
        result = self.original.run(*args, **kwargs)
        self.calls += 1
        self.clock[0] += self.advance
        return result

def session_owner(host, model):
    return model._model if host == 'reference' else model

def expect_denial(callback):
    try:
        callback()
    except AuthorizationError as exc:
        return exc.code
    raise AssertionError('Authorization failure returned a result')

def run(output):
    if output:
        output = Path(output)
        if output.resolve().is_relative_to(ROOT.resolve()) and output.resolve() != (BASE / 'reports/protected-comparison.json').resolve():
            raise ValueError('Output inside repository must be the designated report')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.unlink(missing_ok=True)
    before = sources()
    for alias in ALIASES:
        assert digest((INTEGRATION / 'fixtures' / (alias + '.zip')).read_bytes()) == PINS[alias]['zip_sha256']
    rows, negatives = [], []
    started = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix='vlmp-protected-compare-') as directory, ExitStack() as stack:
        tmp = Path(directory)
        issuer, recipient, install, grant_key = SigningKey.generate(), PrivateKey.generate(), SigningKey.generate(), SigningKey.generate()
        now = int(time.time()); offset = [0]; revoked = [False]
        clock = lambda: now + offset[0]
        service = ReferenceLicenseService(issuer_verify_key=issuer.verify_key, server_signing_key=grant_key,
                                          clock=clock, revoked=lambda _: revoked[0], grant_ttl=60)
        server = make_server(service, '127.0.0.1', 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def stop():
            server.shutdown(); thread.join(timeout=5); server.server_close()
        stack.callback(stop)
        online = OnlineAuthorizer(f'http://127.0.0.1:{server.server_port}/v1/grant', grant_key.verify_key,
                                  install, allow_loopback_http=True)
        for mode in ('offline', 'online'):
            verifier = partial(authorized_package, issuer_verify_key=issuer.verify_key,
                               recipient_private_key=recipient, policy=mode,
                               online_authorizer=online if mode == 'online' else None, now=clock)
            for alias in ALIASES:
                path = INTEGRATION / 'fixtures' / (alias + '.zip')
                raw = path.read_bytes(); manifest, fixtures, atol, rtol = fixture_set(raw)
                envelope = tmp / (mode + '-' + alias)
                seal = seal_package(path, envelope, issuer_signing_key=issuer,
                    recipient_public_key=recipient.public_key, installation_verify_key=install.verify_key,
                    issuer_id='local-test', license_id=mode+'-'+alias, installation_id='fixture', mode=mode,
                    issued_at=now, not_before=now, expires_at=now+3600, manifest_path='model-package.json')
                assert seal['package_sha256'] == PINS[alias]['zip_sha256']
                assert seal['manifest_sha256'] == PINS[alias]['manifest_sha256']
                assert raw != (envelope / 'package.sealed').read_bytes()
                encrypted_sha = digest((envelope / 'package.sealed').read_bytes())
                outcomes = {}
                for host, load in HOSTS.items():
                    offset[0] = 0
                    model = load(envelope, verifier=verifier)
                    try:
                        assert model.zip_sha256 == PINS[alias]['zip_sha256']
                        assert model.manifest_sha256 == PINS[alias]['manifest_sha256']
                        assert model.profile == manifest['profile']
                        predictions = []
                        for fixture_id, frame, expected in fixtures:
                            actual = model.predict(frame.copy())
                            assert matches(actual, expected, atol, rtol), (host, alias, fixture_id, actual, expected)
                            predictions.append({'fixture': fixture_id, 'result': actual})
                        assert model.inference_calls == len(fixtures)
                        outcomes[host] = {'calls': model.inference_calls, 'predictions': predictions}
                    finally:
                        model.close()
                    assert digest((envelope / 'package.sealed').read_bytes()) == encrypted_sha
                    assert path.read_bytes() == raw
                assert matches(outcomes['reference']['predictions'], outcomes['independent']['predictions'], atol, rtol)
                rows.append({'mode': mode, 'model': alias, 'profile': manifest['profile'],
                             'zip_sha256': seal['package_sha256'], 'manifest_sha256': seal['manifest_sha256'],
                             'same_ciphertext_for_both_hosts': True, 'hosts': outcomes})
                frame = fixtures[0][1]
                for host, load in HOSTS.items():
                    def record(case, code):
                        negatives.append({'mode': mode, 'model': alias, 'host': host, 'case': case, 'code': code})
                    # Authentication and decryption must fail before ORT session construction.
                    for case, overrides, shift in (
                        ('wrong-issuer', {'issuer_verify_key': SigningKey.generate().verify_key}, 0),
                        ('wrong-recipient', {'recipient_private_key': PrivateKey.generate()}, 0),
                        ('expired-before-open', {}, 3601),
                    ):
                        offset[0] = shift
                        bad = partial(authorized_package, issuer_verify_key=overrides.get('issuer_verify_key', issuer.verify_key),
                            recipient_private_key=overrides.get('recipient_private_key', recipient), policy=mode,
                            online_authorizer=online if mode == 'online' else None, now=clock)
                        with patch.object(ort, 'InferenceSession', side_effect=AssertionError('ORT started before authorization')):
                            record(case, expect_denial(lambda: load(envelope, verifier=bad)))
                    offset[0] = 0
                    corrupt = tmp / (mode+'-'+alias+'-'+host+'-corrupt'); shutil.copytree(envelope, corrupt)
                    data = bytearray((corrupt / 'package.sealed').read_bytes()); data[-1] ^= 1
                    (corrupt / 'package.sealed').write_bytes(data)
                    with patch.object(ort, 'InferenceSession', side_effect=AssertionError('ORT started before integrity check')):
                        record('tampered-ciphertext', expect_denial(lambda: load(corrupt, verifier=verifier)))
                    if mode == 'online':
                        revoked[0] = True
                        try:
                            with patch.object(ort, 'InferenceSession', side_effect=AssertionError('ORT started before revocation check')):
                                record('revoked-before-open', expect_denial(lambda: load(envelope, verifier=verifier)))
                        finally: revoked[0] = False
                    # An expired entitlement must prevent a new ONNX call.
                    offset[0] = 0; model = load(envelope, verifier=verifier)
                    try:
                        offset[0] = 3601
                        calls = model.inference_calls
                        record('expired-before-predict', expect_denial(lambda: model.predict(frame.copy())))
                        assert model.inference_calls == calls
                    finally: model.close()
                    # Exercise an actual ORT call; license expiry must suppress its result.
                    for case, advance in [('license-expires-during-onnx', 3601)] + (
                            [('grant-expires-during-onnx-license-still-valid', 61)] if mode == 'online' else []):
                        offset[0] = 0; model = load(envelope, verifier=verifier)
                        try:
                            owner = session_owner(host, model)
                            probe = ExpireAfterRun(owner._session, offset, advance); owner._session = probe
                            calls = model.inference_calls
                            code = expect_denial(lambda: model.predict(frame.copy()))
                            assert probe.calls == 1 and model.inference_calls == calls + 1
                            if advance == 61: assert clock() < now + 3600
                            record(case, code)
                        finally: model.close()
                    offset[0] = 0
    grouped = {}
    for entry in negatives:
        key = (entry['mode'], entry['model'], entry['case'])
        grouped.setdefault(key, {})[entry['host']] = entry['code']
    for key, outcomes in grouped.items():
        assert set(outcomes) == set(HOSTS) and len(set(outcomes.values())) == 1, (key, outcomes)
    assert sources() == before, 'Source, schema or package changed during test'
    result = {'status': 'pass', 'checked_at': started, 'test': 'signed-encrypted-vlmp-models-two-hosts',
              'policies': ['offline', 'online'], 'same_protected_envelope_for_each_host': True,
              'profiles': ['detection-onnx-cpu-v1', 'classification-onnx-cpu-v1'],
              'positive_mode_model_pairs': len(rows), 'negative_host_cases': len(negatives),
              'actual_onnx_inference_in_all_positive_cases': True, 'negative_error_codes_match': True, 'runs': rows, 'negative_cases': negatives,
              'sources_sha256': before, 'environment': {'python': platform.python_version(),
              'platform': platform.platform(), 'numpy': np.__version__, 'onnxruntime': ort.__version__},
              'limitations': ['Ephemeral test keys and local loopback online server, not production PKI',
                  'Synthetic CPU ONNX models; no GPU, RTSP, external license provider or production threat assessment',
                  'The two hosts share the security implementation and ONNX Runtime',
                  'Trusted host process; decrypted data and in-memory model cannot be protected from a hostile administrator']}
    if output: output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    print(json.dumps({k: result[k] for k in ('status','positive_mode_model_pairs','negative_host_cases')}, ensure_ascii=False))
    return result

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path,
        default=BASE / 'reports/protected-comparison.json'); run(parser.parse_args().output)
