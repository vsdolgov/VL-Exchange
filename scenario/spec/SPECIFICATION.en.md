# VL Exchange — VL Scenario Package (VLSP) v0.1

**Version 0.1.0. Open normative pilot draft, 2026-10-31. [Русский](SPECIFICATION.md).** MUST, MUST NOT and MAY indicate requirements, prohibitions and options. This document does not claim third-party vendor support or production readiness. Verified results are recorded separately.

## Experimental attribution extension

Publishers may include the optional `org.vl-exchange.attribution` entry in `extensions` with `required: false`. A host that supports this separate [Attribution & Branding profile](../../docs/en/ATTRIBUTION.md) can validate and carry publisher and author information. Hosts unaware of the extension may ignore it. It does not alter the core event schema or existing golden fixtures: attribution can travel in a separate host-provided export envelope. Display and propagation require host support.

## 1. Decision and scope

VLSP is a standalone executable **Python scenario delivery** with open descriptions of external inputs, parameters, state, events, dependencies and errors. Together with **VL Model Package (VLMP)** it forms the inter-vendor part of **VL Exchange**. Operational source code is not automatically published: the interface specification is open and execution can be licensed.

Recipients do not need VLBP, a block graph, the VL editor, NRI, proprietary models_manager or a VL SDK. Neither the scenario nor its consumer may import VLBP or physically depend on its files. The internal VL component marketplace is separate. A host implements the small Python API below and connects it to its own system.

The first profile, `python-scenario-json-v1`, is limited to one Classification input, ordered synchronous calls, JSON state per source and logical run, and JSON event candidates. It does not promise arbitrary Python, all analytics scenarios or GPU portability. Frames, tracks, geometry, video decoding, graphs, asynchronous callbacks and autonomous timers require separate profiles.

## 2. Package and manifest

```text
package-root/
├── scenario-package.json
├── code/
│   ├── __init__.py
│   └── scenario.py
└── golden/
    └── basic.json
```

`scenario-package.json` is strict UTF-8 JSON conforming to [scenario-package.schema.json](scenario-package.schema.json), with `format="vl.scenario-package"` and `spec_version="0.1.0"`. Duplicate keys, comments, NaN/Infinity and unknown core fields are forbidden. Strings contain Unicode scalar values; unpaired surrogates are rejected, UTF-8 is strict and no implicit Unicode normalization is performed.

`scenario_id` is namespaced; `version` identifies delivery/behavior. Publishers MUST NOT reissue the same scenario_id/version with different bytes. Exact identity comprises the original manifest SHA-256 and every `files` digest; names/versions alone are insufficient.

| Field | Contract |
|---|---|
| `entrypoint` | Python package root, scenario.py, Scenario class, ABI version |
| `files` | Complete relative path → SHA-256 inventory |
| `runtime` | PEP 440 Python requirement |
| `input_contract` | Input name, message type, clock domain |
| `event_contract` | Identity algorithm, allowed event types and payload schemas |
| `parameters` | External parameter types, defaults and constraints |
| `state`, `ordering`, `limits` | State ownership, order and deadline/size/count limits |
| `dependencies`, `model_links` | Python packages and explicit VLMP references |
| `required_capabilities`, `extensions` | Negotiated capabilities without silent fallback |
| `golden` | Sequences and expected host events/state/errors |
| `delivery` | Production requires a protected envelope; plaintext is development-only |

Every regular file except the manifest is inventoried, including helpers, documentation and golden fixtures. Relative POSIX paths resolve inside the package root; symlinks, absolute paths, `..`, `.`, empty segments and globs are prohibited. The entrypoint root contains `__init__.py` and `scenario.py`. Unlisted/missing/modified files are rejected before import.

Hosts do not modify deliveries or write bytecode, temporary files or reports into them. Hashes are checked before import. Changes to manifests/fixtures/tolerances create new delivery identity. Digests check integrity; authorship and execution rights are separate security properties.

Receiving systems MUST prevent checked files from changing between verification and import and throughout use, for example with a private copy inaccessible to other processes, suitable permissions or a read-only mount. Separate hashing and import do not prevent replacement between reads. The reference security component uses a private temporary directory; trusted administrators and publishers are outside the attacker model. A development directory writable by another process provides no such guarantee.

## 3. Python API and self-containment

```python
class Scenario:
    def __init__(self, **parameters): ...
    def process(self, inputs: dict, context: dict, state: dict) -> dict: ...
    # Optional close(self) releases resources.
```

