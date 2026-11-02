"""Reference implementation for a narrow, deterministic detection ONNX probe.

This engine implements only the declared fixed-shape CPU profile. It cannot
run arbitrary third party model packages; model publisher is trusted.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath

import numpy as np
import onnx
import onnxruntime as ort
from jsonschema import Draft202012Validator, validators

ROOT = Path(__file__).resolve().parents[2]


class ModelError(ValueError):
    pass


def _pairs(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ModelError("Duplicate JSON key")
        d[k] = v
    return d


def _invalid(value):
    raise ModelError(f"Non-finite value {value}")


def _read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs, parse_constant=_invalid)
        json.dumps(data, allow_nan=False, ensure_ascii=False).encode("utf-8", "strict")
        return data
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise ModelError("Invalid strict JSON") from exc


def _path(root, name):
    if type(name) is not str or not name or "\\" in name or any(p in ("", ".", "..") for p in name.split("/")):
        raise ModelError("Noncanonical package path")
    path = PurePosixPath(name)
    if path.is_absolute():
        raise ModelError("Absolute package path")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ModelError("Symlink is not allowed")
    if not current.is_file() or not current.resolve().is_relative_to(root):
        raise ModelError("Missing or unsafe package file")
    return current


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


_types = Draft202012Validator.TYPE_CHECKER.redefine_many({
    "integer": lambda _, x: type(x) is int,
    "number": lambda _, x: type(x) in (int, float) and math.isfinite(x),
})
StrictValidator = validators.extend(Draft202012Validator, type_checker=_types)


class DetectionModel:
    def __init__(self, root):
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            raise ModelError("Invalid package root")
        self.root = root.resolve(strict=True)
        manifest_file = _path(self.root, "model-package.json")
        self.manifest_sha256 = _digest(manifest_file)
        self.manifest = _read_json(manifest_file)
        schema = _read_json(ROOT / "spec/model-package.schema.json")
        errors = list(StrictValidator(schema).iter_errors(self.manifest))
        if errors:
            raise ModelError(f"Invalid model manifest: {errors[0].message}")
        inventory = self.manifest["files"]
        paths = list(self.root.rglob("*"))
        if any(p.is_symlink() for p in paths):
            raise ModelError("Package contains symlink")
        actual = {p.relative_to(self.root).as_posix() for p in paths if p.is_file()} - {"model-package.json"}
        if actual != set(inventory):
            raise ModelError("File inventory mismatch")
        for name, digest in inventory.items():
            if _digest(_path(self.root, name)) != digest:
                raise ModelError("Package digest mismatch")
        profile = self.manifest["profile"]
        if profile["weights"] not in inventory:
            raise ModelError("Weights not in inventory")
        for item in self.manifest["golden"]:
            if item["input"] not in inventory:
                raise ModelError("Golden input not in inventory")
        weights = _path(self.root, profile["weights"])
        graph = onnx.load(str(weights), load_external_data=False)
        if any(t.data_location == onnx.TensorProto.EXTERNAL or t.external_data for t in graph.graph.initializer):
            raise ModelError("External ONNX weights are unsupported")
        onnx.checker.check_model(graph, full_check=True)
        self.session = ort.InferenceSession(str(weights), providers=["CPUExecutionProvider"])
        inputs, outputs = self.session.get_inputs(), self.session.get_outputs()
        n = profile["max_detections"]
        if (self.manifest["source_input"]["shape"] != [profile["input_shape"][2], profile["input_shape"][3], 3]
            or len({profile["output_boxes"],profile["output_scores"],profile["output_classes"]}) != 3):
            raise ModelError("Input or output declaration is inconsistent")
        categories = profile["categories"]
        if len({x["category_id"] for x in categories}) != len(categories):
            raise ModelError("Duplicate category ID")
        self.categories = {x["category_id"]: x["label"] for x in categories}
        expected_outputs = {profile["output_boxes"]: ("tensor(float)",[1,n,4]),
                            profile["output_scores"]: ("tensor(float)",[1,n]),
                            profile["output_classes"]: ("tensor(int64)",[1,n])}
        if (self.session.get_providers() != ["CPUExecutionProvider"] or len(inputs) != 1 or len(outputs) != 3
            or inputs[0].name != profile["input_name"] or inputs[0].type != "tensor(float)"
            or inputs[0].shape != profile["input_shape"]
            or {x.name:(x.type,x.shape) for x in outputs} != expected_outputs):
            raise ModelError("ONNX graph does not match declared CPU FP32 profile")
        self.profile = profile
        self.inference_calls = 0

    def infer(self, frame_bgr):
        shape = tuple(self.manifest["source_input"]["shape"])
        if not isinstance(frame_bgr, np.ndarray) or frame_bgr.dtype != np.uint8 or frame_bgr.shape != shape:
            raise ModelError(f"Expected BGR uint8 ndarray of shape {shape}")
        rgb = frame_bgr[:, :, ::-1]
        tensor = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None], dtype=np.float32) / np.float32(255.0)
        names = [self.profile["output_boxes"],self.profile["output_scores"],self.profile["output_classes"]]
        boxes, scores, classes = self.session.run(names, {self.profile["input_name"]: tensor})
        self.inference_calls += 1
        n = self.profile["max_detections"]
        if (boxes.shape != (1,n,4) or boxes.dtype != np.float32 or scores.shape != (1,n)
            or scores.dtype != np.float32 or classes.shape != (1,n) or classes.dtype != np.int64
            or not np.isfinite(scores).all() or (scores < 0).any() or (scores > 1).any()):
            raise ModelError("Detector produced invalid ONNX scores")
        detections = []
        for i, score in enumerate(scores[0]):
            if float(score) < self.profile["threshold"]:
                continue
            category = int(classes[0,i])
            box = boxes[0,i]
            if (category not in self.categories or not np.isfinite(box).all() or (box < 0).any() or (box > 1).any()
                or box[0] >= box[2] or box[1] >= box[3]):
                raise ModelError("Detector produced invalid class or bounding box")
            detections.append({"object_id":str(i), "label":self.categories[category],
                               "category_id":category, "score":float(score), "bbox":[float(x) for x in box]})
        value = {"detections": detections}
        schema = _read_json(ROOT / "spec/schemas/detections.schema.json")
        if list(StrictValidator(schema).iter_errors(value)):
            raise ModelError("Detection result is invalid")
        return value

    def close(self):
        self.session = None


def load_model(root):
    return DetectionModel(root)
