"""Independent public VLMP integration; never imports the reference executor.

Shares only the declared Python package ABI, schema and ONNX Runtime backend.
"""
from pathlib import Path
import hashlib,importlib.util,json,platform,sys,uuid
import numpy as np
import onnxruntime as ort
from jsonschema import Draft202012Validator
ROOT=Path(__file__).resolve().parents[1]
def sha(path):
 digest=hashlib.sha256()
 with Path(path).open('rb') as stream:
  for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
 return digest.hexdigest()
def run(root):
 root=Path(root).resolve();sys.dont_write_bytecode=True
 snapshot=lambda:{p.relative_to(root).as_posix():sha(p) for p in root.rglob('*') if p.is_file()}
 before=snapshot(); m=json.loads((root/'model-package.json').read_text())
 Draft202012Validator(json.loads((ROOT/'spec/model-package.schema.json').read_text())).validate(m)
 if m['profile']!='python-numpy-classification-v1' or set(m['required_capabilities'])!={'python-numpy-classification-v1','onnx-cpu-fp32'} or any(v['required'] for v in m['extensions'].values()):raise ValueError('Unsupported contract')
 if set(before)-{'model-package.json'}!=set(m['files']):raise ValueError('Inventory differs')
 for name,digest in m['files'].items():
  if '\\' in name or any(x in ('','.','..') for x in name.split('/')) or Path(name).is_absolute():raise ValueError('Invalid path')
  current=root
  for component in name.split('/'):
   current/=component
   if current.is_symlink():raise ValueError('Symlink')
  if not current.resolve().is_relative_to(root) or before[name]!=digest:raise ValueError('Digest/path mismatch')
 metadata=None
 if 'org.vl-exchange.attribution' in m['extensions']:
  spec=importlib.util.spec_from_file_location('vl_attribution_validator', ROOT.parent/'attribution/validator.py')
  validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)
  metadata=validator.validate_attribution(root,m)
 fixture_results=[];b=m['backend']
 for fixtures in (m['golden']['fixtures'],list(reversed(m['golden']['fixtures'])),[m['golden']['fixtures'][0],m['golden']['fixtures'][1],m['golden']['fixtures'][0]]):
  namespace='_independent_'+uuid.uuid4().hex;directory=root/m['entrypoint']['root']
  spec=importlib.util.spec_from_file_location(namespace,directory/'__init__.py',submodule_search_locations=[str(directory)])
  package=importlib.util.module_from_spec(spec);sys.modules[namespace]=package;spec.loader.exec_module(package)
  spec=importlib.util.spec_from_file_location(namespace+'.model',directory/m['entrypoint']['module']);module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
  implementation=getattr(module,m['entrypoint']['class'])();engine=ort.InferenceSession(str(root/b['weights']),providers=['CPUExecutionProvider'])
  try:
   for fixture in fixtures:
    image=np.load(root/fixture['input'],allow_pickle=False)
    if image.dtype!=np.uint8 or image.ndim!=3 or image.shape[2]!=3 or not image.flags.c_contiguous:raise ValueError('Image contract')
    prepared=implementation.preprocess(image)
    if set(prepared)!={b['input_name']}:raise ValueError('Input names')
    tensor=prepared[b['input_name']]
    if tensor.dtype!=np.float32 or list(tensor.shape)!=b['input_shape'] or not np.isfinite(tensor).all():raise ValueError('Input tensor contract')
    raw=engine.run([b['output_name']],{b['input_name']:tensor})[0]
    if raw.dtype!=np.float32 or list(raw.shape)!=b['output_shape'] or not np.isfinite(raw).all():raise ValueError('Output tensor contract')
    value=implementation.postprocess({b['output_name']:raw},tuple(x['label'] for x in m['categories']))
    if type(value)!=dict or set(value)!={'label','score'} or type(value['score']) not in (int,float) or not np.isfinite(value['score']) or not 0<=value['score']<=1:raise ValueError('Result contract')
    category=[x for x in m['categories'] if x['label']==value['label']]
    if len(category)!=1:raise ValueError('Category contract')
    output={**category[0],'score':float(value['score'])};expected=fixture['expected']
    passed=all(output[k]==expected[k] for k in ('label','category_id','output_index')) and abs(output['score']-expected['score'])<=m['golden']['atol']+m['golden']['rtol']*abs(expected['score'])
    fixture_results.append({'fixture':fixture['id'],'pass':passed,'result':output})
  finally:
   engine=None
   for key in list(sys.modules):
    if key==namespace or key.startswith(namespace+'.'):sys.modules.pop(key,None)
 unchanged=before==snapshot()
 report={'status':'pass' if unchanged and all(x['pass'] for x in fixture_results) else 'fail','host':'independent-public-vlmp-demonstration','manifest_sha256':before['model-package.json'],'unchanged':unchanged,'calls':fixture_results,'runtime':{'python':platform.python_version(),'numpy':np.__version__,'onnxruntime':ort.__version__},'reference_executor_imported':'public_model' in sys.modules,'not_a_competitor_platform':True,'box5_compatibility_verified':False}
 if metadata is not None: report['attribution']=metadata
 return report
if __name__=='__main__':
 report=run(Path(sys.argv[1]));print(json.dumps(report,indent=2));raise SystemExit(0 if report['status']=='pass' else 1)
