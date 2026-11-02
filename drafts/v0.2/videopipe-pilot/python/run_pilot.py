"""Compare the reference execution with real native VideoPipe graph integration."""
import bootstrap
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import select
import signal
import subprocess
import sys

import numpy as np

from cases import build_cases
from prepare import prepare, sha
from runtime import FIXTURES, Session, environment, verify_fixtures

PILOT = bootstrap.PILOT


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n")


def normalized(response):
    value = deepcopy(response)
    value.pop("transport", None)
    if "error" in value:
        value["error"].pop("message", None)
    return value


class Native:
    def __init__(self, binary, report_dir):
        self.stderr_path = report_dir / "videopipe-stderr.txt"
        self.stderr = self.stderr_path.open("w")
        self.process = subprocess.Popen([str(binary), "--python", sys.executable,
            "--worker", str(PILOT / "python/worker.py")], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.stderr, text=True, bufsize=1,
            start_new_session=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"})
        self.seen, self.frames = 0, 0

    def request(self, command):
        self.process.stdin.write(json.dumps(command, ensure_ascii=False, allow_nan=False) + "\n")
        self.process.stdin.flush()
        ready, _, _ = select.select([self.process.stdout], [], [], 60)
        if not ready:
            raise TimeoutError("Native VideoPipe RPC exceeded 60 seconds; commit is unknown")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"VideoPipe/worker closed stdout; see {self.stderr_path}")
        response = json.loads(line)
        if response.get("error", {}).get("code") == "videopipe_transport_error":
            raise RuntimeError("VideoPipe infrastructure failure; worker commit is unknown: " + response["error"]["message"])
        self.seen += 1
        t = response["transport"]
        assert t["engine"] == "VideoPipe"
        assert t["source_seen"] == t["adapter_seen"] == t["sink_seen"] == self.seen
        assert len(set(t["threads"].values())) == 3, "Must traverse three native processing threads"
        assert t["hooks"]["vlsp_adapter"]["handling"]["count"] == self.seen
        if command["op"] == "process":
            assert t["meta_type"] == "FRAME"
            assert t["native_frame_index"] == self.frames
            assert t["native_channel_index"] == 0
            self.frames += 1
            expected_logical = {**command["context"], "frame_id": command["inputs"]["frame"]["frame_id"]}
            assert t["logical"] == expected_logical
            raw = bytes.fromhex(command["image"]["bgr_hex"])
            digest = 14695981039346656037
            for byte in raw:
                digest = ((digest ^ byte) * 1099511628211) & ((1 << 64)-1)
            assert t["frame_bytes"] == len(raw) and t["frame_fnv1a64"] == f"{digest:016x}"
        else:
            assert t["meta_type"] == "CONTROL" and t["frame_bytes"] == 0
        return response

    def finish(self):
        self.process.stdin.close()
        try:
            code = self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.abort()
            raise TimeoutError("VideoPipe/worker failed to shut down")
        self.stderr.close()
        assert code == 0, f"Native process exit {code}; see {self.stderr_path}"
        assert "worker exited with status" not in self.stderr_path.read_text()

    def abort(self):
        # Kill the complete native+Python process group; a worker blocked on
        # inference must not be left behind when the RPC timeout fires.
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        except ProcessLookupError:
            pass
        self.stderr.close()


