from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np
import onnxruntime as ort
import pytest

from model_package_v02 import ModelError, load_model

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/vl-color-detector"


def digest_tree(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def test_real_onnx_inference_and_golden_are_input_dependent():
    before = digest_tree(EXAMPLE)
    model = load_model(EXAMPLE)
    assert model.session.get_providers() == ["CPUExecutionProvider"]
    outputs = []
    for case in model.manifest["golden"]:
        frame = np.load(EXAMPLE / case["input"], allow_pickle=False)
        actual = model.infer(frame)
        assert actual == case["expected"]
        outputs.append(actual)
    assert [len(x["detections"]) for x in outputs] == [0, 1, 1, 2]
    assert model.inference_calls == 4
    assert digest_tree(EXAMPLE) == before
    model.close()


@pytest.mark.parametrize("frame", [np.zeros((2, 4, 3), dtype=np.float32),
                                   np.zeros((2, 4), dtype=np.uint8),
                                   np.zeros((4, 4, 3), dtype=np.uint8)])
def test_incompatible_image_is_rejected_before_inference(frame):
    model = load_model(EXAMPLE)
    with pytest.raises(ModelError, match="Expected BGR"):
        model.infer(frame)
    assert model.inference_calls == 0
    model.close()


def test_tampered_weight_is_rejected_before_graph_load(tmp_path):
    root = tmp_path / "model"
    shutil.copytree(EXAMPLE, root)
    with (root / "artifacts/detector.onnx").open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ModelError, match="digest"):
        load_model(root)


def test_unknown_or_modified_profile_is_rejected(tmp_path):
    root = tmp_path / "model"
    shutil.copytree(EXAMPLE, root)
    file = root / "model-package.json"
    manifest = json.loads(file.read_text())
    manifest["profile"]["id"] = "unknown-model-profile"
    file.write_text(json.dumps(manifest))
    with pytest.raises(ModelError, match="manifest"):
        load_model(root)


def test_unlisted_or_linked_file_is_rejected(tmp_path):
    root = tmp_path / "model"
    shutil.copytree(EXAMPLE, root)
    (root / "unexpected").write_text("unknown")
    with pytest.raises(ModelError, match="inventory"):
        load_model(root)
    (root / "unexpected").unlink()
    (root / "link").symlink_to(root / "artifacts/detector.onnx")
    with pytest.raises(ModelError, match="symlink"):
        load_model(root)
