# Independent VLMP 0.2.0 host

This separately implemented host loads the new CPU ONNX classification and detection profiles from ZIP. It also recognizes the published `0.2.0-draft.1` classification and `0.2.0-detection-draft.1` detection packages; those formats are dispatched by their original version and retain their own contracts. The historical VLMP 0.1 SDK interface is not implemented here.

```python
from independent.host import load_package

model = load_package("model.zip")
try:
    result = model.predict(frame_bgr_uint8)
finally:
    model.close()
```

`model` exposes `manifest`, `profile`, `zip_sha256`, `manifest_sha256` and `inference_calls`. The manifest property returns a deep copy and metadata properties are read-only, so changes through this public interface cannot alter validated execution settings. The ZIP is checked before and after each prediction and on close. A changed or missing archive fails explicitly; even after such a failure `close()` releases its private temporary directory and reports the mismatch. All new-profile inputs have the exact `[H, W, 3]` size declared by the package; non-contiguous arrays are accepted. The host validates golden NPY headers and exact payload lengths before creating arrays, and loads ONNX weights from validated ZIP bytes with CPU ONNX Runtime.

The new profiles do not execute publisher Python. A legacy classification package does execute its declared Python `Model` for preprocess/postprocess, as required by its historical profile; the publisher and process must be trusted. ZIP integrity checks and an isolated import namespace are not a sandbox. This implementation shares the formal JSON Schema, NumPy, ONNX and ONNX Runtime with the reference implementation; it does not import or wrap the unified reference host. Detection and classification models used by the pilot are synthetic and untrained, and the test does not establish support by a third-party platform or industrial analytics quality.

This host applies an additional 1 MiB limit to the manifest, accepts ZIP input only, rejects `:` in any archive path segment for all supported package versions, and rejects ONNX external tensor data at any nesting depth. For legacy classification, each golden NPY file is bounded to 128 MiB, its header to 10,000 bytes, exact payload length is required, and its expected category/label/index must be consistent before loading the model. Input H/W need not equal the fixed ONNX tensor dimensions: the legacy model's own preprocessing handles resizing. The legacy classification branch requires the single default ONNX opset 13 and rejects local functions and custom operator domains. The legacy detection branch does not impose that new-profile opset or domain restriction; ONNX validation and the capabilities of the installed CPU ONNX Runtime still apply. Optional extensions, including attribution, are ignored and are neither validated nor exported by this host. Compatibility has been checked with the two published legacy ZIPs; these limits mean the result does not establish support for every package accepted by a historical host.
