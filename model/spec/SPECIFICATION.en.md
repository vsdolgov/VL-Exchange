# VLMP: public independent profile

**Proposal 0.2.0-draft.1 · Experimental · 2026-11-01 · [Русский](SPECIFICATION.ru.md)**

This is an additional public profile for discussion, not a replacement for the tested VLMP 0.1.0 / CustomModelProtocol. It removes the demonstration's private SDK dependency by returning an ordinary Python dict from postprocess. Box 5 compatibility has not been verified. The original 0.1 schema is preserved in the [historical section](../../specs/vlmp/0.1/model-package.schema.json). MUST and MUST NOT state requirements of this proposed profile.

## 1. Scope and package

Format `vl.model-package`, version `0.2.0-draft.1`, profile `python-numpy-classification-v1`. One CPU ONNX FP32 classifier with static batch 1 and input `[1,3,2,2]` is supported. The small size deliberately limits a public interoperability probe; it is not industrial model guidance. GPU, dynamic shapes, other result types, custom operators, external ONNX data and arbitrary Python portability are outside scope.

A package is an immutable directory or ZIP whose root contains `model-package.json`, `code/__init__.py`, `code/model.py`, an ONNX file and `golden/*.npy`. A receiving host extracts ZIP safely before execution; the demonstration model hosts consume directories. Structural requirements are defined by [JSON Schema](model-package.schema.json).

Every file except the root manifest is listed in `files: {relative_path: sha256}`. POSIX paths are canonical and relative; absolute paths, backslashes, empty segments, `.`, `..`, symlinks and globs are prohibited. Unlisted, missing or modified files are rejected before import. Extraction additionally needs file count/size limits and zip-slip prevention. Hosts do not write bytecode, logs or temporary files into packages. They prevent replacement after verification; a writable development directory does not protect against another process.

## 2. Manifest fields

| Field | Meaning |
|---|---|
| `format`, `spec_version`, `profile` | Exact format, version and ABI identifiers |
| `model_id`, `version` | Namespaced ID and semantic delivery version; the same pair is never reissued with different bytes |
| `entrypoint` | `root` is a Python package, `module="model.py"`, `class="Model"` |
| `runtime.python` | Nonempty PEP 440 Python constraint |
| `backend` | ONNX Runtime / CPU / FP32, exact weights file, opset 13, input/output names and shapes |
| `source_input` | BGR, uint8, HWC, three channels |
| `categories` | Ordered output index → category ID and label mapping |
| `dependencies` | Distribution name → PEP 440 constraint, with no automatic installation |
| `required_capabilities`, `extensions` | Explicit required capability negotiation |
| `golden` | Fixtures, expected results and frozen tolerances |
| `files` | Complete inventory and file SHA-256 hashes |

The manifest is strict UTF-8 JSON without duplicate keys, comments or NaN/Infinity. Unknown core fields are forbidden. Original manifest bytes are hashed separately; whitespace changes identity. A version number without a digest does not identify an exact delivery.

## 3. ABI

```python
class Model:
    def __init__(self): ...
    def preprocess(self, image): ...
    def postprocess(self, outputs, categories): ...
```

The constructor has no arguments. `preprocess` receives one C-contiguous NumPy uint8 `[H,W,3]` array, H/W>0, BGR, values 0…255. Coordinates start at the upper left. Alpha and implicit RGB conversion are forbidden. Included model code defines resizing, BGR→RGB conversion, normalization and other processing.

`preprocess` returns a dict containing exactly `backend.input_name`; its value is NumPy float32 `[1,3,2,2]` with finite numbers. Silent dtype or shape repairs are forbidden. Making memory contiguous without changing values is allowed.

The host invokes ONNX Runtime with `CPUExecutionProvider`, using exact input/output names. The graph has one float32 input of the declared shape and one float32 output `[1,C]`, where C is the category count. Opset is standard domain `""`, version13; the demo uses IR8. Hosts validate the graph, actual I/O, dtype/shape, finite values and backend loading. Other profiles are not selected automatically.

