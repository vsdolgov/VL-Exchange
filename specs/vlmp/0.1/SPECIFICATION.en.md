# VL Exchange — VL Model Package (VLMP) v0.1

> Historical interface0.1.0 preserved for implementers. [Русский](SPECIFICATION.ru.md). This profile requires external `vlmodels-types1.8.1` and Torch; neither the SDK nor private code is redistributed here. Public executable profile0.2.0-draft.1 is a separate [proposal](../../../model/spec/SPECIFICATION.en.md), not a replacement for0.1.0. The requirements below describe the original pilot; this document does not establish redistribution rights for external dependencies.

**Contract version0.1.0. Status: draft normative specification for a verifiable pilot.** MUST, MUST NOT and MAY state requirements, prohibitions and options. A requirement alone does not establish implementation. Actual runs, versions and limitations belong in separate reports.

## 1. Scope

**VL Model Package (VLMP)** is part of **VL Exchange**: a portable model node containing weights, Python input/output processing, metadata, exact class mapping and verification fixtures. The original document considered a model → block → scenario chain. This is historical, not the current requirement: inter-vendor exchange uses VLMP/VLSP, while VLBP remains an internal VL format. Block/scenario specifications, graphs, rules, scenario state, tracking and camera operations are outside VLMPv0.1.

The mandatory profile comprises `python-custom-v1`, execution profile `inline_cpu_onnx_fp32`, ONNX Runtime with `CPUExecutionProvider`, single-image classification, one input/output, static tensors and batch1. Model input is float32 NCHW; inference output is float32 NC; the final result is `Classification`.

TensorRT, CUDA inference, Torch inference, TensorFlow, dynamic tensors, detections, segmentation and image sequences are not mandatoryv0.1 capabilities. Their existing metadata is preserved but does not imply verification. Support is claimed for an exact package/profile/environment. An opset range does not prove every operator in that range works.

## 2. Preserving existing model packages

Existing `__init__.py`, `model.py`, `model_info.json`, `coco_categories.json`, helpers and weights MUST retain their original bytes and entrypoints. New fields MUST NOT be added to legacy JSON solely for this specification; new requirements live in separate `model-package.json`.

```text
package-root/
├── model-package.json
├── artifacts/                 # unchanged previous model directory
│   ├── __init__.py
│   ├── model.py
│   ├── model_info.json
│   ├── coco_categories.json
│   ├── custom_modules/        # when required by the existing model
│   └── weights*.onnx
└── golden/                    # outside legacy artifacts
    ├── black.npy
    ├── red.npy
    └── blue.npy
```

`artifacts` is not a mandatory directory name; `entrypoint.root` declares its location. A sidecar inside a previous directory MAY be used only after separately verifying that the legacy loader accepts extra files; this cannot be assumed.

Box5 receives its previous model directory and need not parse the wrapper/new manifest. A Box5 adapter MAY validate the manifest then hand the original directory to its existing loader. Compatibility testing must not replace that loader's inference backend with another implementation.

## 3. Manifest and identity

The manifest is strict UTF-8 JSON following [model-package.schema.json](model-package.schema.json). Comments, duplicate JSON keys, NaN and Infinity are forbidden. Unknown fields outside `extensions` are rejected.

| Field | Normative meaning |
|---|---|
| `format` | `vl.model-package` |
| `spec_version` | `0.1.0`, format version, not model or SDK version |
| `name`, `version` | Delivery name/version; package version may differ from legacy model_version |
| `entrypoint` | Legacy root, model.py, Model class, protocol and exact SDK version |
| `legacy` | Exact metadata/categories JSON paths |
| `files` | Complete relative path → file-byte SHA-256 inventory |
| `profiles` | Exact weights, backend, tensor names/types/shapes |
| `dependencies` | Explicit model distributions and version constraints |
| `source_input` | Original-image semantics before preprocess |
| `result` | Result type and output-index/category-ID/label mapping |
| `required_capabilities` | Capabilities required to execute the package |
| `golden` | Verification inputs, expected results and tolerances |
| `extensions` | Separately namespaced extensions |

All paths MUST be relative POSIX without `..`, `.`, absolute prefixes, backslashes, empty segments or globs. Links and paths escaping the root are forbidden. Schema regex validation alone is insufficient: actual resolution must be checked.

`files` MUST inventory every regular delivery file except `model-package.json`, including all Python modules, extra weights and golden inputs. Unlisted/missing/changed files and symlinks are rejected. `entrypoint.root/__init__.py`, `entrypoint.root/model.py`, both legacy JSON files and selected weights are mandatory. Reports/interpreter caches stay outside the package.

