"""Trusted pilot host. Images come only from the current ingress command.

The same reference VLSP interpreter, model loaders and tracker are intentionally
used in-process and behind the native VideoPipe bridge. This checks integration,
not an independently implemented interpreter or production model quality.
"""
import bootstrap
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from functools import partial
import hashlib
import json
import math
from pathlib import Path
import platform
import sys
import tempfile
import time

import numpy as np
import onnxruntime
from nacl.public import PrivateKey
from nacl.signing import SigningKey

from integration_host import pure_greedy_iou
from model_package_v02 import load_model as load_detector
from public_model import ModelExecutor as PublicClassifier
from vl_exchange_security import authorized_package, seal_package
from vl_scenario_v02 import Host, ModelBinding, ScenarioError, ScenarioExecutor, TrackerBinding
from prepare import inventory, sha, safe_name
import zipfile

FIXTURES = bootstrap.PILOT / "fixtures"
_IMPORT_AUDIT = []
_WATCH_IMPORTS = False


def _audit(event, args):
    if event == "exec" and _WATCH_IMPORTS:
        path = getattr(args[0], "co_filename", "")
        if path.endswith(("/code/__init__.py", "/code/scenario.py")):
            _IMPORT_AUDIT.append(path)


sys.addaudithook(_audit)


def verify_fixtures():
    pins = json.loads((FIXTURES / "pins.json").read_text())
    if sha(FIXTURES / "scenario.zip") != pins["scenario_zip_sha256"]:
        raise ValueError("Changed scenario ZIP")
    if inventory(FIXTURES / "scenario") != pins["scenario_files"]:
        raise ValueError("Changed scenario snapshot")
    for alias, record in pins["models"].items():
        if inventory(FIXTURES / "models" / alias) != record["files"]:
            raise ValueError(f"Changed model snapshot: {alias}")
    return pins


