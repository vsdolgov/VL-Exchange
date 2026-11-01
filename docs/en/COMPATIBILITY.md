# Compatibility and evidence

**2026-11-01 · Draft / Experimental · [Русский](../ru/COMPATIBILITY.md)**

A normative specification states a requirement; a report records an actual run. “Prepared” does not mean “verified,” and two local hosts are not two vendor platforms.

| Component | Scope checked | Status |
|---|---|---|
| VLMP public proposal0.2.0-draft.1 | Synthetic classification, CPU FP32 ONNX13/IR8, NumPy ABI, 5 inputs, 13 calls per host | Local reference and independent hosts, shared ONNX Runtime; reports below |
| VLSP0.1 | One Classification, deterministic JSON state/events, 15 golden steps | Reference and independent hosts; independent development/offline/online, complete event/state identity |
| Security0.1 | Signing/encryption, recipient binding, expiry, offline entitlement, online nonce/grant/revocation/lease, negative paths | Automated tests with temporary keys, real loopback HTTP online |
| Pipeline | Actual ONNX result → protected VLSP, offline/online | Explicit Classification mapping, neither a standardized callback nor public0.2 model_links |
| CI | Schemas, local Markdown targets, tests, two hosts, pipeline | GitHub Actions had not run before the first publication. See [GitHub Actions](https://github.com/vsdolgov/VL-Exchange/actions) for current runs. |
| Third-party platform | Partner importing one unchanged ZIP | Pilot planned; NOT TESTED |
| GPU/production sandbox/TLS deployment | Operational properties | NOT TESTED |

## Reproduction and artifacts

After installing README dependencies, run `python tools/verify.py` from the repository root. Every check must succeed. Online tests need loopback sockets; denied sandbox bind is an environment limitation, not a passing result. The current [machine summary](../../reports/summary.json) records each exit code and OS/Python versions. [Pytest output](../../reports/pytest.txt) records the actual test count.

Model: [reference](../../reports/vlmp-reference.json), [independent](../../reports/vlmp-independent.json), [ZIP](../../model/reports/vlmp-public-channel-probe.zip). The common manifest SHA256 is `abcb2f3b91250490bdf4716c48f0e05ef1c9df11c48baf2d463865bd1413b315`; ZIP SHA256 is `b004c02c6f12839007389c71ec60e6bd77b2824a72c27d9bd7ddc71be04752ff`. The [archive-roundtrip report](../../reports/vlmp-archive-roundtrip.json) additionally records actual execution of this ZIP: two separate safe extractions into private temporary directories, 13 calls per host, complete result comparison between hosts and against the golden oracle, and archive/manifest/every-member SHA-256 before and after. No decrypted secrets are involved; temporary directories are removed. Independence concerns loading and orchestration, not the inference engine.

The independent integration does not import the reference runtime; both share ONNX Runtime and model.py, explicitly limiting the independence claim.

Scenario: [reference](../../reports/vlsp-reference.json), [independent](../../reports/vlsp-independent.json), [ZIP](../../scenario/reports/vlsp-target-scenario.zip). ZIP SHA256 is `eb9ea96fda4bbae5b6db2da859b78d4a897858dff1ffc7dc90a07b077e876e35`. The independent host implements its own load/process/state/event-ID path and shares only schemas/test vectors and the crypto component. Its three modes check one unchanged ZIP. Ephemeral encrypted envelopes differ because recipient keys and randomized encryption differ, while the original payload ZIP remains the same.

The [pipeline report](../../reports/pipeline.json) separately records actual_inference, real score and protected scenario results. Prepared Classification fixtures in golden tests are not represented as inference.

The validator also requires at least three genuinely distinct golden arrays by dtype, shape and values, rather than IDs/paths alone. A targeted [model test run](../../reports/model-tests.txt) after this change completed with **14 passed**. The negative case preserves different IDs/paths and updates file digests, so array-content distinctness is the actual failure checked. This is not a new claim about the combined suite total.

The final combined run after this fix completed with **55 passed** and all **9 checks** in `tools/verify.py` passing; current results are in [summary](../../reports/summary.json) and [pytest](../../reports/pytest.txt).

The synthetic scenario delivery is now solution version **1.0.1**: the displayed company name in its docstring changed to **VL**. Scenario behavior, VLSP format0.1 and the Python ABI are unchanged. This byte change has a new manifest/ZIP identity; current reports were rerun against this delivery. Earlier1.0.0 evidence is not relabeled as a1.0.1 run.

Literal-name cleanup removes external company-domain links from both README files. The independent host preserves the legacy vendor namespace deny rule by exact root-digest comparison. A negative regression test checks root/submodule imports before execution and allowed near-matches. Synthetic model/scenario package bytes remain unchanged in this cleanup.

## Historical VLMP0.1

The original October30 local pilot tested a different manifest/ABI: CustomModelProtocol, `vlmodels-types1.8.1`, actual Box5 CustomModel and an independent loader. Its private SDK and test-host materials are excluded from this project. The [full historical0.1 specification](../../specs/vlmp/0.1/SPECIFICATION.en.md) describes the interface for independent adapters; the external SDK is not redistributed. The original [0.1 JSON Schema](../../specs/vlmp/0.1/model-package.schema.json) is retained as a historical contract; public0.2 has not been renamed0.1 and does not inherit Box5 compatibility. A reproducible published Box5 adapter requires an authorized public SDK or a separate adapter implementation and a new run.

## Limitations

Industrial accuracy, third-party adoption and arbitrary models/scenarios/GPUs are untested. Python code is trusted; namespaces are not sandboxes. The loopback license server is a test service; production TLS/HA/key storage are untested. Envelopes do not automatically protect external models, and the public VLMP protected runner is not integrated. Offline expiry relies on trusted clocks and moving an installation requires authorized reissue; immediate offline revocation or administrator resistance is not promised. Publication of the material set is approved; final project licenses and patent policy remain subject to approval.
