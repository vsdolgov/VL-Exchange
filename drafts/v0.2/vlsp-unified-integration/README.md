# VLSP scenario with unified VLMP 0.2.0 models

**English | [Русский](README.ru.md)**

This experiment connects a synthetic VLSP 0.2 scenario to three unified VLMP 0.2.0 model packages: one detector and two different classifiers. A deterministic IoU tracker is supplied by each host. The same scenario ZIP and model ZIPs are used by the reference executor and an independently implemented executor reached through native C++ VideoPipe transport. VideoPipe is an example used to demonstrate portability to another execution environment; it is not required by VLSP.

The [fixtures](fixtures/) include a new scenario package and its model bindings. The new scenario is version `1.0.1` because its model-manifest pins changed; its Python scenario code is byte-for-byte identical to the earlier pilot. The earlier scenario ZIP remains unchanged. The detector and first classifier ZIPs are copies of the published unified VLMP examples. The second classifier is a separate unified package built from the previous pilot's different ONNX graph, with its own golden fixtures. [pins.json](fixtures/pins.json) identifies all ZIPs, manifests and graphs; [build.py](fixtures/build.py) reproduces the new fixtures from the repository sources.

The comparison records events, state, model calls and errors for protected offline and online execution, including negative checks. The reports describe the checks actually performed; they do not imply production certification. The model hosts share ONNX Runtime, NumPy and other dependencies. The scenario is synthetic, uses small static CPU inputs and is not a measure of detection quality or production throughput.


[A subsequent experiment](model-protected/README.md) runs the same scenario with all three model ZIPs separately protected.
## Comparison results

The [summary](reports/comparison.json) and detailed [reference](reports/reference.json) and [VideoPipe](reports/videopipe.json) traces record the macOS arm64 run: 27 suites, 200 commands, 114 frame operations and 53 expected rejections. Both policies matched events, state, ordering, error fields (`stage`, `code`, `fatal`, `retryable`), computed ONNX and tracker traces, lifecycle flags and committed-call counts. Tests cover a wrong model binding, an expired license, online revocation and expiry during processing without a commit. In the latter test both the license and online grant expire; expiry of the grant alone is not isolated.

**Difference:** `process_calls`, `reset_calls` and `close_calls` differ in 62 responses: the independent host counts some attempts before validation whereas the reference host counts them after validation. These metrics are not equivalent and the reports retain both. CI exercises the Python workers directly and does not repeat the native VideoPipe graph.

## Scope of protection

The experiment seals and authorizes the **scenario ZIP**. Model ZIPs are loaded separately and checked against their manifest/file digests and scenario bindings; this experiment does not encrypt or license the model ZIPs. Offline and online use temporary local keys and licenses. Online authorization talks to an ephemeral reference service on `127.0.0.1` over loopback HTTP; binding a local port must be permitted. This is a protocol exercise, not a test of a remote production licensing service, network failure handling or a deployment's key management.

## Reproduce

From the repository root, first set up the Python environment and native VideoPipe executable using the [VideoPipe pilot build instructions](../videopipe-pilot/README.md#build-on-macos-arm64). Those instructions specify the tested macOS arm64 toolchain and pinned VideoPipe and OpenCV sources; the build requires network access. Then run:

```sh
pilot=drafts/v0.2/videopipe-pilot
experiment=drafts/v0.2/vlsp-unified-integration
PYTHONDONTWRITEBYTECODE=1 "$pilot/.venv/bin/python" "$experiment/compare.py"
```

The default output directory is `drafts/v0.2/vlsp-unified-integration/reports/`, containing `comparison.json`, `reference.json` and `videopipe.json`. Use `--reports /path/to/directory` to choose another report directory and `--binary /path/to/executable` to select the native VideoPipe executable.

The experiment covers a synchronous scenario, not asynchronous MLLM execution. It does not establish compatibility with every VLMP package or VideoPipe deployment, real camera input, codecs, RTSP, GPU inference, durable delivery, crash recovery or production security and performance. VideoPipe and OpenCV remain subject to their respective upstream licenses.
