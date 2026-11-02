"""Host-provided, correlated inference and pure tracking services.

Bindings are trusted infrastructure, not executable code supplied by the
scenario. A host must map frame_ref to the authenticated source and frame;
this reference implementation cannot inspect the host's media store.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from typing import Callable
from functools import wraps

from .common import json_copy, same_json
from .errors import ScenarioError


@dataclass(frozen=True)
class ModelBinding:
    kind: str
    manifest_sha256: str
    profile: str
    infer: Callable


@dataclass(frozen=True)
class TrackerBinding:
    implementation: str
    version: str
    track: Callable


@dataclass(frozen=True)
class Host:
    models: dict[str, ModelBinding]
    trackers: dict[str, TrackerBinding]


def _reject(message):
    raise ScenarioError("service_contract", message, fatal=True)


def _text(value, name):
    if type(value) is not str or not 1 <= len(value) <= 256:
        _reject(f"{name} must be a nonempty string of at most 256 characters")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        _reject(f"{name} must contain valid Unicode scalar values")
    return value


def _box(value):
    if (type(value) is not list or len(value) != 4 or any(type(n) not in (int, float)
        or not math.isfinite(n) or not 0 <= n <= 1 for n in value)
        or value[0] >= value[2] or value[1] >= value[3]):
        _reject("bbox must be normalized nonempty xyxy coordinates")
    return value


def _score(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        _reject("score must be finite and between zero and one")


def object_id(model_alias: str, raw_id: str) -> str:
    """Frame-scoped ID, distinct for equal raw IDs from different models."""
    parts = [_text(model_alias, "model_alias").encode(), _text(raw_id, "raw object_id").encode()]
    material = b"VLSP-OBJECT-V1\0" + b"".join(len(x).to_bytes(4, "big") + x for x in parts)
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _detections(value, *, raw, model_alias=None):
    if type(value) is not list:
        _reject("detections must be a list")
    result, seen = [], set()
    for row in value:
        if type(row) is not dict or set(row) != {"object_id", "label", "category_id", "score", "bbox"}:
            _reject("detection fields are invalid")
        row = json_copy(row, code="service_contract")
        _text(row["object_id"], "object_id")
        _text(row["label"], "label")
        if type(row["category_id"]) is not int or row["category_id"] < 0:
            _reject("category_id must be a non-negative integer")
        _score(row["score"])
        _box(row["bbox"])
        if raw:
            row["object_id"] = object_id(model_alias, row["object_id"])
        if row["object_id"] in seen:
            _reject("duplicate object_id within detection result")
        seen.add(row["object_id"])
        result.append(row)
    return result


def _sticky(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            if self._failure_fields is None:
                # The exception handed to publisher Python is mutable. Keep a
                # detached immutable record so catch/edit/rethrow cannot change
                # the host's original failure or its mandatory fatal policy.
                if isinstance(exc, ScenarioError):
                    fields = (exc.code, exc.message, exc.stage)
                else:
                    fields = ("service_failed", f"Host service failed: {type(exc).__name__}: {exc}", "execution")
                self._failure_fields = fields
                self._failure = ScenarioError(fields[0], fields[1], stage=fields[2], fatal=True)
            raise
    return guarded


class CallServices:
    def __init__(self, host: Host, manifest: dict, frame: dict, context: dict):
        self._host, self._frame, self._context = host, json_copy(frame, code="input_contract"), json_copy(context, code="input_contract")
        self._models = {item["alias"]: item for item in manifest["models"]}
        self._trackers = {item["alias"]: item for item in manifest["trackers"]}
        self._limits = manifest["limits"]
        self._objects: dict[str, list[list]] = {}
        self._detected: dict[str, dict] = {}
        self._active_tracks: dict[tuple[str, str], list] = {}
        self.model_calls = self.tracker_calls = 0
        self._active = True
        self._failure = None
        self._failure_fields = None

    def close(self):
        self._active = False

    def _check(self):
        if not self._active:
            _reject("Services are valid only during the current process call")

    @_sticky
    def infer(self, alias, request):
        self._check()
        if alias not in self._models or alias not in self._host.models:
            _reject("Undeclared or unavailable model alias")
        if self.model_calls >= self._limits["max_model_calls"]:
            _reject("max_model_calls exceeded")
        request = json_copy(request, code="service_contract", max_bytes=self._limits["max_input_bytes"])
        if type(request) is not dict or set(request) != {"frame_id", "frame_ref", "object_id", "bbox"}:
            _reject("infer request fields are invalid")
        if (request["frame_id"] != self._frame["frame_id"] or request["frame_ref"] != self._frame["frame_ref"]):
            _reject("frame identity/reference does not belong to this call")
        kind = self._models[alias]["kind"]
        if kind == "detection":
            if request["object_id"] is not None or request["bbox"] is not None:
                _reject("detection must refer to the full frame")
        elif kind == "classification":
            object_ref = _text(request["object_id"], "object_id")
            crop = _box(request["bbox"])
            bounds = self._objects.get(object_ref, [])
            if not any(crop[0] >= bound[0] and crop[1] >= bound[1]
                       and crop[2] <= bound[2] and crop[3] <= bound[3] for bound in bounds):
                _reject("classification crop must be contained within a known object's current bbox")
        else:
            _reject("unsupported model kind")
        self.model_calls += 1
        try:
            response = self._host.models[alias].infer(json_copy(request, code="service_contract"), json_copy(self._context, code="service_contract"))
        except ScenarioError:
            raise
        except Exception as exc:
            raise ScenarioError("service_failed", f"Model {alias} failed: {type(exc).__name__}: {exc}", fatal=True) from exc
        response = json_copy(response, code="service_contract", max_bytes=self._limits["max_input_bytes"])
        if kind == "detection":
            if type(response) is not dict or set(response) != {"detections"}:
                _reject("detection callback must return exactly detections")
            rows = _detections(response["detections"], raw=True, model_alias=alias)
            for row in rows:
                prior = self._detected.get(row["object_id"])
                if prior is not None and not same_json(prior, row):
                    _reject("object_id reused with conflicting detection in one frame")
                if prior is None:
                    self._objects[row["object_id"]] = [json_copy(row["bbox"], code="service_contract")]
                    self._detected[row["object_id"]] = json_copy(row, code="service_contract")
            return {"model_alias": alias, "frame_id": request["frame_id"], "object_id": None,
                    "kind": kind, "detections": rows}
        if type(response) is not dict or set(response) != {"classification"}:
            _reject("classification callback must return exactly classification")
        result = response["classification"]
        if type(result) is not dict or set(result) != {"label", "score", "category_id", "output_index"}:
            _reject("classification fields are invalid")
        _text(result["label"], "label")
        _score(result["score"])
        if any(type(result[k]) is not int or result[k] < 0 for k in ("category_id", "output_index")):
            _reject("classification indices must be non-negative integers")
        return {"model_alias": alias, "frame_id": request["frame_id"], "object_id": request["object_id"],
                "kind": kind, "classification": result}

    @_sticky
    def track(self, alias, request, tracker_state):
        self._check()
        if alias not in self._trackers or alias not in self._host.trackers:
            _reject("Undeclared or unavailable tracker alias")
        if self.tracker_calls >= self._limits["max_tracker_calls"]:
            _reject("max_tracker_calls exceeded")
        request = json_copy(request, code="service_contract", max_bytes=self._limits["max_input_bytes"])
        tracker_state = json_copy(tracker_state, code="service_contract", max_bytes=self._limits["max_state_bytes"])
        if type(tracker_state) is not dict or type(request) is not dict or set(request) != {"frame_id", "detections"}:
            _reject("tracker request fields or state are invalid")
        if request["frame_id"] != self._frame["frame_id"]:
            _reject("tracker frame mismatch")
        rows = _detections(request["detections"], raw=False)
        if any(row["object_id"] not in self._detected or not same_json(row, self._detected[row["object_id"]]) for row in rows):
            _reject("tracker detections must originate in this frame")
        self.tracker_calls += 1
        try:
            response = self._host.trackers[alias].track(json_copy(request, code="service_contract"), tracker_state, json_copy(self._context, code="service_contract"))
        except ScenarioError:
            raise
        except Exception as exc:
            raise ScenarioError("service_failed", f"Tracker {alias} failed: {type(exc).__name__}: {exc}", fatal=True) from exc
        response = json_copy(response, code="service_contract", max_bytes=self._limits["max_input_bytes"] + self._limits["max_state_bytes"])
        if type(response) is not dict or set(response) != {"tracks", "state"} or type(response["state"]) is not dict or type(response["tracks"]) is not list:
            _reject("tracker callback must return tracks and JSON state")
        json_copy(response["state"], code="service_contract", max_bytes=self._limits["max_state_bytes"])
        inputs = {row["object_id"] for row in rows}
        objects, tracks = set(), set()
        for track in response["tracks"]:
            if type(track) is not dict or set(track) != {"object_id", "track_id", "bbox"}:
                _reject("track fields are invalid")
            obj, tid = _text(track["object_id"], "object_id"), _text(track["track_id"], "track_id")
            _box(track["bbox"])
            if obj not in inputs or obj in objects or tid in tracks:
                _reject("tracker object/track identity invalid or duplicated")
            objects.add(obj); tracks.add(tid)
            self._objects[obj].append(json_copy(track["bbox"], code="service_contract"))
            self._active_tracks[(alias, tid)] = json_copy(track["bbox"], code="service_contract")
        return {"tracker_alias": alias, "frame_id": request["frame_id"], "tracks": response["tracks"], "state": response["state"]}
