"""Format examples and compatibility invariants independent of any host."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

from jsonschema import Draft202012Validator
import pytest

PROJECT = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[4]
SCHEMA = json.loads((PROJECT / "spec/model-package.schema.json").read_text())


def manifest(name):
    with zipfile.ZipFile(PROJECT / "examples" / f"{name}.zip") as archive:
        return json.loads(archive.read("model-package.json"))


@pytest.mark.parametrize("name", ["classification", "detection"])
def test_examples_validate_and_inventory_matches_exact_zip(name):
    Draft202012Validator.check_schema(SCHEMA)
    with zipfile.ZipFile(PROJECT / "examples" / f"{name}.zip") as archive:
        data = json.loads(archive.read("model-package.json"))
        assert not list(Draft202012Validator(SCHEMA).iter_errors(data))
        assert set(archive.namelist()) == set(data["files"]) | {"model-package.json"}
        assert all(hashlib.sha256(archive.read(path)).hexdigest() == digest
                   for path, digest in data["files"].items())
        assert len(data["golden"]["fixtures"]) >= 3


@pytest.mark.parametrize("name,mutator", [
    ("classification", lambda m: m.update(spec_version="0.2.0-draft.1")),
    ("classification", lambda m: m.update(profile="detection-onnx-cpu-v1")),
    ("classification", lambda m: m["backend"]["outputs"].update(unexpected="out")),
    ("classification", lambda m: m["backend"].update(weights="artifacts/v1:model.onnx")),
    ("classification", lambda m: m["result"].update(type="vl.detections.v1")),
    ("classification", lambda m: m["golden"]["fixtures"][0]["expected"].update(score=1.1)),
    ("classification", lambda m: m.update(required_capabilities=["onnx-cpu-fp32", "unknown"])),
    ("detection", lambda m: m["result"].update(threshold=-0.1)),
    ("detection", lambda m: m["result"].update(bbox_space="pixels")),
    ("detection", lambda m: m["backend"]["outputs"].pop("classes")),
    ("detection", lambda m: m["source_input"].update(color_space="RGB")),
    ("detection", lambda m: m["files"].update({"golden/v1:frame.npy": "0" * 64})),
    ("detection", lambda m: m["golden"].update(fixtures=m["golden"]["fixtures"][:2])),
])
def test_profile_mismatches_and_invalid_fields_are_rejected(name, mutator):
    data = deepcopy(manifest(name))
    mutator(data)
    assert list(Draft202012Validator(SCHEMA).iter_errors(data))


@pytest.mark.parametrize("profile", ["classification", "detection"])
def test_old_schema_and_examples_remain_distinct_and_valid(profile):
    if profile == "classification":
        schema = ROOT / "model/spec/model-package.schema.json"
        example = ROOT / "model/examples/channel-classifier/model-package.json"
    else:
        schema = ROOT / "drafts/v0.2/model/spec/model-package.schema.json"
        example = ROOT / "drafts/v0.2/model/examples/vl-color-detector/model-package.json"
    old_schema = json.loads(schema.read_text())
    old = json.loads(example.read_text())
    assert not list(Draft202012Validator(old_schema).iter_errors(old))
    assert list(Draft202012Validator(SCHEMA).iter_errors(old))


def test_builder_reproducibly_emits_identical_packages_without_touching_pinned_examples(tmp_path, monkeypatch):
    target = PROJECT / "tools/build_examples.py"
    spec = importlib.util.spec_from_file_location("vlmp_unified_builder_test", target)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    monkeypatch.setattr(builder, "PROJECT", tmp_path)
    builder.main()
    for name in ("classification", "detection"):
        assert (tmp_path / "examples" / f"{name}.zip").read_bytes() == (PROJECT / "examples" / f"{name}.zip").read_bytes()