Identity uses original-manifest SHA-256 and matching file digests. Hosts MUST verify digests before Python import, preserve bytes and avoid writing package `__pycache__`. Hashes establish integrity/equal bytes, not publisher authenticity.

## 4. Legacy metadata and weight selection

`legacy.model_info` and `legacy.categories` MUST reference `model_info.json` and `coco_categories.json` inside entrypoint.root. `model_type` MUST equal `custom_model`, `task_type` must be `image_classification`; selected `inline_cpu_onnx_fp32` must appear in legacy deploy_profile.profiles, and legacy default_profile must belong to that list.

The new manifest does not change the legacy default profile: the pilot selects CPU explicitly. Silent fallback to GPU, another backend, weights or precision is forbidden.

`profiles[0].weights` identifies **one exact inventoried file**. Legacy weights_path can retain its previous glob; the manifest unambiguously selects a matching file. If the legacy loader resolves weights itself, the adapter MUST verify it selects that exact file. Ambiguity fails before inference.

model_info.img_size height/width MUST equal manifest/graph H/W. input_names match preprocess result keys and graph inputs; output_names match graph outputs and postprocess argument keys. Named-output order must not depend on unspecified container iteration.

## 5. SDK, dependencies and backend

`python-custom-v1` fixes these methods/attributes:

```python
class Model:
    input_names: Sequence[str]
    output_names: Sequence[str]
    def __init__(self, **model_attributes): ...
    def preprocess(self, data: NDArray) -> Mapping[str, NDArray | torch.Tensor]: ...
    def postprocess(self, outputs: Mapping[str, torch.Tensor],
                    categories: Sequence[str]) -> Classification: ...
```

`Classification` is the existing `vlmodels_types.Classification` with label:str and score:float. Its fields/model calls are not extended with neutral category_id/output_index; the external serializer adds them.

The manifest MUST declare exact entrypoint.sdk.version. Changing the installed SDK is not automatically compatible; another SDK requires a separate compatibility entry and repeat test. The historical pilot interface was vlmodels-types1.8.1; an older1.5.0 documentation example does not describe the verified deployment.

`dependencies` covers model.py/helpers. version_specifier is a nonempty PEP440 constraint checked against actual distributions; names are normalized per Python packaging. SDK and backend dependencies are checked separately. `onnxruntime` or `onnxruntime-gpu` MAY supply the backend when CPUExecutionProvider is actually used. Reports record distributions, versions, Python and providers. Broad declarations do not prove every allowed version was tested.

Packages do not install dependencies during import/inference or modify sys.path. Internal imports are relative, e.g. `from .custom_modules import utils`; declared external imports are ordinary, e.g. `import numpy`.

ONNX has one float32 input `[1,3,H,W]` and one float32 output `[1,C]`, with all dimensions positive fixed integers. External ONNX data and custom operator domains are outsidev0.1 and rejected. Standard `""` / `ai.onnx` domain is allowed. Manifest opset7–15 must match the graph standard-domain import. Hosts validate graph and actual backend loading. The smoke target is opset13/IR8, which does not verify all other opsets/operators.

## 6. Original images and inference tensors

preprocess receives one C-contiguous NumPy uint8 `[H_source,W_source,3]` array, H/W>0, **BGR**, integer values0–255. Origin is top-left; first axis rows, second columns. Alpha and implicit RGB conversion are forbidden. Original image size may differ from inference tensor size; preprocess performs the conversion.

Included immutable Python code defines BGR→RGB, normalization, resize/crop and other processing. Hosts MUST NOT add their own color conversion/normalization/resize. Golden tests MUST include distinguishable channel inputs to detect BGR/RGB errors.

preprocess returns exactly declared names, with CPU NumPy arrays or CPU torch tensors. After representation normalization each input has exact declared dtype/shape. Silent float64/float16→float32 or shape repair is forbidden; making memory contiguous without value changes is allowed. Numbers are finite.

The backend runs the chosen CPU graph. Outputs are name/dtype/shape checked, then passed to postprocess as CPU Mapping[str,torch.Tensor]. Creating a torch view/copy over NumPy without changing numbers is permitted. Model code interprets logits/probabilities.

## 7. Categories and final results

Output C equals legacy categories.categories count and result.category_map length. For every i=0…C−1, category_map[i].output_index==i, label==legacy category[i].name, category_id==legacy category[i].id.

Legacy arrays are **not sorted by ID**. IDs are metadata identifiers, may have gaps and are not intrinsically tensor indices. IDs/labels are unique. postprocess receives a tuple of category names in original order.

The mandatory classification profile does not hide the selected class after postprocess; every category's exclude MUST be false. Legacy thresholds/colors/localizations/is_violation remain metadata and cause no implicit host processing. Any threshold logic belongs to model code and golden outputs.

