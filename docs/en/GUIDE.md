# VL Exchange practical guide

**Draft / Experimental · [Русский](../ru/GUIDE.md)**

## Exporting a model

Choose a supported profile before choosing a weights format. Original VLMP0.1 requires CustomModelProtocol/SDK; public profile0.2.0-draft.1 is a separate proposal without private SDKs. They are not interchangeable. Start with the [synthetic example](../../model/examples/channel-classifier) and [exact contract](../../model/spec/SPECIFICATION.en.md).

Implement Model.preprocess/postprocess and include helpers. Specify BGR uint8 HWC source, RGB/resize/normalization in code, ONNX names/shapes, output labels and category_id. Declare public dependencies, Python version and capabilities. Add at least three different `.npy` inputs without pickle, expected label/score/ID/index and tolerances. Inventory every non-manifest file and its SHA-256 in `files`. Do not add customer data without permission.

To reproduce the educational example from the repository root:

```sh
python model/tools/build_demo.py
python model/src/public_model.py validate model/examples/channel-classifier
python model/src/public_model.py golden model/examples/channel-classifier
python model/tools/independent_host.py model/examples/channel-classifier
python tools/model_archive_roundtrip.py
```

Rebuild before freezing. Both hosts must use one unchanged delivery. Record manifest/file/ZIP digests and environment versions. Matching top1 without checking scores is insufficient.

`model_archive_roundtrip.py` takes one unchanged ZIP, checks safe paths, absence of symlinks/case collisions, resource limits and complete inventory, extracts it separately for each host into private temporary directories and launches both through subprocesses. All 13 results per host are checked against the golden oracle and each other; archive/manifest/every-member SHA-256 are compared before and after, then temporary directories are removed. This actually tests archive delivery on local hosts; it does not replace testing another vendor platform.

## Exporting a scenario

Implement `Scenario(**parameters)` and `process(inputs,context,state)` returning only `{events:[{event_type,payload}],state}`. Keep all state affecting future events in JSON state. Code does not access a VL graph or perform external business operations itself. Define event meaning, thresholds, units and time semantics. Supply schemas/defaults/limits and golden sequences with complete host events.

```sh
python scenario/tools/build_demo.py
python -m vl_scenario_package validate scenario/examples/target-scenario --policy development --check-environment
python -m vl_scenario_package golden scenario/examples/target-scenario --policy development
python scenario/tools/independent_host.py scenario/reports/vlsp-target-scenario.zip
```

The last command exercises the same ZIP in the independent host in development/offline/online modes. A temporary loopback HTTP server is used only for online testing. No persistent keys are stored. The full [VLSP contract](../../scenario/spec/SPECIFICATION.en.md) defines events, state, reset and failures.

## Receiving-platform adapter

1. Declare exact format/version/profile/capabilities. Reject unknown required features before import. Check inventory, graph, dependencies and rights separately.
2. Explicitly map cameras/streams to source_id and logical sessions to run_id. Assign strictly increasing sequence and nondecreasing UTC Unix event_time_ms. Convert source PTS explicitly.
3. Obtain actual model inference and supply `{classification:{label,score,category_id,output_index}}`. A JSON fixture is not inference. VLSP0.1 does not specify hidden model callbacks; public VLMP0.2 is connected through explicit mapping, while original model_links remain0.1.
4. Hosts own JSON state per manifest/instance/source/run, construct event envelopes/IDs and commit events/state atomically only after all checks. External delivery/outbox/deduplication belongs to integration.
5. Provide process isolation, filesystem/network rights and limits. Import namespaces and post-return timeout checks are neither sandboxing nor preemption. Both demonstration hosts execute trusted Python.
6. For commercial delivery, separate issuer trust from package contents. Validate an offline entitlement or online grant before decrypt/import and recheck the same lease before commit. Administrators are trusted; a protected model runner for the new public profile still needs integration.

## Protected end-to-end example

```sh
python tools/test_pipeline.py
```

This performs real ONNX inference and passes Classification to protected VLSP offline and online. Recipient/installation/issuer/server keys are generated temporarily; envelopes and plaintext are deleted afterwards. The protected delivery protocol is specified [separately](../security/SPECIFICATION.en.md). This tests trusted prototypes and is not a production key-management runbook.

## AI assistance during migration

AI assistants can simplify writing adapters, understanding another API and moving application logic. Our position is that a shared contract lets this assistance produce verifiable results and reduce repeated integration effort while preserving competition between platforms and developers. AI does not replace agreement on event semantics, licensing terms or tests of one unchanged package in independent systems; no specific percentage saving is claimed.

## FAQ

**Must we use the VL platform or cloud?** No. Recipients implement the open contracts and their own adapters; offline delivery needs no cloud.

**Why add this alongside ONNX/APIs/ONVIF?** Those technologies solve their own tasks. Transferring application behavior additionally requires processing, state, event semantics and golden tests. VL Exchange complements existing technologies.

**Must source and weights be public?** No. Interfaces are open; commercial rights and protected delivery are separate. Public examples are synthetic. Final project licenses remain subject to approval.

**Will a paid license automatically work on another host?** No. That is the intended commercial principle when rights-holder terms permit it. Current delivery is bound to a recipient key; transfer requires authorized reissue, and offline revocation is not immediate.

**Is every signed model safe?** No. Signatures authenticate a key, not code safety. Trusted publishers/administrators are prerequisites of the current runner; untrusted code needs receiving-platform isolation.

**What is verified today?** See the [matrix](COMPATIBILITY.md); two local hosts do not establish competitor or GPU support.