`postprocess` receives `{output_name: NumPy_float32_[1,C]}` and a tuple of labels in the original category order. It returns exactly `{"label": str, "score": number}`. Label must map uniquely to a category; score must be finite and in[0,1], with bool not treated as a number. Class selection, softmax, tie-breaking and thresholds belong to model code, not the host.

## 4. Categories and result

`output_index` is consecutive 0…C−1 in array order. `category_id` and label are unique; ID need not equal index and must not be used for sorting. Hosts complete the result as `{label, score, category_id, output_index}`. For VLSP, the object is placed under `classification`; the public host never supplies a private SDK object.

In the demo, ReduceMean averages normalized RGB channels and Softmax produces probabilities; NumPy argmax picks the first index on a tie. Inputs with distinct channels detect BGR/RGB mistakes. This classifies synthetic colors and is not an industrial detector.

## 5. Dependencies and import

Before import, verify manifest/schema, inventory/digests, capabilities/extensions, file references, ONNX graph and dependencies. Unknown required capabilities or extensions cause rejection; optional extensions are retained without changing core behavior. Required capabilities are `python-numpy-classification-v1` and `onnx-cpu-fp32`.

Hosts check actual Python/distribution versions against PEP440 constraints. A declared range is not evidence that every version was tested. Runtime never installs dependencies or downloads code/models. Helpers are inventoried and use relative imports. A separate import namespace prevents naming conflicts but is not a sandbox.

## 6. Lifecycle and failures

Order: validate → import → Model() → CPU backend → sequential preprocess/inference/postprocess → release model/backend/namespace. One instance does not receive concurrent calls. Results must not depend on previous images in this profile. Errors do not become empty or stale success results; CLI returns a nonzero status with type/message. Create a new instance before retrying a runtime failure.

Hosts reject invalid inputs, tensor/result contracts, labels/scores, digests, paths, dependencies or required features. Reports distinguish static validation from execution: schema validation does not demonstrate inference. The minimal independent host demonstrates a separate load/inference path for this verified demo; it is not a complete production validator for untrusted models.

## 7. Golden tests and independence

At least three different `.npy` uint8 BGR inputs are required, loaded with `allow_pickle=False`. Each is inventoried. Fixture IDs and paths are unique. At least three arrays differ in their combined dtype, shape and values; different filenames, IDs or `.npy` headers alone do not make images distinct. Labels, category IDs and indices match exactly. Scores satisfy `abs(actual-expected) <= atol + rtol*abs(expected)`, with 0≤atol,rtol≤0.001; tolerance is frozen in the manifest before testing. The demo contains five images, forward/reverse orders on new instances, and repetition of the first image after another — 13 calls per host.

A claim of two independent systems requires equal manifest/file/ZIP digests, no modifications, separate load/orchestration implementations, all fixtures within tolerance, recorded OS/Python/backend versions and limitations. Two CLIs wrapping one executor are not two implementations. Shared ONNX Runtime is allowed, but independent inference engines are not claimed. Both current hosts are local; neither a third-party platform nor Box5 has been tested for this profile.

## 8. Protection and licensing

SHA-256 against a trusted reference verifies integrity, not authorship. Publisher signatures, execution rights, weight confidentiality and administrator resistance are distinct properties. The common [security envelope](../../docs/security/SPECIFICATION.en.md) can protect an exact ZIP and reference `model-package.json`; a protected model runner for this new profile has not been integrated or verified.

The open demo executes trusted development code. Commercial authors need not publish weights/code, while receiving platforms need the open contracts. The end-system administrator is trusted; memory/plaintext access can expose weights. A separate license is not automatically transferable: an installation move requires contractual permission and authorized reissue. The [matrix](../../docs/en/COMPATIBILITY.md) distinguishes this planned integration from verified protected VLSP execution.
