# Contributing to VL Exchange

**Draft · English | [Русский](CONTRIBUTING.ru.md)**

Start with the problem you want to solve and a small synthetic example.

For a new interface, describe its version and profile, inputs and outputs, state and timing, errors, security risks, and compatibility. Include matching English and Russian texts, a JSON Schema, and golden tests.

Propose breaking changes separately and explain how existing users can migrate. Do not change published files without changing their version.

## Check your changes

Set up the environment described in the [README](README.md), then run `python tools/verify.py`. Online licensing tests need a loopback connection. Do not report a network failure as a pass.

Run the same package on two independently implemented hosts. Keep the files and tolerances unchanged, and record the versions and digests.

## What to share

Do not publish commercial model weights, customer data, private SDKs, license keys, or third-party code without redistribution rights. You do not have to disclose your commercial implementation to contribute.

The project's inbound and outbound licenses are still awaiting approval. Draft terms do not grant rights.

See [change governance](docs/governance/GOVERNANCE.en.md), [the first partner pilot](docs/governance/PILOT.en.md), and the [license proposal](docs/governance/LICENSE-PROPOSAL.en.md).
