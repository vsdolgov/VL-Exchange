"""Independent VLMP CPU host, with adapters for the two published legacy profiles.

This implementation does not import the 0.2.0 reference host. JSON Schema,
ONNX and ONNX Runtime are deliberately shared dependencies. Legacy classification
packages contain publisher-supplied Python code, which must be trusted: this host
does not provide a sandbox.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import copy
import hashlib
import io
import math
from pathlib import Path
import sys
import threading
import uuid

import numpy as np
import onnx
import onnxruntime as ort
from packaging.specifiers import InvalidSpecifier, SpecifierSet

from ._storage import PackageError, archive_digest, read_manifest, strict_json, unpack


_HERE = Path(__file__).resolve()
_UNIFIED = _HERE.parents[1]
_V02 = _HERE.parents[2]
_REPO = _HERE.parents[4]
_SCHEMAS = {
    "0.2.0": _UNIFIED / "spec" / "model-package.schema.json",
    "0.2.0-draft.1": _REPO / "model" / "spec" / "model-package.schema.json",
    "0.2.0-detection-draft.1": _V02 / "model" / "spec" / "model-package.schema.json",
}


def _contains_external_tensors(message):
    if isinstance(message, onnx.TensorProto):
        if message.data_location == onnx.TensorProto.EXTERNAL or message.external_data:
            return True
    for descriptor, value in message.ListFields():
        if descriptor.message_type is not None:
            if descriptor.is_repeated:
                if any(_contains_external_tensors(item) for item in value):
                    return True
            elif _contains_external_tensors(value):
                return True
    return False


def _verify_graph(path, expected_opset):
    try:
        data = path.read_bytes()
        model = onnx.load_model_from_string(data)
        if _contains_external_tensors(model):
            raise PackageError("external ONNX tensors are unsupported")
        if expected_opset is not None and (model.functions or [(op.domain, op.version) for op in model.opset_import] != [("", expected_opset)]):
            raise PackageError("unsupported ONNX opset or custom function")
        # Subgraphs are covered by full_check; custom-domain nodes are prohibited at all depths.
        def check_graph(graph):
            for node in graph.node:
                if node.domain not in ("", "ai.onnx"):
                    raise PackageError("custom ONNX operator is unsupported")
                for attr in node.attribute:
                    if attr.type == onnx.AttributeProto.GRAPH:
                        check_graph(attr.g)
                    elif attr.type == onnx.AttributeProto.GRAPHS:
                        for child in attr.graphs:
                            check_graph(child)
        if expected_opset is not None:
            check_graph(model.graph)
        onnx.checker.check_model(model, full_check=True)
        return data
    except PackageError:
        raise
    except Exception as exc:
        raise PackageError("invalid or unsupported ONNX graph") from exc


def _unique_categories(categories, *, indexed=True):
    if not categories:
        raise PackageError("empty categories")
    ids = [item["category_id"] for item in categories]
    labels = [item["label"] for item in categories]
    if len(set(ids)) != len(ids) or (indexed and len(set(labels)) != len(labels)):
        raise PackageError("duplicate category ID or label")
    if indexed and [item["output_index"] for item in categories] != list(range(len(categories))):
        raise PackageError("category indices must be contiguous and ordered")


def _version_dependencies(manifest):
    try:
        py = ".".join(map(str, sys.version_info[:3]))
        if not SpecifierSet(manifest["runtime"]["python"]).contains(py):
            raise PackageError("unsupported Python runtime")
        for name, constraint in manifest["dependencies"].items():
            if not SpecifierSet(constraint).contains(importlib.metadata.version(name)):
                raise PackageError("unsupported dependency version")
    except (InvalidSpecifier, importlib.metadata.PackageNotFoundError) as exc:
        raise PackageError("unsupported or unavailable dependency") from exc


def _dynamic_load(root, manifest):
    entry = manifest["entrypoint"]
    directory = root / entry["root"]
    namespace = "_vlmp_independent_" + uuid.uuid4().hex
    previous_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(namespace, directory / "__init__.py",
                                                       submodule_search_locations=[str(directory)])
        if spec is None or spec.loader is None:
            raise PackageError("cannot load model package")
        module = importlib.util.module_from_spec(spec)
        sys.modules[namespace] = module
        spec.loader.exec_module(module)
        spec = importlib.util.spec_from_file_location(namespace + ".model", directory / entry["module"])
        if spec is None or spec.loader is None:
            raise PackageError("cannot load model module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return namespace, getattr(module, entry["class"])()
    except Exception:
        _clear_namespace(namespace)
        raise
    finally:
        sys.dont_write_bytecode = previous_bytecode


def _clear_namespace(prefix):
    if prefix:
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                sys.modules.pop(name, None)


def load_fixture(path, shape=None):
    """Parse and bound NPY metadata before constructing any NumPy array."""
    try:
        raw = path.read_bytes()
        stream = io.BytesIO(raw)
        version = np.lib.format.read_magic(stream)
        if version not in ((1, 0), (2, 0), (3, 0)):
            raise PackageError("unsupported NPY version")
        declared_shape, fortran, dtype = np.lib.format._read_array_header(stream, version, max_header_size=10000)
        if dtype != np.dtype("uint8") or dtype.fields is not None or (shape is not None and tuple(shape) != declared_shape):
            raise PackageError("golden input dtype or shape mismatch")
        if len(declared_shape) != 3 or declared_shape[2] != 3 or any(type(x) is not int or x < 1 for x in declared_shape):
            raise PackageError("invalid or oversized golden dimensions")
        if shape is not None and any(x > 4096 for x in declared_shape[:2]):
            raise PackageError("invalid or oversized golden dimensions")
        required = declared_shape[0] * declared_shape[1] * declared_shape[2]
        if required > 128 * 1024 * 1024:
            raise PackageError("golden input exceeds host per-file limit")
        if len(raw) - stream.tell() != required:
            raise PackageError("NPY payload length mismatch")
        return np.frombuffer(raw, dtype=np.uint8, offset=stream.tell()).reshape(declared_shape, order="F" if fortran else "C")
    except PackageError:
        raise
    except (OSError, ValueError, EOFError, TypeError, OverflowError) as exc:
        raise PackageError("invalid NPY golden input") from exc


def _validate_golden(root, manifest, shape):
    fixtures = manifest["golden"]["fixtures"]
    ids = [item["id"] for item in fixtures]
    paths = [item["input"] for item in fixtures]
    if len(set(ids)) != len(ids) or len(set(paths)) != len(paths):
        raise PackageError("golden fixture IDs and paths must be unique")
    categories = {item["category_id"]: item for item in manifest["categories"]}
    unique = set()
    for fixture in fixtures:
        if fixture["input"] not in manifest["files"]:
            raise PackageError("golden input not in inventory")
        frame = load_fixture(root / fixture["input"], shape)
        unique.add(hashlib.sha256(frame.tobytes(order="C")).digest())
        expected = fixture["expected"]
        if manifest["profile"] == "classification-onnx-cpu-v1":
            item = categories.get(expected["category_id"])
            if item is None or item["label"] != expected["label"] or item["output_index"] != expected["output_index"]:
                raise PackageError("golden classification category mismatch")
        else:
            identifiers = set()
            previous_index = -1
            for item in expected["detections"]:
                category = categories.get(item["category_id"])
                box = item["bbox"]
                object_id = item["object_id"]
                if not object_id.isascii() or not object_id.isdecimal() or str(int(object_id)) != object_id:
                    raise PackageError("invalid golden detection object ID")
                index = int(object_id)
                if index <= previous_index or index >= manifest["result"]["max_detections"] or object_id in identifiers or category is None or category["label"] != item["label"] or box[0] >= box[2] or box[1] >= box[3] or item["score"] < manifest["result"]["threshold"]:
                    raise PackageError("invalid golden detection")
                previous_index = index
                identifiers.add(object_id)
    if len(unique) < 3:
        raise PackageError("at least three distinct golden inputs are required")


def _validate_legacy_classification_golden(root, manifest):
    fixtures = manifest["golden"]["fixtures"]
    ids, paths, contents = set(), set(), set()
    categories = {item["category_id"]: item for item in manifest["categories"]}
    for fixture in fixtures:
        if fixture["id"] in ids or fixture["input"] in paths:
            raise PackageError("duplicate legacy golden fixture ID or path")
        ids.add(fixture["id"])
        paths.add(fixture["input"])
        if fixture["input"] not in manifest["files"]:
            raise PackageError("legacy golden input not in inventory")
        image = load_fixture(root / fixture["input"])
        if not image.flags.c_contiguous:
            raise PackageError("legacy classification golden input must be C-contiguous")
        contents.add((image.dtype.str, image.shape, hashlib.sha256(image.tobytes(order="C")).digest()))
        expected = fixture["expected"]
        category = categories.get(expected["category_id"])
        if category is None or category["label"] != expected["label"] or category["output_index"] != expected["output_index"]:
            raise PackageError("legacy golden classification category mismatch")
    if len(contents) < 3:
        raise PackageError("at least three distinct legacy golden inputs are required")


class IndependentModel:
    def __init__(self, zip_path):
        self._lock = threading.RLock()
        self._context = unpack(zip_path)
        self._root = None
        self._namespace = None
        self._legacy_model = None
        self._session = None
        self._closed = False
        self.inference_calls = 0
        self._archive_path = Path(zip_path)
        try:
            self._root, self._zip_sha256 = self._context.__enter__()
            manifest_path = self._root / "model-package.json"
            if not manifest_path.is_file() or manifest_path.stat().st_size > 1024 * 1024:
                raise PackageError("missing or oversized model manifest")
            candidate = strict_json(manifest_path.read_bytes())
            version = candidate.get("spec_version") if isinstance(candidate, dict) else None
            if version not in _SCHEMAS:
                raise PackageError("unsupported model package version")
            self._manifest, self._manifest_sha256 = read_manifest(self._root, _SCHEMAS[version])
            self._spec_version = version
            self._initialize()
        except Exception as exc:
            self._cleanup()
            if isinstance(exc, PackageError):
                raise
            raise PackageError("model package initialization failed") from exc

    @property
    def manifest(self):
        """Return a copy; external mutations cannot alter validated execution policy."""
        return copy.deepcopy(self._manifest)

    @property
    def profile(self):
        return self._profile

    @property
    def spec_version(self):
        return self._spec_version

    @property
    def zip_sha256(self):
        return self._zip_sha256

    @property
    def manifest_sha256(self):
        return self._manifest_sha256

    def _initialize(self):
        m = self._manifest
        version = self._spec_version
        if version == "0.2.0":
            self._profile = m["profile"]
            if set(m["required_capabilities"]) != {"onnx-cpu-fp32", self._profile}:
                raise PackageError("unknown or missing required capability")
            if any(item["required"] for item in m["extensions"].values()):
                raise PackageError("unsupported required extension")
            self._categories = m["categories"]
            _unique_categories(self._categories)
            b = m["backend"]
            self._shape = b["input_shape"]
            if any(type(item) is not int for item in [b["opset"], *self._shape, *m["source_input"]["shape"]]):
                raise PackageError("opset and shape values must be JSON integers")
            if m["source_input"]["shape"] != [self._shape[2], self._shape[3], 3]:
                raise PackageError("source and backend input shapes differ")
            self._profile_type = "classification" if self._profile == "classification-onnx-cpu-v1" else "detection"
            self._outputs = b["outputs"]
            self._threshold = m["result"].get("threshold")
            self._max_detections = m["result"].get("max_detections")
            self._golden = m["golden"]["fixtures"]
            weights, input_name, opset = b["weights"], b["input_name"], b["opset"]
            _validate_golden(self._root, m, m["source_input"]["shape"])
        elif version == "0.2.0-draft.1":
            self._profile = m["profile"]
            if set(m["required_capabilities"]) != {"onnx-cpu-fp32", self._profile} or any(
                item["required"] for item in m["extensions"].values()
            ):
                raise PackageError("unsupported legacy requirement")
            _version_dependencies(m)
            self._categories = m["categories"]
            _unique_categories(self._categories)
            b = m["backend"]
            self._shape = b["input_shape"]
            self._profile_type = "legacy-classification"
            self._outputs = {"scores": b["output_name"]}
            self._threshold = self._max_detections = None
            self._golden = m["golden"]["fixtures"]
            weights, input_name, opset = b["weights"], b["input_name"], b["opset"]
            _validate_legacy_classification_golden(self._root, m)
            if m["entrypoint"]["root"] + "/__init__.py" not in m["files"] or m["entrypoint"]["root"] + "/" + m["entrypoint"]["module"] not in m["files"]:
                raise PackageError("legacy Python entrypoint not in inventory")
        else:
            p = m["profile"]
            self._profile = p["id"]
            self._categories = p["categories"]
            _unique_categories(self._categories, indexed=False)
            self._shape = p["input_shape"]
            if m["source_input"]["shape"] != [self._shape[2], self._shape[3], 3]:
                raise PackageError("source and backend input shapes differ")
            self._profile_type = "detection"
            self._outputs = {name: p["output_" + name] for name in ("boxes", "scores", "classes")}
            self._threshold, self._max_detections = p["threshold"], p["max_detections"]
            self._golden = m["golden"]
            # The published legacy detection profile did not pin an ONNX opset.
            weights, input_name, opset = p["weights"], p["input_name"], None
        self._input_name = input_name
        if weights not in m["files"] or any(item["input"] not in m["files"] for item in self._golden):
            raise PackageError("model or golden inputs not in inventory")
        names = list(self._outputs.values())
        if len(set(names)) != len(names):
            raise PackageError("output names must be unique")
        try:
            model_bytes = _verify_graph(self._root / weights, opset)
            options = ort.SessionOptions()
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            self._session = ort.InferenceSession(model_bytes, sess_options=options,
                                                  providers=["CPUExecutionProvider"])
            self._validate_ort_metadata()
            if self._profile_type == "legacy-classification":
                self._namespace, self._legacy_model = _dynamic_load(self._root, m)
        except PackageError:
            raise
        except Exception as exc:
            raise PackageError("cannot initialize ONNX model") from exc

    def _validate_ort_metadata(self):
        inputs = self._session.get_inputs()
        outputs = self._session.get_outputs()
        if len(inputs) != 1 or inputs[0].name != self._input_name or inputs[0].type != "tensor(float)" or inputs[0].shape != self._shape:
            raise PackageError("ONNX input metadata differs from manifest")
        if self._profile_type.endswith("classification"):
            expected = {self._outputs["scores"]: ("tensor(float)", [1, len(self._categories)])}
        else:
            n = self._max_detections
            expected = {self._outputs["boxes"]: ("tensor(float)", [1, n, 4]),
                        self._outputs["scores"]: ("tensor(float)", [1, n]),
                        self._outputs["classes"]: ("tensor(int64)", [1, n])}
        if self._session.get_providers() != ["CPUExecutionProvider"] or len(outputs) != len(expected) or {
            node.name: (node.type, node.shape) for node in outputs
        } != expected:
            raise PackageError("ONNX output metadata differs from manifest")

    def predict(self, frame):
        with self._lock:
            if self._closed:
                raise PackageError("model already closed")
            if archive_digest(self._archive_path) != self._zip_sha256:
                raise PackageError("archive changed before inference")
            try:
                return self._predict_frame(frame)
            finally:
                if archive_digest(self._archive_path) != self._zip_sha256:
                    raise PackageError("archive changed during inference")

    def _predict_frame(self, frame):
        with self._lock:
            if self._closed:
                raise PackageError("model already closed")
            if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3 or not frame.shape[0] or not frame.shape[1]:
                raise PackageError("expected BGR uint8 HWC frame")
            if self._profile_type == "legacy-classification" and not frame.flags.c_contiguous:
                raise PackageError("legacy classification requires a contiguous frame")
            if self._profile_type != "legacy-classification" and list(frame.shape) != [self._shape[2], self._shape[3], 3]:
                raise PackageError("frame shape differs from source_input")
            try:
                if self._legacy_model is not None:
                    prepared = self._legacy_model.preprocess(frame.copy())
                    if type(prepared) is not dict or set(prepared) != {self._input_name}:
                        raise PackageError("invalid legacy preprocessing result")
                    tensor = prepared[self._input_name]
                else:
                    tensor = np.ascontiguousarray(frame[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32) / np.float32(255.0)
                if not isinstance(tensor, np.ndarray) or tensor.dtype != np.float32 or list(tensor.shape) != self._shape or not np.isfinite(tensor).all():
                    raise PackageError("invalid model input tensor")
                output_names = ([self._outputs["scores"]] if self._profile_type.endswith("classification")
                                else [self._outputs[key] for key in ("boxes", "scores", "classes")])
                outputs = self._session.run(output_names, {self._input_name: tensor})
                self.inference_calls += 1
                if self._profile_type == "legacy-classification":
                    raw = outputs[0]
                    if raw.dtype != np.float32 or list(raw.shape) != [1, len(self._categories)] or not np.isfinite(raw).all():
                        raise PackageError("invalid classification tensor")
                    value = self._legacy_model.postprocess({self._outputs["scores"]: raw}, tuple(c["label"] for c in self._categories))
                    if type(value) is not dict or set(value) != {"label", "score"} or type(value["score"]) not in (int, float) or not math.isfinite(value["score"]) or not 0 <= value["score"] <= 1:
                        raise PackageError("invalid legacy classification result")
                    matches = [c for c in self._categories if c["label"] == value["label"]]
                    if len(matches) != 1:
                        raise PackageError("classification label is not declared")
                    return {**matches[0], "score": float(value["score"])}
                if self._profile_type == "classification":
                    scores = outputs[0]
                    if scores.dtype != np.float32 or list(scores.shape) != [1, len(self._categories)] or not np.isfinite(scores).all() or (scores < 0).any() or (scores > 1).any() or abs(float(np.sum(scores, dtype=np.float64)) - 1.0) > 1e-4:
                        raise PackageError("invalid classification probabilities")
                    index = int(np.argmax(scores[0]))
                    return {**self._categories[index], "score": float(scores[0, index])}
                boxes, scores, classes = outputs
                n = self._max_detections
                if boxes.dtype != np.float32 or list(boxes.shape) != [1, n, 4] or scores.dtype != np.float32 or list(scores.shape) != [1, n] or classes.dtype != np.int64 or list(classes.shape) != [1, n] or not np.isfinite(scores).all() or (scores < 0).any() or (scores > 1).any():
                    raise PackageError("invalid detection tensors")
                categories = {item["category_id"]: item["label"] for item in self._categories}
                detections = []
                for index, score in enumerate(scores[0]):
                    if float(score) < self._threshold:
                        continue
                    category = int(classes[0, index])
                    box = boxes[0, index]
                    if category not in categories or not np.isfinite(box).all() or (box < 0).any() or (box > 1).any() or box[0] >= box[2] or box[1] >= box[3]:
                        raise PackageError("invalid detection class or box")
                    detections.append({"object_id": str(index), "label": categories[category],
                                       "category_id": category, "score": float(score),
                                       "bbox": [float(x) for x in box]})
                return {"detections": detections}
            except PackageError:
                raise
            except Exception as exc:
                raise PackageError("inference failed") from exc

    def _cleanup(self):
        _clear_namespace(self._namespace)
        self._namespace = None
        self._legacy_model = None
        self._session = None
        self._closed = True
        if self._root is not None:
            self._root = None
            self._context.__exit__(None, None, None)

    def close(self):
        with self._lock:
            if not self._closed:
                self._cleanup()


def load_package(zip_path):
    """Load one ZIP, validating before ONNX or legacy publisher Python is executed."""
    return IndependentModel(zip_path)