class InferenceHost:
    def __init__(self, pins):
        self.pins = pins
        self.detector = load_detector(FIXTURES / "models/detector")
        self.classifiers = {alias: PublicClassifier(FIXTURES / "models" / alias)
                            for alias in ("classifier_a", "classifier_b")}
        self.trace = []
        self.counts = Counter()
        self.active = None
        self.fault = None
        self.per_call = Counter()
        self.expire = None

    def begin(self, command, expire):
        image = command["image"]
        width, height = image["width"], image["height"]
        raw = bytes.fromhex(image["bgr_hex"])
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0 or len(raw) != width * height * 3:
            raise ScenarioError("input_contract", "Malformed BGR ingress image")
        frame = command["inputs"].get("frame", {})
        if frame.get("width") != width or frame.get("height") != height:
            raise ScenarioError("input_contract", "Descriptor dimensions differ from the actual ingress cv::Mat image")
        pixels = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3).copy()
        pixels.flags.writeable = False
        self.active = (deepcopy(frame), deepcopy(command["context"]), pixels)
        self.fault, self.per_call, self.expire = command.get("fault"), Counter(), expire

    def end(self):
        self.active, self.fault, self.expire = None, None, None

    def frame(self, request, context):
        if self.active is None:
            raise ValueError("No active scoped frame")
        frame, expected_context, pixels = self.active
        if (context != expected_context or request["frame_id"] != frame["frame_id"]
                or request["frame_ref"] != frame["frame_ref"]):
            raise ValueError("Frame/source/run reference is outside the current ingress scope")
        return pixels

    def inject(self, alias, output, trace):
        fault = self.fault or {}
        self.per_call[alias] += 1
        if fault.get("alias") != alias or self.per_call[alias] != fault.get("call", 1):
            return output
        mode = fault["mode"]
        trace["fault_injection"] = mode
        if mode == "exception":
            trace["returned"] = "exception"
            raise RuntimeError(f"Injected {alias} failure after actual computation")
        changed = deepcopy(output)
        if mode == "reverse_detections":
            changed["detections"].reverse()
        elif mode == "duplicate_objects":
            changed["detections"].append(deepcopy(changed["detections"][0]))
        elif mode == "unknown_object":
            changed["tracks"][0]["object_id"] = "not-a-current-frame-object"
        elif mode == "duplicate_tracks":
            changed["tracks"][1]["track_id"] = changed["tracks"][0]["track_id"]
        elif mode == "bad_score":
            changed["classification"]["score"] = 1.5
        elif mode == "expire_lease":
            self.expire()
        else:
            raise ValueError(f"Unknown fault mode {mode}")
        trace["returned_output"] = deepcopy(changed)
        return changed

    def infer(self, alias):
        def callback(request, context):
            pixels = self.frame(request, context)
            crop = pixels
            if alias != "detector":
                x1, y1, x2, y2 = request["bbox"]
                height, width = pixels.shape[:2]
                crop = np.ascontiguousarray(pixels[math.floor(y1*height):math.ceil(y2*height),
                                                   math.floor(x1*width):math.ceil(x2*width)])
                output = {"classification": self.classifiers[alias].predict(crop)}
            else:
                output = self.detector.infer(pixels)
            self.counts[alias] += 1
            trace = {"service": "model", "alias": alias, "context": deepcopy(context),
                     "request": {key: deepcopy(value) for key, value in request.items() if key != "frame_ref"},
                     "frame_sha256": hashlib.sha256(pixels.tobytes()).hexdigest(),
                     "crop_sha256": hashlib.sha256(crop.tobytes()).hexdigest(),
                     "crop_shape": list(crop.shape), "backend": "CPUExecutionProvider",
                     "manifest_sha256": self.pins["models"][alias]["manifest_sha256"],
                     "weights_sha256": self.pins["models"][alias]["weights_sha256"],
                     "inference_executed": True, "actual_output": deepcopy(output)}
            self.trace.append(trace)
            return self.inject(alias, output, trace)
        return callback

    def track(self, request, state, context):
        if self.active is None or context != self.active[1] or request["frame_id"] != self.active[0]["frame_id"]:
            raise ValueError("Tracker outside current frame scope")
        output = pure_greedy_iou(request, state, context)
        self.counts["tracker"] += 1
        trace = {"service": "tracker", "alias": "tracker", "context": deepcopy(context),
                 "request": deepcopy(request), "input_state": deepcopy(state),
                 "implementation": "org.vlexchange.demo.greedy-iou", "version": "1.0.0",
                 "actual_output": deepcopy(output)}
        self.trace.append(trace)
        return self.inject("tracker", output, trace)

    def bindings(self, overrides):
        models = {alias: ModelBinding("detection" if alias == "detector" else "classification",
                    pin["manifest_sha256"], "detection-onnx-cpu-v1" if alias == "detector" else "python-numpy-classification-v1",
                    self.infer(alias)) for alias, pin in self.pins["models"].items()}
        if overrides.get("missing"):
            models.pop(overrides["missing"])
        if overrides.get("wrong_digest"):
            alias = overrides["wrong_digest"]
            binding = models[alias]
            models[alias] = ModelBinding(binding.kind, "0"*64, binding.profile, binding.infer)
        return Host(models, {"tracker": TrackerBinding("org.vlexchange.demo.greedy-iou", "1.0.0", self.track)})

    def close(self):
        self.detector.close()
        for model in self.classifiers.values():
            model.close()


