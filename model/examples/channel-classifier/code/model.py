"""Synthetic interoperability probe; no training or private SDK."""
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
