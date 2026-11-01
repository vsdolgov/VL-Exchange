# First cross-vendor pilot

[Русский](PILOT.ru.md) · Plan, not an executed test

The goal is one unchanged package executed by VL and a partner's independently implemented host, with comparable results and preservation of the customer's agreed commercial rights. Participant, dates and commitments are unagreed. Prior contacts do not imply participation consent.

## Partner handoff

Provide an approved Draft tag, both specifications and schemas, an open synthetic ZIP, manifest/inventory/digest, golden inputs and expected outputs, negative vectors, quick start and minimal host. Disclose shared crypto/runtime stack and profile limits. Treat original SDK-based VLMP0.1 and the new public experimental profile as distinct; the public path must not require private SDK access.

The partner supplies declared Python/OS/CPU/runtime versions, input/context and event adapters, independent host lifecycle, trust-root policy, safe worker and offline/online installation description. No visual-editor or internal-block migration is required. For commercial-protection tests the publisher issues separate test keys and entitlements without production secrets or customer data.

## Sequence and acceptance

1. Agree exact profile/version, distribution rights, comparison and publication criteria. Set ONNX atol/rtol in advance; compare events, IDs, order errors and state according to their contracts.
2. Record ZIP and file SHA-256 before handoff. The partner implements a host independently of the reference executor and discloses dependencies/shared components.
3. Run the same ZIP on both systems: VLMP golden inputs, repetitions/order; full VLSP sequence, reset, Unicode IDs, state and errors. Recheck package digest afterwards.
4. Test offline and online: wrong keys/signatures, tamper, expiry, replay, revocation, server failure and expiry during calls. Scenarios must not publish external events before supervisor commit.
5. Test worker isolation and resource limits separately; successful unit tests do not establish those guarantees.
6. Exercise migration to a new installation with envelope reissuance and the same original ZIP; confirm contractual rights without repurchasing analytics. For offline document revocation limits and agreed deactivation.
7. Success requires all mandatory vectors to pass without changing ZIP, results within agreed tolerances, documented failures/limits and reproducibility by another engineer.

The public report contains dates, agreed participant names, profile/version, commit, environments, commands, hashes, result tables and known limits. Publish partner names/logos only with consent. Exclude industrial-system addresses, keys, personal-data logs and commercial weights.

A proposed schedule after agreement is week 1 for contracts/adapters, week 2 for joint tests, week 3 for migration/security and reporting. This is an assistant estimate, not a party commitment or launch date.