Results contain a declared label and finite score in[0,1]. The external serializer uniquely maps label and emits:

```json
{"results":[{"label":"red","score":0.5761168847658291,"category_id":1,"output_index":0}]}
```

The envelope follows [classification-result.schema.json](schemas/classification-result.schema.json). One image yields exactly one result. Unknown labels, missing scores, NaN/Infinity or out-of-range scores are errors, not empty results. Model code owns class selection, softmax and tie-breaking. Demo argmax picks the lowest output index on a tie; this is not imposed universally without a separate contract.

## 8. Construction, state and lifecycle

Conformance runs use **only defaults** from legacy model_info.model_attributes. Attributes have permitted scalar types and correctly typed defaults; pass all unchanged as Model keyword arguments. Missing/empty attributes mean no kwargs. Nondefault external overrides are outside this test; the legacy loader replaces the whole defaults dict rather than merging. Hosts must not silently change that behavior.

Lifecycle: structure/version/digest checks → dependencies → isolated-package import → Model construction → CPU backend → sequential predict → release backend/model. Legacy integration uses existing initialize/predict/release; no new mandatory close method is introduced.

Predict calls on one instance cannot overlap. Each completes preprocess→inference→postprocess before the next, preserving models that retain source size between stages. Instance state is not shared.

v0.1 classification must not depend on previous images. Golden fixtures run forward/reverse on new instances and repeat one input after another. An error ends the call; retry requires a new instance unless safe recovery is demonstrated. Resources are released on errors, with no stale successful output.

Import namespaces do not promise process/hardware isolation. Python execution executes package code; static validation does not import it. Operational isolation/policies are separately declared and are not assumed properties of the legacy loader.

## 9. Failures and extensions

Manifest/version/digest/path/dependency/required-capability errors are detected before import. Graph, I/O and runtime failures do not fall back to other models/precision/backends.

Machine errors MUST contain a stable code/class and message; a validation/loading/execution stage is recommended. Internal exception class names may differ. CLI exits nonzero; APIs raise or return a separate error type. Errors are not successful result envelopes.

required_capabilities includes python-custom-v1, onnx-cpu-fp32 and classification-v1. Unknown required capabilities are rejected. Extensions use reverse-DNS keys with `{required:boolean,data:...}`. Unknown required extensions fail before import; optional ones are preserved uninterpreted and do not alter mandatory behavior. Changing core semantics requires a new contract version or required capability.

## 10. Golden tests and conformance claims

At least three distinct golden inputs are required, each `.npy` loaded without pickle and containing valid BGR uint8 HWC images. IDs/paths are unique and at least three contents differ. Inputs are inventoried and canonical expected results live in the manifest. Demo inputs include black and distinct channels; nonuniform images/changed H/W are recommended when resizing.

Labels, category_id and output_index match **exactly**. Finite scores satisfy:

```text
abs(actual_score - expected_score) <= atol + rtol * abs(expected_score)
```

0≤atol,rtol≤0.001; demo recommendations are atol1e-6/rtol1e-5. Tolerances are selected before testing and bound by manifest digest; failed tests cannot be “fixed” by widening them without a new package version. Top1 alone is insufficient.

Tests include all fixtures, repeated calls, changed order and new instances. Negatives include digest changes, missing files, wrong shape/dtype, unknown required capability/extension, invalid category mapping and unsupported profiles.

“One package works in two independent executors” requires matching manifest/all-file identity, separate loading/inference orchestration, unchanged model.py/JSON/weights/profile, every golden result within tolerance, and reported Python/SDK/backend/distributions/providers, host versions and limitations.

A shared ONNX Runtime and type SDK are allowed: independence means different integrations/loaders, not different mathematical inference engines. Two CLIs wrapping one adapter do not demonstrate independence. Box5 compatibility requires a separate run of its actual loader; source reading or a neutral runner is insufficient. An isolated process/container with Box5 libraries is not production deployment.

This tests portability of the selected model node/inputs, not industrial detector accuracy, complete scenario portability, arbitrary Python or other backends.

## 11. Sources and independent implementation

The [unchanged0.1 JSON Schema](model-package.schema.json) and [result schema](schemas/classification-result.schema.json) define structure. This text defines ABI, semantics and conformance; obtain external SDK1.8.1 under lawful terms or create a compatible adapter against the published interface and test it separately. This repository includes no private SDK, deployed platform source or customer data. Public quickstart uses the proposed neutral0.2 profile; it is not a0.1/Box5 execution. Historical/current results are separated in the [matrix](../../../docs/en/COMPATIBILITY.md).
