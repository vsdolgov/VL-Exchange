from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import zipfile

import numpy as np
import onnx
import pytest

from independent.host import PackageError, load_fixture, load_package


ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT.parents[2]


def _archive(name):
    return ROOT / "examples" / (name + ".zip")


def _modified(source, target, change):
    with zipfile.ZipFile(source) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files["model-package.json"])
    change(manifest, files)
    files["model-package.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as out:
        for name, data in files.items():
            out.writestr(name, data)


@pytest.mark.parametrize("profile", ["classification", "detection"])
def test_new_profiles_real_onnx_and_unchanged_zip(profile):
    archive = _archive(profile)
    before = hashlib.sha256(archive.read_bytes()).hexdigest()
    model = load_package(archive)
    try:
        assert model.zip_sha256 == before
        assert model.spec_version == "0.2.0"
        fixture = model.manifest["golden"]["fixtures"][0]
        image = load_fixture(model._root / fixture["input"], model.manifest["source_input"]["shape"])
        actual = model.predict(image)
        if profile == "classification":
            assert actual["label"] == fixture["expected"]["label"]
            assert actual["score"] == pytest.approx(fixture["expected"]["score"], abs=1e-6)
        else:
            assert actual == fixture["expected"]
        # A strided array with the same HWC values is explicitly supported.
        storage = np.empty((image.shape[0], image.shape[1] * 2, 3), dtype=np.uint8)
        strided = storage[:, ::2, :]
        strided[:] = image
        assert not strided.flags.c_contiguous
        assert model.predict(strided) == actual
    finally:
        model.close()
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == before
    with pytest.raises(PackageError):
        model.predict(image)


@pytest.mark.parametrize("path", [
    "model/reports/vlmp-public-channel-probe.zip",
    "drafts/v0.2/model/examples/vl-color-detector.zip",
])
def test_published_legacy_zip_still_loads(path):
    source = REPO / path
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    model = load_package(source)
    try:
        golden = model.manifest["golden"]
        fixture = golden["fixtures"][0] if isinstance(golden, dict) else golden[0]
        image = np.load(model._root / fixture["input"], allow_pickle=False)
        assert model.predict(image)
        assert model.zip_sha256 == digest
    finally:
        model.close()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest


def test_legacy_detection_does_not_inherit_new_opset_limit(tmp_path):
    path = tmp_path / "legacy-detector.zip"
    def use_opset_twelve(manifest, files):
        name = manifest["profile"]["weights"]
        graph = onnx.load_model_from_string(files[name])
        graph.opset_import[0].version = 12
        files[name] = graph.SerializeToString()
        manifest["files"][name] = hashlib.sha256(files[name]).hexdigest()
    _modified(REPO / "drafts/v0.2/model/examples/vl-color-detector.zip", path, use_opset_twelve)
    model = load_package(path)
    try:
        fixture = model.manifest["golden"][-1]
        image = np.load(model._root / fixture["input"], allow_pickle=False)
        assert model.predict(image) == fixture["expected"]
    finally:
        model.close()


@pytest.mark.parametrize("profile", ["classification", "detection"])
def test_public_manifest_cannot_mutate_validated_execution(profile):
    model = load_package(_archive(profile))
    try:
        view = model.manifest
        fixture = view["golden"]["fixtures"][-1]
        image = load_fixture(model._root / fixture["input"], view["source_input"]["shape"])
        before = model.predict(image)
        view["categories"][0]["label"] = "mutated"
        view["source_input"]["shape"][0] = 100
        view["backend"]["input_shape"][2] = 100
        view["backend"]["outputs"]["scores"] = "nonexistent"
        if profile == "detection":
            view["result"]["threshold"] = 1.0
        assert model.predict(image) == before
        assert model.manifest["categories"][0]["label"] != "mutated"
        with pytest.raises(AttributeError):
            model.profile = "forged"
        with pytest.raises(AttributeError):
            model.zip_sha256 = "0" * 64
        assert not hasattr(model, "shape")
        assert not hasattr(model, "categories")
    finally:
        model.close()


@pytest.mark.parametrize("path", [
    "model/reports/vlmp-public-channel-probe.zip",
    "drafts/v0.2/model/examples/vl-color-detector.zip",
])
def test_legacy_public_manifest_is_an_isolated_copy(path):
    model = load_package(REPO / path)
    try:
        view = model.manifest
        golden = view["golden"]
        fixture = golden["fixtures"][-1] if isinstance(golden, dict) else golden[-1]
        image = np.load(model._root / fixture["input"], allow_pickle=False)
        before = model.predict(image)
        if isinstance(view["profile"], dict):
            view["profile"]["categories"][0]["label"] = "mutated"
            view["profile"]["threshold"] = 1.0
        else:
            view["categories"][0]["label"] = "mutated"
            view["backend"]["input_shape"][2] = 100
        assert model.predict(image) == before
        with pytest.raises(AttributeError):
            model.spec_version = "forged"
    finally:
        model.close()


def test_legacy_classification_rejects_nondistinct_golden(tmp_path):
    path = tmp_path / "legacy-classifier.zip"
    def repeat_fixtures(manifest, files):
        fixtures = manifest["golden"]["fixtures"]
        shared = files[fixtures[0]["input"]]
        for fixture in fixtures[:4]:
            name = fixture["input"]
            files[name] = shared
            manifest["files"][name] = hashlib.sha256(shared).hexdigest()
    _modified(REPO / "model/reports/vlmp-public-channel-probe.zip", path, repeat_fixtures)
    with pytest.raises(PackageError, match="three distinct legacy golden"):
        load_package(path)


def test_legacy_classification_rejects_inconsistent_golden_category(tmp_path):
    path = tmp_path / "legacy-classifier.zip"
    def corrupt(manifest, files):
        manifest["golden"]["fixtures"][0]["expected"]["label"] = "green"
    _modified(REPO / "model/reports/vlmp-public-channel-probe.zip", path, corrupt)
    with pytest.raises(PackageError, match="legacy golden classification category mismatch"):
        load_package(path)


def test_legacy_golden_may_differ_from_onnx_input_size(tmp_path):
    path = tmp_path / "legacy-classifier.zip"
    def enlarge(manifest, files):
        name = manifest["golden"]["fixtures"][0]["input"]
        buffer = io.BytesIO()
        np.save(buffer, np.zeros((5, 7, 3), dtype=np.uint8), allow_pickle=False)
        files[name] = buffer.getvalue()
        manifest["files"][name] = hashlib.sha256(files[name]).hexdigest()
    _modified(REPO / "model/reports/vlmp-public-channel-probe.zip", path, enlarge)
    model = load_package(path)
    try:
        fixture = model.manifest["golden"]["fixtures"][0]
        image = load_fixture(model._root / fixture["input"])
        assert image.shape == (5, 7, 3)
        assert model.predict(image)["label"] == fixture["expected"]["label"]
    finally:
        model.close()


def test_legacy_large_fake_npy_shape_rejected_before_allocation(tmp_path):
    path = tmp_path / "legacy-classifier.zip"
    def corrupt(manifest, files):
        name = manifest["golden"]["fixtures"][0]["input"]
        buffer = io.BytesIO()
        np.lib.format.write_array_header_1_0(buffer, {
            "descr": "|u1", "fortran_order": False, "shape": (10**12, 3, 3),
        })
        files[name] = buffer.getvalue()
        manifest["files"][name] = hashlib.sha256(files[name]).hexdigest()
    _modified(REPO / "model/reports/vlmp-public-channel-probe.zip", path, corrupt)
    with pytest.raises(PackageError, match="golden input exceeds host per-file limit"):
        load_package(path)


def test_zip_changed_after_load_is_rejected(tmp_path):
    path = tmp_path / "model.zip"
    path.write_bytes(_archive("classification").read_bytes())
    model = load_package(path)
    image = np.zeros(model.manifest["source_input"]["shape"], dtype=np.uint8)
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(PackageError, match="changed"):
        model.predict(image)
    with pytest.raises(PackageError, match="changed"):
        model.close()


def test_large_fake_npy_shape_rejected_before_inference(tmp_path):
    path = tmp_path / "model.zip"
    def corrupt(manifest, files):
        name = manifest["golden"]["fixtures"][0]["input"]
        output = io.BytesIO()
        np.lib.format.write_array_header_1_0(output, {
            "descr": "|u1", "fortran_order": False, "shape": (10**12, 3, 3),
        })
        files[name] = output.getvalue()
        manifest["files"][name] = hashlib.sha256(files[name]).hexdigest()
    _modified(_archive("classification"), path, corrupt)
    with pytest.raises(PackageError, match="golden input dtype or shape mismatch"):
        load_package(path)


def test_fractional_representation_rejected(tmp_path):
    path = tmp_path / "model.zip"
    _modified(_archive("classification"), path, lambda manifest, files: manifest["backend"]["input_shape"].__setitem__(0, 1.0))
    with pytest.raises(PackageError):
        load_package(path)


def test_unknown_required_capability_rejected(tmp_path):
    path = tmp_path / "model.zip"
    _modified(_archive("classification"), path, lambda manifest, files: manifest["required_capabilities"].append("vendor.unknown.v1"))
    with pytest.raises(PackageError, match="capability"):
        load_package(path)


def test_undeclared_file_rejected(tmp_path):
    path = tmp_path / "model.zip"
    _modified(_archive("classification"), path, lambda manifest, files: files.__setitem__("untracked.txt", b"unexpected"))
    with pytest.raises(PackageError, match="inventory"):
        load_package(path)


def test_graph_metadata_mismatch_rejected(tmp_path):
    path = tmp_path / "model.zip"
    def alter_graph(manifest, files):
        name = manifest["backend"]["weights"]
        graph = onnx.load_model_from_string(files[name])
        graph.graph.input[0].type.tensor_type.shape.dim[2].dim_value += 1
        files[name] = graph.SerializeToString()
        manifest["files"][name] = hashlib.sha256(files[name]).hexdigest()
    _modified(_archive("classification"), path, alter_graph)
    with pytest.raises(PackageError):
        load_package(path)


def test_detection_output_bindings_do_not_depend_on_json_key_order(tmp_path):
    path = tmp_path / "model.zip"
    def reorder(manifest, files):
        outputs = manifest["backend"]["outputs"]
        manifest["backend"]["outputs"] = {key: outputs[key] for key in ("classes", "scores", "boxes")}
    _modified(_archive("detection"), path, reorder)
    model = load_package(path)
    try:
        fixture = model.manifest["golden"]["fixtures"][-1]
        image = load_fixture(model._root / fixture["input"], model.manifest["source_input"]["shape"])
        assert model.predict(image) == fixture["expected"]
    finally:
        model.close()


def test_zip_slip_rejected(tmp_path):
    path = tmp_path / "model.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../escape", b"forbidden")
    with pytest.raises(PackageError):
        load_package(path)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("entry", ["artifacts:v1/weights.bin", "artifacts/sub:v1/weights.bin"])
def test_zip_rejects_colon_in_any_path_segment(tmp_path, entry):
    path = tmp_path / "model.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(entry, b"forbidden")
    with pytest.raises(PackageError, match="nonportable package path"):
        load_package(path)


def test_duplicate_zip_name_rejected(tmp_path):
    path = tmp_path / "model.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("model-package.json", b"{}")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("model-package.json", b"{}")
    with pytest.raises(PackageError, match="duplicate ZIP"):
        load_package(path)