class Session:
    def __init__(self):
        self.pins = verify_fixtures()
        self.host = None
        self.executor = None
        self.stack = None
        self.published = []
        self.policy = None
        self.offset = 0
        self.closed = False

    def _close(self):
        if self.stack is not None:
            self.stack.close()
            self.stack = None
        elif self.executor is not None:
            self.executor.close()

    def _open(self, command):
        global _WATCH_IMPORTS
        self._close()
        self.executor, self.published, self.offset = None, [], 0
        verify_fixtures()
        if self.host is None:
            self.host = InferenceHost(self.pins)
        self.host.counts.clear()
        self.policy = command.get("policy", "offline")
        if self.policy not in ("offline", "development"):
            raise ScenarioError("policy_rejected", "Pilot supports protected offline and explicit development only")
        stack = ExitStack()
        try:
            temp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="vlsp-videopipe-")))
            host = self.host.bindings(command.get("bindings", {}))
            if self.policy == "development":
                root = temp / "scenario"
                root.mkdir()
                with zipfile.ZipFile(FIXTURES / "scenario.zip") as zf:
                    for info in zf.infolist():
                        target = root / safe_name(info.filename)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(zf.read(info))
                _WATCH_IMPORTS = True
                self.executor = stack.enter_context(ScenarioExecutor(root, policy="development", host=host))
            else:
                issuer, recipient, installation = SigningKey.generate(), PrivateKey.generate(), SigningKey.generate()
                now = int(time.time())
                envelope = temp / "protected"
                sealed = seal_package(FIXTURES / "scenario.zip", envelope, issuer_signing_key=issuer,
                    recipient_public_key=recipient.public_key, installation_verify_key=installation.verify_key,
                    issuer_id="videopipe-pilot-ephemeral", license_id="pilot-offline",
                    installation_id="pilot-local-installation", mode="offline", issued_at=now,
                    not_before=now, expires_at=now+3600, manifest_path="scenario-package.json")
                if sealed["package_sha256"] != self.pins["scenario_zip_sha256"]:
                    raise ValueError("Protected envelope licenses a different ZIP")
                verifier = partial(authorized_package, issuer_verify_key=issuer.verify_key,
                    recipient_private_key=recipient, policy="offline", now=lambda: int(time.time()) + self.offset)
                _WATCH_IMPORTS = True
                self.executor = stack.enter_context(ScenarioExecutor.open_protected(envelope, verifier=verifier, host=host))
            self.stack = stack
        except BaseException:
            stack.close()
            self.executor = None
            raise
        finally:
            _WATCH_IMPORTS = False
        return {"policy": self.policy, "protected": self.policy == "offline",
                "scenario_zip_sha256": self.pins["scenario_zip_sha256"],
                "scenario_manifest_sha256": self.pins["scenario_manifest_sha256"],
                "models": {alias: {key: pin[key] for key in ("manifest_sha256", "weights_sha256")}
                           for alias, pin in self.pins["models"].items()}}

    def handle(self, command):
        if self.host is not None:
            self.host.trace.clear()
        response = {"id": command.get("id"), "ok": True}
        imports_before = len(_IMPORT_AUDIT)
        op = command.get("op")
        try:
            if op == "open":
                response["opened"] = self._open(command)
            elif op == "shutdown":
                self.shutdown()
            elif op == "close":
                self._close()
            else:
                if self.executor is None:
                    raise ScenarioError("lifecycle", "No open scenario instance")
                if op == "process":
                    try:
                        self.host.begin(command, lambda: setattr(self, "offset", self.offset + 3601))
                        result = self.executor.process(deepcopy(command["inputs"]), deepcopy(command["context"]))
                    finally:
                        self.host.end()
                    response["result"] = result
                    self.published.extend(deepcopy(result["events"]))
                elif op == "reset":
                    reset = command.get("reset", {key: command[key] for key in ("source_id", "run_id") if key in command})
                    self.executor.reset(**reset)
                elif op != "snapshot":
                    raise ScenarioError("input_contract", "Unknown pilot operation")
        except ScenarioError as exc:
            response.update(ok=False, error=exc.to_dict())
        # Unexpected adapter/programming exceptions are deliberately not converted
        # to scenario errors: the harness must fail, not report false equivalence.
        response["snapshot"] = self.executor.snapshot() if self.executor is not None else {"state": {}, "order": {}}
        response["published_events"] = deepcopy(self.published)
        response["trace"] = deepcopy(self.host.trace) if self.host is not None else []
        response["calls"] = dict(sorted(self.host.counts.items())) if self.host is not None else {}
        response["lifecycle"] = self.executor.report()["lifecycle"] if self.executor is not None else None
        response["scenario_imports"] = len(_IMPORT_AUDIT) - imports_before
        return response

    def shutdown(self):
        if self.closed:
            return
        self._close()
        if self.host is not None:
            self.host.close()
        self.closed = True


def environment():
    return {"python": platform.python_version(), "platform": platform.platform(),
            "machine": platform.machine(), "numpy": np.__version__, "onnxruntime": onnxruntime.__version__,
            "available_providers": onnxruntime.get_available_providers()}
