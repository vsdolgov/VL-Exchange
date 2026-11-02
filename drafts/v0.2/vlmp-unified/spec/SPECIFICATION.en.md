# VL Model Package 0.2.0 — classification and detection

**Status: final VL Exchange project specification edition 0.2.0.** This edition defines a common package envelope and two explicitly negotiated profiles. Publication does not establish approval by an industry body, compatibility of any particular platform or production readiness. MUST, MUST NOT and MAY indicate requirements, prohibitions and options. Results of particular tests are reported separately.

The profiles are `classification-onnx-cpu-v1` and `detection-onnx-cpu-v1`. Both are limited to ONNX Runtime, CPU, FP32, one static input and batch=1. These limitations describe the specified profiles; GPU, dynamic dimensions, other backends, segmentation, MLLMs and arbitrary neural networks are not claimed by this edition. Future profiles need their own published contracts and host capability checks. Classification and detection have different results: a common envelope does not make them interchangeable.

## 1. Package and manifest

A package is a ZIP or a verified immutable directory containing `model-package.json` at the root and exactly the files in `files`. A conforming host MUST accept ZIPs; directory support is optional and declared separately. The manifest is strict UTF-8 JSON without duplicate keys, comments, NaN or Infinity, conforms to [the schema](model-package.schema.json), and contains `format="vl.model-package"` and exact `spec_version="0.2.0"`. Unknown core fields and unsupported profiles are rejected. `model_id` and `version` identify a release; the same pair must not be reused with different bytes. SHA-256 of exact manifest bytes and SHA-256 of the entire ZIP are recorded separately.

All integer fields, including dimensions, opset, indices and IDs, require JSON integer representations without fractional or exponent notation; `1.0`, `1e0` and booleans are rejected. JSON Schema validates mathematical types and cannot enforce this rule on its own: hosts must also check the original representation during parsing.

| Field | Meaning |
|---|---|
| `profile` | Exact `classification-onnx-cpu-v1` or `detection-onnx-cpu-v1` |
| `backend` | Engine, device, precision, ONNX path, opset, input name/shape and output names |
| `source_input` | Source image format and exact `[H,W,3]` shape |
| `categories` | Array of `{output_index,category_id,label}` |
| `result` | Result type and, for detection, filtering and coordinate rules |
| `golden` | Numeric tolerances and at least three expected outputs |
| `files` | Relative path → SHA-256 of exact file bytes, excluding the manifest |
| `required_capabilities`, `extensions` | Required capabilities and extensions with `required` and `data` |

Categories have unique `category_id` and `label`; `output_index` equals the position in the array, starting at zero. `source_input.shape` MUST equal `[backend.input_shape[2], backend.input_shape[3], 3]`. Input/output names are nonempty; detection output names are pairwise distinct. Weights and every golden input MUST appear in `files`. Golden IDs and paths are unique; at least three inputs differ in array contents considering dtype, shape and values. The publisher fixes `atol` and `rtol` before testing, each in `[0,0.001]`. Compare numbers using `abs(actual-expected) <= atol + rtol*abs(expected)`; strings, IDs, object count and order must match exactly.

Golden inputs use NPY versions 1.0, 2.0 or 3.0 and contain only a `uint8` array of the exact declared `source_input.shape`. Before allocating memory, hosts check a header of at most 10,000 bytes, dtype and bounded dimensions; payload length must equal `H×W×3` bytes. Object dtype, pickle, truncated files and extra bytes are prohibited; NumPy loads use `allow_pickle=False`. C and Fortran array orders are permitted.

Every golden expected result MUST be consistent with the declared categories and selected profile. For classification, `category_id`, `label` and `output_index` must identify the same `categories` entry. For detection, each expected `category_id` and `label` must match a category, `score` must meet the threshold, `bbox` must have valid bounds, and `object_id` must be a canonical nonnegative decimal integer using ASCII digits without leading zeros, less than `max_detections`. Expected detection indices must strictly increase; IDs cannot repeat. Hosts MUST reject inconsistent golden data before inference; whether the expected values match the graph is checked separately by running the golden tests.

`required_capabilities` includes `onnx-cpu-fp32` and the exact profile name. A host MUST NOT execute a package when a required capability or extension (`required=true`) is unsupported. An unknown optional extension MAY be ignored without changing required behavior. Manifest validation alone does not establish inference.

## 2. ZIP, inventory and graph validation

