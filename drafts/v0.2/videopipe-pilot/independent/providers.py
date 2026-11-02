"""Pixel-backed providers for the independent VideoPipe host.

Only the VLMP ONNX package loaders are shared with other hosts.  Frame ingress,
crop selection, service dispatch, fault probes and the pure tracker are local
to this host; no scenario runtime or prerecorded answers are used here.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from model_package_v02 import load_model
from public_model import ModelExecutor


class ProviderInputError(ValueError):
    """The current command did not supply usable, correlated frame pixels."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bbox(value) -> bool:
    return (
        type(value) is list and len(value) == 4
        and all(type(x) in (int, float) and math.isfinite(x) and 0 <= x <= 1
                for x in value)
        and value[0] < value[2] and value[1] < value[3]
    )


def _iou(a, b) -> float:
    width = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    intersection = width * height
    union = ((a[2] - a[0]) * (a[3] - a[1])
             + (b[2] - b[0]) * (b[3] - b[1]) - intersection)
    return intersection / union if union > 0 else 0.0


def _greedy_iou(request: dict, state: dict) -> dict:
    """Implement org.vlexchange.demo.greedy-iou/1.0.0 as a pure function."""
    prior = state["tracks"]
    available = set(prior)
    next_id = state["next_id"]
    tracks, next_tracks = [], {}
    detections = sorted(request["detections"],
                        key=lambda d: (d["bbox"][0], d["bbox"][1], d["object_id"]))
    for detection in detections:
        box = detection["bbox"]
        candidates = sorted((-_iou(box, prior[track_id]), track_id)
                            for track_id in available)
        if candidates and -candidates[0][0] >= 0.5:
            track_id = candidates[0][1]
            available.remove(track_id)
        else:
            track_id = f"t{next_id:04d}"
            next_id += 1
        next_tracks[track_id] = deepcopy(box)
        tracks.append({"object_id": detection["object_id"],
                       "track_id": track_id, "bbox": deepcopy(box)})
    return {"tracks": tracks, "state": {"next_id": next_id, "tracks": next_tracks}}


