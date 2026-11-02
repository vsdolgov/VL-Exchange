"""Independent VLSP/VideoPipe JSONL worker using the unified VLMP host.

The native VideoPipe node forwards the actual cv::Mat pixels in the image field.
The old independent VLSP interpreter and tracker are reused, never the reference
VLSP or VLMP runtimes. The online service is an ephemeral signed loopback issuer.
"""
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import zipfile

ROOT = Path(__file__).resolve().parents[4]
PILOT = ROOT / 'drafts/v0.2/videopipe-pilot'
FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
sys.path.insert(0, str(PILOT / 'independent'))
import bootstrap
# Guard both the VLSP reference runtime and the distinct VLMP reference host,
# including source loaded under an arbitrary package alias.
bootstrap.FORBIDDEN = bootstrap.FORBIDDEN | frozenset({'vlmp_reference'})
bootstrap._paths = bootstrap._paths + (str(ROOT / 'drafts/v0.2/vlmp-unified/reference/'),)
from independent.engine import Engine, Fault
from independent.providers import ProviderSet
from worker import Session as PilotSession
from nacl.public import PrivateKey
from nacl.signing import SigningKey
from vl_exchange_security import OnlineAuthorizer, authorized_package, seal_package
from vl_exchange_security.reference_server import ReferenceLicenseService, make_server

# Load as a distinct package because the VLSP pilot also uses `independent`.
model_path = ROOT / 'drafts/v0.2/vlmp-unified/independent'
spec = importlib.util.spec_from_file_location('vlmp_independent', model_path / '__init__.py', submodule_search_locations=[str(model_path)])
model_package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = model_package
spec.loader.exec_module(model_package)
from vlmp_independent.host import load_package


def sha(data):
    return hashlib.sha256(data).hexdigest()


def verify():
    pins = json.loads((FIXTURES / 'pins.json').read_text())
    for alias in ('scenario', 'detector', 'classifier_a', 'classifier_b'):
        data = (FIXTURES / (alias + '.zip')).read_bytes()
        if sha(data) != pins[alias]['zip_sha256']:
            raise ValueError('ZIP pin mismatch: ' + alias)
        with zipfile.ZipFile(FIXTURES / (alias + '.zip')) as archive:
            filename = 'scenario-package.json' if alias == 'scenario' else 'model-package.json'
            raw = archive.read(filename)
            if sha(raw) != pins[alias]['manifest_sha256']:
                raise ValueError('Manifest pin mismatch: ' + alias)
            if alias != 'scenario':
                manifest = json.loads(raw)
                if sha(archive.read(manifest['backend']['weights'])) != pins[alias]['weights_sha256']:
                    raise ValueError('Weights pin mismatch: ' + alias)
    return pins


def tensor_descriptor(name, data):
    import numpy as np
    data = np.ascontiguousarray(data)
    return {'name': name, 'shape': list(data.shape), 'dtype': str(data.dtype),
            'sha256': sha(data.tobytes(order='C'))}


class ObservedSession:
    """Observe the real ONNX Runtime call without executing an extra inference."""
    def __init__(self, original):
        self.original = original
        self.input = self.outputs = None

    def __getattr__(self, name):
        return getattr(self.original, name)

    def run(self, names, feeds, *args, **kwargs):
        (name, tensor), = feeds.items()
        self.input = tensor_descriptor(name, tensor)
        values = self.original.run(names, feeds, *args, **kwargs)
        self.outputs = [tensor_descriptor(name, value) for name, value in zip(names, values)]
        return values


class WrappedModel:
    def __init__(self, path):
        self.host = load_package(path)
        if self.host.spec_version != '0.2.0':
            raise ValueError('Expected unified VLMP 0.2.0')
        self.session = ObservedSession(self.host._session)
        self.host._session = self.session
        self.manifest = self.host.manifest

    def infer(self, crop):
        return self.host.predict(crop)

    def predict(self, crop):
        return self.host.predict(crop)

    def close(self):
        self.host.close()


