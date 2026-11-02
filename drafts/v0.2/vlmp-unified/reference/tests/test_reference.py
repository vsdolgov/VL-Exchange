"""Behavioural tests for the reference VLMP package boundary."""
from pathlib import Path
import importlib.util
import io
import json
import stat
import warnings
import zipfile

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT.parents[2]
spec = importlib.util.spec_from_file_location("vlmp_unified_reference_test", ROOT / "reference/runtime.py")
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


def _rewrite(source, target, transform):
    with zipfile.ZipFile(source) as archive:
        members = {info.filename: archive.read(info) for info in archive.infolist()}
    transform(members)
    with zipfile.ZipFile(target, "w") as archive:
        for path, data in members.items():
            archive.writestr(path, data)


@pytest.mark.parametrize("profile", ["classification", "detection"])
def test_inference_on_noncontiguous_image_preserves_real_results(profile):
    with reference.load_package(ROOT / f"examples/{profile}.zip") as model:
        fixture = model.manifest["golden"]["fixtures"][0]
        image = np.load(io.BytesIO(model.snapshot.content(fixture["input"])), allow_pickle=False)
        expanded = np.zeros((image.shape[0], image.shape[1] * 2, 3), dtype=np.uint8)
        expanded[:, ::2] = image
        noncontiguous = expanded[:, ::2]
        assert not noncontiguous.flags.c_contiguous
        actual = model.predict(image)
        assert model.predict(noncontiguous) == actual
        json.dumps(actual, allow_nan=False)


@pytest.mark.parametrize("path,shape,expected", [
    ("model/reports/vlmp-public-channel-probe.zip", (2, 2, 3), "python-numpy-classification-v1"),
    ("drafts/v0.2/model/examples/vl-color-detector.zip", (2, 4, 3), "detection-onnx-cpu-v1"),
])
def test_legacy_published_zip_runs_without_change(path, shape, expected):
    source = REPO / path
    before = reference.digest(source.read_bytes())
    with reference.load_package(source) as model:
        assert model.profile == expected
        assert model.zip_sha256 == before
        result = model.predict(np.zeros(shape, dtype=np.uint8))
        assert type(result) is dict
        json.dumps(result, allow_nan=False)
    assert reference.digest(source.read_bytes()) == before


def test_rejects_tampering_and_a_changed_open_zip(tmp_path):
    source = ROOT / "examples/classification.zip"
    changed = tmp_path / "tampered.zip"
    def alter(members):
        name = next(path for path in members if path.endswith(".onnx"))
        members[name] += b"unexpected"
    _rewrite(source, changed, alter)
    with pytest.raises(reference.PackageError, match="digest"):
        reference.load_package(changed)

    copy = tmp_path / "valid.zip"
    copy.write_bytes(source.read_bytes())
    model = reference.load_package(copy)
    copy.write_bytes(copy.read_bytes() + b"changed")
    with pytest.raises(reference.PackageError, match="changed"):
        model.predict(np.zeros(model.manifest["source_input"]["shape"], dtype=np.uint8))
    with pytest.raises(reference.PackageError, match="changed"):
        model.close()


@pytest.mark.parametrize("bad_path", ["../escape", "/absolute", "a/../escape", "a\\escape", "C:/escape", "C:relative", "artifacts:v1/model.onnx", "artifacts/model:v1.onnx"])
def test_rejects_unsafe_archive_paths(tmp_path, bad_path):
    target = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(bad_path, b"x")
        archive.writestr("model-package.json", b"{}")
    with pytest.raises(reference.PackageError):
        reference.ArchiveSnapshot(target)


def test_rejects_duplicate_zip_members(tmp_path):
    target = tmp_path / "duplicate.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("model-package.json", b"{}")
            archive.writestr("model-package.json", b"{}")
    with pytest.raises(reference.PackageError, match="duplicate"):
        reference.ArchiveSnapshot(target)


def test_oversized_npy_header_is_rejected_before_numpy_load(monkeypatch):
    stream = io.BytesIO()
    np.lib.format.write_array_header_1_0(stream, {
        "descr": np.lib.format.dtype_to_descr(np.dtype("uint8")),
        "fortran_order": False,
        "shape": (999999999, 2, 3),
    })
    monkeypatch.setattr(np, "load", lambda *args, **kwargs: pytest.fail("np.load must not allocate"))
    with pytest.raises(reference.PackageError):
        reference._npy(stream.getvalue())


def test_fortran_order_npy_fixture_is_accepted_and_preserves_pixels():
    image = np.asfortranarray(np.arange(24, dtype=np.uint8).reshape(2, 4, 3))
    assert image.flags.f_contiguous and not image.flags.c_contiguous
    stream = io.BytesIO()
    np.save(stream, image, allow_pickle=False)
    recovered = reference._npy(stream.getvalue())
    assert recovered.flags.f_contiguous and np.array_equal(recovered, image)


