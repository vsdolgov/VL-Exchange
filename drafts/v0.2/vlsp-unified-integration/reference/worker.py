"""Reference VLSP executor backed by unchanged unified VLMP ZIPs.

The JSONL protocol is shared with the native VideoPipe pilot. Keys, envelope,
and the online issuer are ephemeral and local to this process.
"""
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from functools import partial
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import threading
import time
import zipfile

import numpy as np
from nacl.public import PrivateKey
from nacl.signing import SigningKey

ROOT = Path(__file__).resolve().parents[4]
PILOT = ROOT / 'drafts/v0.2/videopipe-pilot/python'
sys.path.insert(0, str(PILOT))
import bootstrap  # noqa: E402 - sets up the reference executor's local dependencies
import runtime as pilot  # noqa: E402
from vl_scenario_v02 import Host, ModelBinding, TrackerBinding, ScenarioExecutor, ScenarioError
from vl_exchange_security import OnlineAuthorizer, authorized_package, seal_package
from vl_exchange_security.reference_server import ReferenceLicenseService, make_server

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
spec = importlib.util.spec_from_file_location('_unified_model_reference', ROOT / 'drafts/v0.2/vlmp-unified/reference/runtime.py')
unified = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = unified
spec.loader.exec_module(unified)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tensor_record(name, value):
    value = np.ascontiguousarray(value)
    return {'name': name, 'shape': list(value.shape), 'dtype': str(value.dtype),
            'sha256': sha(value.tobytes())}


class TracedSession:
    """Observe the actual ORT call, rather than reconstructing it afterwards."""
    def __init__(self, session):
        self.session = session
        self.input = None
        self.outputs = None

    def run(self, output_names, feed):
        [(name, tensor)] = feed.items()
        self.input = tensor_record(name, tensor)
        outputs = self.session.run(output_names, feed)
        self.outputs = [tensor_record(name, tensor) for name, tensor in zip(output_names, outputs)]
        return outputs


def pins():
    scenario = FIXTURES / 'scenario.zip'
    with zipfile.ZipFile(scenario) as archive:
        manifest = archive.read('scenario-package.json')
    result = {'scenario_zip_sha256': sha(scenario.read_bytes()),
              'scenario_manifest_sha256': sha(manifest), 'models': {}}
    for alias in ('detector', 'classifier_a', 'classifier_b'):
        path = FIXTURES / (alias + '.zip')
        with zipfile.ZipFile(path) as archive:
            raw = archive.read('model-package.json')
            model = json.loads(raw)
            weights = archive.read(model['backend']['weights'])
        result['models'][alias] = {'manifest_sha256': sha(raw), 'weights_sha256': sha(weights),
                                   'zip_sha256': sha(path.read_bytes()), 'profile': model['profile']}
    expected = json.loads((FIXTURES / 'pins.json').read_text(encoding='utf-8'))
    if (expected['scenario']['zip_sha256'] != result['scenario_zip_sha256'] or
            expected['scenario']['manifest_sha256'] != result['scenario_manifest_sha256']):
        raise ValueError('Scenario fixture differs from published pins')
    for alias, model in result['models'].items():
        if any(model[key] != value for key, value in expected[alias].items()):
            raise ValueError('Model fixture differs from published pins: ' + alias)
    return result


class InferenceHost(pilot.InferenceHost):
    def __init__(self, fixture_pins):
        self.pins = fixture_pins
        self.models = {alias: unified.load_package(FIXTURES / (alias + '.zip'))
                       for alias in ('detector', 'classifier_a', 'classifier_b')}
        for model in self.models.values():
            model._session = TracedSession(model._session)
        self.trace, self.counts, self.active, self.fault = [], Counter(), None, None
        self.per_call, self.expire = Counter(), None

    def infer(self, alias):
        def callback(request, context):
            pixels = self.frame(request, context)
            crop = pixels
            if alias != 'detector':
                x1, y1, x2, y2 = request['bbox']
                height, width = pixels.shape[:2]
                crop = np.ascontiguousarray(pixels[math.floor(y1*height):math.ceil(y2*height),
                                                   math.floor(x1*width):math.ceil(x2*width)])
            model = self.models[alias]
            raw = model.predict(crop)
            output = raw if alias == 'detector' else {'classification': raw}
            self.counts[alias] += 1
            pin = self.pins['models'][alias]
            trace = {'service': 'model', 'alias': alias, 'context': deepcopy(context),
                     'request': {k: deepcopy(v) for k, v in request.items() if k != 'frame_ref'},
                     'frame_sha256': sha(pixels.tobytes()), 'crop_sha256': sha(crop.tobytes()),
                     'crop_shape': list(crop.shape), 'backend': 'CPUExecutionProvider',
                     'manifest_sha256': pin['manifest_sha256'], 'weights_sha256': pin['weights_sha256'],
                     'zip_sha256': pin['zip_sha256'], 'profile': pin['profile'],
                     'onnx_input': deepcopy(model._session.input), 'onnx_outputs': deepcopy(model._session.outputs),
                     'inference_executed': True, 'actual_output': deepcopy(output)}
            self.trace.append(trace)
            return self.inject(alias, output, trace)
        return callback

    def bindings(self, overrides):
        models = {alias: ModelBinding('detection' if alias == 'detector' else 'classification',
                  model.manifest_sha256, model.profile, self.infer(alias))
                  for alias, model in self.models.items()}
        if overrides.get('missing'):
            models.pop(overrides['missing'])
        if overrides.get('wrong_digest'):
            alias = overrides['wrong_digest']
            binding = models[alias]
            models[alias] = ModelBinding(binding.kind, '0'*64, binding.profile, binding.infer)
        return Host(models, {'tracker': TrackerBinding('org.vlexchange.demo.greedy-iou', '1.0.0', self.track)})

    def close(self):
        for model in self.models.values():
            model.close()


