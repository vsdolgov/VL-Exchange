"""Both independently implemented hosts must reject invalid model outputs."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import zipfile

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper
import pytest

from reference.runtime import load_package as reference_load
from independent.host import load_package as independent_load

BASE = Path(__file__).resolve().parents[1]


def invalid_graph(manifest, kind):
    backend = manifest["backend"]
    source = helper.make_tensor_value_info(backend["input_name"], TensorProto.FLOAT, backend["input_shape"])
    outputs = []
    nodes = []

    def constant(name, array, tensor_type):
        array = np.asarray(array)
        outputs.append(helper.make_tensor_value_info(name, tensor_type, list(array.shape)))
        nodes.append(helper.make_node("Constant", [], [name], value=numpy_helper.from_array(array)))

    if manifest["profile"] == "classification-onnx-cpu-v1":
        scores = [float("nan"), 0.5, 0.5] if kind == "nan" else [0.2, 0.2, 0.2]
        constant(backend["outputs"]["scores"], np.array([scores], dtype=np.float32), TensorProto.FLOAT)
    else:
        boxes = [[[0.5, 0.5, 0.5, 1.0], [0.0, 0.0, 1.0, 1.0]]]
        classes = [[1, 1]] if kind == "bbox" else [[999, 1]]
        constant(backend["outputs"]["boxes"], np.array(boxes, dtype=np.float32), TensorProto.FLOAT)
        constant(backend["outputs"]["scores"], np.array([[0.9, 0.9]], dtype=np.float32), TensorProto.FLOAT)
        constant(backend["outputs"]["classes"], np.array(classes, dtype=np.int64), TensorProto.INT64)
    graph = helper.make_graph(nodes, "invalid-output-probe", [source], outputs)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    return model.SerializeToString()


@pytest.mark.parametrize("profile,kind", [
    ("classification", "nan"),
    ("classification", "not-normalized"),
    ("detection", "bbox"),
    ("detection", "unknown-category"),
])
@pytest.mark.parametrize("load", [reference_load, independent_load], ids=["reference", "independent"])
def test_rejects_bad_outputs_after_real_inference(tmp_path, profile, kind, load):
    with zipfile.ZipFile(BASE / "examples" / f"{profile}.zip") as source:
        files = {info.filename: source.read(info) for info in source.infolist()}
    manifest = json.loads(files["model-package.json"])
    weight_path = manifest["backend"]["weights"]
    files[weight_path] = invalid_graph(manifest, kind)
    manifest["files"][weight_path] = hashlib.sha256(files[weight_path]).hexdigest()
    files["model-package.json"] = json.dumps(manifest, ensure_ascii=False).encode()
    fixture = manifest["golden"]["fixtures"][0]["input"]
    frame = np.load(io.BytesIO(files[fixture]), allow_pickle=False)
    path = tmp_path / "invalid-output.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    model = load(path)
    try:
        with pytest.raises(ValueError):
            model.predict(frame)
        assert model.inference_calls == 1  # The rejection follows a real ONNX call.
    finally:
        model.close()
