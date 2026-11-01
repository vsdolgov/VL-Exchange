# Evolving VL Exchange

[Русский](GOVERNANCE.ru.md) · Proposal, Draft / Experimental

VL is the initiator. The project is not industry-adopted, no working group has been established, and no external participation or consent is claimed. Implementations require no private SDK, visual editor or VL cloud. Commercial packages may remain proprietary.

Each format has its own version and named profile: format, runtime and business-solution versions are distinct. In 0.x changes may break compatibility but require a new version/profile ID and migration notes; published contracts are not silently rewritten. For 1.x we propose MAJOR for breaking changes, MINOR for compatible extensions, PATCH for fixes without wire-contract changes. Strict parsers reject unknown required capabilities rather than guessing profiles.

A change proposal includes problem, use case, alternatives, compatibility impact, RU/EN text, schemas, positive/negative vectors and migration plan. Authors need not contribute proprietary code or weights. Public review lasts at least 14 calendar days, or 30 for disputed breaking changes. These periods are proposed, not an existing organizational policy.

Until a working group exists, the VL maintainer records decisions and reasons publicly. After two independent organizations join, a technical council is proposed: one representative per organization, disclosed conflicts, two-thirds quorum and two-thirds agreement of participants, without unilateral vendor veto. Membership, authority and patent terms must be agreed before this procedure applies.

A release candidate includes immutable commit/tag, changelog, schemas and demo checksums. Stable publication requires two independently developed hosts testing the same unchanged ZIP, documented environments and a public report. Shared runtime/crypto dependencies are disclosed. Two local hosts are not described as two commercial platforms. Schema conformance and publisher signature alone confer neither compatibility nor security certification.

The corporate site and technical portal identify the exact release/profile and link to the same tagged GitHub source. Future compatibility claims must refer to a specific solution version rather than being inferred from ONNX availability; unfilled export statuses are hidden in the public catalog. Historical tests and current public-profile results remain separate. The project repository is [vsdolgov/VL-Exchange](https://github.com/vsdolgov/VL-Exchange). A vulnerability contact still requires approval; do not post exploits or secrets in public issues.