def assertions(command, expected, actual, before, pins):
    assert actual["id"] == command["id"]
    assert actual["ok"] is expected["ok"], actual.get("error")
    if "result" in expected:
        assert actual["result"] == expected["result"], (actual.get("result"), expected["result"])
    if "error" in expected:
        assert "result" not in actual, "An error must not carry a partially computed successful result"
        for key, value in expected["error"].items():
            assert actual["error"][key] == value, actual["error"]
    if expected.get("no_calls"):
        assert actual["trace"] == [], "Rejected command must not invoke model/tracker callbacks"
        if command["op"] != "open":
            assert actual["calls"] == before["calls"]
    if expected.get("no_commit"):
        if command["op"] == "open":
            assert actual["snapshot"] == {"state": {}, "order": {}} and actual["published_events"] == []
        else:
            assert actual["snapshot"] == before["snapshot"], "State/order changed on failed process"
            assert actual["published_events"] == before["published_events"], "Candidate event leaked on failure"
    if command["op"] == "open":
        if actual["ok"]:
            assert actual["scenario_imports"] == 2, "Both __init__ and scenario must execute only after validation"
            assert actual["opened"]["scenario_zip_sha256"] == pins["scenario_zip_sha256"]
            assert actual["opened"]["scenario_manifest_sha256"] == pins["scenario_manifest_sha256"]
            assert len({m["weights_sha256"] for m in actual["opened"]["models"].values()}) == 3
            assert actual["opened"]["protected"] is (command.get("policy", "offline") == "offline")
        else:
            assert actual["scenario_imports"] == 0, "Required dependency must be rejected before any scenario code executes"
    if command["op"] == "process":
        frame = command["inputs"]["frame"]
        image = command["image"]
        raw = bytes.fromhex(image["bgr_hex"])
        pixels = np.frombuffer(raw, dtype=np.uint8).reshape(image["height"], image["width"], 3)
        for trace in actual["trace"]:
            assert trace["context"] == command["context"]
            assert trace["request"]["frame_id"] == frame["frame_id"]
            if trace["service"] != "model":
                continue
            alias = trace["alias"]
            assert trace["inference_executed"] is True
            assert trace["manifest_sha256"] == pins["models"][alias]["manifest_sha256"]
            assert trace["weights_sha256"] == pins["models"][alias]["weights_sha256"]
            assert trace["frame_sha256"] == hashlib.sha256(raw).hexdigest()
            crop = pixels
            if alias != "detector":
                x1, y1, x2, y2 = trace["request"]["bbox"]
                h, w = pixels.shape[:2]
                crop = pixels[math.floor(y1*h):math.ceil(y2*h), math.floor(x1*w):math.ceil(x2*w)]
            assert trace["crop_shape"] == list(crop.shape)
            assert trace["crop_sha256"] == hashlib.sha256(crop.tobytes()).hexdigest()
        if command.get("fault", {}).get("alias") == "classifier_b" and command["fault"].get("call") == 2:
            aliases = [row["alias"] for row in actual["trace"]]
            assert aliases == ["detector", "tracker", "classifier_a", "classifier_b", "classifier_a", "classifier_b"]
            assert actual["published_events"] == before["published_events"]


def extra_suites(cases):
    # Reuse independent literal expected results, without changing the package.
    baseline = cases[0]["commands"]
    a1, a2 = deepcopy(baseline[1]), deepcopy(baseline[3])
    def op(name, **kwargs):
        return {"op": name, **kwargs, "_expect": {"ok": True}}
    def rejected(code, *, stage, retryable=False):
        return {"ok": False, "error": {"code": code, "stage": stage, "fatal": False, "retryable": retryable},
                "no_commit": True, "no_calls": True}
    expired = deepcopy(a2)
    expired["fault"] = {"alias": "classifier_b", "mode": "expire_lease", "call": 2}
    expired["_expect"] = rejected("authorization_failed", stage="security", retryable=True)
    expired["_expect"].pop("no_calls")
    still_expired = deepcopy(a2)
    still_expired["_expect"] = rejected("authorization_failed", stage="security", retryable=True)
    cases.append({"name": "original-offline-execution-lease-expires-before-commit", "commands": [
        op("open"), deepcopy(a1), expired, still_expired, op("close"), op("open"), deepcopy(a1), op("close")]})
    after_close = deepcopy(a2)
    after_close["_expect"] = rejected("lifecycle", stage="lifecycle")
    reset_closed = op("reset")
    reset_closed["_expect"] = rejected("lifecycle", stage="lifecycle")
    cases.append({"name": "close-is-idempotent-and-instance-cannot-restart", "commands": [
        op("open"), deepcopy(a1), op("close"), after_close, reset_closed, op("close")]})
    wrong_dimensions = deepcopy(a2)
    wrong_dimensions["inputs"]["frame"]["height"] = 1
    wrong_dimensions["_expect"] = rejected("input_contract", stage="input")
    cases.append({"name": "actual-image-dimensions-checked-before-inference", "commands": [
        op("open"), deepcopy(a1), wrong_dimensions, deepcopy(a2), op("close")]})
    cases.append({"name": "explicit-development-delivery-smoke", "commands": [
        op("open", policy="development"), deepcopy(a1), deepcopy(a2), op("close")]})
    empty = deepcopy(a1)
    empty["image"]["bgr_hex"] = "00" * 24
    empty["_expect"] = {"ok": True, "result": {"events": [], "state": {
        "tracker": {"next_id": 1, "tracks": {}}, "streaks": {}}}}
    cases.append({"name": "successful-empty-detection-is-distinct-from-service-error", "commands": [
        op("open"), empty, op("close")]})
    return cases