def test_float_integer_fields_rejected_before_session(tmp_path):
    source = ROOT / "examples/classification.zip"
    target = tmp_path / "float-integer.zip"
    def alter(members):
        manifest = json.loads(members["model-package.json"])
        manifest["backend"]["input_shape"][0] = 1.0
        members["model-package.json"] = json.dumps(manifest).encode()
    _rewrite(source, target, alter)
    with pytest.raises(reference.PackageError, match="integer"):
        reference.load_package(target)


@pytest.mark.parametrize("mutation", [
    "incomplete_inventory", "unknown_capability", "required_extension",
    "duplicate_category", "reused_fixture", "wrong_input_name", "wrong_output_name",
])
def test_rejects_inconsistent_manifest_before_inference(tmp_path, mutation):
    source = ROOT / "examples/classification.zip"
    target = tmp_path / (mutation + ".zip")
    def alter(members):
        manifest = json.loads(members["model-package.json"])
        if mutation == "incomplete_inventory":
            manifest["files"].pop(next(iter(manifest["files"])))
        elif mutation == "unknown_capability":
            manifest["required_capabilities"].append("unknown-required")
        elif mutation == "required_extension":
            manifest["extensions"]["org.example.required"] = {"required": True, "data": {}}
        elif mutation == "duplicate_category":
            manifest["categories"][1]["category_id"] = manifest["categories"][0]["category_id"]
        elif mutation == "reused_fixture":
            manifest["golden"]["fixtures"][1]["input"] = manifest["golden"]["fixtures"][0]["input"]
        elif mutation == "wrong_input_name":
            manifest["backend"]["input_name"] = "other-input"
        elif mutation == "wrong_output_name":
            manifest["backend"]["outputs"]["scores"] = "other-output"
        members["model-package.json"] = json.dumps(manifest).encode()
    _rewrite(source, target, alter)
    with pytest.raises(reference.PackageError):
        reference.load_package(target)


@pytest.mark.parametrize("mutation", ["opset", "graph_input", "graph_output", "external_tensor", "custom_node"])
def test_rejects_tampered_graph_even_with_matching_inventory(tmp_path, mutation):
    source = ROOT / "examples/classification.zip"
    target = tmp_path / (mutation + ".zip")
    def alter(members):
        manifest = json.loads(members["model-package.json"])
        weights = manifest["backend"]["weights"]
        graph = reference.onnx.load_model_from_string(members[weights])
        if mutation == "opset":
            graph.opset_import[0].version = 12
        elif mutation == "graph_input":
            graph.graph.input[0].type.tensor_type.shape.dim[2].dim_value += 1
        elif mutation == "graph_output":
            graph.graph.output[0].type.tensor_type.shape.dim[1].dim_value += 1
        elif mutation == "external_tensor":
            tensor = graph.graph.initializer.add()
            tensor.name = "external"
            tensor.data_location = reference.onnx.TensorProto.EXTERNAL
            field = tensor.external_data.add(); field.key = "location"; field.value = "../../outside"
        elif mutation == "custom_node":
            graph.graph.node[0].domain = "org.example.custom"
        members[weights] = graph.SerializeToString()
        manifest["files"][weights] = reference.digest(members[weights])
        members["model-package.json"] = json.dumps(manifest).encode()
    _rewrite(source, target, alter)
    with pytest.raises(reference.PackageError):
        reference.load_package(target)


def test_rejects_symlink_archive_member(tmp_path):
    target = tmp_path / "link.zip"
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(info, "outside")
        archive.writestr("model-package.json", "{}")
    with pytest.raises(reference.PackageError, match="entry type"):
        reference.ArchiveSnapshot(target)


@pytest.mark.parametrize("profile", ["classification", "detection"])
def test_input_validation_call_count_and_closed_state(profile):
    model = reference.load_package(ROOT / f"examples/{profile}.zip")
    shape = model.manifest["source_input"]["shape"]
    assert model.inference_calls == 0
    with pytest.raises(reference.PackageError):
        model.predict(np.zeros(shape, dtype=np.float32))
    with pytest.raises(reference.PackageError):
        model.predict(np.zeros((1, 1, 3), dtype=np.uint8))
    assert model.inference_calls == 0
    model.predict(np.zeros(shape, dtype=np.uint8))
    assert model.inference_calls == 1
    model.close()
    model.close()
    with pytest.raises(reference.PackageError, match="closed"):
        model.predict(np.zeros(shape, dtype=np.uint8))


@pytest.mark.parametrize("bad_scores", [
    np.array([[np.nan, 0.3, 0.7]], dtype=np.float32),
    np.array([[0.9, 0.9, 0.9]], dtype=np.float32),
    np.array([[1.01, -0.01, 0.0]], dtype=np.float32),
])
def test_invalid_classification_result_is_error_not_success(bad_scores):
    with reference.load_package(ROOT / "examples/classification.zip") as model:
        class FakeSession:
            def run(self, *_args):
                return [bad_scores]
        model._session = FakeSession()
        with pytest.raises(reference.PackageError, match="probabilities"):
            model.predict(np.zeros(model.manifest["source_input"]["shape"], dtype=np.uint8))
        assert model.inference_calls == 1