`process` returns **candidates**, not final external envelopes:

```json
{"events":[{"event_type":"org.vlexchange.target-observed.v1","payload":{"label":"red","score":0.5,"count":2}}],"state":{"count":2}}
```

The exact schema is [raw-return.schema.json](schemas/raw-return.schema.json); candidates use [event-candidate.schema.json](schemas/event-candidate.schema.json). Candidates contain only event_type and payload. They cannot set event_id/source/time: the host creates and validates these. An empty events list is successful execution without an event.

Self-containment means scenario logic is included and all external dependencies are declared. It does not mean the package installs Python, drivers or models. Code does not import VLBP/NRI/VL internals; standard library and declared neutral dependencies are allowed. Helper modules are inventoried and use relative imports. Runtime pip/apt, sys.path hacks and hidden code loading are outside the profile.

`runtime.python_version_specifier` and dependencies are checked against actual distributions. The demo's >=3.12,<4 range is a declaration, not proof of testing every Python/OS in the range. Actual Python/backend/OS versions are recorded.

Parameters have string/number/integer/boolean types and defaults; constraints/enums are checked before construction. Boolean is not integer and all numbers are finite. Hosts take all defaults, apply only declared valid overrides and pass exactly that set to Scenario(**parameters). No implicit device, SDK/context, secrets or paths are added. Changing parameters requires a new instance.

## 4. Input and host mapping

`inputs` contains exactly `input_contract.name`, whose value is one [Classification](schemas/classification.schema.json): label, score in[0,1], category_id and output_index. Lists/batches/SDK objects/additional payloads are rejected. Classification matches one canonical VLMP `results` element; hosts extract it explicitly, preserving ID/index and score.

The receiving system maps its cameras/models/streams: it extracts Classification and assigns source_id/run_id/sequence and correct UTC event time. Hosts do not infer source from a label or alter label/score to trigger a scenario. Mapping is part of integration and is tested separately; valid JSON does not prove actual inference or model quality.

## 5. Context, logical runs and clock domain

Context follows [context.schema.json](schemas/context.schema.json): source_id, run_id and nonnegative integer sequence/event_time_ms. Extra fields are forbidden.

- **source_id** is a logical host source.
- The host assigns **run_id** uniquely to an independent logical run. It is neither a license installation_id nor automatically a process UUID.
- Sequence strictly increases within source_id/run_id. Gaps are allowed; duplicates/backward sequence fail before process.
- Event_time_ms never decreases within that pair; equal timestamps are allowed. No global ordering is imposed between sources/runs.
- The v0.1 clock domain is **UTC Unix milliseconds**, without a separate leap-second representation. Source-relative PTS requires explicit conversion; other clock domains require another profile.

Invalid calls do not advance ordering. Event time is not a license/deadline clock. Golden fixture timestamps are synthetic, not real event dates. A new physical installation, process restart or network retry does not itself define a new logical run: hosts decide with replay/deduplication in mind.

## 6. Formal event boundary

Each event_type is declared in `event_contract.types` with a self-contained payload JSON Schema. Unknown types, extra candidate fields or invalid payloads cause fatal output errors. Schemas cannot contain $ref/$dynamicRef/$id. Event types are unique.

Publishers describe event meaning, payload units, trigger conditions and time semantics in the normative profile or inventoried documentation. Shape alone does not define business meaning. Changing meaning requires a new scenario/type version; matching names do not establish semantic equivalence.

For demo `org.vlexchange.target-observed.v1`, label==target_label and score>=min_score count consecutive **received observations** within a source/run. Sequence gaps do not invent missing frames. A mismatch resets count to0; a match increments it up to1_000_000. A candidate occurs once when `previous_count < min_count <= count`; the third match after the default threshold2 does not emit again. Payload label/score come from the triggering observation, count is the new count. This is neither dwell time, physical-presence proof nor ground-truth model quality.

The host builds the complete external event:

```json
{"event_id":"sha256:…","event_type":"org.vlexchange.target-observed.v1","source_id":"camera-a","run_id":"run-1","sequence":2,"event_time_ms":1700000000000,"payload":{"label":"red","score":0.5,"count":2}}
```

The full schema is [event.schema.json](schemas/event.schema.json). Source/run/sequence/time exactly echo accepted context. This profile attributes events to the current observation time; interval start/end, retrospective times and event lifecycle require an extension. Candidate order is deterministic; ordinal is its zero-based list index.

