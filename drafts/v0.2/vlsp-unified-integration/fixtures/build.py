#!/usr/bin/env python3
"""Reproducibly assemble the integration fixtures without changing prior releases."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import zipfile

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
PILOT = ROOT / "drafts/v0.2/videopipe-pilot/fixtures"
UNIFIED = ROOT / "drafts/v0.2/vlmp-unified/examples"
ORIGINAL_SHA = "1461028eb6176c72bb0229eb8b28b7e697b6a4fcc1f7d420e095631bf4e0f332"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def dump(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def readzip(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names) or any(name.startswith("/") or ".." in Path(name).parts for name in names):
            raise ValueError(f"Unsafe ZIP: {path}")
        return {name: archive.read(name) for name in names}


def archive(name, entries):
    with zipfile.ZipFile(HERE / name, "w", compression=zipfile.ZIP_STORED) as result:
        for filename, content in sorted(entries.items()):
            info = zipfile.ZipInfo(filename, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            result.writestr(info, content)


def new_event_id(manifest, context, event_type, ordinal):
    fields = [manifest["scenario_id"], manifest["version"], context["source_id"], context["run_id"],
              str(context["sequence"]), event_type, str(ordinal)]
    material = b"VLSP-EVENT-V1\0"
    for field in fields:
        encoded = field.encode("utf-8")
        material += len(encoded).to_bytes(4, "big") + encoded
    return "sha256:" + digest(material)


def main():
    original_zip = PILOT / "scenario.zip"
    if digest(original_zip.read_bytes()) != ORIGINAL_SHA:
        raise ValueError("The previously published scenario ZIP has changed")
    for src, dst in [("detection.zip", "detector.zip"), ("classification.zip", "classifier_a.zip")]:
        (HERE / dst).write_bytes((UNIFIED / src).read_bytes())

    # Same proven graph and golden data as the old second classifier, with the
    # code-free unified 0.2 manifest, source input and result ABI.
    model_files = readzip(HERE / "classifier_a.zip")
    model = json.loads(model_files.pop("model-package.json"))
    old_root = PILOT / "models/classifier_b"
    previous = json.loads((old_root / "model-package.json").read_bytes())
    model["model_id"] = "org.vlexchange.examples.unified-classification-strong"
    model["golden"] = previous["golden"]
    model_files = {"artifacts/model.onnx": (old_root / "weights.onnx").read_bytes()}
    for fixture in model["golden"]["fixtures"]:
        source = np.load(old_root / fixture["input"], allow_pickle=False)
        # Legacy Python preprocessing sampled endpoints into a 2x2 tensor.
        # Unified profile fixes the source shape, so perform that sampling in
        # the fixture itself while preserving the original expected output.
        rows = np.linspace(0, source.shape[0] - 1, 2).astype(np.int64)
        cols = np.linspace(0, source.shape[1] - 1, 2).astype(np.int64)
        target = source[rows[:, None], cols[None, :]]
        output = io.BytesIO()
        np.save(output, target, allow_pickle=False)
        model_files[fixture["input"]] = output.getvalue()
    model["files"] = {path: digest(content) for path, content in sorted(model_files.items())}
    model_files["model-package.json"] = dump(model)
    archive("classifier_b.zip", model_files)

    original = readzip(original_zip)
    scenario = json.loads(original.pop("scenario-package.json"))
    # A new immutable scenario package identity is required because model pins
    # changed. Source code remains byte-for-byte identical to the pilot.
    scenario["version"] = "1.0.1"
    for binding in scenario["models"]:
        alias = binding["alias"]
        model_manifest = readzip(HERE / (alias + ".zip"))["model-package.json"]
        unified_manifest = json.loads(model_manifest)
        binding["profile"] = unified_manifest["profile"]
        binding["manifest_sha256"] = digest(model_manifest)
    golden = json.loads(original["golden/basic.json"])
    for step in golden["steps"]:
        for ordinal, event in enumerate(step.get("expected", {}).get("events", [])):
            event["event_id"] = new_event_id(scenario, step["context"], event["event_type"], ordinal)
    original["golden/basic.json"] = dump(golden)
    scenario["files"] = {path: digest(content) for path, content in sorted(original.items())}
    original["scenario-package.json"] = dump(scenario)
    archive("scenario.zip", original)

    pins = {"previous_scenario_zip_sha256": ORIGINAL_SHA}
    for alias in ("scenario", "detector", "classifier_a", "classifier_b"):
        path = HERE / (alias + ".zip")
        contents = readzip(path)
        manifest_name = "scenario-package.json" if alias == "scenario" else "model-package.json"
        manifest = json.loads(contents[manifest_name])
        pins[alias] = {"zip_sha256": digest(path.read_bytes()),
                       "manifest_sha256": digest(contents[manifest_name])}
        if alias != "scenario":
            pins[alias]["weights_sha256"] = digest(contents[manifest["backend"]["weights"]])
    assert pins["classifier_a"]["weights_sha256"] != pins["classifier_b"]["weights_sha256"]
    assert original["code/scenario.py"] == readzip(original_zip)["code/scenario.py"]
    (HERE / "pins.json").write_bytes(dump(pins))
    print(json.dumps(pins, indent=2))


if __name__ == "__main__":
    main()
