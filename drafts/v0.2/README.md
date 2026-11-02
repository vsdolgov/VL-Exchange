# VL Exchange 0.2 specifications

**English | [Русский](README.ru.md)**

This directory contains the VL Exchange project's specifications for VLSP 0.2, unified VLMP 0.2.0 and the prior separate VLMP detection profile. Their publication does not establish industry approval or production readiness. The reference implementations and pilot remain experimental. The historical `drafts/v0.2` path is retained to preserve links; it does not describe the current status of these specification documents. Existing public examples and contracts remain available from the [repository home](../../README.md).

## Which document should I read?

| If you want to… | Start here |
|---|---|
| Understand a scenario with several models and a tracker | [VLSP 0.2 specification](scenario/spec/SPECIFICATION.md), [manifest schema](scenario/spec/scenario-package.schema.json) and [conformance checks](scenario/spec/CONFORMANCE.md) |
| Implement errors, state transitions and lifecycle | [Error and lifecycle contract](scenario/spec/ERRORS-LIFECYCLE.md) |
| Understand host capabilities, input queues and diagnostics | [Optional profiles](scenario/spec/OPTIONAL-PROFILES.md); their individual message schemas are in [schemas](scenario/spec/schemas) |
| Submit a job to an asynchronous MLLM and correlate the result later | [Async MLLM profile](scenario/spec/ASYNC-MLLM-PROFILE.md) and [message example](scenario/spec/async-message-fixture.json) |
| Understand the prototype's persistent authorization snapshot | [Persistent lease draft](security-runtime/PERSISTENT-LEASE.ru.md) |
| Run classification and detection packages using VLMP 0.2.0 | [Unified specification and two-host test](vlmp-unified/README.md) |
| Inspect the separate detection-model profile | [VLMP detection specification](model/spec/SPECIFICATION.md), [manifest schema](model/spec/model-package.schema.json) and [detection result schema](model/spec/schemas/detections.schema.json) |

The previous detection profile retains wire identifier `0.2.0-detection-draft.1`; the previous classification profile retains `0.2.0-draft.1`. Their original schemas and package bytes remain unchanged. The new unified `0.2.0` defines its own schema and explicit profiles; a host must advertise support for the relevant version and profile. None of these facts establishes Box 5 compatibility. VLSP 0.2 also does not automatically imply compatibility with VLSP 0.1.

## What was checked locally

VideoPipe is used as an example of a different execution environment to demonstrate the generality and portability of a single VLSP package. It is not a required dependency of the standard; one pilot does not establish compatibility with all platforms.

One unchanged scenario ZIP, SHA-256 `1461028eb6176c72bb0229eb8b28b7e697b6a4fcc1f7d420e095631bf4e0f332`, was run in a reference host and a separately implemented host connected to a C++ VideoPipe graph. The synchronous scenario uses a detector, an IoU tracker and two distinct classifiers. The hosts share model loaders, ONNX Runtime and security components, so this is not a completely independent implementation of every component. No third-party vendor platform participated.

After aligning the error and lifecycle rules, the local comparison passed **115 of 115 command comparisons**, including matching error fields for 28 expected failures. An additional input-queue test passed **12 of 12 matching cases** across both hosts, including overflow, stop, restart, failure and an unknown commit outcome after a lost reply. In the continuous `block` test each host processed **120 of 120 frames** without loss; a separate 300-frame burst tested explicit drop accounting. The regression suite reported **142 passing tests**.

The [queue report](reports/STREAM-VERIFICATION.md), [machine-readable queue results](reports/stream-load.json), [comparison results](reports/comparison.json) and [test summary](reports/stream-pytest.txt) are an archive of that local experiment. The [ported VideoPipe pilot](videopipe-pilot/README.md) contains build and rerun instructions with pinned external dependencies; numerical measurements vary with the environment.

This was a synthetic local CPU experiment with 4×2 pixel frames and untrained ONNX models. It does not establish analytics quality or production throughput. The bounded queue was in the Python worker before the scenario, after the frame passed through native VideoPipe components: it did not bound upstream VideoPipe queues or the memory of blocked producers. RTSP, codecs, GPUs, durable delivery, crash recovery and external platform acceptance were not established by this test. VideoPipe was tested with the synchronous scenario; **the asynchronous MLLM profile was not tested through VideoPipe**.

## Async prototype and conformance

The async profile describes later delivery of a correlated MLLM result. Local prototype experiments and a separate test with an MLLM do not establish full conformance to that profile. In particular, the profile requires idempotent resubmission, while the current `AsyncScenarioExecutor` prototype rejects a repeated submission of the same job within a single `process` call. A timeout also does not guarantee interruption of an already submitted external request; recovery, media retention and external delivery need their own guarantees and tests.

Optional profiles are independent capabilities, not automatic properties of a host. A host should claim support only for the semantics and checks it actually implements. In the stream profile, queue capacity counts waiting frames; a frame being processed is additional. An unstructured callback failure or a lost response may occur after commit, so the result can be explicitly unknown and is not proof of rollback or exactly-once processing.

## Run the included tests

With Python 3.12 and the root `requirements.txt` installed, run from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH='drafts/v0.2/scenario/src:drafts/v0.2/model/src:drafts/v0.2/security-runtime/src' python -m pytest -q -p no:cacheprovider --import-mode=importlib drafts/v0.2/scenario/tests drafts/v0.2/model/tests
```

These tests cover the included reference components and mock providers. Repeating the VideoPipe comparison requires the separate [native pilot build](videopipe-pilot/README.md). The external MLLM experiment is not part of these tests.
