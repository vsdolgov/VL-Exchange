"""The separation guard must actually block reference imports and execution.

This verifies an engineering safeguard in a child process, not sandbox security.
No reference source is read or executed by the probe.
"""
import json
from pathlib import Path
import subprocess
import sys


def test_worker_guard_rejects_reference_imports_and_source_execution():
    pilot = Path(__file__).resolve().parents[2]
    script = r'''
import importlib.util, json, sys
from pathlib import Path
pilot = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("guard_probe", pilot / "independent/bootstrap.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
blocked = []
for name in ("vl_scenario_v02", "integration_host"):
    try:
        __import__(name)
    except ImportError:
        blocked.append(name)
filename = str(pilot.parent / "scenario/src/vl_scenario_v02/executor.py")
try:
    exec(compile("raise AssertionError('guard did not prevent execution')", filename, "exec"), {})
except ImportError:
    blocked.append("reference-source-exec")
assert len(blocked) == 3
assert guard.evidence()["loaded_reference_modules"] == []
assert len(guard.evidence()["forbidden_import_attempts"]) == 3
print(json.dumps({"blocked": blocked, "loaded_reference_modules": []}))
'''
    result = subprocess.run([sys.executable, "-c", script, str(pilot)],
                            capture_output=True, text=True, timeout=15, check=True)
    evidence = json.loads(result.stdout)
    assert evidence["blocked"] == ["vl_scenario_v02", "integration_host", "reference-source-exec"]
    assert evidence["loaded_reference_modules"] == []