File paths are relative canonical POSIX paths: for cross-platform portability, colons in any component, absolute paths, drive-qualified paths (`C:/...`, `C:relative`), backslashes, empty components, `.`/`..`, NUL, symlinks, special files, duplicate ZIP names and files outside `files` are forbidden. Extraction must stay within its isolated directory. Hosts reject archives over 128 MiB, more than 1024 entries, over 512 MiB expanded data, or an individual file over 128 MiB. Additional host limits must result in explicit refusal. Before opening the graph, validate every path, the complete inventory, sizes and SHA-256. The manifest, files and graph must not change between validation and use. The same inventory, symlink and size rules apply to directories.

Hosts reject external ONNX weight data and local ONNX functions, and validate the graph, declared opset and exact ONNX input/output names, types and shapes before execution. For these two profiles, `opset_import` contains exactly one standard-domain `""` entry at version 13; a node domain, including in nested graphs, is `""` or `"ai.onnx"`; custom domains/operators are unsupported. The profiles support only one `float32 [1,3,H,W]` input, `1 ≤ H,W ≤ 4096` and the CPU execution provider. `H` and `W` come from the package, not the demo dimensions. The profiles do not require execution of undeclared package Python code. Integrity checks do not replace signatures, rights or isolation; protected delivery uses the separate [security profile](../../../../docs/security/SPECIFICATION.en.md).

## 3. Common input and preprocessing

`infer` accepts a BGR `numpy.ndarray` image of dtype `uint8 [H,W,3]` whose dimensions exactly match `source_input.shape`; implicit conversion of other input types to arrays is prohibited. The input MAY be non-contiguous; the prepared tensor is C-contiguous. The host MUST NOT silently change size, dtype or color semantics or accept a graph inconsistent with its manifest. The caller performs any resize/crop and retains the coordinate transform separately. The host converts BGR→RGB, transposes HWC→NCHW, adds the batch dimension and converts to float32 divided by 255. Normalized detection coordinates refer to the image passed to `infer`, not automatically to the original camera frame.

## 4. Classification

`backend.outputs` contains only `scores`, the name of a single ONNX `float32 [1,C]` output where `C` equals the number of categories. Values must be finite and in `[0,1]`, and their sum must be within `0.0001` of 1. These are probabilities, not arbitrary logits. Wrong shape, dtype or values produce an error. `vl.classification.v1` has exactly `{label,score,category_id,output_index}`. `output_index` is the maximum probability's index; ties select the first index. Label and category ID come from that category.

## 5. Detection

`backend.outputs` contains `boxes`, `scores`, `classes`, naming three ONNX outputs `float32 [1,N,4]`, `float32 [1,N]` and `int64 [1,N]`; `N=result.max_detections`, `1 ≤ N ≤ 1000`. `result` contains `type="vl.detections.v1"`, `threshold` between 0 and 1, and `bbox_space="normalized-xyxy-input-frame"`. Every score is finite and in `[0,1]`. The host retains rows whose score is greater than or equal to threshold, in original order. `object_id` is the decimal ONNX row index before filtering and is unique only within that one result. Each retained category ID must be declared; its finite `bbox=[x_min,y_min,x_max,y_max]` is within `[0,1]`, with `x_min < x_max` and `y_min < y_max`. Label comes from the category. An example result is `{"detections":[{"object_id":"0","label":"person","category_id":1,"score":0.9,"bbox":[0,0,1,1]}]}`; no objects means an empty array. NMS and output packing are part of the graph; raw outputs of other detector families are outside this profile.

## 6. Compatibility and evidence

`0.2.0` is a new manifest and digest; it is not automatically compatible with the previous `0.2.0-draft.1` or `0.2.0-detection-draft.1`. Existing ZIPs, schemas and loaders remain separate. A host claiming an old version validates it under its corresponding old contract; an unknown version is rejected before execution. Migrating to the new manifest creates a new ZIP and digests. Protected delivery requires new signatures and rights; a scenario pinned to the old model manifest digest needs an explicitly updated binding.

To substantiate portability of each profile, the same unchanged ZIP is executed on two independently implemented hosts. Hosts check golden inputs/results within recorded tolerances, errors and legacy compatibility separately. Reports disclose shared libraries, model identity, limitations and environment. Test models are synthetic and untrained: results are not evidence of industrial analytics quality or support by a third-party platform.
