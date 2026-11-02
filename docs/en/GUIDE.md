# VL Exchange practical guide

**Guide to the final VL Exchange project specifications · [Русский](../ru/GUIDE.md)**

This guide shows how to package a model or scenario and connect it to a receiving platform. The examples below use the classification profile's final project specification and the legacy VLSP 0.1 interface; they are synthetic and run locally. The model's wire identifier remains pre-release, and the implementations remain prototypes. For the distinct multi-model interface, see [VLSP 0.2](../../drafts/v0.2/scenario/spec/SPECIFICATION.md). Final project status does not establish industry approval, production readiness or support by third-party platforms.

## Exporting a model

First choose a supported profile. The original VLMP 0.1 requires `CustomModelProtocol` and its SDK. The public classification specification retains the pre-release wire identifier `0.2.0-draft.1` for compatibility and does not require a private SDK. The `draft` fragment is part of the identifier, not the current document status. These profiles are not interchangeable. Start with the [synthetic example](../../model/examples/channel-classifier) and its [interface specification](../../model/spec/SPECIFICATION.en.md).

To prepare a package:

1. Implement `Model.preprocess` and `Model.postprocess`, and include the helper functions they need.
2. Describe the BGR `uint8` HWC input, RGB conversion, resizing and normalization. Specify the ONNX input and output names and shapes, labels, and `category_id`.
3. List the public dependencies, Python version and required capabilities.
4. Add at least three different `.npy` inputs without pickle. For each, include the expected label, score, ID, index and comparison tolerances.
5. List every file except the manifest in `files`, with its SHA-256 hash. Do not add customer data without permission.

To rebuild and check the example, run these commands from the repository root:

```sh
python model/tools/build_demo.py
python model/src/public_model.py validate model/examples/channel-classifier
python model/src/public_model.py golden model/examples/channel-classifier
python model/tools/independent_host.py model/examples/channel-classifier
python tools/model_archive_roundtrip.py
```

Rebuild before fixing the package version. Both hosts must run the same unchanged package. Record the hashes of the manifest, files and ZIP, along with the environment versions. Checking only the top result is not enough: compare scores as well.

`model_archive_roundtrip.py` checks the ZIP's paths, absence of symlinks and case-colliding names, resource limits and complete file list. It then extracts the same ZIP separately for each host into private temporary directories and runs both hosts in separate processes. It compares all 13 results per host with the expected results and with each other. It also checks the SHA-256 hashes of the archive, manifest and every file before and after the run, then removes the temporary directories. This tests archive delivery on local hosts; it does not replace a test on another vendor's platform.

## Exporting a scenario

Implement `Scenario(**parameters)` and `process(inputs,context,state)`. The latter returns only `{events:[{event_type,payload}],state}`. Keep everything that can affect future events in the JSON state. The code does not access a VL graph or perform external business operations itself.

Describe what each event means, including thresholds, units and timing. Provide schemas, defaults, limits and test sequences with complete expected host events.

```sh
python scenario/tools/build_demo.py
python -m vl_scenario_package validate scenario/examples/target-scenario --policy development --check-environment
python -m vl_scenario_package golden scenario/examples/target-scenario --policy development
python scenario/tools/independent_host.py scenario/reports/vlsp-target-scenario.zip
```

The last command runs the same ZIP on the independent host in development, offline and online modes. Online testing uses a temporary local HTTP server. No persistent keys are stored. See the [VLSP specification](../../scenario/spec/SPECIFICATION.en.md) for events, state, reset and error behavior.

## Receiving-platform adapter

The receiving platform needs an adapter to prepare inputs, run the package and handle results:

1. Declare the exact format, version, profile and capabilities. Reject unknown required features before importing code. Check the file list, graph, dependencies and rights separately.
2. Map cameras or streams to `source_id` and logical sessions to `run_id`. Use a strictly increasing `sequence` and a nondecreasing UTC Unix `event_time_ms`. Convert the source PTS explicitly.
3. Run the model and pass its actual result as `{classification:{label,score,category_id,output_index}}`. A JSON test fixture is not model inference. VLSP 0.1 does not define hidden model callbacks. Connect the public VLMP 0.2 profile through explicit mapping; original `model_links` remain at 0.1.
4. The host manages JSON state for each manifest, instance, source and run. It creates event envelopes and IDs, then commits events and state atomically after all checks pass. External delivery, outbox and deduplication are the responsibility of the integration.
5. Provide process isolation and filesystem, network and resource limits. Import namespaces and timeout checks performed after a call returns do not provide sandboxing or preemption. Both demonstration hosts run trusted Python code.
6. For commercial delivery, establish issuer trust independently of package contents. Validate an offline entitlement or online grant before decryption and import, then recheck the same lease before commit. Administrators are trusted. A protected model runner for the new public profile still needs integration.

## Attribution and branding

Add the optional `org.vl-exchange.attribution` extension to a VLMP or VLSP manifest to describe the publisher and one or more model or scenario authors. A supporting host validates and carries the declaration, and may present it in a live view, a derived video or a separate analytics export. Older hosts may ignore the extension. See the [Attribution & Branding profile](ATTRIBUTION.md) for the schema, policy, offline operation and trust boundaries. The specification's final project status does not establish support by third-party hosts.

## Protected end-to-end example

```sh
python tools/test_pipeline.py
```

This test runs real ONNX inference, then passes the classification result to protected VLSP in offline and online modes. It generates temporary recipient, installation, issuer and server keys, and removes the envelopes and plaintext afterwards. The protected delivery protocol is described [separately](../security/SPECIFICATION.en.md). This is a test of trusted prototypes, not a production guide to key management.

## AI assistance during migration

AI assistants can help write adapters, understand another API and move application logic. A shared interface can make that work easier to verify and reduce repeated integration effort while preserving competition between platforms and developers. AI does not replace agreement on event meaning and licensing, or testing the same unchanged package in independent systems. We do not claim a specific percentage saving.

## FAQ

**Must we use the VL platform or cloud?** No. A receiving platform implements the open interfaces and its own adapter. Offline delivery does not require a cloud service.

**Why use this alongside ONNX, APIs and ONVIF?** These technologies solve different parts of the problem. To transfer application behavior, platforms also need to agree on processing, state, event meaning and expected test results. VL Exchange complements existing technologies.

**Must source code and weights be public?** No. The interfaces are open; commercial rights and protected delivery are separate. Public examples use synthetic data. Final project licenses still await approval.

**Will a paid license automatically work on another host?** No. Portability is the intended commercial principle where the rights holder's terms allow it. Current delivery is bound to a recipient key. Moving it requires an authorized reissue; offline revocation is not immediate.

**Is every signed model safe?** No. A signature authenticates a key; it does not prove code is safe. The current runner assumes trusted publishers and administrators. The receiving platform must isolate untrusted code.

**What has been verified?** See the [compatibility matrix](COMPATIBILITY.md). Two local hosts do not establish support for another vendor's platform or for GPUs.
