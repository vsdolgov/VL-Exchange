#!/usr/bin/env python3
"""Build deterministic synthetic VLMP 0.2.0 ZIPs from public ONNX probes.

No old package or manifest is modified. The expected values come from the
documented construction of the synthetic graphs, not a host under test.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import tempfile
import zipfile

import numpy as np

ROOT = Path(__file__).resolve().parents[4]
PROJECT = Path(__file__).resolve().parents[1]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def base(profile: str, source: list[int]) -> dict:
    h, w, _ = source
    return {
        "format": "vl.model-package", "spec_version": "0.2.0",
        "model_id": f"org.vlexchange.examples.unified-{profile}", "version": "1.0.0",
        "profile": f"{profile}-onnx-cpu-v1",
        "backend": {"engine": "onnxruntime", "device": "cpu", "precision": "fp32",
                    "weights": "artifacts/model.onnx", "opset": 13, "input_name": "images",
                    "input_shape": [1, 3, h, w]},
        "source_input": {"dtype": "uint8", "layout": "HWC", "color_space": "BGR", "shape": source},
        "categories": [], "result": {},
        "golden": {"atol": 0.000001, "rtol": 0.00001, "fixtures": []},
        "files": {}, "required_capabilities": ["onnx-cpu-fp32", f"{profile}-onnx-cpu-v1"],
        "extensions": {},
    }


def add_file(root: Path, name: str, data: bytes) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def add_array(root: Path, name: str, arr: np.ndarray) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, arr, allow_pickle=False)


def build_classification(root: Path) -> dict:
    model = base("classification", [2, 2, 3])
    model["backend"]["outputs"] = {"scores": "probabilities"}
    model["result"] = {"type": "vl.classification.v1"}
    model["categories"] = [{"output_index": i, "category_id": i + 1, "label": label}
                           for i, label in enumerate(("red", "green", "blue"))]
    source = ROOT / "model/examples/channel-classifier/weights.onnx"
    add_file(root, model["backend"]["weights"], source.read_bytes())
    cases = [("black", [0, 0, 0], 0, 1 / 3),
             ("red", [0, 0, 255], 0, math.e / (math.e + 2)),
             ("green", [0, 255, 0], 1, math.e / (math.e + 2)),
             ("blue", [255, 0, 0], 2, math.e / (math.e + 2))]
    for case, color, index, score in cases:
        name = f"golden/{case}.npy"
        image = np.empty((2, 2, 3), dtype=np.uint8)
        image[:] = color
        add_array(root, name, image)
        category = model["categories"][index]
        model["golden"]["fixtures"].append({"id": case, "input": name,
            "expected": {"label": category["label"], "score": score,
                         "category_id": category["category_id"], "output_index": index}})
    return model


def build_detection(root: Path) -> dict:
    source = ROOT / "drafts/v0.2/model/examples/vl-color-detector"
    previous = json.loads((source / "model-package.json").read_text(encoding="utf-8"))
    model = base("detection", [2, 4, 3])
    model["backend"]["outputs"] = {"boxes": "boxes", "scores": "scores", "classes": "classes"}
    model["result"] = {"type": "vl.detections.v1", "threshold": 0.2,
                       "max_detections": 2, "bbox_space": "normalized-xyxy-input-frame"}
    model["categories"] = [{"output_index": 0, "category_id": 1, "label": "person"}]
    add_file(root, model["backend"]["weights"], (source / "artifacts/detector.onnx").read_bytes())
    for item in previous["golden"]:
        name = item["input"]
        add_file(root, name, (source / name).read_bytes())
        model["golden"]["fixtures"].append({"id": Path(name).stem, "input": name, "expected": item["expected"]})
    return model


def write_archive(root: Path, manifest: dict, destination: Path) -> None:
    manifest["files"] = {p.relative_to(root).as_posix(): sha(p)
                         for p in sorted(root.rglob("*")) if p.is_file()}
    add_file(root, "model-package.json", (json.dumps(manifest, ensure_ascii=False, indent=2,
                                                       allow_nan=False) + "\n").encode("utf-8"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Stored entries avoid zlib-version-dependent output across build hosts.
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                entry = zipfile.ZipInfo(path.relative_to(root).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.compress_type = zipfile.ZIP_STORED
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, path.read_bytes(), compress_type=zipfile.ZIP_STORED)


def main() -> None:
    outputs = {}
    for profile, build in (("classification", build_classification), ("detection", build_detection)):
        with tempfile.TemporaryDirectory(prefix="vlmp-unified-builder-") as directory:
            root = Path(directory)
            manifest = build(root)
            target = PROJECT / "examples" / f"{profile}.zip"
            write_archive(root, manifest, target)
            outputs[profile] = {"path": str(target.relative_to(PROJECT)), "sha256": sha(target),
                                "manifest_sha256": sha(root / "model-package.json")}
    print(json.dumps(outputs, indent=2))


if __name__ == "__main__":
    main()
