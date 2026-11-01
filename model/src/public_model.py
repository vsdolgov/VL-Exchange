"""Experimental public VLMP profile. No private SDK. Not a Python sandbox."""
from __future__ import annotations
import hashlib
import importlib.util
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import platform
import sys
import uuid
import numpy as np
import onnx
import onnxruntime as ort
from jsonschema import Draft202012Validator
from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES = {"python-numpy-classification-v1", "onnx-cpu-fp32"}

class PackageError(ValueError):
    pass

def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackageError("duplicate_json_key")
        result[key] = value
    return result

def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(PackageError("nonfinite_json")))

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def path_in(root, name):
    if not isinstance(name,str) or not name or "\\" in name or any(p in ("", ".", "..") for p in name.split("/")) or PurePosixPath(name).is_absolute():
        raise PackageError("unsafe_path")
    root=Path(root).resolve(); current=root
    for part in name.split("/"):
        current=current/part
        if current.is_symlink(): raise PackageError("symlink")
    if not current.is_file() or not current.resolve().is_relative_to(root): raise PackageError("missing_file")
    return current

def validate(root, *, environment=True):
    root=Path(root).resolve()
    manifest=read_json(path_in(root,"model-package.json"))
    Draft202012Validator(read_json(ROOT/"spec/model-package.schema.json")).validate(manifest)
    if set(manifest["required_capabilities"])-CAPABILITIES: raise PackageError("unsupported_capability")
    if any(v["required"] for v in manifest["extensions"].values()): raise PackageError("unsupported_extension")
    actual=set()
    for path in root.rglob("*"):
        if path.is_symlink(): raise PackageError("symlink")
        if path.is_file() and path.relative_to(root).as_posix()!="model-package.json": actual.add(path.relative_to(root).as_posix())
    if actual!=set(manifest["files"]): raise PackageError("inventory_mismatch")
    for name,digest in manifest["files"].items():
        if sha(path_in(root,name))!=digest: raise PackageError("digest_mismatch")
    e=manifest["entrypoint"]
    for name in (e["root"]+"/__init__.py",e["root"]+"/"+e["module"],manifest["backend"]["weights"]):
        if name not in manifest["files"]: raise PackageError("missing_inventory_reference")
        path_in(root,name)
    cats=manifest["categories"]
    if [c["output_index"] for c in cats]!=list(range(len(cats))): raise PackageError("category_order")
    if len({c["label"] for c in cats})!=len(cats) or len({c["category_id"] for c in cats})!=len(cats): raise PackageError("duplicate_category")
    if manifest["backend"]["output_shape"]!=[1,len(cats)]: raise PackageError("category_shape")
    ids=set(); inputs=set(); input_contents=set()
    for f in manifest["golden"]["fixtures"]:
        if f["id"] in ids or f["input"] in inputs: raise PackageError("duplicate_fixture")
        ids.add(f["id"]);inputs.add(f["input"])
        if f["input"] not in manifest["files"]: raise PackageError("missing_fixture_inventory")
        image=np.load(path_in(root,f["input"]),allow_pickle=False)
        check_image(image)
        input_contents.add((image.dtype.str,tuple(image.shape),hashlib.sha256(image.tobytes(order="C")).hexdigest()))
    if len(input_contents)<3: raise PackageError("golden_inputs_not_distinct")
    graph=onnx.load(path_in(root,manifest["backend"]["weights"]),load_external_data=False)
    if any(t.data_location==onnx.TensorProto.EXTERNAL or t.external_data for t in graph.graph.initializer): raise PackageError("external_weights")
    if [(x.domain,x.version) for x in graph.opset_import] != [("",13)]: raise PackageError("unsupported_opset")
    if any(n.domain not in ("","ai.onnx") for n in graph.graph.node): raise PackageError("custom_operators")
    onnx.checker.check_model(graph,full_check=True)
    for values,key,shape_key in ((graph.graph.input,"input_name","input_shape"),(graph.graph.output,"output_name","output_shape")):
        if len(values)!=1 or values[0].name!=manifest["backend"][key]: raise PackageError("graph_names")
        t=values[0].type.tensor_type
        if t.elem_type!=onnx.TensorProto.FLOAT or [d.dim_value for d in t.shape.dim]!=manifest["backend"][shape_key]: raise PackageError("graph_shape")
    if environment:
        if platform.python_version() not in SpecifierSet(manifest["runtime"]["python"]): raise PackageError("python_version")
        for name,constraint in manifest["dependencies"].items():
            if importlib.metadata.version(name) not in SpecifierSet(constraint): raise PackageError("dependency_version")
    return manifest