class UnifiedProviders(ProviderSet):
    def __init__(self, pins):
        self.pins = deepcopy(pins)
        self.models, self.trackers = {}, {}
        self.trace, self.counts, self._calls = [], Counter(), Counter()
        self._executors, self._identities = {}, {}
        self._active = self._fault = self._expire = None
        self._closed = False
        try:
            for alias in ('detector', 'classifier_a', 'classifier_b'):
                model = WrappedModel(FIXTURES / (alias + '.zip'))
                self._executors[alias] = model
                info = pins[alias]
                if model.host.manifest_sha256 != info['manifest_sha256'] or model.host.zip_sha256 != info['zip_sha256']:
                    raise ValueError('Model identity mismatch: ' + alias)
                kind = 'detection' if alias == 'detector' else 'classification'
                self._identities[alias] = {'manifest_sha256': info['manifest_sha256'],
                                           'weights_sha256': info['weights_sha256'], 'kind': kind}
                self.models[alias] = SimpleNamespace(kind=kind, manifest_sha256=info['manifest_sha256'],
                    profile=model.host.profile,
                    infer=lambda request, context, alias=alias: self._infer(alias, request, context))
            self.trackers['tracker'] = SimpleNamespace(implementation='org.vlexchange.demo.greedy-iou',
                version='1.0.0', track=lambda request, state, context: self._track(request, state, context))
        except BaseException:
            self.close()
            raise

    def _infer(self, alias, request, context):
        session = self._executors[alias].session
        session.input = session.outputs = None
        try:
            return super()._infer(alias, request, context)
        finally:
            if session.input is not None and self.trace and self.trace[-1].get('alias') == alias:
                self.trace[-1]['onnx_input'] = session.input
                self.trace[-1]['onnx_outputs'] = session.outputs
                self.trace[-1]['zip_sha256'] = self.pins[alias]['zip_sha256']
                self.trace[-1]['profile'] = self._executors[alias].host.profile


class Session(PilotSession):
    def __init__(self):
        self.pins = verify()
        self.providers = self.engine = self.resources = None
        self.offset, self.published, self.closed = 0, [], False

    def open(self, command):
        self.close()
        self.engine, self.offset, self.published = None, 0, []
        verify()
        if self.providers is None:
            self.providers = UnifiedProviders(self.pins)
        self.providers.counts.clear()
        bindings = self.providers.bindings(command.get('bindings', {}))
        policy, fault = command.get('policy', 'offline'), command.get('license_fault')
        if policy not in ('offline', 'online'):
            raise Fault('policy_rejected', 'Unsupported pilot delivery policy', 'security', False)
        if fault not in (None, 'expired', 'revoked'):
            raise Fault('input_contract', 'Unknown license fault', 'input', False)
        if fault == 'revoked' and policy != 'online':
            raise Fault('input_contract', 'Revocation requires online policy', 'input', False)
        resources = ExitStack()
        try:
            temp = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix='vlsp-unified-independent-')))
            issuer, recipient, installation = SigningKey.generate(), PrivateKey.generate(), SigningKey.generate()
            server_key = SigningKey.generate()
            now = int(time.time())
            seal_package(FIXTURES / 'scenario.zip', temp / 'sealed', issuer_signing_key=issuer,
                recipient_public_key=recipient.public_key, installation_verify_key=installation.verify_key,
                issuer_id='fixture-issuer', license_id='fixture-' + policy,
                installation_id='fixture-installation', mode=policy, issued_at=now,
                not_before=now, expires_at=now+3600, manifest_path='scenario-package.json')
            if fault == 'expired':
                self.offset = 3601
            authorizer = None
            if policy == 'online':
                service = ReferenceLicenseService(issuer_verify_key=issuer.verify_key,
                    server_signing_key=server_key, revoked=lambda _: fault == 'revoked')
                server = make_server(service, '127.0.0.1', 0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                def stop_server():
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()
                resources.callback(stop_server)
                authorizer = OnlineAuthorizer(f'http://127.0.0.1:{server.server_port}/v1/grant',
                    server_key.verify_key, installation, allow_loopback_http=True)
            try:
                authorization = resources.enter_context(authorized_package(temp / 'sealed',
                    issuer_verify_key=issuer.verify_key, recipient_private_key=recipient, policy=policy,
                    online_authorizer=authorizer, now=lambda: int(time.time()) + self.offset))
            except Exception as exc:
                raise Fault('authorization', str(exc), 'authorization', False, False) from exc
            with bootstrap.observe_imports():
                self.engine = Engine(authorization.root, bindings, authorization=authorization,
                                     parameters=command.get('parameters'))
            self.resources = resources
        except BaseException:
            resources.close()
            self.engine = None
            raise
        return {'policy': policy, 'protected': True, 'scenario_zip_sha256': self.pins['scenario']['zip_sha256'],
                'scenario_manifest_sha256': self.pins['scenario']['manifest_sha256'],
                'models': {alias: {**{key: self.pins[alias][key] for key in ('manifest_sha256', 'weights_sha256', 'zip_sha256')},
                                   'profile': self.providers._executors[alias].host.profile}
                           for alias in ('detector', 'classifier_a', 'classifier_b')}}

    def handle(self, command):
        if command.get('op') == 'process' and command.get('expire_before'):
            self.offset += 3601
        answer = super().handle(command)
        answer['independence']['provider_runtime'] = 'independent VLSP providers + vlmp_independent.host'
        answer['independence']['model_runtime'] = 'vlmp_independent.host'
        return answer


def main():
    session = Session()
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            command = json.loads(line, parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
            print(json.dumps(session.handle(command), ensure_ascii=False, allow_nan=False, separators=(',', ':')), flush=True)
            if command['op'] == 'shutdown':
                break
    finally:
        session.shutdown()


if __name__ == '__main__':
    main()
