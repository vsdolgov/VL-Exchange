"""Deterministic, read-only cases for the VideoPipe/VLSP v0.2 pilot.

``build_cases(package_root)`` returns JSON-serializable suites::

    {"name": str, "commands": [{"op": "open|process|reset|close", ...}]}

The harness MUST remove ``_expect`` before sending a command to either worker.
Expectations are a harness contract, never input to the system under test:

* ``ok`` is the expected command outcome.
* ``result`` is the exact successful VLSP ``{"events": ..., "state": ...}``.
* ``error`` is a subset of the actual error envelope; messages are not compared.
* ``no_commit`` requires unchanged state/order and no newly published events.
* ``no_calls`` requires zero model AND tracker calls for this command.

Each process carries lossless pixels as ``image={width,height,bgr_hex}``.
``fault={alias,mode,call?}`` injects a host-service fault; ``call`` counts calls
to that alias within the current process, starting at 1. These fault cases are
rejection probes and must not be reported as successful real inference.
``bindings={missing|wrong_digest: alias}`` changes the trusted host registry
before open. Every suite starts with a fresh protected open (the worker's
default policy) and ends with close. A second open follows close explicitly.

The package, golden fixtures, model files and ZIP are never modified here.
Additional source/run/reset assertions are external test vectors using the
same fixed red-left/blue-right image and unchanged scenario package.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import numpy as np


def build_cases(package_root: Path) -> list[dict]:
    """Build ten suites without importing a scenario executor or model loader."""
    root = Path(package_root)
    manifest = json.loads((root / "scenario-package.json").read_text("utf-8"))
    golden = json.loads((root / "golden/basic.json").read_text("utf-8"))
    media = json.loads((root / "golden/media.json").read_text("utf-8"))

    def image_at(relative: str) -> dict:
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
            raise ValueError("Unsafe fixture image path")
        image = np.load(path, allow_pickle=False)
        if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("Fixture image must be BGR uint8 HWC")
        return {"width": int(image.shape[1]), "height": int(image.shape[0]),
                "bgr_hex": np.ascontiguousarray(image).tobytes().hex()}

    fixture_images = {}
    for row in media["frames"]:
        path = root / row["path"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Fixture image digest mismatch")
        key = (row["source_id"], row["run_id"], row["frame_id"], row["frame_ref"])
        if key in fixture_images:
            raise ValueError("Duplicate fixture frame identity")
        fixture_images[key] = image_at(row["path"])
    both = image_at("golden/frames/both.npy")
    first_state = deepcopy(golden["steps"][0]["expected"]["state"])
    event_type = manifest["event_contract"]["types"][0]["event_type"]
    base_time = 1700000000000

    def opened(**kwargs) -> dict:
        return {"op": "open", **kwargs, "_expect": {"ok": True}}

    def closed() -> dict:
        return {"op": "close", "_expect": {"ok": True}}

    def reset(source: str | None = None, run: str | None = None) -> dict:
        command = {"op": "reset", "_expect": {"ok": True}}
        if source is not None:
            command["source_id"] = source
        if run is not None:
            command["run_id"] = run
        return command

    def error(code: str, *, fatal: bool = False, no_calls: bool = False,
              stage: str | None = None) -> dict:
        detail = {"code": code, "fatal": fatal, "retryable": False}
        if stage is not None:
            detail["stage"] = stage
        return {"ok": False, "error": detail, "no_commit": True,
                **({"no_calls": True} if no_calls else {})}

    def event_id(context: dict) -> str:
        # Independent implementation of the published length-prefixed test
        # vector; no import from either host under comparison.
        fields = [manifest["scenario_id"], manifest["version"],
                  context["source_id"], context["run_id"],
                  str(context["sequence"]), event_type, "0"]
        material = b"VLSP-EVENT-V1\0"
        for field in fields:
            encoded = field.encode("utf-8", "strict")
            material += len(encoded).to_bytes(4, "big") + encoded
        return "sha256:" + hashlib.sha256(material).hexdigest()

    def process(source: str, run: str, frame: str, seq: int, count: int,
                *, emits: bool = False, timestamp: int | None = None,
                fault: dict | None = None, expect: dict | None = None) -> dict:
        context = {"source_id": source, "run_id": run, "sequence": seq,
                   "event_time_ms": base_time + seq * 1000 if timestamp is None else timestamp}
        descriptor = {"frame_id": frame, "frame_ref": f"{source}/{run}/{frame}",
                      "width": both["width"], "height": both["height"]}
        state = deepcopy(first_state)
        state["streaks"]["t0001"] = count
        events = []
        if emits:
            events.append({"event_id": event_id(context), "event_type": event_type,
                           **context, "payload": {"frame_id": frame, "track_id": "t0001",
                           "classifier_a": "red", "classifier_b": "red", "count": count}})
        command = {"op": "process", "inputs": {"frame": descriptor},
                   "context": context, "image": deepcopy(both),
                   "_expect": {"ok": True, "result": {"events": events, "state": state}}
                   if expect is None else deepcopy(expect)}
        if fault is not None:
            command["fault"] = deepcopy(fault)
        return command

    # Preserve every original golden step exactly, including opaque refs and
    # the right-only frame. The caller must not regenerate or rewrite the ZIP.
    baseline = [opened()]
    for step in golden["steps"]:
        if "reset" in step:
            baseline.append({"op": "reset", **deepcopy(step["reset"]),
                             "_expect": {"ok": True}})
            continue
        frame, context = step["inputs"]["frame"], step["context"]
        key = (context["source_id"], context["run_id"], frame["frame_id"], frame["frame_ref"])
        expectation = step["expected"]
        if "error" in expectation:
            expect = {"ok": False, "error": deepcopy(expectation["error"]),
                      "no_commit": True, "no_calls": True}
        else:
            expect = {"ok": True, "result": deepcopy(expectation)}
        baseline.append({"op": "process", "inputs": deepcopy(step["inputs"]),
                         "context": deepcopy(context), "image": deepcopy(fixture_images[key]),
                         "_expect": expect})
    baseline.append(closed())
    suites = [{"name": "baseline-nine-golden-steps", "commands": baseline}]

    suites.append({"name": "source-run-isolation-and-scoped-resets", "commands": [
        opened(),
        process("camera-a", "run-1", "a1", 1, 1),
        process("camera-b", "run-1", "b1", 1, 1),
        process("camera-a", "run-2", "a1", 1, 1),
        process("camera-a", "run-1", "a2", 2, 2, emits=True),
        reset("camera-a", "run-1"),
        process("camera-b", "run-1", "b2", 2, 2, emits=True),
        process("camera-a", "run-2", "a2", 2, 2, emits=True),
        process("camera-a", "run-1", "a1", 1, 1),
        process("camera-a", "run-1", "a2", 2, 2, emits=True),
        reset("camera-a"),
        process("camera-b", "run-1", "b3", 3, 3),
        process("camera-a", "run-1", "a1", 1, 1),
        process("camera-a", "run-2", "a1", 1, 1),
        reset(),
        process("camera-b", "run-1", "b1", 1, 1),
        closed(),
    ]})

    order_error = error("input_order", no_calls=True, stage="input")
    input_error = error("input_contract", no_calls=True, stage="input")
    invalid_context = process("camera-a", "run-1", "bad-context", 2, 0, expect=input_error)
    invalid_context["context"]["event_time_ms"] = True
    suites.append({"name": "order-rejections-remain-healthy-and-allow-gap-equal-time", "commands": [
        opened(),
        process("camera-a", "run-1", "a1", 1, 1),
        process("camera-a", "run-1", "duplicate-seq", 1, 0, expect=order_error),
        process("camera-a", "run-1", "backward-seq", 0, 0, expect=order_error),
        process("camera-a", "run-1", "backward-time", 2, 0,
                timestamp=base_time + 999, expect=order_error),
        process("camera-a", "run-1", "a1", 2, 0, expect=order_error),
        invalid_context,
        process("camera-a", "run-1", "gap-equal-time", 5, 2,
                timestamp=base_time + 1000, emits=True),
        process("camera-a", "run-1", "a6", 6, 3),
        closed(),
    ]})

    reverse = {"alias": "detector", "mode": "reverse_detections"}
    suites.append({"name": "object-correlation-survives-detection-order-reversal", "commands": [
        opened(),
        process("camera-a", "run-1", "a1", 1, 1, fault=reverse),
        process("camera-a", "run-1", "a2", 2, 2, emits=True, fault=reverse),
        closed(),
    ]})

    def fatal_suite(name: str, fault: dict, failure_code: str, *, recover: bool = False) -> dict:
        fatal_error = error(failure_code, fatal=True)
        dead_error = error("lifecycle", fatal=True, no_calls=True, stage="lifecycle")
        commands = [opened(),
                    process("camera-a", "run-1", "a1", 1, 1),
                    process("camera-b", "run-1", "b1", 1, 1),
                    process("camera-a", "run-1", "a2", 2, 0,
                            fault=fault, expect=fatal_error),
                    # Fatality applies to the whole instance, including another
                    # source, and a reset must not resurrect it.
                    process("camera-b", "run-1", "b2", 2, 0, expect=dead_error),
                    {"op": "reset", "source_id": "camera-a", "run_id": "run-1",
                     "_expect": deepcopy(dead_error)}, closed()]
        if recover:
            commands += [opened(), process("camera-a", "run-1", "a1", 1, 1),
                         process("camera-a", "run-1", "a2", 2, 2, emits=True), closed()]
        return {"name": name, "commands": commands}

    suites.append(fatal_suite("late-classifier-exception-atomicity-fatal-and-fresh-recovery",
                  {"alias": "classifier_b", "mode": "exception", "call": 2},
                  "service_failed", recover=True))
    suites.append(fatal_suite("detector-duplicate-object-id-rejected",
                  {"alias": "detector", "mode": "duplicate_objects"}, "service_contract"))
    suites.append(fatal_suite("tracker-unknown-object-id-rejected",
                  {"alias": "tracker", "mode": "unknown_object"}, "service_contract"))
    suites.append(fatal_suite("tracker-duplicate-track-id-rejected",
                  {"alias": "tracker", "mode": "duplicate_tracks"}, "service_contract"))
    suites.append(fatal_suite("late-classifier-invalid-score-rejected-without-candidate-leak",
                  {"alias": "classifier_b", "mode": "bad_score", "call": 2, "value": 1.5},
                  "service_contract"))

    missing = opened(bindings={"missing": "classifier_b"})
    missing["_expect"] = error("dependency_missing", no_calls=True, stage="requirements")
    wrong_digest = opened(bindings={"wrong_digest": "classifier_b"})
    wrong_digest["_expect"] = error("model_link_mismatch", no_calls=True, stage="requirements")
    suites.append({"name": "required-bindings-rejected-before-import-and-clean-reopen", "commands": [
        missing, closed(), wrong_digest, closed(), opened(),
        process("camera-a", "run-1", "a1", 1, 1), closed(), closed(),
    ]})
    return suites