### event_id algorithm: sha256-scenario-run-v1

1. Take fields in this order: **scenario_id, version, source_id, run_id, decimal sequence, event_type, decimal ordinal**.
2. Strings contain Unicode scalar values and encode as strict UTF-8 without normalization. Sequence/ordinal are nonnegative integers written in decimal without leading zeros; zero is `0`.
3. Start with ASCII `VLSP-EVENT-V1` followed by byte00.
4. For each field append its UTF-8 byte length as uint32 big-endian, then the bytes. Length excludes its own4bytes; lengths >=2^32 are rejected.
5. Return `sha256:` followed by the64lowercase-hex SHA-256 digest of all bytes.

A language-independent Unicode vector, including exact hash_input_hex, is in [event-id-test-vector.json](event-id-test-vector.json). Neither JSON serialization nor platform encoding is used. Manifest digest is excluded because golden events are inventoried and would create a cycle.

Replay with identical scenario_id/version/source/run/sequence/type/ordinal produces the same ID. A new independent run_id produces a different ID. This helps consumers deduplicate but does not guarantee exactly-once network delivery or a durable outbox. Publishers must keep scenario_id/version immutable so one ID cannot describe different implementations.

## 7. State, reset and failures

The host owns state in namespace `(manifest digest, instance identity, source_id, run_id)`. Each new namespace receives a deep copy of `state.initial`. State is a finite JSON object validated by a self-contained draft2020-12 schema without ref/id. Inputs/context/state are independent copies. Mutating inputs/context is forbidden.

All state affecting future events lives in returned state. Hidden globals/self state/shared vendor GraphContext are forbidden. A technical cache is permitted only if it cannot affect results. One instance does not process concurrent calls.

The host validates candidates, state, limits, ordering and the current execution lease, creates envelopes/IDs and then atomically publishes events and replaces state/last sequence/time. Failure causes no commit/publication. Atomicity refers to the local operation; crash recovery, external delivery and outbox are not specified.

`reset(source_id,run_id)` clears state and ordering for that pair; source-only reset clears all runs of that source; reset without arguments clears the instance. A run without source is invalid. Reset is not automatic on dates/timeouts. Replaying the same logical run after reset yields the same event IDs; a new logical session requires a new run_id. JSON state does not guarantee cross-version migration.

Errors contain stage/code/message/fatal/retryable. Invalid input/context/order before process yields input_contract/input_order with fatal=false,retryable=false and unchanged state/order. Exceptions, input/context mutation, invalid candidates/events/state or post-process limit/deadline failures are fatal=true,retryable=false; further process/reset calls on that instance are forbidden, requiring a new instance. Optional close runs on cleanup and does not automatically repair failures. There is no implicit retry, stale output or fallback scenario.

## 8. Resources and determinism

`max_process_ms` measures synchronous process using a monotonic wall clock. Late return is rejected without commit; this is not hard preemption or a guarantee that hung Python stops. Worker termination and CPU/GPU/RAM/network isolation are separate host capabilities. Import time is excluded from the process budget.

`max_input_bytes`, `max_state_bytes`, `max_event_bytes`, `max_events_per_call` are checked at the applicable before/after-call stages. Size is UTF-8 compact JSON with sort_keys, ensure_ascii=false, allow_nan=false and separators(',',':'). Event limits apply to the full host-built envelope. This does not constrain transient Python allocations.

The mandatory profile is deterministic in params/inputs/context/state. Wall clock, random UUIDs and undeclared external responses do not determine events/state. A base scenario does not send direct notifications, write databases or perform irreversible business operations: it returns candidates. This permits withholding results on license/deadline failure. Hosts route only after commit under their own policy.

## 9. Models and dependencies

`model_links` contains alias, exact VLMP manifest_sha256, profile and required. The host resolves paths/registries externally. Before import, required references are checked for exact manifest hash, format/spec/profile/result and complete file inventory/digests. Invalid required references fail; unavailable optional references are reported explicitly. This lean check reports onnx_graph_validated=false and inference_executed=false; linking does not require ONNX imports.

Before actual inference, a VLMP host separately performs full schema/graph/backend validation. Models are not automatically bundled/downloaded; a reference grants no weight rights. The common encrypted envelope protects only included bytes; external models are licensed/protected separately.

