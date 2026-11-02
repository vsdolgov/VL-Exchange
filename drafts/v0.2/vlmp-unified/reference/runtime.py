"""Reference host for the unified VLMP package profile.

This local reference executes trusted packages; it is not a sandbox.  Package
contents are read from one bounded, validated in-memory ZIP snapshot so no
archive extraction or Python imports are needed for the built-in CPU profiles.
"""
from __future__ import annotations

import copy
from contextlib import ExitStack
import hashlib
import io
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import sys
import tempfile
from types import MappingProxyType
import zipfile

import numpy as np
import onnx
import onnxruntime as ort
from jsonschema import Draft202012Validator, validators


class PackageError(ValueError):
    """Package validation or model execution failure."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackageError("duplicate JSON key")
        result[key] = value
    return result


def _bad_constant(value):
    raise PackageError("non-finite JSON number")


def strict_json(data):
    try:
        obj = json.loads(data.decode("utf-8", "strict"), object_pairs_hook=_pairs,
                         parse_constant=_bad_constant)
        json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8", "strict")
        return obj
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise PackageError("invalid strict UTF-8 JSON") from exc


def _name(name):
    if type(name) is not str or not name or "\\" in name or "\x00" in name or ":" in name:
        raise PackageError("invalid archive path")
    parts = name.split("/")
    if (any(part in ("", ".", "..") for part in parts) or PurePosixPath(name).is_absolute()
            or PureWindowsPath(name).drive):
        raise PackageError("noncanonical archive path")
    return name


def digest(data):
    return hashlib.sha256(data).hexdigest()


class ArchiveSnapshot:
    """Read exact ZIP bytes once and validate bounded files and inventory."""

    MAX_ARCHIVE = 128 * 1024 * 1024
    MAX_FILE = 128 * 1024 * 1024
    MAX_TOTAL = 512 * 1024 * 1024
    MAX_FILES = 1024

    @property
    def path(self):
        return self._path

    @property
    def files(self):
        return MappingProxyType(self._files)

    @property
    def manifest(self):
        return copy.deepcopy(self._manifest)

    @property
    def zip_sha256(self):
        return self._zip_sha256

    @property
    def manifest_sha256(self):
        return self._manifest_sha256

    def __init__(self, path):
        self._path = Path(path)
        if self._path.is_symlink() or not self._path.is_file():
            raise PackageError("package must be a regular ZIP file")
        with self._path.open("rb") as stream:
            raw = stream.read(self.MAX_ARCHIVE + 1)
        if len(raw) > self.MAX_ARCHIVE:
            raise PackageError("archive exceeds reference host size limit")
        self._zip_sha256 = digest(raw)
        self._files = {}
        try:
            with zipfile.ZipFile(io.BytesIO(raw), "r") as archive:
                entries = archive.infolist()
                if len(entries) > self.MAX_FILES:
                    raise PackageError("too many archive entries")
                total = 0
                names = set()
                for entry in entries:
                    if entry.orig_filename != entry.filename:
                        raise PackageError("archive path contains a NUL byte")
                    filename = entry.filename[:-1] if entry.is_dir() else entry.filename
                    _name(filename)
                    if filename in names:
                        raise PackageError("duplicate archive entry")
                    names.add(filename)
                    mode = entry.external_attr >> 16
                    kind = stat.S_IFMT(mode)
                    if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise PackageError("unsupported archive entry type")
                    if entry.flag_bits & 1:
                        raise PackageError("encrypted ZIP is unsupported")
                    if entry.is_dir():
                        if kind == stat.S_IFREG:
                            raise PackageError("inconsistent archive entry type")
                        continue
                    if kind == stat.S_IFDIR:
                        raise PackageError("inconsistent archive entry type")
                    if entry.file_size > self.MAX_FILE:
                        raise PackageError("archive entry exceeds size limit")
                    total += entry.file_size
                    if total > self.MAX_TOTAL:
                        raise PackageError("expanded package exceeds size limit")
                    data = archive.read(entry)
                    if len(data) != entry.file_size:
                        raise PackageError("archive entry size mismatch")
                    self._files[filename] = data
        except (zipfile.BadZipFile, RuntimeError, OSError, EOFError) as exc:
            raise PackageError("invalid ZIP archive") from exc
        if "model-package.json" not in self._files:
            raise PackageError("missing manifest")
        self._manifest_sha256 = digest(self._files["model-package.json"])
        self._manifest = strict_json(self._files["model-package.json"])

    def check_inventory(self, inventory):
        if type(inventory) is not dict or set(inventory) != set(self._files) - {"model-package.json"}:
            raise PackageError("package inventory mismatch")
        for name, expected in inventory.items():
            _name(name)
            if type(expected) is not str or digest(self._files[name]) != expected:
                raise PackageError("package digest mismatch")

    def content(self, name):
        _name(name)
        if name not in self._files or name == "model-package.json":
            raise PackageError("referenced file missing from package inventory")
        return self._files[name]

    def assert_unchanged(self):
        if self._path.is_symlink() or not self._path.is_file():
            raise PackageError("input ZIP changed during execution")
        with self._path.open("rb") as stream:
            raw = stream.read(self.MAX_ARCHIVE + 1)
        if digest(raw) != self._zip_sha256:
            raise PackageError("input ZIP changed during execution")


_types = Draft202012Validator.TYPE_CHECKER.redefine_many({
    "integer": lambda _, x: type(x) is int,
    "number": lambda _, x: type(x) in (int, float) and math.isfinite(x),
})
StrictValidator = validators.extend(Draft202012Validator, type_checker=_types)


def _validate_graph(graph_bytes):
    try:
        graph = onnx.load_model_from_string(graph_bytes)
        if graph.functions:
            raise PackageError("ONNX local functions are unsupported")
        def scan(message, depth=0):
            if depth > 100:
                raise PackageError("ONNX nesting exceeds reference host limit")
            if isinstance(message, onnx.TensorProto) and (
                    message.data_location == onnx.TensorProto.EXTERNAL or message.external_data):
                raise PackageError("external ONNX data is unsupported")
            if isinstance(message, onnx.NodeProto) and message.domain not in ("", "ai.onnx"):
                raise PackageError("custom ONNX operators are unsupported")
            for descriptor, value in message.ListFields():
                if descriptor.message_type is not None:
                    for child in value if descriptor.is_repeated else (value,):
                        scan(child, depth + 1)
        scan(graph)
        onnx.checker.check_model(graph, full_check=True)
    except PackageError:
        raise
    except Exception as exc:
        raise PackageError("invalid ONNX graph") from exc
    return graph


def _image(image, shape=None):
    if (not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3
            or image.shape[2] != 3 or min(image.shape[:2]) < 1
            or (shape is not None and list(image.shape) != shape)):
        raise PackageError("expected BGR uint8 image of declared shape")
    return image


def _npy(data):
    try:
        # Parse and bound the header before np.load can allocate an array based
        # on attacker-controlled dimensions, even when the ZIP itself is tiny.
        stream = io.BytesIO(data)
        version = np.lib.format.read_magic(stream)
        if version not in {(1, 0), (2, 0), (3, 0)}:
            raise PackageError("unsupported NPY version")
        shape, _fortran, dtype = np.lib.format._read_array_header(stream, version, max_header_size=10000)
        if (len(shape) != 3 or any(type(size) is not int for size in shape)
                or not 1 <= shape[0] <= 4096 or not 1 <= shape[1] <= 4096 or shape[2] != 3
                or dtype != np.dtype(np.uint8)
                or len(data) - stream.tell() != math.prod(shape)):
            raise PackageError("invalid or oversized NPY image fixture")
        image = np.load(io.BytesIO(data), allow_pickle=False, max_header_size=10000)
    except PackageError:
        raise
    except Exception as exc:
        raise PackageError("invalid NPY fixture") from exc
    return _image(image)


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[2]


class _ContextModel:
    @property
    def snapshot(self):
        return self._snapshot

    @property
    def manifest(self):
        return copy.deepcopy(self._manifest)

    @property
    def profile(self):
        return self._profile

    @property
    def inference_calls(self):
        return self._inference_calls

    @property
    def zip_sha256(self):
        return self._snapshot.zip_sha256

    @property
    def manifest_sha256(self):
        return self._snapshot.manifest_sha256

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


class UnifiedModel(_ContextModel):
    """Synchronous fixed-shape CPU ONNX reference implementation."""

    @property
    def categories(self):
        return copy.deepcopy(self._categories)

    @property
    def category_by_id(self):
        return copy.deepcopy(self._category_by_id)

    def __init__(self, snapshot):
        self._snapshot = snapshot
        self._manifest = manifest = snapshot.manifest
        self._inference_calls = 0
        schema = strict_json((ROOT / "spec/model-package.schema.json").read_bytes())
        errors = list(StrictValidator(schema).iter_errors(manifest))
        if errors:
            raise PackageError("invalid unified manifest: " + errors[0].message)
        snapshot.check_inventory(manifest["files"])
        self._profile = profile = manifest["profile"]
        expected_capabilities = {profile, "onnx-cpu-fp32"}
        capabilities = manifest["required_capabilities"]
        if set(capabilities) != expected_capabilities or len(capabilities) != len(expected_capabilities):
            raise PackageError("unsupported or missing required capabilities")
        if any(value["required"] for value in manifest["extensions"].values()):
            raise PackageError("required extension unsupported")
        backend = manifest["backend"]
        source = manifest["source_input"]
        shape = backend["input_shape"]
        if (type(backend["opset"]) is not int
                or any(type(value) is not int for value in shape)
                or any(type(value) is not int for value in source["shape"])):
            raise PackageError("integer fields must use JSON integer representation")
        if source["shape"] != [shape[2], shape[3], 3]:
            raise PackageError("source image shape does not match ONNX input")
        categories = manifest["categories"]
        if [item["output_index"] for item in categories] != list(range(len(categories))):
            raise PackageError("category output indices must be ordered and contiguous")
        if (len({c["category_id"] for c in categories}) != len(categories)
                or len({c["label"] for c in categories}) != len(categories)):
            raise PackageError("duplicate category ID or label")
        self._categories = categories
        self._category_by_id = {item["category_id"]: item for item in categories}
        golden = manifest["golden"]["fixtures"]
        ids, inputs, contents = set(), set(), set()
        for fixture in golden:
            if fixture["id"] in ids or fixture["input"] in inputs:
                raise PackageError("duplicate golden fixture ID or path")
            ids.add(fixture["id"])
            inputs.add(fixture["input"])
            image = _npy(snapshot.content(fixture["input"]))
            _image(image, source["shape"])
            contents.add((image.dtype.str, image.shape, digest(image.tobytes())))
            expected = fixture["expected"]
            if profile == "classification-onnx-cpu-v1":
                category = self._category_by_id.get(expected["category_id"])
                if (category is None or category["label"] != expected["label"]
                        or category["output_index"] != expected["output_index"]):
                    raise PackageError("golden classification category mismatch")
            else:
                previous_index = -1
                for item in expected["detections"]:
                    object_id = item["object_id"]
                    if (not object_id.isascii() or not object_id.isdecimal()
                            or str(int(object_id)) != object_id):
                        raise PackageError("invalid golden detection object ID")
                    index = int(object_id)
                    category = self._category_by_id.get(item["category_id"])
                    box = item["bbox"]
                    if (index <= previous_index or index >= manifest["result"]["max_detections"]
                            or category is None or category["label"] != item["label"]
                            or box[0] >= box[2] or box[1] >= box[3]
                            or item["score"] < manifest["result"]["threshold"]):
                        raise PackageError("invalid golden detection result")
                    previous_index = index
        if len(contents) < 3:
            raise PackageError("at least three distinct golden inputs required")
        graph_bytes = snapshot.content(backend["weights"])
        graph = _validate_graph(graph_bytes)
        if [(entry.domain, entry.version) for entry in graph.opset_import] != [("", backend["opset"])]:
            raise PackageError("ONNX opset mismatch")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        try:
            self._session = ort.InferenceSession(graph_bytes, sess_options=options,
                                                providers=["CPUExecutionProvider"])
        except Exception as exc:
            raise PackageError("cannot initialize ONNX Runtime") from exc
        model_inputs = self._session.get_inputs()
        outputs = self._session.get_outputs()
        if (self._session.get_providers() != ["CPUExecutionProvider"] or len(model_inputs) != 1
                or model_inputs[0].name != backend["input_name"]
                or model_inputs[0].type != "tensor(float)" or model_inputs[0].shape != shape):
            raise PackageError("ONNX input does not match manifest")
        output_names = backend["outputs"]
        if len(set(output_names.values())) != len(output_names):
            raise PackageError("ONNX output names are not distinct")
        if profile == "classification-onnx-cpu-v1":
            wanted = {output_names["scores"]: ("tensor(float)", [1, len(categories)])}
        elif profile == "detection-onnx-cpu-v1":
            count = manifest["result"]["max_detections"]
            wanted = {output_names["boxes"]: ("tensor(float)", [1, count, 4]),
                      output_names["scores"]: ("tensor(float)", [1, count]),
                      output_names["classes"]: ("tensor(int64)", [1, count])}
        else:
            raise PackageError("unsupported unified model profile")
        if len(outputs) != len(wanted) or {item.name: (item.type, item.shape) for item in outputs} != wanted:
            raise PackageError("ONNX outputs do not match manifest")

    def predict(self, image):
        if self._session is None:
            raise PackageError("model already closed")
        self._snapshot.assert_unchanged()
        image = _image(image, self._manifest["source_input"]["shape"])
        tensor = np.ascontiguousarray(image[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32)
        tensor /= np.float32(255.0)
        backend = self._manifest["backend"]
        names = backend["outputs"]
        if self._profile == "classification-onnx-cpu-v1":
            try:
                scores = self._session.run([names["scores"]], {backend["input_name"]: tensor})[0]
                self._inference_calls += 1
            except Exception as exc:
                raise PackageError("classification inference failed") from exc
            if (not isinstance(scores, np.ndarray) or scores.dtype != np.float32
                    or scores.shape != (1, len(self._categories)) or not np.isfinite(scores).all()
                    or (scores < 0).any() or (scores > 1).any()
                    or abs(float(scores.sum(dtype=np.float64)) - 1.0) > 1e-4):
                raise PackageError("invalid classification probabilities")
            index = int(np.argmax(scores[0]))
            result = {**self._categories[index], "score": float(scores[0, index])}
        else:
            try:
                boxes, scores, classes = self._session.run(
                    [names["boxes"], names["scores"], names["classes"]], {backend["input_name"]: tensor})
                self._inference_calls += 1
            except Exception as exc:
                raise PackageError("detection inference failed") from exc
            count = self._manifest["result"]["max_detections"]
            if (boxes.dtype != np.float32 or boxes.shape != (1, count, 4)
                    or scores.dtype != np.float32 or scores.shape != (1, count)
                    or classes.dtype != np.int64 or classes.shape != (1, count)
                    or not np.isfinite(scores).all() or (scores < 0).any() or (scores > 1).any()):
                raise PackageError("invalid detection output tensors")
            detections = []
            for index, score in enumerate(scores[0]):
                if float(score) < self._manifest["result"]["threshold"]:
                    continue
                category_id, box = int(classes[0, index]), boxes[0, index]
                if (category_id not in self._category_by_id or not np.isfinite(box).all()
                        or (box < 0).any() or (box > 1).any()
                        or box[0] >= box[2] or box[1] >= box[3]):
                    raise PackageError("invalid detection category or box")
                detections.append({"object_id": str(index), "label": self._category_by_id[category_id]["label"],
                                   "category_id": category_id, "score": float(score),
                                   "bbox": [float(value) for value in box]})
            result = {"detections": detections}
        self._snapshot.assert_unchanged()
        return result

    def close(self):
        if self._session is None:
            return
        self._session = None
        self._snapshot.assert_unchanged()


def _legacy_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PackageError("legacy loader is not available")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


class LegacyModel(_ContextModel):
    """Route unchanged legacy ZIP snapshots to their original reference loader."""

    def __init__(self, snapshot):
        self._snapshot = snapshot
        self._manifest = snapshot.manifest
        self._inference_calls = 0
        self._closed = False
        snapshot.check_inventory(self._manifest.get("files"))
        self._temp = tempfile.TemporaryDirectory(prefix="vlmp-legacy-")
        root = Path(self._temp.name)
        try:
            for name, data in snapshot.files.items():
                destination = root.joinpath(*PurePosixPath(name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with destination.open("xb") as stream:
                    stream.write(data)
                destination.chmod(0o600)
            if self._manifest["spec_version"] == "0.2.0-draft.1":
                module = _legacy_module(REPOSITORY / "model/src/public_model.py", "_vlmp_legacy_classification")
                self._model = module.ModelExecutor(root)
                self._legacy_kind = "classification"
                self._profile = self._manifest["profile"]
            elif self._manifest["spec_version"] == "0.2.0-detection-draft.1":
                module = _legacy_module(REPOSITORY / "drafts/v0.2/model/src/model_package_v02/runtime.py", "_vlmp_legacy_detection")
                self._model = module.load_model(root)
                self._legacy_kind = "detection"
                self._profile = self._manifest["profile"]["id"]
            else:
                raise PackageError("unsupported legacy model version")
        except Exception:
            self._temp.cleanup()
            raise

    def predict(self, image):
        if self._closed:
            raise PackageError("model already closed")
        self._snapshot.assert_unchanged()
        result = self._model.predict(image) if self._legacy_kind == "classification" else self._model.infer(image)
        self._inference_calls += 1  # Legacy path: successful loader calls, not an ORT-level probe.
        self._snapshot.assert_unchanged()
        return result

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._model.close()
        finally:
            self._temp.cleanup()
        self._snapshot.assert_unchanged()


def _load_snapshot(snapshot):
    manifest = snapshot.manifest
    if type(manifest) is not dict or manifest.get("format") != "vl.model-package":
        raise PackageError("unsupported package format")
    version = manifest.get("spec_version")
    if version == "0.2.0":
        return UnifiedModel(snapshot)
    if version in {"0.2.0-draft.1", "0.2.0-detection-draft.1"}:
        return LegacyModel(snapshot)
    raise PackageError("unsupported package version")


def load_package(path):
    """Load an unprotected ZIP. Protection is explicitly selected by the caller."""
    return _load_snapshot(ArchiveSnapshot(path))


class _ProtectedSnapshot(ArchiveSnapshot):
    """Snapshot the authenticated extracted payload, retaining its signed ZIP hash.

    The security runtime authenticates the original ZIP and extracts it in a
    private directory. We do not reconstruct a ZIP or mistake it for the signed
    original. Each prediction checks that the extracted files remain unchanged.
    """
    def __init__(self, authorization):
        claims = authorization.claims
        if (type(claims) is not dict or claims.get("package_format") != "vl.model-package"
                or claims.get("manifest_path") != "model-package.json"):
            raise PackageError("protected payload is not a VLMP model package")
        self._path = Path(authorization.root)
        self._files = self._read_directory()
        if "model-package.json" not in self._files:
            raise PackageError("missing protected model manifest")
        self._manifest_sha256 = digest(self._files["model-package.json"])
        if self._manifest_sha256 != claims.get("manifest_sha256"):
            raise PackageError("protected manifest digest mismatch")
        signed_zip = claims.get("package_sha256")
        if (type(signed_zip) is not str or len(signed_zip) != 64
                or any(c not in "0123456789abcdef" for c in signed_zip)
                or authorization.package_sha256 != signed_zip):
            raise PackageError("protected package digest mismatch")
        self._zip_sha256 = signed_zip
        self._manifest = strict_json(self._files["model-package.json"])

    def _read_directory(self):
        root = self._path
        if root.is_symlink() or not root.is_dir():
            raise PackageError("protected payload directory is invalid")
        files = {}
        total = 0
        def onerror(error):
            raise PackageError("cannot read protected payload") from error
        for base, dirs, names in os.walk(root, topdown=True, followlinks=False, onerror=onerror):
            directory = Path(base)
            for name in dirs:
                child = directory / name
                _name(child.relative_to(root).as_posix())
                if child.is_symlink() or not child.is_dir():
                    raise PackageError("invalid protected directory entry")
            for name in names:
                path = directory / name
                relative = _name(path.relative_to(root).as_posix())
                if path.is_symlink() or not path.is_file():
                    raise PackageError("invalid protected file entry")
                if len(files) >= self.MAX_FILES:
                    raise PackageError("too many protected files")
                with path.open("rb") as stream:
                    data = stream.read(self.MAX_FILE + 1)
                if len(data) > self.MAX_FILE:
                    raise PackageError("protected file exceeds size limit")
                total += len(data)
                if total > self.MAX_TOTAL:
                    raise PackageError("protected payload exceeds size limit")
                files[relative] = data
        return files

    def assert_unchanged(self):
        if self._read_directory() != self._files:
            raise PackageError("protected payload changed during execution")


class _ProtectedModel:
    """Own the authorization context until model close and guard each result."""
    def __init__(self, model, authorization, resources):
        self._model = model
        self._authorization = authorization
        self._resources = resources
        self._closed = False

    @property
    def manifest(self): return self._model.manifest
    @property
    def profile(self): return self._model.profile
    @property
    def snapshot(self): return self._model.snapshot
    @property
    def zip_sha256(self): return self._model.zip_sha256
    @property
    def manifest_sha256(self): return self._model.manifest_sha256
    @property
    def inference_calls(self): return self._model.inference_calls

    def predict(self, image):
        if self._closed:
            raise PackageError("model already closed")
        from vl_exchange_security import ExecutionLease
        lease = self._authorization.before_process()
        if not isinstance(lease, ExecutionLease):
            raise PackageError("verifier did not issue an execution lease")
        result = self._model.predict(image)
        lease.assert_current()
        return result

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._model.close()
        finally:
            self._resources.close()

    def __enter__(self):
        if self._closed:
            raise PackageError("model already closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


def load_protected(envelope, *, verifier):
    """Authorize/decrypt a VLMP envelope, load the model, and own its lifetime.

    `verifier` is a trusted callable returning the existing security runtime's
    authorized_package context manager with the operator's selected policy.
    Authorization is checked before any ONNX session is built and again after
    initialization. Every predict captures and rechecks its original lease.
    """
    from vl_exchange_security import AuthorizedPackage, ExecutionLease
    if not callable(verifier):
        raise PackageError("a trusted protected-package verifier is required")
    resources = ExitStack()
    model = None
    try:
        authorization = resources.enter_context(verifier(envelope))
        if not isinstance(authorization, AuthorizedPackage):
            raise PackageError("verifier did not return an AuthorizedPackage")
        lease = authorization.before_process()
        if not isinstance(lease, ExecutionLease):
            raise PackageError("verifier did not issue an execution lease")
        model = _load_snapshot(_ProtectedSnapshot(authorization))
        lease.assert_current()
        return _ProtectedModel(model, authorization, resources)
    except BaseException:
        try:
            if model is not None:
                model.close()
        finally:
            resources.close()
        raise
