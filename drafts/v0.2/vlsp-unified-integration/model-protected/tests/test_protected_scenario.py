"""Regressions for aggregate model leases and error-safe cleanup."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / 'drafts/v0.2/security-runtime/src'))
spec = importlib.util.spec_from_file_location('protected_scenario_support_test', HERE / 'support.py')
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class FakeLease:
    def __init__(self):
        self._owner = self._license_raw = self._license_signature = None
        self._issuer_key = self._clock = self._mode = self._server_key = self._grant = None
        self.expired = False
        self.checked = 0
    def assert_current(self):
        self.checked += 1
        if self.expired:
            raise ValueError('expired original model lease')


class FakeAuthorization:
    def __init__(self): self.leases = []
    def before_process(self):
        lease = FakeLease()
        self.leases.append(lease)
        return lease


def test_later_process_cannot_replace_an_earlier_model_lease():
    registry = support.LeaseRegistry()
    scenario = FakeAuthorization()
    model = FakeAuthorization()
    scenario_guard = support.CompositeAuthorization(scenario, registry)
    model_guard = support.LeaseRecorder(model, registry)
    first = scenario_guard.before_process()
    first_model = model_guard.before_process()
    second = scenario_guard.before_process()
    second_model = model_guard.before_process()
    second.assert_current()
    first_model.expired = True
    with pytest.raises(ValueError, match='expired original model lease'):
        first.assert_current()
    assert first_model.checked == 1 and second_model.checked == 1
    assert scenario.leases[0].checked == 1 and scenario.leases[1].checked == 1


@pytest.mark.parametrize('worker,cls,attr', [
    ('reference_worker.py', 'ProtectedInferenceHost', 'models'),
    ('independent_worker.py', 'ProtectedProviders', '_executors'),
])
def test_all_models_and_issuer_closed_even_if_two_models_fail(worker, cls, attr):
    # Isolate Python imports: the independent worker installs an import guard.
    program = '''\
import importlib.util,sys
from pathlib import Path
path = Path(sys.argv[1]); sys.path.insert(0, str(path.parent))
spec = importlib.util.spec_from_file_location('scenario_worker_cleanup_test', path)
worker = importlib.util.module_from_spec(spec); sys.modules[spec.name] = worker; spec.loader.exec_module(worker)
seen = []
class Resource:
    def __init__(self, name, fail=False): self.name, self.fail = name, fail
    def close(self):
        seen.append(self.name)
        if self.fail: raise ValueError(self.name)
obj = getattr(worker, sys.argv[2]).__new__(getattr(worker, sys.argv[2]))
setattr(obj, sys.argv[3], {'first': Resource('first', True), 'second': Resource('second', True), 'third': Resource('third')})
obj.protection = Resource('issuer')
if sys.argv[3] == '_executors':
    obj._active = obj._fault = obj._expire = None
    obj._closed = False
try: obj.close()
except ValueError as error: assert str(error) == 'first'
else: raise AssertionError('First cleanup error was swallowed')
assert seen == ['first', 'second', 'third', 'issuer'], seen
assert not getattr(obj, sys.argv[3])
if sys.argv[3] == '_executors': assert obj._closed
'''
    subprocess.run([sys.executable, '-c', program, str(HERE / worker), cls, attr],
                   check=True, timeout=30, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