def check_image(image):
    if not isinstance(image,np.ndarray) or image.dtype!=np.uint8 or image.ndim!=3 or image.shape[2]!=3 or min(image.shape[:2])<1 or not image.flags.c_contiguous:
        raise PackageError("input_contract")

def _tensor(value,shape):
    if not isinstance(value,np.ndarray) or value.dtype!=np.float32 or list(value.shape)!=shape or not np.isfinite(value).all(): raise PackageError("tensor_contract")
    return np.ascontiguousarray(value)

class ModelExecutor:
    def __init__(self,root):
        self.root=Path(root).resolve();self.manifest=validate(self.root)
        self.namespace="_vlmp_public_"+uuid.uuid4().hex
        e=self.manifest["entrypoint"]; directory=self.root/e["root"]
        sys.dont_write_bytecode=True
        package=importlib.util.spec_from_file_location(self.namespace,directory/"__init__.py",submodule_search_locations=[str(directory)])
        module=importlib.util.module_from_spec(package);sys.modules[self.namespace]=module;package.loader.exec_module(module)
        spec=importlib.util.spec_from_file_location(self.namespace+".model",directory/e["module"])
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        self.model=getattr(module,e["class"])()
        b=self.manifest["backend"]
        self.session=ort.InferenceSession(str(self.root/b["weights"]),providers=["CPUExecutionProvider"])
    def predict(self,image):
        check_image(image);b=self.manifest["backend"]
        prepared=self.model.preprocess(image)
        if type(prepared)!=dict or set(prepared)!={b["input_name"]}: raise PackageError("input_names")
        tensor=_tensor(prepared[b["input_name"]],b["input_shape"])
        out=self.session.run([b["output_name"]],{b["input_name"]:tensor})[0]
        result=self.model.postprocess({b["output_name"]:_tensor(out,b["output_shape"])},tuple(c["label"] for c in self.manifest["categories"]))
        if type(result)!=dict or set(result)!={"label","score"}:raise PackageError("result_contract")
        if type(result["score"]) not in (float,int) or not np.isfinite(result["score"]) or not 0<=result["score"]<=1:raise PackageError("score_contract")
        candidates=[c for c in self.manifest["categories"] if c["label"]==result["label"]]
        if len(candidates)!=1:raise PackageError("unknown_label")
        return {**candidates[0],"score":float(result["score"])}
    def close(self):
        self.session=None;self.model=None
        for name in list(sys.modules):
            if name==self.namespace or name.startswith(self.namespace+"."):sys.modules.pop(name,None)

def golden(root):
    root=Path(root).resolve(); before={p.relative_to(root).as_posix():sha(p) for p in root.rglob("*") if p.is_file()}
    manifest=validate(root); outcomes=[]
    fixtures=manifest["golden"]["fixtures"]
    for order in (fixtures,list(reversed(fixtures)),[fixtures[0],fixtures[1],fixtures[0]]):
        executor=ModelExecutor(root)
        try:
            for f in order:
                result=executor.predict(np.load(root/f["input"],allow_pickle=False));expected=f["expected"]
                ok=all(result[k]==expected[k] for k in ("label","category_id","output_index")) and abs(result["score"]-expected["score"])<=manifest["golden"]["atol"]+manifest["golden"]["rtol"]*abs(expected["score"])
                outcomes.append({"fixture":f["id"],"pass":ok,"result":result})
        finally:executor.close()
    after={p.relative_to(root).as_posix():sha(p) for p in root.rglob("*") if p.is_file()}
    return {"status":"pass" if all(x["pass"] for x in outcomes) and before==after else "fail","profile":manifest["profile"],"manifest_sha256":sha(root/"model-package.json"),"unchanged":before==after,"calls":outcomes,"runtime":{"python":platform.python_version(),"numpy":np.__version__,"onnxruntime":ort.__version__,"providers":["CPUExecutionProvider"]},"box5_compatibility_verified":False}

if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument("command",choices=("validate","golden"));parser.add_argument("package",type=Path);args=parser.parse_args()
    try:
        report=golden(args.package) if args.command=="golden" else {"status":"pass","manifest_sha256":sha(args.package/"model-package.json"),"model_code_executed":False,"profile":validate(args.package)["profile"]}
        print(json.dumps(report,indent=2));raise SystemExit(0 if report["status"]=="pass" else 1)
    except Exception as exc:
        print(json.dumps({"status":"error","type":type(exc).__name__,"message":str(exc)}));raise SystemExit(1)
