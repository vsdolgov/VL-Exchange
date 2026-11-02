# VLMP 0.2.0: a unified package for classification and detection

**English | [Русский](README.ru.md)**

VL Model Package `0.2.0` combines a common package structure with two explicit model profiles, classification and detection. Their results remain different. The [specification](spec/SPECIFICATION.en.md) defines the contract; the [JSON Schema](spec/model-package.schema.json) defines the manifest shape.

These profiles cover static ONNX, CPU, FP32 and batch size 1. The package specifies image dimensions; the host converts BGR to RGB and prepares the tensor. Resize and crop happen before the call and are not implicit profile features. GPU, other engines, dynamic dimensions, segmentation and arbitrary ONNX models are outside the claimed scope.

## Two-host verification

The synthetic [classification.zip](examples/classification.zip) and [detection.zip](examples/detection.zip) are run on a reference host and a separately implemented independent host. Each host receives the same unchanged ZIP; real ONNX Runtime CPU calls are compared with golden outputs and each other. The comparison also repeats calls in reverse order and after other inputs, checks rejection paths and verifies ZIP identity. The hosts share JSON Schema, NumPy and ONNX Runtime. Both are local; neither a third-party platform nor Box 5 participated, and the untrained synthetic models do not measure industrial analytics quality.

The [verification report in Russian](reports/VERIFICATION.md) and [machine-readable results](reports/comparison.json) record ZIP, ONNX graph and source hashes, each output and negative checks. The separate [reference](reference/tests) and [independent](independent/tests) tests cover path safety, integrity, graph contracts and invalid inputs.

## Older packages

Both hosts retain separate paths for the published `0.2.0-draft.1` classification and `0.2.0-detection-draft.1` detection formats. Each host was tested with one previous ZIP of each format; complete compatibility with every valid legacy package is not claimed. The original ZIPs, schemas and loaders remain unchanged. The new hosts apply additional ZIP-path limits; the independent host also limits manifest size and rejects external ONNX tensor data. See [its documentation](independent/README.md). The legacy classification ABI executes publisher-supplied Python and requires a trusted environment; neither host is a sandbox. Historical VLMP 0.1 / CustomModelProtocol with a private SDK is outside this verification.

Migrating a model to the new manifest creates a new ZIP and new hashes. Protected delivery needs new signatures and execution rights. A VLSP scenario pinned to the old model-manifest digest needs an explicitly updated binding and a new scenario package: its existing ZIP does not change automatically. Running the new model package through existing VLSP hosts and protected delivery was not separately tested here.

## Reproduce the checks

From the repository root on macOS or Linux, using Python 3.12 and the root `requirements.txt` dependencies:

```sh
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="drafts/v0.2/vlmp-unified:model/src:scenario/src:security-runtime/src:drafts/v0.2/model/src"
python drafts/v0.2/vlmp-unified/tools/compare_hosts.py
python -m pytest -q --import-mode=importlib -p no:cacheprovider \
  drafts/v0.2/vlmp-unified/tests \
  drafts/v0.2/vlmp-unified/reference/tests \
  drafts/v0.2/vlmp-unified/independent/tests
```

`compare_hosts.py --output /tmp/vlmp-comparison.json` also writes JSON. Rebuild the demonstration ZIPs byte-for-byte with `python drafts/v0.2/vlmp-unified/tools/build_examples.py`; previously published packages are not changed. The tests use local synthetic data.
