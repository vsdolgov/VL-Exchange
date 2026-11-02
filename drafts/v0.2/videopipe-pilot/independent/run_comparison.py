"""Second experiment: independent host inside the pinned real VideoPipe graph.

The parent harness may import the reference implementation as the comparator.
The native child executes a different worker with reference imports blocked.
The normative stage/code/fatal/retryable fields must match without translation.
"""
from pathlib import Path
import sys

PILOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PILOT / "python"))
import bootstrap
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess

from cases import build_cases
from prepare import prepare, sha
from runtime import FIXTURES, Session, environment, verify_fixtures
from run_pilot import Native, assertions, extra_suites, hashes, source_paths, write_json

REPORTS = PILOT / "independent/reports"

class IndependentNative(Native):
    def __init__(self, binary, report_dir):
        self.stderr_path = report_dir / "videopipe-stderr.txt"
        self.stderr = self.stderr_path.open("w")
        self.process = subprocess.Popen([str(binary), "--python", sys.executable,
            "--worker", str(PILOT / "independent/worker.py")], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.stderr, text=True, bufsize=1,
            start_new_session=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"})
        self.seen, self.frames = 0, 0


def clean_error(response):
    return {key: value for key, value in response.get("error", {}).items() if key != "message"}


def actual_trace(response):
    # Fault injection can deliberately corrupt data differently in two hosts.
    # Compare the actual computed backend output before that corruption.
    return [{key: value for key, value in row.items() if key not in ("returned_output", "returned")}
            for row in response["trace"]]


def check_independent(command, expected, response, before, reference, pins, suite):
    assert response["id"] == command["id"]
    assert response["ok"] is expected["ok"], response.get("error")
    independence = response["independence"]
    assert independence["guard_active"] and not independence["forbidden_import_attempts"]
    assert independence["loaded_reference_modules"] == []
    if "result" in expected:
        assert response["result"] == expected["result"], (response.get("result"), expected["result"])
    if not response["ok"]:
        assert "result" not in response
        assert clean_error(response) == clean_error(reference), (suite, clean_error(reference), clean_error(response))
        for key, value in expected["error"].items():
            assert raw_field(response, key) == value, (suite, response["error"])
    if expected.get("no_commit"):
        if command["op"] == "open":
            assert response["snapshot"] == {"state": {}, "order": {}} and response["published_events"] == []
        else:
            assert response["snapshot"] == before["snapshot"]
            assert response["published_events"] == before["published_events"]
    if expected.get("no_calls"):
        assert response["trace"] == []
        if command["op"] != "open":
            assert response["calls"] == before["calls"]
    if command["op"] == "open":
        if response["ok"]:
            assert response["scenario_imports"] == 2
            assert response["opened"] == reference["opened"]
            assert response["opened"]["scenario_zip_sha256"] == pins["scenario_zip_sha256"]
        else:
            assert response["scenario_imports"] == 0
    # Business/state boundary equality is required even when raw diagnostics
    # differ. Internal counters/lifecycle policy are recorded separately.
    for field in ("snapshot", "published_events", "calls", "scenario_imports"):
        assert response[field] == reference[field], (suite, field)
    assert response.get("result") == reference.get("result")
    assert actual_trace(response) == actual_trace(reference), (suite, "computed service trace differs")


def raw_field(response, key):
    return response["error"][key]


def run():
    REPORTS.mkdir(parents=True, exist_ok=True)
    for stale in ("comparison.json", "comparison-failed.json"):
        (REPORTS / stale).unlink(missing_ok=True)
    pins = prepare()
    binary = PILOT / "build/adapter/vlsp_videopipe"
    build = json.loads((PILOT / "build/cpp-build-manifest.json").read_text())
    binary_before = sha(binary)
    assert binary_before == build["binaries"]["build/adapter/vlsp_videopipe"]
    for relative, digest in build["adapter_sources"].items():
        assert sha(PILOT / "cpp" / relative) == digest
    paths = source_paths() + sorted((PILOT / "independent").rglob("*.py"))
    before_sources = hashes(paths)
    cases = extra_suites(build_cases(FIXTURES / "scenario"))
    reference, native = Session(), IndependentNative(binary, REPORTS)
    refs, others, comparisons, differences = [], [], [], []
    before_ref = before_other = {"snapshot": {"state": {}, "order": {}}, "published_events": [], "calls": {}}
    command_id, success = 0, False
    try:
        for suite in cases:
            for index, original in enumerate(suite["commands"]):
                command = deepcopy(original)
                expected = command.pop("_expect")
                command_id += 1
                command["id"] = command_id
                ref = reference.handle(command)
                other = native.request(command)
                row = {"suite": suite["name"], "index": index, "command": command, "expected": expected}
                refs.append({**row, "actual": ref})
                others.append({**row, "actual": other})
                assertions(command, expected, ref, before_ref, pins)
                check_independent(command, expected, other, before_other, ref, pins, suite["name"])
                diagnostic = {}
                if clean_error(ref) != clean_error(other):
                    diagnostic["raw_error"] = {"reference": clean_error(ref), "independent": clean_error(other)}
                if ref["lifecycle"] != other["lifecycle"]:
                    diagnostic["host_lifecycle_metrics_or_policy"] = {"reference": ref["lifecycle"], "independent": other["lifecycle"]}
                if ref["trace"] != other["trace"]:
                    diagnostic["injected_corrupted_diagnostics_differ"] = True
                if diagnostic:
                    differences.append({"id": command_id, "suite": suite["name"], "op": command["op"], **diagnostic})
                comparisons.append({"id": command_id, "suite": suite["name"], "index": index,
                    "op": command["op"], "pass": True, "ok": ref["ok"],
                    "external_events_state_order_equal": True, "computed_backend_trace_equal": True,
                    "raw_error_envelope_equal": clean_error(ref) == clean_error(other),
                    "reference_error": clean_error(ref), "independent_error": clean_error(other)})
                before_ref, before_other = ref, other
            print(json.dumps({"suite": suite["name"], "status": "pass"}), flush=True)
        command_id += 1
        command = {"id": command_id, "op": "shutdown"}
        end_ref, end_other = reference.handle(command), native.request(command)
        assert end_ref["snapshot"] == end_other["snapshot"]
        assert not end_other["independence"]["loaded_reference_modules"]
        native.finish()
        verify_fixtures()
        assert hashes(paths) == before_sources, "Sources changed during the comparison"
        assert sha(binary) == binary_before
        success = True
    finally:
        reference.shutdown()
        if not success:
            native.abort()
        write_json(REPORTS / "reference.json", {"runs": refs})
        write_json(REPORTS / "independent-videopipe.json", {"runs": others})
        if not success:
            write_json(REPORTS / "comparison-failed.json", {"status": "failed", "completed": comparisons})
    report = {"status": "pass",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "test": "reference-vs-separately-implemented-host-in-real-videopipe",
        "scenario_zip_sha256": pins["scenario_zip_sha256"], "scenario_manifest_sha256": pins["scenario_manifest_sha256"],
        "native_binary_sha256": binary_before, "videopipe_commit": build["videopipe"]["commit"],
        "environment": environment(), "sources_sha256": before_sources,
        "suites": len(cases), "commands_compared": len(comparisons),
        "process_commands": sum(row["op"] == "process" for row in comparisons),
        "expected_rejections": sum(not row["ok"] for row in comparisons),
        "successful_process_oracles": sum(row["op"] == "process" and row["ok"] for row in comparisons),
        "business_events_state_and_order_match": True, "computed_model_and_tracker_results_match": True,
        "raw_error_envelopes_all_equal": not any("raw_error" in row for row in differences),
        "error_comparison": "exact stage/code/fatal/retryable; diagnostic message excluded; no category translation",
        "diagnostic_and_policy_differences": differences, "comparisons": comparisons,
        "native_shutdown_transport": end_other["transport"], "independence": end_other["independence"],
        "sources_schemas_binary_and_fixture_snapshots_unchanged": True,
        "reference_fix": {"problem": "A scenario could edit/rethrow a service exception with nonfatal flags while the host instance was failed",
            "change": "Host-owned detached service-error record and mandatory fatal/nonretryable process exceptions",
            "before_artifact": "reference-fault-before-fix.json", "regression_cases": 2,
            "scenario_archive_changed": False},
        "limitations": ["Separately written local host, not an independent vendor or formal clean-room implementation",
            "Schemas, model package loaders, ONNX Runtime, crypto and native VideoPipe transport are shared",
            "Scenario orchestration, validation, services, tracker, state/order, lifecycle and event identity are separately implemented",
            "Internal counters and diagnostic trace details are not a normative compatibility interface",
            "Synthetic CPU models and lossless 4x2 BGR frames; RTSP, GPU, load, async MLLM and production isolation untested"]}
    write_json(REPORTS / "comparison.json", report)
    print(json.dumps({key: report[key] for key in ("status", "suites", "commands_compared", "process_commands",
          "expected_rejections", "successful_process_oracles", "raw_error_envelopes_all_equal")}), flush=True)


if __name__ == "__main__":
    run()