A Classification-only scenario not invoking a model has model_links=[]; the host may supply any suitable model result. Classification type alone does not identify the producing model. Provenance requirements belong to integration or another explicitly declared profile. Internal model-invocation callback APIs are not standardized in v0.1 and are never silently injected into Scenario.

A real end-to-end test executes VLMP and passes its actual result into protected VLSP. Prepared Classification JSON is a fixture, not inference evidence. The [earlier VLMP pilot](../../docs/en/COMPATIBILITY.md) checked a classifier and two loaders, not this scenario contract or a competitor platform. Current model_links validation accepts the original VLMP0.1 shape; the proposed public0.2 model is connected explicitly by the demonstration pipeline, not disguised as a resolved0.1 link.

## 10. Mandatory protected operational delivery

`delivery` sets production `vl.exchange.protected` v0.1.0 and plaintext `development-only`. Production hosts do not treat direct plaintext paths as licenses. Development needs an explicit mode and is not claimed as protected. Package-supplied secrets, trust anchors or `authorized=true` claims grant no rights.

The separate common [VL Exchange Security](../../docs/security/SPECIFICATION.en.md) component has no VLBP dependency. Both **offline signed entitlements** and **online signed short-lived grants** are supported. Mode is signed explicitly; silent online→offline fallback is forbidden. Trusted administrators supply keys/trust/installation identity/server endpoints; private keys are not packaged.

Signatures, issuer/audience/subject, rights, package/archive/manifest digests, issued/not-before/expires times and installation keys are checked before decrypt/import under the security profile. Online additionally binds nonce/license/package/installation and is verified with an administrator-provided server key. HTTPS is required except explicit loopback tests. Denial/revocation/network failure/unsupported licensing fail closed.

Each process call obtains an immutable **ExecutionLease**; the **same lease** is rechecked after return and before commit. Concurrent renewal does not retroactively extend an in-flight call. Expired permission prevents event/state commit. This does not promise forcible interruption of executing Python. Offline expiry relies on trusted clocks/rollback policy; immediate offline revocation without a channel is not promised.

Publishers and their Python code, and host administrators, are trusted. **The reference runner is not a Python sandbox.** Arbitrary untrusted scripts cannot share a process/OS identity with keys, plaintext or trusted memory. The platform separately provides OS permissions, filesystem/network/process isolation, temporary file/log/dump protection and input confidentiality.

A third party without the recipient private key cannot execute a copied encrypted delivery. Copying a host secret, cloning the trusted system, replacing runtime or leaking plaintext outside trusted authorization is outside this guarantee. Absolute DRM or protection of a compromised plaintext runner is not claimed.

## 11. Golden tests, extensions and claims

Golden files follow [sequence.schema.json](schemas/sequence.schema.json), with calls containing inputs/context/expected or explicit reset. Expected events are **full host envelopes**, not raw candidates; state is committed state; errors compare code/fatal/retryable without bytewise diagnostic-message comparison. Each sequence starts a new instance and runs steps sequentially.

Keysets/strings/IDs/flags/integers match exactly. Numeric payloads satisfy `abs(actual-expected) <= atol + rtol*abs(expected)`, 0≤atol,rtol≤0.001. Tolerances are frozen before execution and inventoried through the manifest. Demo tolerances are0 because scenarios copy scores without floating-point inference.

Tests cover threshold boundaries/third hit/streak breaks, two sources, two runs of one source, equal time/sequence gaps, error no-commit, reset/replay same ID, Unicode vector/no normalization, limits/unknown events/dependencies/capabilities, offline/online positive and negative paths. Both independent hosts must produce identical IDs for identical inputs/run_id including Unicode; two CLIs over the same loader/process do not prove independence.

Required capabilities: python-scenario-json-v1, classification-v1, event-json-v1, source-run-state-v1, event-id-sha256-v1. Unknown required capabilities/extensions fail before import. Optional extensions do not change core behavior. Another clock domain, input type, side effects, model callback, event lifecycle or state migration needs a separately negotiated capability/new version, not silent approximation.

See [CONFORMANCE.md](CONFORMANCE.md). The [placeholder manifest](example-manifest.json) with zero digests is not executable. The [example sequence](example-sequence.json) contains synthetic events, not industrial analytics results.

Reports distinguish specification, static validation, independent host execution, protected offline/online execution, actual VLMP→VLSP chains and deployment. Pilot success does not mean third-party adoption or portability of all scenarios/models/GPUs.