class ProviderSet:
    """Own three ONNX executors and expose the host's trusted provider registry.

    ``counts`` and ``trace`` are cumulative over this object's lifetime.  Fault
    call ordinals, active pixels and the expiry callback are scoped to one
    ``begin``/``end`` interval.  All callbacks return backend data; the scenario
    service adapter adds the response correlation envelope.
    """

    def __init__(self, pins: dict, fixture_root: Path):
        self.pins = deepcopy(pins)
        self.fixture_root = Path(fixture_root).resolve()
        self.models = {}
        self.trackers = {}
        self.trace = []
        self.counts = Counter()
        self._calls = Counter()
        self._executors = {}
        self._identities = {}
        self._active = None
        self._fault = None
        self._expire = None
        self._closed = False
        try:
            for alias, pinned in self.pins["models"].items():
                root = self.fixture_root / "models" / alias
                manifest_data = (root / "model-package.json").read_bytes()
                manifest_digest = _sha(manifest_data)
                if manifest_digest != pinned["manifest_sha256"]:
                    raise ValueError(f"Pinned model manifest mismatch: {alias}")
                manifest = json.loads(manifest_data)
                profile = manifest["profile"]
                detection = type(profile) is dict
                profile_id = profile["id"] if detection else profile
                weights_name = profile["weights"] if detection else manifest["backend"]["weights"]
                weights_digest = _sha((root / weights_name).read_bytes())
                if weights_digest != pinned["weights_sha256"]:
                    raise ValueError(f"Pinned model weights mismatch: {alias}")
                executor = load_model(root) if detection else ModelExecutor(root)
                self._executors[alias] = executor
                if executor.session.get_providers() != ["CPUExecutionProvider"]:
                    raise ValueError(f"Expected CPU ONNX provider: {alias}")
                kind = "detection" if detection else "classification"
                self._identities[alias] = {
                    "manifest_sha256": manifest_digest,
                    "weights_sha256": weights_digest,
                    "kind": kind,
                }
                self.models[alias] = SimpleNamespace(
                    kind=kind, manifest_sha256=manifest_digest, profile=profile_id,
                    infer=lambda request, context, alias=alias: self._infer(alias, request, context),
                )
            self.trackers["tracker"] = SimpleNamespace(
                implementation="org.vlexchange.demo.greedy-iou", version="1.0.0",
                track=lambda request, state, context: self._track(request, state, context),
            )
        except BaseException:
            self.close()
            raise

    def bindings(self, overrides: dict | None = None) -> SimpleNamespace:
        """Return an isolated registry, including deliberate binding probes."""
        models = {alias: SimpleNamespace(**vars(binding))
                  for alias, binding in self.models.items()}
        trackers = {alias: SimpleNamespace(**vars(binding))
                    for alias, binding in self.trackers.items()}
        overrides = overrides or {}
        missing = overrides.get("missing")
        if missing is not None:
            models.pop(missing, None)
            trackers.pop(missing, None)
        wrong_digest = overrides.get("wrong_digest")
        if wrong_digest in models:
            models[wrong_digest].manifest_sha256 = "0" * 64
        return SimpleNamespace(models=models, trackers=trackers)

    def begin(self, command: dict, expire_callback=None) -> None:
        """Bind lossless ingress pixels for exactly the current process call."""
        self.end()
        self._calls.clear()
        if self._closed:
            raise ProviderInputError("Providers are closed")
        if type(command) is not dict:
            raise ProviderInputError("Expected a process command object")
        image = command.get("image")
        if type(image) is not dict or set(image) != {"width", "height", "bgr_hex"}:
            raise ProviderInputError("Expected width, height and lossless BGR hex pixels")
        width, height = image["width"], image["height"]
        if type(width) is not int or type(height) is not int or min(width, height) < 1:
            raise ProviderInputError("Image dimensions must be positive integers")
        encoded = image["bgr_hex"]
        # Check length before allocating decoded data.  bytes.fromhex accepts
        # whitespace, which is excluded so the wire form remains unambiguous.
        if (type(encoded) is not str or len(encoded) != width * height * 6
                or any(c not in "0123456789abcdefABCDEF" for c in encoded)):
            raise ProviderInputError("Image BGR hex length or encoding is invalid")
        inputs = command.get("inputs")
        frame = inputs.get("frame") if type(inputs) is dict else None
        if (type(frame) is not dict or frame.get("width") != width
                or frame.get("height") != height
                or type(frame.get("width")) is not int
                or type(frame.get("height")) is not int
                or type(frame.get("frame_id")) is not str
                or not frame["frame_id"]
                or type(frame.get("frame_ref")) is not str
                or not frame["frame_ref"]):
            raise ProviderInputError("Ingress pixels do not match the current frame descriptor")
        shape = (height, width, 3)
        for alias, identity in self._identities.items():
            if identity["kind"] == "detection":
                expected = tuple(self._executors[alias].manifest["source_input"]["shape"])
                if shape != expected:
                    raise ProviderInputError(f"Frame shape is incompatible with {alias}")
        raw = bytes.fromhex(encoded)
        pixels = np.frombuffer(raw, dtype=np.uint8).reshape(shape)
        pixels.flags.writeable = False
        self._active = {"frame": deepcopy(frame), "context": deepcopy(command.get("context")),
                        "pixels": pixels, "sha256": _sha(raw)}
        self._fault = deepcopy(command.get("fault"))
        self._expire = expire_callback

    def end(self) -> None:
        self._active = None
        self._fault = None
        self._expire = None

    def _current_pixels(self, request: dict, context: dict) -> np.ndarray:
        active = self._active
        if active is None:
            raise ProviderInputError("No active ingress frame")
        frame = active["frame"]
        if (request.get("frame_id") != frame["frame_id"]
                or request.get("frame_ref") != frame["frame_ref"]
                or context != active["context"]):
            raise ProviderInputError("Model request does not refer to the current ingress")
        return active["pixels"]

    def _infer(self, alias: str, request: dict, context: dict) -> dict:
        pixels = self._current_pixels(request, context)
        identity = self._identities[alias]
        executor = self._executors[alias]
        if identity["kind"] == "detection":
            if request.get("object_id") is not None or request.get("bbox") is not None:
                raise ProviderInputError("Detection must use the complete ingress frame")
            crop = pixels
        else:
            box = request.get("bbox")
            if not _bbox(box) or type(request.get("object_id")) is not str or not request["object_id"]:
                raise ProviderInputError("Classification requires an object and a valid normalized bbox")
            height, width = pixels.shape[:2]
            left, top = math.floor(box[0] * width), math.floor(box[1] * height)
            right, bottom = math.ceil(box[2] * width), math.ceil(box[3] * height)
            crop = np.ascontiguousarray(pixels[top:bottom, left:right])
            if not crop.size:
                raise ProviderInputError("Classification crop is empty")
        # Every requested model service executes its ONNX graph before any
        # response fault is injected; trace actual_output records that result.
        self.counts[alias] += 1
        self._calls[alias] += 1
        if identity["kind"] == "detection":
            actual = executor.infer(crop)
        else:
            actual = {"classification": executor.predict(crop)}
        entry = {
            "service": "model", "alias": alias, "context": deepcopy(context),
            "request": {k: deepcopy(v) for k, v in request.items() if k != "frame_ref"},
            "frame_sha256": self._active["sha256"],
            "crop_sha256": _sha(crop.tobytes(order="C")), "crop_shape": list(crop.shape),
            "backend": "CPUExecutionProvider", "manifest_sha256": identity["manifest_sha256"],
            "weights_sha256": identity["weights_sha256"], "inference_executed": True,
            "actual_output": deepcopy(actual),
        }
        self.trace.append(entry)
        return self._returned(alias, actual, entry)

    def _track(self, request: dict, state: dict, context: dict) -> dict:
        if (self._active is None or request.get("frame_id") != self._active["frame"]["frame_id"]
                or context != self._active["context"]):
            raise ProviderInputError("Tracker request does not refer to the current ingress")
        self.counts["tracker"] += 1
        self._calls["tracker"] += 1
        actual = _greedy_iou(deepcopy(request), deepcopy(state))
        entry = {
            "service": "tracker", "alias": "tracker", "context": deepcopy(context),
            "request": deepcopy(request), "input_state": deepcopy(state),
            "implementation": "org.vlexchange.demo.greedy-iou", "version": "1.0.0",
            "actual_output": deepcopy(actual),
        }
        self.trace.append(entry)
        return self._returned("tracker", actual, entry)

    def _returned(self, alias: str, actual: dict, entry: dict) -> dict:
        result = deepcopy(actual)
        fault = self._fault or {}
        if fault.get("alias") == alias and self._calls[alias] == fault.get("call", 1):
            mode = fault["mode"]
            entry["fault_injection"] = mode
            if mode == "exception":
                entry["returned"] = "exception"
                raise RuntimeError(f"Injected provider exception: {alias}")
            if mode == "reverse_detections":
                result["detections"].reverse()
            elif mode == "duplicate_objects":
                if result["detections"]:
                    result["detections"].append(deepcopy(result["detections"][0]))
            elif mode == "unknown_object":
                if result["tracks"]:
                    result["tracks"][0]["object_id"] = "unknown-object"
            elif mode == "duplicate_tracks":
                if len(result["tracks"]) > 1:
                    result["tracks"][1]["track_id"] = result["tracks"][0]["track_id"]
            elif mode == "bad_score":
                result["classification"]["score"] = 1.5
            elif mode == "expire_lease":
                if self._expire is None:
                    raise RuntimeError("Expiry probe has no lease callback")
                self._expire()
            else:
                raise ValueError(f"Unknown provider fault: {mode}")
            entry["returned_output"] = deepcopy(result)
        return result

    def close(self) -> None:
        self.end()
        for executor in self._executors.values():
            executor.close()
        self._executors.clear()
        self._closed = True