def test_invalid_retained_detection_box_is_error_not_empty_result():
    with reference.load_package(ROOT / "examples/detection.zip") as model:
        class FakeSession:
            def run(self, *_args):
                return [np.array([[[0.7, 0.0, 0.2, 1.0], [0.0, 0.0, 0.5, 1.0]]], dtype=np.float32),
                        np.array([[0.9, 0.0]], dtype=np.float32),
                        np.array([[1, 1]], dtype=np.int64)]
        model._session = FakeSession()
        with pytest.raises(reference.PackageError, match="box"):
            model.predict(np.zeros(model.manifest["source_input"]["shape"], dtype=np.uint8))
        assert model.inference_calls == 1


@pytest.mark.parametrize("path", [
    "drafts/v0.2/vlmp-unified/examples/classification.zip",
    "drafts/v0.2/vlmp-unified/examples/detection.zip",
    "model/reports/vlmp-public-channel-probe.zip",
    "drafts/v0.2/model/examples/vl-color-detector.zip",
])
def test_public_manifest_and_snapshot_are_defensive_copies(path):
    source = REPO / path
    with reference.load_package(source) as model:
        original = model.manifest
        golden = original["golden"]
        fixtures = golden if isinstance(golden, list) else golden["fixtures"]
        frame = np.load(io.BytesIO(model.snapshot.content(fixtures[-1]["input"])), allow_pickle=False)
        before = model.predict(frame)

        # Exercise multiple nested branches, including the separately exposed
        # snapshot.  These edits must never affect validated execution state.
        public = model.manifest
        public["files"].clear()
        public["version"] = "999.0.0"
        if "categories" in public:
            public["categories"][0]["label"] = "tampered"
        if isinstance(public["profile"], dict):
            public["profile"]["threshold"] = 1.0
            public["profile"]["categories"][0]["label"] = "tampered"
        if "result" in public and "threshold" in public["result"]:
            public["result"]["threshold"] = 1.0
        if "backend" in public:
            public["backend"]["input_name"] = "tampered"
        public["source_input"]["shape"] = [1, 1, 3]
        model.snapshot.manifest["files"].clear()
        if isinstance(model, reference.UnifiedModel):
            model.categories[0]["label"] = "tampered"
            model.category_by_id[next(iter(model.category_by_id))]["label"] = "tampered"
        with pytest.raises(TypeError):
            model.snapshot.files["model-package.json"] = b"changed"
        with pytest.raises(AttributeError):
            model.profile = "tampered"
        with pytest.raises(AttributeError):
            model.snapshot.path = Path("/tmp/other.zip")

        assert model.manifest == original
        assert model.predict(frame) == before
        assert model.inference_calls == 2


@pytest.mark.parametrize("mutation", ["wrong_category", "wrong_label", "wrong_output_index"])
def test_invalid_golden_classification_semantics_rejected(tmp_path, mutation):
    target = tmp_path / (mutation + ".zip")
    def alter(members):
        manifest = json.loads(members["model-package.json"])
        expected = manifest["golden"]["fixtures"][0]["expected"]
        if mutation == "wrong_category":
            expected["category_id"] = 999
        elif mutation == "wrong_label":
            expected["label"] = "undeclared"
        else:
            expected["output_index"] = 999
        members["model-package.json"] = json.dumps(manifest).encode()
    _rewrite(ROOT / "examples/classification.zip", target, alter)
    with pytest.raises(reference.PackageError, match="golden classification"):
        reference.load_package(target)


@pytest.mark.parametrize("mutation", ["bad_id", "out_of_range_id", "order", "label", "box", "threshold"])
def test_invalid_golden_detection_semantics_rejected(tmp_path, mutation):
    target = tmp_path / (mutation + ".zip")
    def alter(members):
        manifest = json.loads(members["model-package.json"])
        detections = next(f["expected"]["detections"] for f in manifest["golden"]["fixtures"]
                          if len(f["expected"]["detections"]) >= 2)
        if mutation == "bad_id":
            detections[0]["object_id"] = "00"
        elif mutation == "out_of_range_id":
            detections[0]["object_id"] = str(manifest["result"]["max_detections"])
        elif mutation == "order":
            detections.reverse()
        elif mutation == "label":
            detections[0]["label"] = "undeclared"
        elif mutation == "box":
            detections[0]["bbox"] = [0.7, 0.0, 0.2, 1.0]
        else:
            detections[0]["score"] = 0.0
        members["model-package.json"] = json.dumps(manifest).encode()
    _rewrite(ROOT / "examples/detection.zip", target, alter)
    with pytest.raises(reference.PackageError, match="golden detection"):
        reference.load_package(target)
