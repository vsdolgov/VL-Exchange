# VL Exchange specification release — November 3, 2026

By the project initiator's decision, this release publishes final VL Exchange project editions of VLSP 0.1 and 0.2, the optional asynchronous MLLM profile, separate VLMP classification and detection profiles, the protected-delivery profile and the attribution extension. A final project edition fixes the project's requirements for its respective profile; it does not imply approval by an industry body, compatibility of a particular product or production readiness.

Read this release alongside the [VLSP 0.2 specification](../../drafts/v0.2/scenario/spec/SPECIFICATION.md), [async profile](../../drafts/v0.2/scenario/spec/ASYNC-MLLM-PROFILE.md) and [overview of evidence and limitations](../../drafts/v0.2/README.md). The VLSP wire contract version is `0.2.0`. The `drafts/v0.2` directory remains as a historical path to preserve existing links.

## Identifier compatibility

The published model profiles have separate schemas and existing wire identifiers `0.2.0-draft.1` for classification and `0.2.0-detection-draft.1` for detection. These remain pre-release format identifiers: finalizing their documentation does not turn them into one final `0.2.0` format. They are preserved unchanged to retain package bytes and digests. A unified schema or final package identifiers require a separate release and migration plan. The `draft/2020-12` string in `$schema` names a JSON Schema version and also remains unchanged.

## Evidence and outstanding work

Synchronous VLSP was tested on one unchanged ZIP with two locally and independently written scenario hosts; the report discloses shared model loaders, ONNX Runtime and cryptographic components. This is not a test of two commercial platforms. Full async-profile conformance on two independent hosts has not been established: in particular, the reference prototype rejects a duplicate `submit_mllm` for the same job within one `process`, and cross-language identifier canonicalization still needs a separate definition and verification. Test results and limitations remain visible rather than being replaced by the final-edition label.

The [governance proposal](GOVERNANCE.en.md) associated a stable release with two independent hosts testing the same unchanged ZIP. At the initiator's express decision, the final project editions are published before that proposed criterion has been satisfied for every individual profile; this must not be presented as a passed test. External compatibility, RTSP, GPU, production operation and durable delivery require separate testing. Legal licenses and patent policy remain unapproved; this release does not itself grant additional rights.

Historical reports, examples, results and schema identifiers are retained. Future incompatible changes require a new identifier and migration instructions.

The archived VLMP 0.1 specification in `specs/vlmp/0.1` remains a historical document with its original status; its descriptions of later proposals reflect the time when the archive was prepared.
