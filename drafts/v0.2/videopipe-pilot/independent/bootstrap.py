"""Worker path setup and observation of accidental reference-code reuse.

This is an engineering guard for this experiment, not a Python security sandbox.
Shared model loaders, JSON Schemas, ONNX Runtime and crypto remain allowed.
"""
from contextlib import contextmanager
from importlib.abc import MetaPathFinder
from pathlib import Path
import sys

sys.dont_write_bytecode = True
PILOT = Path(__file__).resolve().parents[1]
WORKSPACE = PILOT.parents[2]
for relative in ("drafts/v0.2/model/src", "model/src", "drafts/v0.2/security-runtime/src"):
    sys.path.insert(0, str(WORKSPACE / relative))
sys.path.insert(0, str(PILOT))

FORBIDDEN = frozenset({"vl_scenario_v02", "vl_scenario_package", "vl_block_package",
                       "integration_host", "independent_host", "runtime", "run_pilot"})
ATTEMPTS = []
SCENARIO_EXECS = []
_watch = False
_paths = (str(WORKSPACE / "drafts/v0.2/scenario/src/"),
          str(WORKSPACE / "drafts/v0.2/videopipe-pilot/python/integration_host.py"),
          str(WORKSPACE / "drafts/v0.2/videopipe-pilot/python/independent_host.py"),
          str(PILOT / "python/runtime.py"), str(PILOT / "python/worker.py"))


class _Guard(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in FORBIDDEN:
            ATTEMPTS.append(fullname)
            raise ImportError("Reference scenario implementation forbidden in this worker: " + fullname)
        return None


sys.meta_path.insert(0, _Guard())


def _audit(event, args):
    if event != "exec":
        return
    filename = getattr(args[0], "co_filename", "")
    if any(filename.startswith(prefix) for prefix in _paths):
        ATTEMPTS.append(filename)
        raise ImportError("Reference scenario source execution forbidden: " + filename)
    if _watch and "/code/" in filename and filename.endswith(".py"):
        SCENARIO_EXECS.append(filename)


sys.addaudithook(_audit)


@contextmanager
def observe_imports():
    global _watch
    before = _watch
    _watch = True
    try:
        yield
    finally:
        _watch = before


def evidence():
    loaded = sorted(name for name in sys.modules if name.split(".", 1)[0] in FORBIDDEN)
    return {"guard_active": True, "forbidden_import_attempts": list(ATTEMPTS),
            "loaded_reference_modules": loaded,
            "scenario_runtime": "independent.engine", "provider_runtime": "independent.providers"}
