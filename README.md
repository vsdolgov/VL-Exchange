# VL Exchange

**A VL initiative · English | [Русский](README.ru.md)**

VL Exchange is a project developing an **open standard for exchanging video analytics models and scenarios** between compatible platforms. This repository contains the project's final specifications, reference implementations, examples and tests; individual prototypes and other documents may still be experimental.

Our goal: a developer can sell a solution on any compatible platform, a customer can choose a platform without losing analytics already paid for, and an integrator can transfer a working solution instead of building it again. Portability also depends on the solution owner's license.

The exchange specifications are intended to be open, without a commercial license. The precise legal licenses for this project's specifications, schemas and code have not yet been approved; publication alone does not grant the proposed license terms. The intention is that platforms can implement the interfaces in commercial products, and models and scenarios can have their own licenses. See the [license proposal](docs/governance/LICENSE-PROPOSAL.en.md).

## Why this matters

A model file is only part of a working solution. A receiving platform also needs to know how to prepare input, interpret the result and decide when to create an event. For example, platforms may use different rules for “a person in a zone.” This is an illustrative use case, not a ready-made detector in this repository.

AI can help developers understand another platform and write adapters. Shared exchange rules can reduce repeated integration work while platforms continue to compete on their products. Partners still need to agree on event meanings, licenses and tests.

## How it works

1. A developer prepares a package with a model or scenario and its instructions.
2. A platform accepts the package through its own adapter — the component that connects the package to that platform.
3. The model returns a result; the scenario uses results and context to create events.

**VLMP (VL Model Package)** packages a model, its dependencies, data processing, result descriptions and test examples. **VLSP (VL Scenario Package)** packages executable scenario logic with its inputs, parameters, state and events. An adapter can be implemented independently; a VL runtime, cloud or visual editor is not required. The separate internal VL block format VLBP is not part of the public exchange interface.

VL Exchange complements ONNX, RTSP and ONVIF: those technologies address model computation, video transport and device interoperability; the packages add application-level information needed to run the solution.

## Attribution and branding

An optional profile, finalized within VL Exchange, lets a model or scenario package describe its publisher, authors, preferred credit and display policy. A receiving platform that supports it can carry this information in analytics exports and show a credit such as `Analytics by Detector Lab` on a live view or a derived video. Different model, scenario and platform authors remain distinct. The profile works without a network connection; an older host may ignore it. Support by third-party platforms is not yet established. See the [Attribution & Branding profile](docs/en/ATTRIBUTION.md).

## A real example in this repository

The public demo uses **synthetic colour classification**, not a person detector. The model example produces labels such as `red`, `green` and `blue`. In the scenario example, two consecutive `red` results for the same source and run, each with a score of at least 0.5, trigger a JSON event of type `org.vlexchange.target-observed.v1` with `label`, `score` and `count` in its payload. See the [model example](model/examples/channel-classifier) and the [scenario test sequence](scenario/examples/target-scenario/golden/basic.json).

The same idea can later describe other scenarios, such as a person in a zone, after suitable models, profiles and tests are implemented. The example does not establish detector quality or compatibility with another vendor's platform.

## What is available today

- [VLMP 0.2.0](drafts/v0.2/vlmp-unified/README.md) defines one package format with classification and detection profiles. Both can be tested locally on two independently implemented hosts; each host was also tested with one ZIP of each earlier format.
- Local tests exercise two separately implemented hosts. The public VLMP example uses the same ONNX Runtime in both; the historical Box 5 pilot used a different VLMP profile.
- A protected-delivery prototype tests signatures, encryption and online/offline execution rights with a local test service. It is not a sandbox or a production license server.
- The [VLSP 0.2 release](drafts/v0.2/README.md) describes a multi-model scenario, optional asynchronous MLLM and input-queue profiles, and a separate VLMP detection profile. VideoPipe is an example of another execution environment used to demonstrate portability; the comparison covered the synchronous scenario, not asynchronous MLLM. The prototypes and pilot remain experimental.

These specifications are final within VL Exchange; they are **not an approved industry standard**, and compatibility with other vendors' platforms has not been tested. The public VLMP classification profile `0.2.0-draft.1` has not been tested with Box 5 and does not inherit the earlier 0.1 pilot's result. VLSP 0.2 describes tracker services, but does not claim support for arbitrary models, GPUs, trackers or all video analytics. The `draft` fragments in the existing classification and detection wire identifiers remain unchanged for compatibility; they do not indicate the current status of their specification documents. See the [compatibility matrix](docs/en/COMPATIBILITY.md) for profiles, tests and limits.

The [specification release note](docs/governance/SPECIFICATION-RELEASE-2026-11-03.en.md) records its scope, retained pre-release format identifiers, evidence and outstanding checks.

## Try the examples

The commands below are for macOS or Linux and require Git, CPython 3.12 and internet access to install the public dependencies. From a terminal:

```sh
git clone https://github.com/vsdolgov/VL-Exchange.git
cd VL-Exchange
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export PYTHONDONTWRITEBYTECODE=1
python model/src/public_model.py golden model/examples/channel-classifier
PYTHONPATH=scenario/src python -m vl_scenario_package golden scenario/examples/target-scenario --policy development
```

Both checks print a JSON report containing `"status": "pass"` on success. The first compares model outputs with expected results. The second checks scenario events and state using prepared JSON classification results; it does not call the model. The model-to-event chain above explains the intended integration; a separate [pipeline report](reports/pipeline.json) records a local test with an actual model result. These are synthetic local examples, not a test of an external platform. `--policy development` permits the plaintext example; the default operational CLI path requires protected delivery. For full checks, setup details and adapter instructions, see the [integration guide](docs/en/GUIDE.md) and [compatibility matrix](docs/en/COMPATIBILITY.md).

## Where to go next

- **Model or scenario developer:** inspect the [examples](model/examples/channel-classifier) and [packaging guide](docs/en/GUIDE.md).
- **Platform developer:** read the [adapter guide](docs/en/GUIDE.md) and the [VLMP](model/spec/SPECIFICATION.en.md) and [VLSP](scenario/spec/SPECIFICATION.en.md) specifications.
- **Integrator or potential pilot partner:** review [what has been tested](docs/en/COMPATIBILITY.md) and [the first pilot plan](docs/governance/PILOT.en.md).
- **Questions or proposals:** open a [GitHub issue](https://github.com/vsdolgov/VL-Exchange/issues).

## Documentation

| Task | English | Русский |
|---|---|---|
| VLMP 0.2.0: classification and detection | [Specification](drafts/v0.2/vlmp-unified/spec/SPECIFICATION.en.md) | [Спецификация](drafts/v0.2/vlmp-unified/spec/SPECIFICATION.ru.md) |
| Previous classification profile | [Specification](model/spec/SPECIFICATION.en.md) | [Спецификация](model/spec/SPECIFICATION.ru.md) |
| VLSP: code, state, events | [Specification](scenario/spec/SPECIFICATION.en.md) | [Спецификация](scenario/spec/SPECIFICATION.md) |
| VLSP 0.2 and VLMP detection specifications | [Overview](drafts/v0.2/README.md) | [Обзор](drafts/v0.2/README.ru.md) |
| Create a package and platform adapter | [Guide](docs/en/GUIDE.md) | [Руководство](docs/ru/GUIDE.md) |
| Compatibility and results | [Matrix](docs/en/COMPATIBILITY.md) | [Матрица](docs/ru/COMPATIBILITY.md) |
| Attribution & Branding | [Profile](docs/en/ATTRIBUTION.md) | [Профиль](docs/ru/ATTRIBUTION.md) |
| Protection and licensing | [Profile](docs/security/SPECIFICATION.en.md) | [Профиль](docs/security/SPECIFICATION.ru.md) |
| Threat model | [Limitations](docs/security/THREAT-MODEL.en.md) | [Ограничения](docs/security/THREAT-MODEL.ru.md) |
| Development rules | [Governance](docs/governance/GOVERNANCE.en.md) | [Governance](docs/governance/GOVERNANCE.ru.md) |
| Project licenses — pending approval | [Proposal](docs/governance/LICENSE-PROPOSAL.en.md) | [Предложение](docs/governance/LICENSE-PROPOSAL.ru.md) |
| First external pilot | [Plan](docs/governance/PILOT.en.md) | [План](docs/governance/PILOT.ru.md) |

Historical VLMP0.1 for implementers: [full English specification](specs/vlmp/0.1/SPECIFICATION.en.md) / [Русский](specs/vlmp/0.1/SPECIFICATION.ru.md). This profile requires an external SDK that is not redistributed here.

[Public VLMP JSON Schema](model/spec/model-package.schema.json), [VLSP JSON Schema](scenario/spec/scenario-package.schema.json), [message contracts](scenario/spec/schemas), [original VLMP 0.1 schema](specs/vlmp/0.1/model-package.schema.json).

## Participation and rights

Developers, platform vendors and integrators are invited to propose interfaces and test an unchanged package on their systems. Participation does not require sharing commercial model weights or scenario code. A synthetic example, an adapter and a reproducible report with limitations are enough to start a discussion.

The standard's exchange specifications are intended to remain open and not require a commercial license. The project's exact licenses for specifications, schemas and code, and its patent policy, are still pending. There is no effective LICENSE file yet; see the [license proposal](docs/governance/LICENSE-PROPOSAL.en.md). A commercial package remains subject to its owner's terms. In the current protected-delivery prototype, moving an installation requires an authorized reissue of licenses and keys; copying a secret does not transfer rights. Do not add keys, customer recordings, private SDKs or model weights without permission. See the [contribution rules](CONTRIBUTING.md) and [changelog](CHANGELOG.md).
