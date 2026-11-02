"""The conformance runner must never report success with checks disabled."""
import os
import json
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


def test_optimized_python_is_rejected_and_replaces_stale_success(tmp_path):
    runner = Path(__file__).resolve().parents[1] / "tools/compare_hosts.py"
    report = tmp_path / "comparison.json"
    report.write_text('{"status":"pass"}')
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-O", str(runner), "--output", str(report)],
                            capture_output=True, text=True, env=env)
    assert result.returncode != 0
    assert "requires assertions" in result.stderr
    assert json.loads(report.read_text())["status"] == "fail"


def _load_runner():
    path = Path(__file__).resolve().parents[1] / "tools/compare_hosts.py"
    spec = importlib.util.spec_from_file_location("vlmp_comparison_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_invalid_input_replaces_stale_success(tmp_path, monkeypatch):
    runner = _load_runner()
    package = tmp_path / "tampered.zip"
    package.write_bytes(runner.PACKAGES["unified-classification"].read_bytes() + b"tampered")
    report = tmp_path / "comparison.json"
    report.write_text('{"status":"pass"}')
    monkeypatch.setattr(runner, "PACKAGES", {"unified-classification": package})
    monkeypatch.setattr(sys, "argv", ["compare_hosts.py", "--output", str(report)])
    with pytest.raises(AssertionError, match="unexpected package bytes"):
        runner.main()
    assert json.loads(report.read_text())["status"] == "fail"


def test_output_cannot_overwrite_input_package(monkeypatch):
    runner = _load_runner()
    package = runner.PACKAGES["unified-classification"]
    before = package.read_bytes()
    monkeypatch.setattr(sys, "argv", ["compare_hosts.py", "--output", str(package)])
    with pytest.raises(SystemExit, match="must not overwrite"):
        runner.main()
    assert package.read_bytes() == before
