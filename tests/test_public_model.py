from pathlib import Path
import hashlib,importlib.util,json,shutil,subprocess,sys
import numpy as np
import pytest
from public_model import ModelExecutor,PackageError,golden,validate
ROOT=Path(__file__).resolve().parents[1]
PACKAGE=ROOT/'model/examples/channel-classifier'
@pytest.fixture
def variant(tmp_path):
 target=tmp_path/'model';shutil.copytree(PACKAGE,target);return target

def update(root,change):
 path=root/'model-package.json';m=json.loads(path.read_text());change(m);path.write_text(json.dumps(m));return m

def test_two_independent_loaders_match_same_immutable_package():
 before={p.relative_to(PACKAGE).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in PACKAGE.rglob('*') if p.is_file()}
 reference=golden(PACKAGE)
 process=subprocess.run([sys.executable,str(ROOT/'model/tools/independent_host.py'),str(PACKAGE)],text=True,capture_output=True,check=True)
 independent=json.loads(process.stdout)
 assert reference['status']==independent['status']=='pass'
 assert independent['reference_executor_imported'] is False
 assert reference['calls']==independent['calls']
 assert reference['manifest_sha256']==independent['manifest_sha256']
 assert len(reference['calls'])==13
 assert before=={p.relative_to(PACKAGE).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in PACKAGE.rglob('*') if p.is_file()}

def test_digest_rejected_before_model_import(variant):
 (variant/'code/model.py').write_text('raise RuntimeError("must not import")')
 with pytest.raises(PackageError,match='digest_mismatch'):validate(variant)

def test_unknown_required_capability(variant):
 update(variant,lambda m:m['required_capabilities'].append('unknown-required-feature'))
 with pytest.raises(PackageError,match='unsupported_capability'):validate(variant)

def test_optional_extension_does_not_modify_result(variant):
 update(variant,lambda m:m['extensions'].update({'org.example.note':{'required':False,'data':{'note':'retained'}}}))
 assert golden(variant)['status']=='pass'

def test_required_extension_rejected(variant):
 update(variant,lambda m:m['extensions'].update({'org.example.required':{'required':True,'data':{}}}))
 with pytest.raises(PackageError,match='unsupported_extension'):validate(variant)

def test_category_order_rejected(variant):
 update(variant,lambda m:m['categories'].reverse())
 with pytest.raises(PackageError,match='category_order'):validate(variant)

def test_symlink_rejected(variant):
 file=variant/'golden/red.npy';file.unlink();file.symlink_to(PACKAGE/'golden/red.npy')
 with pytest.raises(PackageError,match='symlink'):validate(variant)

def test_unlisted_file_rejected(variant):
 (variant/'unlisted.txt').write_text('extra')
 with pytest.raises(PackageError,match='inventory_mismatch'):validate(variant)

@pytest.mark.parametrize('image',[np.zeros((2,2,3),dtype=np.float32),np.zeros((2,2,4),dtype=np.uint8),np.zeros((2,2,3),dtype=np.uint8)[:,:,::-1]])
def test_input_dtype_shape_and_layout_rejected(image):
 executor=ModelExecutor(PACKAGE)
 try:
  with pytest.raises(PackageError,match='input_contract'):executor.predict(image)
 finally:executor.close()

def test_duplicate_manifest_keys_rejected(variant):
 path=variant/'model-package.json';raw=path.read_text();path.write_text(raw.replace('"format":','"format":"vl.model-package","format":',1))
 with pytest.raises(PackageError,match='duplicate_json_key'):validate(variant)

def test_zip_build_is_reproducible():
 archive=ROOT/'model/reports/vlmp-public-channel-probe.zip';before=hashlib.sha256(archive.read_bytes()).hexdigest()
 subprocess.run([sys.executable,str(ROOT/'model/tools/build_demo.py')],check=True,capture_output=True)
 assert hashlib.sha256(archive.read_bytes()).hexdigest()==before


def test_golden_distinctness_uses_array_content_not_names_or_file_hashes(variant):
    manifest=json.loads((variant/"model-package.json").read_text())
    # Keep five different fixture IDs/paths but only two distinct images. Update
    # every changed file digest so content-distinctness is the intended failure.
    images=[np.load(PACKAGE/"golden/black.npy",allow_pickle=False),
            np.load(PACKAGE/"golden/red.npy",allow_pickle=False)]
    for index,fixture in enumerate(manifest["golden"]["fixtures"]):
        path=variant/fixture["input"]
        np.save(path,images[index%2].copy(),allow_pickle=False)
        manifest["files"][fixture["input"]]=hashlib.sha256(path.read_bytes()).hexdigest()
    (variant/"model-package.json").write_text(json.dumps(manifest))
    assert len({f["id"] for f in manifest["golden"]["fixtures"]})==5
    assert len({f["input"] for f in manifest["golden"]["fixtures"]})==5
    with pytest.raises(PackageError,match="golden_inputs_not_distinct"):
        validate(variant)