class Session(pilot.Session):
    def __init__(self):
        self.pins = pins()
        self.host = self.executor = self.stack = None
        self.published, self.policy, self.offset, self.closed = [], None, 0, False

    def handle(self, command):
        if command.get('op') == 'process' and command.get('expire_before'):
            self.offset += 3601
        return super().handle(command)

    def _open(self, command):
        self._close()
        self.executor, self.published, self.offset = None, [], 0
        if pins() != self.pins:
            raise ValueError('Fixture ZIP changed')
        if self.host is None:
            self.host = InferenceHost(self.pins)
        self.host.counts.clear()
        self.policy = command.get('policy', 'offline')
        if self.policy not in ('offline', 'online'):
            raise ScenarioError('policy_rejected', 'Only protected offline and online policies are supported')
        stack = ExitStack()
        try:
            temp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='vlsp-unified-')))
            issuer, recipient, installation = SigningKey.generate(), PrivateKey.generate(), SigningKey.generate()
            now = int(time.time())
            clock = lambda: now + self.offset
            fault = command.get('license_fault')
            if fault not in (None, 'expired', 'revoked'):
                raise ScenarioError('input_contract', 'Unknown license fault')
            if fault == 'revoked' and self.policy != 'online':
                raise ScenarioError('input_contract', 'Revocation requires online policy')
            authorizer = None
            if self.policy == 'online':
                server_key = SigningKey.generate()
                service = ReferenceLicenseService(issuer_verify_key=issuer.verify_key, server_signing_key=server_key,
                    clock=clock, revoked=lambda _: fault == 'revoked')
                server = make_server(service, '127.0.0.1', 0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                def stop_server():
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()
                stack.callback(stop_server)
                authorizer = OnlineAuthorizer(f'http://127.0.0.1:{server.server_port}/v1/grant',
                                              server_key.verify_key, installation, allow_loopback_http=True)
            envelope = temp / 'protected'
            sealed = seal_package(FIXTURES / 'scenario.zip', envelope, issuer_signing_key=issuer,
                recipient_public_key=recipient.public_key, installation_verify_key=installation.verify_key,
                issuer_id='local-fixture-issuer', license_id='fixture-'+self.policy,
                installation_id='local-fixture-installation', mode=self.policy, issued_at=now,
                not_before=now, expires_at=now+3600, manifest_path='scenario-package.json')
            if sealed['package_sha256'] != self.pins['scenario_zip_sha256']:
                raise ValueError('Envelope bound to another scenario ZIP')
            if fault == 'expired':
                self.offset = 3601
            verifier = partial(authorized_package, issuer_verify_key=issuer.verify_key,
                recipient_private_key=recipient, policy=self.policy, online_authorizer=authorizer, now=clock)
            pilot._WATCH_IMPORTS = True
            self.executor = stack.enter_context(ScenarioExecutor.open_protected(
                envelope, verifier=verifier, host=self.host.bindings(command.get('bindings', {}))))
            self.stack = stack
        except BaseException:
            stack.close()
            self.executor = None
            raise
        finally:
            pilot._WATCH_IMPORTS = False
        return {'policy': self.policy, 'protected': True,
                'scenario_zip_sha256': self.pins['scenario_zip_sha256'],
                'scenario_manifest_sha256': self.pins['scenario_manifest_sha256'],
                'models': deepcopy(self.pins['models'])}


def main():
    session = Session()
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            command = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON')))
            response = session.handle(command)
            print(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(',', ':')), flush=True)
            if command['op'] == 'shutdown':
                break
    finally:
        session.shutdown()


if __name__ == '__main__':
    main()
