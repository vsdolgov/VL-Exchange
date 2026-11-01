# VLSP0.1 conformance checklist

[Русский](CONFORMANCE.md) · [Specification](SPECIFICATION.en.md) · [Security](../../docs/security/SPECIFICATION.en.md)

Use PASS / FAIL / NOT TESTED. Requirements are not test results; a checklist alone is not certification.

A01 strict format/version/schema/JSON/Unicode; A02 complete manifest/file identity and path safety before import; A03 self-contained code without editor/private SDK; A04 immutable ID/version, actual runtime versions; A05 declared semantics and self-contained payload/state schemas; A06 exact required model links, distinguishing graph/inference not performed.

B01 constructor only gets validated defaults/overrides; B02 detached inputs/context/state and no input/context mutation; B03 validate candidates and let host create envelope/ID/source/run/time; B04 UTC Unix time and per-source/run ordering, invalid calls make no progress; B05 isolated states and reset, fatal instances cannot revive; B06 sizes/count/deadline checked without claiming preemption; B07 no commit on error/timeout/expired lease and no stale/fallback result.

C01 threshold boundary/third hit/streak break/multiple sources; C02 independent runs; C03 reset/replay same ID and new runs new IDs; C04 exact length-prefixed strict UTF-8 Unicode vector with no normalization; C05 independent hosts agree on complete events/state without sharing executor; C06 no exactly-once/outbox/ground-truth claim; C07 unknown event/required extension/capability, duplicate order, parameters and integrity negatives.

D01 explicit development-only plaintext, protected production; D02 offline/online positives with administrator-supplied trust; D03 bad signatures/keys/subject/digests/rights/time fail before import; D04 nonce/grant/refusal/revocation/network/TLS policy without offline downgrade; D05 immutable pre-call lease rechecked before commit despite renewal; D06 trusted publisher/admin assumptions and no sandbox claim; D07 external VLMP bytes/licenses not automatically protected and no secrets in artifacts.

E01 actual VLMP inference into protected VLSP, fixtures are not inference; E02 unchanged package across hosts and recorded versions/digests; E03 distinguish source analysis, execution and deployment; E04 no competitor/GPU/universal portability claim. Limit any result claim to the items actually tested.
