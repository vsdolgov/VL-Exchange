# VL Exchange

**Experimental proposal by VL · English | [Русский](README.ru.md)**

[GitHub: vsdolgov/VL-Exchange](https://github.com/vsdolgov/VL-Exchange)

Video analytics should be easier to move between platforms.

A developer should be able to sell their solution on any compatible platform; a customer should be able to choose a platform without losing analytics they have already paid for; an integrator should be able to transfer working solutions instead of developing them again.

## Why it matters

A model file alone is not enough to move a working solution. The new platform also needs to know how to prepare images, interpret results and decide when to raise an event. For example, two platforms may use different rules for “a person in a zone.”

VL Exchange proposes shared package formats for this information. They build on existing technologies: ONNX describes model computations, RTSP carries media streams, and ONVIF defines interoperability interfaces. The packages add the information a platform needs to run the application.

AI can help developers understand another platform and write adapters. Shared formats can reduce repeated integration work further, while leaving platforms free to compete. Teams still need to agree on event meanings, licensing and tests; we do not claim a specific saving.

## How it works

- **VLMP (VL Model Package)** brings together a model, its dependencies, data processing, result descriptions and test examples.
- **VLSP (VL Scenario Package)** describes executable scenario logic, including its inputs, parameters, state and events.
- A platform supports a package by implementing its own adapter. It does not need a VL runtime or cloud service.

VLBP is VL’s separate internal block format. It is not required by the public interface, and neither is a particular visual editor or block architecture.

The interface specification is open. Models and application code may still be commercial, encrypted and licensed for offline or online use. A compatible platform does not automatically grant a license: the rights holder must allow the solution to be moved without another purchase. The current protected-delivery prototype ties a delivery to an installation, so a move requires an authorized reissue of licenses and keys; copying a secret does not transfer rights.

## Current status

This is an experimental proposal for discussion, **not an approved industry standard**. We have not tested compatibility with other vendors’ platforms. The examples use synthetic data and do not demonstrate the quality of industrial detectors.

| Component | Version and profile | What has been tested |
|---|---|---|
| Original VLMP pilot | 0.1.0, CustomModelProtocol / CPU ONNX FP32 | A historical local pilot using an actual Box 5 loader. Its private SDK is not included here. |
| Public VLMP | **0.2.0-draft.1**, python-numpy-classification-v1 | A standalone example with two local loaders using the same ONNX Runtime, without private SDKs. This profile has not been tested with Box 5. |
| VLSP | 0.1.0, python-scenario-json-v1 | Two separately implemented hosts run a classification example and produce JSON events and state, without VLBP. |
| Protected delivery | 0.1.0 | A prototype for encryption, signatures and offline/online permissions, using a local test service. It is not a sandbox or a production license server. |

The public VLMP profile is a new proposal, separate from the earlier 0.1 pilot. It does not inherit the pilot’s Box 5 result. These profiles do not claim support for arbitrary models, GPUs, tracks or every type of video analytics. See the [compatibility matrix](docs/en/COMPATIBILITY.md) for results and commands.

## Quick start

CPython 3.12 and public dependencies are required. From the repository root:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="model/src:scenario/src:security-runtime/src"
python model/src/public_model.py validate model/examples/channel-classifier
python model/src/public_model.py golden model/examples/channel-classifier
python model/tools/independent_host.py model/examples/channel-classifier
python tools/model_archive_roundtrip.py
python -m vl_scenario_package golden scenario/examples/target-scenario --policy development
python scenario/tools/independent_host.py scenario/reports/vlsp-target-scenario.zip
python -m pytest -q
python tools/check_links.py
```

Online tests bind HTTP only to loopback; the environment must permit local sockets. `--policy development` is explicit for the open example; the default operational CLI path requires protected delivery. Public examples contain no persistent keys: tests generate temporary keys and remove decrypted data.

## Documentation

| Task | English | Русский |
|---|---|---|
| Public VLMP: exact contract | [Specification](model/spec/SPECIFICATION.en.md) | [Спецификация](model/spec/SPECIFICATION.ru.md) |
| VLSP: code, state, events | [Specification](scenario/spec/SPECIFICATION.en.md) | [Спецификация](scenario/spec/SPECIFICATION.md) |
| Create a package and platform adapter | [Guide](docs/en/GUIDE.md) | [Руководство](docs/ru/GUIDE.md) |
| Compatibility and results | [Matrix](docs/en/COMPATIBILITY.md) | [Матрица](docs/ru/COMPATIBILITY.md) |
| Protection and licensing | [Profile](docs/security/SPECIFICATION.en.md) | [Профиль](docs/security/SPECIFICATION.ru.md) |
| Threat model | [Limitations](docs/security/THREAT-MODEL.en.md) | [Ограничения](docs/security/THREAT-MODEL.ru.md) |
| Development rules | [Governance](docs/governance/GOVERNANCE.en.md) | [Governance](docs/governance/GOVERNANCE.ru.md) |
| Project licenses — pending approval | [Proposal](docs/governance/LICENSE-PROPOSAL.en.md) | [Предложение](docs/governance/LICENSE-PROPOSAL.ru.md) |
| First external pilot | [Plan](docs/governance/PILOT.en.md) | [План](docs/governance/PILOT.ru.md) |

Historical VLMP0.1 for implementers: [full English specification](specs/vlmp/0.1/SPECIFICATION.en.md) / [Русский](specs/vlmp/0.1/SPECIFICATION.ru.md). This profile requires an external SDK that is not redistributed here.

[Public VLMP JSON Schema](model/spec/model-package.schema.json), [VLSP JSON Schema](scenario/spec/scenario-package.schema.json), [message contracts](scenario/spec/schemas), [original VLMP 0.1 schema](specs/vlmp/0.1/model-package.schema.json).

## Participation and rights

Developers, platform vendors and integrators are invited to suggest interfaces and test one unchanged package on their systems. You do not need to share commercial model weights or scenario code: a synthetic example, an adapter and a reproducible report with limitations are enough to start a discussion.

The final licenses for code, schemas and text, and the patent policy, are still pending. Publishing the source does not grant the proposed Apache-2.0 or CC-BY-4.0 rights. There is no effective LICENSE file yet; see the [licensing proposal](docs/governance/LICENSE-PROPOSAL.en.md). Do not add keys, customer recordings, private SDKs or model weights without permission. See the [contribution rules](CONTRIBUTING.md) and [changelog](CHANGELOG.md).
