"""Independent VLSP/VideoPipe worker: scenario and all VLMP models protected."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
INTEGRATION = HERE.parent
spec = importlib.util.spec_from_file_location('_scenario_independent_worker', INTEGRATION / 'independent/worker.py')
base = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = base
spec.loader.exec_module(base)
from support import CompositeAuthorization, LeaseRecorder, LeaseRegistry, ModelProtection, close_all
from vlmp_independent.host import load_protected

class ProtectedProviders(base.UnifiedProviders):
    def __init__(self, pins, owner):
        self.owner = owner
        self.protection = ModelProtection(base.FIXTURES, owner.mode)
        old_loader = base.load_package
        def protected_loader(path):
            alias = path.stem
            envelope, digest = self.protection.envelopes[alias]
            model = load_protected(envelope, verifier=self.protection.verifier)
            model._authorization = LeaseRecorder(model._authorization, owner.model_leases)
            if digest != pins[alias]['zip_sha256'] or model.zip_sha256 != digest or model.manifest_sha256 != pins[alias]['manifest_sha256']:
                model.close()
                raise ValueError('Signed model identity mismatch: ' + alias)
            return model
        try:
            base.load_package = protected_loader
            super().__init__(pins)
        except BaseException:
            self.protection.close()
            raise
        finally:
            base.load_package = old_loader

    def _returned(self, alias, actual, entry):
        fault = self._fault or {}
        if (fault.get('mode') == 'expire_model_after_return' and fault.get('alias') == alias
                and self._calls[alias] == fault.get('call', 1)):
            entry['fault_injection'] = fault['mode']
            entry['returned_output'] = deepcopy(actual)
            self.protection.expire()
            return deepcopy(actual)
        return super()._returned(alias, actual, entry)

    def close(self):
        self.end()
        actions = [executor.close for executor in self._executors.values()] + [self.protection.close]
        self._executors.clear()
        self._closed = True
        close_all(actions)

class Session(base.Session):
    def __init__(self):
        super().__init__()
        self.mode = 'offline'
        self.model_leases = LeaseRegistry()
        base.UnifiedProviders = lambda pins: ProtectedProviders(pins, self)
    def close(self):
        try: super().close()
        finally:
            if self.providers is not None:
                self.providers.close()
                self.providers = None
    def open(self, command):
        self.mode = command.get('policy', 'offline')
        result = super().open(command)
        self.engine.authorization = CompositeAuthorization(self.engine.authorization, self.model_leases)
        result['models_protected'] = True
        result['model_licenses'] = {alias: {key: wrapped.host._authorization.claims[key]
            for key in ('mode', 'license_id', 'package_sha256', 'manifest_sha256')}
            for alias, wrapped in self.providers._executors.items()}
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
