# VLSP with protected VLMP packages

English | [Русский](README.ru.md)

This check extends the [previous experiment](../README.md): the scenario and each of the three unchanged VLMP 0.2.0 model ZIPs are delivered in protected form. The detector and two different classifiers have separate encrypted envelopes and signed licenses. Each host calls its own `load_protected` implementation; online mode uses an ephemeral local issuer. The scenario and models have separately verified execution rights.

The [report](reports/comparison.json) records 18 commands: each mode was checked on the reference host and through the native VideoPipe graph with the independent host. In each mode two golden frames produced the expected event; after reopening, expiry of model rights after the last classifier call but before scenario commit caused `authorization_failed`, without changing state or published events. The scenario license's clock remained valid. ONNX inputs and outputs, tracker traces, state, events and error fields are compared. Source files, scenario and model ZIPs and the native binary are fingerprinted before and after the run.

The integration adapter retains the original model-call leases and checks them together with the scenario lease before commit. This is a demonstration adapter for sequential synchronous processing: concurrent `process` calls are neither supported nor tested. It is not a new standard VLSP API. The local online issuer and keys are temporary; production deployment, network resilience, GPU, RTSP and synthetic model quality are outside this run.

## Reproduce

From the repository root, build the native adapter and Python environment using the [VideoPipe instructions](../../videopipe-pilot/README.md#build-on-macos-arm64), then run:

```sh
pilot=drafts/v0.2/videopipe-pilot
experiment=drafts/v0.2/vlsp-unified-integration/model-protected
PYTHONDONTWRITEBYTECODE=1 "$pilot/.venv/bin/python" "$experiment/compare.py"
```

Binding a local `127.0.0.1` port must be permitted. `--binary` chooses an existing native executable and `--output` chooses the report path. A failed run deletes a stale PASS report. Separate lifecycle and original-lease regression checks do not run VideoPipe:

```sh
python -m pytest -q -p no:cacheprovider --import-mode=importlib \
  drafts/v0.2/vlsp-unified-integration/model-protected/tests
```
