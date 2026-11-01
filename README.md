# VL Exchange

**Draft / Experimental · initiated by VL · English | [Русский](README.ru.md)**

[GitHub: vsdolgov/VL-Exchange](https://github.com/vsdolgov/VL-Exchange)

A developer should be able to sell their solution on any compatible platform; a customer should be able to choose a platform without losing analytics they have already paid for; an integrator should be able to transfer working solutions instead of developing them again.

Changing a platform often means rebuilding video analytics that already work. A neural network can be exported, but channel order, normalization, classes, thresholds, coordinates, scenario state and event conditions do not travel with the network file. The same phrase, “person in a zone,” may represent different conditions, time intervals and duplicate suppression rules.

The engineering chain is **model → input data → preprocessing → inference → postprocessing → context → scenario → event**. ONNX describes model computations, RTSP helps carry media streams and ONVIF defines its own interoperability interfaces; the receiving platform still needs an unambiguous application contract. VL Exchange proposes this contract on top of existing technologies.

AI assistants can simplify writing adapters, understanding another API and moving application logic. Our position is that a shared contract lets this assistance produce verifiable results and reduce repeated integration effort while preserving competition between platforms and developers. AI does not replace agreement on event semantics, licensing terms or tests of one unchanged package in independent systems; no specific percentage saving is claimed.

## The proposal

**VLMP — VL Model Package** binds a model, dependencies, executable data processing, result semantics and golden examples. **VLSP — VL Scenario Package** transfers executable scenario logic with contracts for inputs, parameters, state and events. Each receiving vendor implements a compatible adapter on their own platform. Neither a VL runtime nor a VL cloud service is mandatory.

VLBP is a separate internal VL block format. It is outside the public inter-vendor contract; neither the visual editor nor the block architecture is required.

The interface specification is open. Models and application code may remain commercial and be delivered encrypted with offline or online execution rights. Technical compatibility does not transfer a license automatically: switching platforms without buying analytics again must be permitted by the rights holder's agreement. The current protected-delivery prototype binds a delivery to an installation; moving it requires an authorized reissue of licenses and keys. Copying an old secret does not transfer rights.

## Status and scope

This is a VL proposal for industry discussion, not an approved industry standard. Compatibility with third-party vendor platforms has not been tested. Examples are synthetic and do not demonstrate industrial detector quality.

| Component | Version and profile | Status |
|---|---|---|
| Original VLMP pilot | 0.1.0, CustomModelProtocol / CPU ONNX FP32 | Historical local pilot with an actual Box 5 loader; its private SDK is excluded from this repository |
| Public VLMP | **proposal 0.2.0-draft.1**, python-numpy-classification-v1 | Standalone example without private SDKs; two local loaders, shared ONNX Runtime; this profile has not been verified with Box 5 |
| VLSP | 0.1.0, python-scenario-json-v1 | Two separately implemented hosts, Classification → JSON events/state; no VLBP |
| Protected delivery | 0.1.0 | Encryption, signatures, offline entitlement and online grants with a loopback test service; neither a sandbox nor a production license server |

The public VLMP profile is explicitly a new proposal: it does not modify the previously tested 0.1 package and does not inherit its Box 5 claim. Profiles do not promise arbitrary models, GPUs, tracks or all forms of video analytics. Verified results and commands are in the [compatibility matrix](docs/en/COMPATIBILITY.md).

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

Model developers, platform vendors and integrators are invited to propose contracts and run one unchanged package on their systems. Public participation does not require transferring commercial weights or scenario logic. A synthetic example, adapter and reproducible report with limitations are sufficient for discussion.

The project repository is [vsdolgov/VL-Exchange](https://github.com/vsdolgov/VL-Exchange). Publication of this material set has been approved. Final licenses for code, schemas and text, and the patent policy, remain unapproved: publishing source does not grant the proposed Apache-2.0 or CC-BY-4.0 permissions. There is no effective LICENSE file; the [licensing proposal](docs/governance/LICENSE-PROPOSAL.en.md) remains pending. Do not add keys, customer recordings, private SDKs or weights without permission. See [contribution rules](CONTRIBUTING.md) and [changes](CHANGELOG.md).
