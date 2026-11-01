"""Sequence-level conformance; fixtures are JSON inputs, not model inference."""

from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager

from .common import package_path, read_json, sha256_file
from .errors import ScenarioError
from .executor import ScenarioExecutor
from .validation import validate_package


def _same(actual, expected, *, atol, rtol):
    if type(actual) is int or type(expected) is int:
        return type(actual) is int and type(expected) is int and actual == expected
    if type(actual) is float and type(expected) is float:
        return abs(actual - expected) <= atol + rtol * abs(expected)
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(_same(actual[key], expected[key], atol=atol, rtol=rtol) for key in expected)
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_same(a, e, atol=atol, rtol=rtol) for a, e in zip(actual, expected))
    return actual == expected


def run_steps(executor: ScenarioExecutor, steps: list[dict], *, atol=0, rtol=0) -> list[dict]:
    checks = []
    for index, step in enumerate(steps):
        if "reset" in step:
            executor.reset(step["reset"].get("source_id"), step["reset"].get("run_id"))
            checks.append({"step": index, "kind": "reset", "pass": True,
                           "source_id": step["reset"].get("source_id"), "run_id": step["reset"].get("run_id"), "snapshot": executor.snapshot()})
            continue
        expected = step["expected"]
        previous = executor.snapshot()
        try:
            actual = executor.process(step["inputs"], step["context"])
        except ScenarioError as exc:
            if "error" not in expected or any(exc.to_dict().get(key) != value for key, value in expected["error"].items()):
                raise ScenarioError("golden_mismatch", f"Step {index}: unexpected error {exc.to_dict()}") from exc
            unchanged = previous == executor.snapshot()
            if not unchanged:
                raise ScenarioError("golden_mismatch", f"Step {index}: failed input changed committed state or ordering")
            checks.append({"step": index, "kind": "error", "pass": True,
                           "expected": expected["error"], "actual": exc.to_dict(), "no_commit": True})
        else:
            if "error" in expected or not _same(actual, expected, atol=atol, rtol=rtol):
                raise ScenarioError("golden_mismatch", f"Step {index}: expected {expected!r}, got {actual!r}")
            checks.append({"step": index, "kind": "process", "pass": True, "expected": expected, "actual": actual})
    return checks


def _check_open_package(root, *, parameters, model_packages, authorized):
    package = validate_package(root, parameters=parameters, model_packages=model_packages, check_environment=True)
    sequences = package.manifest["golden"]["sequences"]
    tolerance = package.manifest["golden"]
    cases = []
    last_runtime = None
    for suite, ordered in (("forward", sequences), ("reverse", list(reversed(sequences)))):
        for sequence in ordered:
            steps = read_json(package_path(package.root, sequence["path"]))["steps"]
            manager = _executor_manager(root, authorized=authorized, parameters=parameters, model_packages=model_packages)
            with manager as executor:
                checks = run_steps(executor, steps, atol=tolerance["atol"], rtol=tolerance["rtol"])
                repeated = None
                if suite == "forward" and not executor.report()["lifecycle"]["failed"]:
                    executor.reset()
                    repeated = run_steps(executor, steps, atol=tolerance["atol"], rtol=tolerance["rtol"])
            # Closing is idempotent; a closed instance must never process again.
            executor.close()
            first_call = next(step for step in steps if "inputs" in step)
            try:
                executor.process(first_call["inputs"], first_call["context"])
            except ScenarioError as exc:
                if exc.code != "lifecycle":
                    raise
            else:
                raise ScenarioError("golden_mismatch", "A closed executor accepted a process call")
            report = executor.report()
            if report["lifecycle"]["close_calls"] != 1:
                raise ScenarioError("golden_mismatch", "Close was not idempotent")
            last_runtime = report["runtime"]
            cases.append({"suite": suite, "sequence": sequence["id"], "fresh_instance": True,
                          "pass": True, "checks": checks, "repeat_after_reset": repeated,
                          "closed_call_rejected": True, "execution": report})
    final = validate_package(root, parameters=parameters, model_packages=model_packages)
    if final.manifest_sha256 != package.manifest_sha256 or final.manifest["files"] != package.manifest["files"]:
        raise ScenarioError("digest_mismatch", "Package changed during conformance execution")
    return {"status": "pass", "executor": "vlsp-independent-json", "format": package.manifest["format"],
            "manifest_sha256": package.manifest_sha256, "package_unchanged": True,
            "files_verified": len(package.manifest["files"]), "runtime": last_runtime,
            "delivery": report["delivery"],
            "model_input_mode": "JSON fixtures; no model inference executed by this scenario conformance run",
            "model_links": package.model_links, "sequence_checks": cases}


@contextmanager
def _executor_manager(root, *, authorized, parameters, model_packages):
    if authorized is None:
        with ScenarioExecutor(root, policy="development", parameters=parameters, model_packages=model_packages) as executor:
            yield executor
    else:
        @contextmanager
        def existing_authorization(_path):
            yield authorized
        with ScenarioExecutor.open_protected(root, verifier=existing_authorization, parameters=parameters, model_packages=model_packages) as executor:
            yield executor


def check_golden(source: str | Path, *, parameters=None, model_packages=None, policy="protected", verifier=None):
    if policy == "development":
        return _check_open_package(source, parameters=parameters, model_packages=model_packages, authorized=None)
    if policy != "protected" or not callable(verifier):
        raise ScenarioError("policy_rejected", "External conformance requires a configured protected opener; plaintext is development-only")
    try:
        with verifier(Path(source)) as authorized:
            return _check_open_package(authorized.root, parameters=parameters, model_packages=model_packages, authorized=authorized)
    except ScenarioError:
        raise
    except Exception as exc:
        raise ScenarioError("authorization_failed", f"Protected conformance authorization failed: {exc}") from exc
