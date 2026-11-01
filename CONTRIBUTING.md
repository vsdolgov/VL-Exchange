# Contributing to VL Exchange

**Draft · English | [Русский](CONTRIBUTING.ru.md)**

Start with a problem statement and a minimal synthetic example. A new contract proposal specifies versions/profiles, inputs/outputs, state/time, failure semantics, threats and compatibility. Include equivalent RU/EN text, JSON Schema and golden tests. Propose incompatible changes separately with a migration plan; never replace published bytes under the same version.

Run `python tools/verify.py` in the README environment. Checks require loopback for online licensing; network failures are not PASS. Give one unchanged package to two separately implemented hosts, preserving files and tolerances, and record versions/digests.

Do not publish commercial weights, customer data, private SDKs, license keys or third-party code without redistribution rights. Participation does not require disclosure of commercial implementations. Final outbound/inbound licenses are pending approval; draft terms must not be represented as granted rights.

See [change governance](docs/governance/GOVERNANCE.en.md), [the first partner pilot](docs/governance/PILOT.en.md) and [license proposal](docs/governance/LICENSE-PROPOSAL.en.md).
