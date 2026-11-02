"""Reference VLSP worker: scenario and every VLMP model are protected."""
from collections import Counter
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
INTEGRATION = HERE.parent
spec = importlib.util.spec_from_file_location('_scenario_reference_worker', INTEGRATION / 'reference/worker.py')
base = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = base
spec.loader.exec_module(base)
from support import CompositeAuthorization, LeaseRecorder, LeaseRegistry, ModelProtection, close_all

class ModelAdapter:
    def __init__(self, model, leases):
        self.model = model
        model._authorization = LeaseRecorder(model._authorization, leases)
        self._session = base.TracedSession(model._model._session)
        model._model._session = self._session
    def __getattr__(self, name): return getattr(self.model, name)
    def predict(self, image): return self.model.predict(image)
    def close(self): self.model.close()

class ProtectedInferenceHost(base.InferenceHost):
    def __init__(self, pins, owner):
        self.pins, self.owner = pins, owner
        self.models = {}
        self.trace, self.counts, self.active, self.fault = [], Counter(), None, None
        self.per_call, self.expire = Counter(), None
        self.protection = ModelProtection(base.FIXTURES, owner.mode)
        try:
            for alias in ('detector', 'classifier_a', 'classifier_b'):
                path, digest = self.protection.envelopes[alias]
                model = base.unified.load_protected(path, verifier=self.protection.verifier)
                wrapped = ModelAdapter(model, owner.model_leases)
                self.models[alias] = wrapped
                if digest != pins['models'][alias]['zip_sha256'] or model.zip_sha256 != digest or model.manifest_sha256 != pins['models'][alias]['manifest_sha256']:
                    raise ValueError('Signed model identity mismatch: ' + alias)
        except BaseException:
            self.close()
            raise

    def inject(self, alias, output, trace):
        fault = self.fault or {}
        if (fault.get('mode') == 'expire_model_after_return' and fault.get('alias') == alias
                and self.per_call[alias] + 1 == fault.get('call', 1)):
            self.per_call[alias] += 1
            trace['fault_injection'] = fault['mode']
            trace['returned_output'] = deepcopy(output)
            self.protection.expire()
            return output
        return super().inject(alias, output, trace)

    def close(self):
        actions = [model.close for model in self.models.values()] + [self.protection.close]
        self.models.clear()
        close_all(actions)

class Session(base.Session):
    def __init__(self):
        super().__init__()
        self.mode = 'offline'
        self.model_leases = LeaseRegistry()
        base.InferenceHost = lambda pins: ProtectedInferenceHost(pins, self)
    def _close(self):
        try: super()._close()
        finally:
            if self.host is not None:
                self.host.close()
                self.host = None
    def _open(self, command):
        self.mode = command.get('policy', 'offline')
        result = super()._open(command)
        self.executor._authorized = CompositeAuthorization(self.executor._authorized, self.model_leases)
        result['models_protected'] = True
        result['model_licenses'] = {alias: {key: model.model._authorization.claims[key]
            for key in ('mode', 'license_id', 'package_sha256', 'manifest_sha256')}
            for alias, model in self.host.models.items()}
        return result

def main():
    session = Session()
    try:
        for line in sys.stdin:
            if not line.strip(): continue
            command = json.loads(line)
            print(json.dumps(session.handle(command), ensure_ascii=False, allow_nan=False, separators=(',', ':')), flush=True)
            if command['op'] == 'shutdown': break
    finally: session.shutdown()

if __name__ == '__main__': main()
