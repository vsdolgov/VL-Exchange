"""Reproducible synthetic public proposal: no trained weights or external data."""
from pathlib import Path
import hashlib,json,zipfile
import numpy as np
import onnx
from onnx import helper,TensorProto
ROOT=Path(__file__).resolve().parents[1]
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
def build():
 p=ROOT/'examples/channel-classifier';(p/'code').mkdir(parents=True,exist_ok=True);(p/'golden').mkdir(exist_ok=True)
 (p/'code/__init__.py').write_text('')
 (p/'code/model.py').write_text('''"""Synthetic interoperability probe; no training or private SDK."""
import numpy as np
class Model:
    def preprocess(self, image):
        rows=np.linspace(0,image.shape[0]-1,2).astype(np.int64)
        cols=np.linspace(0,image.shape[1]-1,2).astype(np.int64)
        rgb=image[rows[:,None],cols[None,:],::-1]
        return {"images":np.ascontiguousarray(rgb.transpose(2,0,1)[None],dtype=np.float32)/np.float32(255)}
    def postprocess(self, outputs, categories):
        probabilities=outputs["probabilities"][0]
        index=int(np.argmax(probabilities))
        return {"label":categories[index],"score":float(probabilities[index])}
''')
 graph=helper.make_graph([helper.make_node('ReduceMean',['images'],['means'],axes=[2,3],keepdims=0),helper.make_node('Softmax',['means'],['probabilities'],axis=1)],'synthetic_channel_probe',[helper.make_tensor_value_info('images',TensorProto.FLOAT,[1,3,2,2])],[helper.make_tensor_value_info('probabilities',TensorProto.FLOAT,[1,3])])
 model=helper.make_model(graph,producer_name='VL Exchange synthetic public proposal',opset_imports=[helper.make_opsetid('',13)]);model.ir_version=8;onnx.checker.check_model(model);onnx.save(model,p/'weights.onnx')
 images={'black':np.zeros((2,2,3),dtype=np.uint8),'red':np.full((2,2,3),[0,0,255],dtype=np.uint8),'green':np.full((3,5,3),[0,255,0],dtype=np.uint8),'blue':np.full((5,3,3),[255,0,0],dtype=np.uint8),'mixed':np.array([[[0,0,255],[0,255,0]],[[255,0,0],[255,0,0]]],dtype=np.uint8)}
 labels=['red','green','blue']; fixtures=[]
 for name,image in images.items():
  np.save(p/f'golden/{name}.npy',image,allow_pickle=False)
  rows=np.linspace(0,image.shape[0]-1,2).astype(np.int64);cols=np.linspace(0,image.shape[1]-1,2).astype(np.int64)
  logits=image[rows[:,None],cols[None,:],::-1].astype(np.float64).mean(axis=(0,1))/255
  probs=np.exp(logits-logits.max());probs/=probs.sum();i=int(np.argmax(probs))
  fixtures.append({'id':name,'input':f'golden/{name}.npy','expected':{'label':labels[i],'score':float(probs[i]),'category_id':i+1,'output_index':i}})
 manifest={'format':'vl.model-package','spec_version':'0.2.0-draft.1','model_id':'org.vlexchange.examples.channel-probe','version':'1.0.0','profile':'python-numpy-classification-v1','entrypoint':{'root':'code','module':'model.py','class':'Model'},'runtime':{'python':'>=3.12,<4'},'backend':{'engine':'onnxruntime','device':'cpu','precision':'fp32','weights':'weights.onnx','opset':13,'input_name':'images','input_shape':[1,3,2,2],'output_name':'probabilities','output_shape':[1,3]},'source_input':{'color_space':'BGR','dtype':'uint8','layout':'HWC','channels':3},'categories':[{'output_index':i,'category_id':i+1,'label':label} for i,label in enumerate(labels)],'dependencies':{'numpy':'>=1.26,<3','onnxruntime':'>=1.20,<2'},'required_capabilities':['python-numpy-classification-v1','onnx-cpu-fp32'],'extensions':{},'golden':{'atol':1e-6,'rtol':1e-5,'fixtures':fixtures},'files':{f.relative_to(p).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(p.rglob('*')) if f.is_file() and f.name!='model-package.json'}}
 write(p/'model-package.json',manifest)
 report=ROOT/'reports';report.mkdir(exist_ok=True)
 archive=report/'vlmp-public-channel-probe.zip'
 with zipfile.ZipFile(archive,'w') as z:
  for f in sorted(p.rglob('*')):
   if f.is_file():
    info=zipfile.ZipInfo(f.relative_to(p).as_posix(),date_time=(2026,11,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16;z.writestr(info,f.read_bytes())
 print(json.dumps({'status':'pass','archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'manifest_sha256':hashlib.sha256((p/'model-package.json').read_bytes()).hexdigest(),'fixtures':len(fixtures)}))
if __name__=='__main__':build()