def source_paths():
    sources = [PILOT / "python" / name for name in ("runtime.py", "worker.py", "cases.py", "run_pilot.py", "prepare.py", "bootstrap.py")]
    for directory in ("drafts/v0.2/scenario/src/vl_scenario_v02", "drafts/v0.2/model/src/model_package_v02", "drafts/v0.2/security-runtime/src/vl_exchange_security"):
        sources.extend(sorted((bootstrap.WORKSPACE / directory).glob("*.py")))
    sources += [bootstrap.WORKSPACE / "model/src/public_model.py", bootstrap.WORKSPACE / "drafts/v0.2/videopipe-pilot/python/integration_host.py"]
    for directory in ("drafts/v0.2/scenario/spec", "drafts/v0.2/model/spec", "model/spec"):
        sources.extend(sorted((bootstrap.WORKSPACE / directory).rglob("*.json")))
    return sources


def hashes(paths):
    return {p.relative_to(bootstrap.WORKSPACE).as_posix(): sha(p) for p in paths}


def run(binary, report_dir):
    report_dir.mkdir(parents=True, exist_ok=True)
    # A failed rerun must not leave an old PASS as if it described this run.
    for stale in ("comparison.json", "comparison-failed.json"):
        (report_dir / stale).unlink(missing_ok=True)
    binary_before = sha(binary)
    sources = source_paths()
    source_before = hashes(sources)
    build_manifest = json.loads((PILOT / "build/cpp-build-manifest.json").read_text())
    for relative, digest in build_manifest["binaries"].items():
        assert sha(PILOT / relative) == digest, f"Binary differs from build manifest: {relative}"
    for relative, digest in build_manifest["adapter_sources"].items():
        assert sha(PILOT / "cpp" / relative) == digest, f"Adapter source differs from compiled manifest: {relative}"
    upstream = PILOT / "vendor/VideoPipe"
    commit = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    assert commit == build_manifest["videopipe"]["commit"]
    assert not subprocess.check_output(["git", "-C", str(upstream), "status", "--porcelain", "--untracked-files=no"], text=True)
    pins = prepare()
    cases = extra_suites(build_cases(FIXTURES / "scenario"))
    reference, native = Session(), Native(binary, report_dir)
    ref_rows, vp_rows, comparisons = [], [], []
    before_ref = before_vp = {"snapshot": {"state": {}, "order": {}}, "published_events": [], "calls": {}}
    command_id, successful = 0, False
    try:
        for suite in cases:
            for index, original in enumerate(suite["commands"]):
                command = deepcopy(original)
                expected = command.pop("_expect")
                command_id += 1
                command["id"] = command_id
                ref = reference.handle(command)
                vp = native.request(command)
                assertions(command, expected, ref, before_ref, pins)
                assertions(command, expected, vp, before_vp, pins)
                assert normalized(ref) == normalized(vp), f"Cross-runtime mismatch in {suite['name']} step {index}"
                row = {"suite": suite["name"], "index": index, "command": command, "expected": expected}
                ref_rows.append({**row, "actual": ref})
                vp_rows.append({**row, "actual": vp})
                comparisons.append({"suite": suite["name"], "index": index, "id": command_id,
                    "op": command["op"], "pass": True, "ok": ref["ok"],
                    "error_code": ref.get("error", {}).get("code"),
                    "compared": ["events", "state", "ordering", "published_events", "error.stage/code/fatal/retryable",
                                 "lifecycle", "actual_model_and_tracker_trace", "import_boundary"]})
                before_ref, before_vp = ref, vp
            print(json.dumps({"suite": suite["name"], "status": "pass", "commands": len(suite["commands"])}), flush=True)
        command_id += 1
        command = {"op": "shutdown", "id": command_id}
        end_ref, end_vp = reference.handle(command), native.request(command)
        assert normalized(end_ref) == normalized(end_vp)
        native.finish()
        verify_fixtures()
        assert sha(binary) == binary_before, "Native binary changed during the run"
        assert hashes(source_paths()) == source_before, "Runtime sources or schemas changed during the run; rerun against a stable snapshot"
        assert sha(bootstrap.WORKSPACE / "drafts/v0.2/scenario/examples/vlsp-color-cascade.zip") == pins["scenario_zip_sha256"]
        # Explicit numerical fingerprints detect accidentally using classifier A
        # for both aliases despite identical labels in scenario events.
        fingerprints = {}
        for row in ref_rows:
            if row["suite"] != "baseline-nine-golden-steps":
                continue
            for entry in row["actual"]["trace"]:
                if entry["alias"] in ("classifier_a", "classifier_b") and entry["request"]["bbox"] == [0.0, 0.0, 0.5, 1.0]:
                    fingerprints.setdefault(entry["alias"], entry["actual_output"]["classification"]["score"])
        assert abs(fingerprints["classifier_a"] - 0.5761168847658291) < 1e-6
        assert abs(fingerprints["classifier_b"] - 0.7869859933853149) < 1e-6
        assert fingerprints["classifier_a"] != fingerprints["classifier_b"]
        successful = True
    finally:
        reference.shutdown()
        if not successful:
            native.abort()
        write_json(report_dir / "reference.json", {"runs": ref_rows})
        write_json(report_dir / "videopipe.json", {"runs": vp_rows})
        if not successful:
            write_json(report_dir / "comparison-failed.json", {"status": "failed", "completed": comparisons})
    process_rows = [row for row in comparisons if row["op"] == "process"]
    baseline_trace = [trace for row in ref_rows if row["suite"] == "baseline-nine-golden-steps" for trace in row["actual"]["trace"]]
    baseline_counts = {alias: sum(row["alias"] == alias for row in baseline_trace)
                       for alias in ("detector", "classifier_a", "classifier_b", "tracker")}
    report = {"status": "pass", "checked_at": datetime.now(timezone.utc).isoformat(),
        "test": "unchanged-vlsp-reference-vs-real-videopipe-adapter",
        "scenario_zip_sha256": pins["scenario_zip_sha256"], "scenario_manifest_sha256": pins["scenario_manifest_sha256"],
        "models": {alias: {key: row[key] for key in ("manifest_sha256", "weights_sha256")} for alias, row in pins["models"].items()},
        "environment": environment(), "native_binary_sha256": binary_before, "native_build": build_manifest,
        "suites": len(cases), "commands_compared": len(comparisons), "process_commands": len(process_rows),
        "expected_errors_compared": sum(not row["ok"] for row in comparisons),
        "baseline_model_and_tracker_calls_each_host": baseline_counts,
        "classifier_red_crop_score_fingerprints": fingerprints,
        "transport_shutdown": end_vp["transport"], "graceful_native_and_worker_exit": True,
        "scenario_zip_and_all_dependency_snapshots_unchanged": True,
        "source_sha256": source_before, "runtime_sources_schemas_and_binary_unchanged_during_run": True,
        "comparisons": comparisons,
        "limitations": ["Reference ScenarioExecutor, model loaders, ONNX Runtime, tracker and security implementation are shared",
            "VideoPipe is a real C++ graph integration, not a third independently implemented VLSP interpreter",
            "Synthetic untrained CPU ONNX models; no industrial analytical quality claim",
            "Headless upstream subset; no RTSP/video codec, GUI, native VideoPipe inference backend, GPU or load benchmark",
            "One command in flight; no parallel fork/join or asynchronous MLLM",
            "Offline protection uses ephemeral keys; lease expiry uses a controlled host clock offset",
            "Local harness ingress; native channel 0 multiplexes logical cameras, authentication is outside this adapter",
            "A transport failure has unknown worker commit and is an infrastructure failure; crash recovery is not implemented"]}
    write_json(report_dir / "comparison.json", report)
    print(json.dumps({key: report[key] for key in ("status", "suites", "commands_compared", "process_commands",
                                                  "expected_errors_compared", "scenario_zip_sha256")}), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, default=PILOT / "build/adapter/vlsp_videopipe")
    parser.add_argument("--reports", type=Path, default=PILOT / "reports")
    args = parser.parse_args()
    run(args.binary.absolute(), args.reports.absolute())
