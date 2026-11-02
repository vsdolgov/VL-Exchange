"""Run real CPU ONNX models and a pure tracker with one protected VLSP package.

All images and models are synthetic, untrained interoperability probes. This is
a local host adapter, not Box 5 integration or a third-party product.
"""
from functools import partial
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time

import numpy as np
from nacl.public import PrivateKey
from nacl.signing import SigningKey
from public_model import ModelExecutor as PublicClassifier
from model_package_v02 import load_model as load_detector
from vl_exchange_security import OnlineAuthorizer, authorized_package, seal_package
from vl_exchange_security.reference_server import ReferenceLicenseService, make_server
from vl_scenario_v02 import ScenarioExecutor, ScenarioError, Host, ModelBinding, TrackerBinding

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "scenario/examples/color-cascade"
ARCHIVE = ROOT / "scenario/examples/vlsp-color-cascade.zip"
MODELS = {
    "detector": ROOT / "model/examples/vl-color-detector",
    "classifier_a": ROOT.parents[1] / "model/examples/channel-classifier",
    "classifier_b": ROOT / "videopipe-pilot/fixtures/models/classifier_b",
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_hashes(root):
    return {p.relative_to(root).as_posix(): sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


def overlap(a, b):
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a, area_b = (a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1])
    return intersection / (area_a + area_b - intersection) if intersection else 0.0


def pure_greedy_iou(request, state, context):
    """Deterministic greedy IoU association, pure JSON state; misses expire immediately."""
    prior = state.get("tracks", {})
    next_id = state.get("next_id", 1)
    available, tracks, new = set(prior), [], {}
    for detection in sorted(request["detections"], key=lambda x: (x["bbox"][0], x["bbox"][1], x["object_id"])):
        candidates = sorted(((overlap(detection["bbox"], prior[tid]), tid) for tid in available), key=lambda x: (-x[0], x[1]))
        if candidates and candidates[0][0] >= 0.5:
            tid = candidates[0][1]
            available.remove(tid)
        else:
            tid, next_id = f"t{next_id:04d}", next_id + 1
        bbox = detection["bbox"]
        tracks.append({"object_id": detection["object_id"], "track_id": tid, "bbox": bbox})
        new[tid] = bbox
    return {"tracks": tracks, "state": {"next_id": next_id, "tracks": new}}


class DemoHost:
    def __init__(self, scenario_root=PACKAGE):
        self.detector = load_detector(MODELS["detector"])
        self.hashes = {alias: sha(root / "model-package.json") for alias, root in MODELS.items()}
        self.classifiers = {alias: PublicClassifier(MODELS[alias]) for alias in ("classifier_a", "classifier_b")}
        golden = json.loads((scenario_root / "golden/basic.json").read_text())
        entries = json.loads((scenario_root / "golden/media.json").read_text())["frames"]
        media = {(row["source_id"], row["run_id"], row["frame_id"], row["frame_ref"]): row for row in entries}
        if len(media) != len(entries):
            raise ValueError("duplicate frame fixture binding")
        self.frames = {}
        for step in golden["steps"]:
            if "inputs" not in step:
                continue
            frame, context = step["inputs"]["frame"], step["context"]
            key = (context["source_id"], context["run_id"], frame["frame_id"], frame["frame_ref"])
            row = media[key]
            path = scenario_root / row["path"]
            if not path.resolve().is_relative_to(scenario_root.resolve()) or path.is_symlink() or sha(path) != row["sha256"]:
                raise ValueError("unsafe or changed frame fixture")
            pixels = np.load(path, allow_pickle=False)
            if pixels.shape != (frame["height"], frame["width"], 3) or pixels.dtype != np.uint8:
                raise ValueError("frame dimensions or pixel format do not match the host registry")
            self.frames[key] = pixels
        self.calls = []
        self.tracker_calls = []
        self.host = Host(models={
            "detector": ModelBinding("detection", self.hashes["detector"], "detection-onnx-cpu-v1", self.detector_callback),
            "classifier_a": ModelBinding("classification", self.hashes["classifier_a"], "python-numpy-classification-v1", self.classifier("classifier_a")),
            "classifier_b": ModelBinding("classification", self.hashes["classifier_b"], "python-numpy-classification-v1", self.classifier("classifier_b")),
        }, trackers={"tracker": TrackerBinding("org.vlexchange.demo.greedy-iou", "1.0.0", self.track)})

    def get_frame(self, request, context):
        key = (context["source_id"], context["run_id"], request["frame_id"], request["frame_ref"])
        if key not in self.frames:
            raise ValueError("frame reference is not bound to the source, run and frame")
        return self.frames[key]

    def detector_callback(self, request, context):
        result = self.detector.infer(self.get_frame(request, context))
        self.calls.append({"alias": "detector", "frame_id": request["frame_id"], "detections": len(result["detections"])})
        return result

    def classifier(self, alias):
        def infer(request, context):
            frame = self.get_frame(request, context)
            x1, y1, x2, y2 = request["bbox"]
            height, width = frame.shape[:2]
            crop = np.ascontiguousarray(frame[math.floor(y1 * height):math.ceil(y2 * height), math.floor(x1 * width):math.ceil(x2 * width)])
            prediction = self.classifiers[alias].predict(crop)
            self.calls.append({"alias": alias, "frame_id": request["frame_id"], "object_id": request["object_id"],
                               "label": prediction["label"], "score": prediction["score"]})
            return {"classification": prediction}
        return infer

    def track(self, request, state, context):
        result = pure_greedy_iou(request, state, context)
        self.tracker_calls.append({"frame_id": request["frame_id"], "track_ids": [x["track_id"] for x in result["tracks"]]})
        return result

    def close(self):
        self.detector.close()
        for model in self.classifiers.values():
            model.close()


def check_sequence(executor):
    sequence = json.loads((PACKAGE / "golden/basic.json").read_text())
    checks, first_event, replay_event = [], None, None
    for index, step in enumerate(sequence["steps"]):
        if "reset" in step:
            executor.reset(**step["reset"])
            checks.append({"index": index, "kind": "reset", "pass": True})
            continue
        snapshot = executor.snapshot()
        try:
            actual = executor.process(step["inputs"], step["context"])
        except ScenarioError as exc:
            expected = step["expected"].get("error")
            assert expected is not None and all(exc.to_dict()[k] == v for k, v in expected.items())
            assert executor.snapshot() == snapshot
            checks.append({"index": index, "kind": "error", "pass": True, "actual": exc.to_dict(), "no_commit": True})
            continue
        assert actual == step["expected"], f"Step {index}: actual={actual!r} expected={step['expected']!r}"
        if actual["events"]:
            if first_event is None:
                first_event = actual["events"][0]["event_id"]
            else:
                replay_event = actual["events"][0]["event_id"]
        checks.append({"index": index, "kind": "process", "pass": True, "actual": actual})
    assert first_event and first_event == replay_event
    return {"status": "pass", "checks": checks, "same_event_id_after_replay": first_event == replay_event}


def main():
    before = {alias: package_hashes(root) for alias, root in MODELS.items()}
    before_scenario, archive_before = package_hashes(PACKAGE), sha(ARCHIVE)
    weights = {alias: root / json.loads((root / "model-package.json").read_text())["profile"]["weights"]
               if alias == "detector" else root / json.loads((root / "model-package.json").read_text())["backend"]["weights"]
               for alias, root in MODELS.items()}
    assert len({sha(p) for p in weights.values()}) == 3
    issuer, recipient, installation, server_key = SigningKey.generate(), PrivateKey.generate(), SigningKey.generate(), SigningKey.generate()
    service = ReferenceLicenseService(issuer_verify_key=issuer.verify_key, server_signing_key=server_key)
    server = make_server(service, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    modes = {}
    try:
        with tempfile.TemporaryDirectory(prefix="vlsp-v02-integration-") as temp:
            for mode in ("offline", "online"):
                adapter = DemoHost()
                try:
                    envelope = Path(temp) / mode
                    now = int(time.time())
                    info = seal_package(ARCHIVE, envelope, issuer_signing_key=issuer, recipient_public_key=recipient.public_key,
                        installation_verify_key=installation.verify_key, issuer_id="fixture-issuer", license_id="fixture-"+mode,
                        installation_id="fixture-installation", mode=mode, issued_at=now, not_before=now, expires_at=now+3600,
                        manifest_path="scenario-package.json")
                    authorizer = None if mode == "offline" else OnlineAuthorizer(
                        f"http://127.0.0.1:{server.server_port}/v1/grant", server_key.verify_key, installation, allow_loopback_http=True)
                    verifier = partial(authorized_package, issuer_verify_key=issuer.verify_key,
                                       recipient_private_key=recipient, policy=mode, online_authorizer=authorizer)
                    with ScenarioExecutor.open_protected(envelope, host=adapter.host, verifier=verifier) as executor:
                        result = check_sequence(executor)
                    assert info["package_sha256"] == archive_before
                    assert set(x["alias"] for x in adapter.calls) == set(MODELS)
                    modes[mode] = {**result, "signed_archive_sha256": info["package_sha256"], "model_calls": adapter.calls,
                                   "tracker_calls": adapter.tracker_calls, "real_detector_inference_calls": adapter.detector.inference_calls}
                finally:
                    adapter.close()
    finally:
        server.shutdown(); thread.join(timeout=5); server.server_close()
    assert before == {alias: package_hashes(root) for alias, root in MODELS.items()}
    assert before_scenario == package_hashes(PACKAGE) and archive_before == sha(ARCHIVE)
    report = {"status": "pass", "test": "vlsp-v02-real-three-onnx-models-and-pure-iou-tracker",
              "scenario_zip_sha256": archive_before, "scenario_manifest_sha256": sha(PACKAGE / "scenario-package.json"),
              "models": {alias: {"manifest_sha256": sha(root / "model-package.json"),
                                  "weights_sha256": sha(weights[alias])} for alias, root in MODELS.items()},
              "modes": modes, "models_and_scenario_unchanged": True,
              "limitations": ["Models are synthetic untrained color probes, not people/PPE detectors",
                              "Host is a local prototype, not Box 5 or competitor system",
                              "Online license server is local loopback test service",
                              "Model licenses are separate from scenario license",
                              "Synchronous frame service profile only; asynchronous MLLM is separate"]}
    (ROOT / "reports/multimodel-protected-integration.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    print(json.dumps({"status": "pass", "modes": list(modes), "steps_each": len(next(iter(modes.values()))["checks"]),
                      "distinct_model_graphs": 3, "scenario_zip_sha256": archive_before}))


if __name__ == "__main__":
    main()
