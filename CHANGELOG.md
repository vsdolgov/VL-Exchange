# Changelog

English | [Русский](CHANGELOG.ru.md)

## 2026-11-03 — Final VL Exchange project editions

Published final project editions of the specifications and documented the scope, retained pre-release identifiers and verification limits in the [release note](docs/governance/SPECIFICATION-RELEASE-2026-11-03.en.md). This does not establish industry recognition, production readiness or additional license rights. Existing package bytes and wire identifiers remain unchanged.

## 2026-11-02 — VLSP 0.2 and detection drafts

Added experimental VLSP 0.2 specifications for multiple model and tracker services, error/lifecycle rules, optional input queues and asynchronous MLLM jobs. Added a separate VLMP detection draft, synthetic examples, selected reference components and tests. The historical VideoPipe and queue comparison is recorded with its limitations; it does not establish async support in VideoPipe or production performance. Detection and the earlier classification proposal have different draft identifiers and schemas. See the [0.2 overview](drafts/v0.2/README.md).

## 2026-11-01 — Draft / Experimental publication candidate

Prepared a standalone bilingual repository. VLSP0.1 and the common security profile were curated without private platform source; the public security API defaults to a scenario manifest. Added **proposed** VLMP0.2.0-draft.1 / python-numpy-classification-v1 for reproduction without a private SDK; it neither replaces VLMP0.1 nor has been tested with Box5. Added two independent model hosts, two scenario hosts, synthetic ZIPs, conformance/security tests, actual inference→protected scenario integration and GitHub Actions. Publication of the material set in vsdolgov/VL-Exchange is approved; licenses and patent policy remain pending. README links the repository to the published initiative, technical portal and article.

Displayed company branding is **VL**. External company-domain links have been removed. The legacy dependency-deny rule is preserved by exact root-digest matching without embedding the old name. The scenario docstring branding changed package bytes, so the synthetic solution was rebuilt as **1.0.1** with a new manifest/ZIP identity; VLSP format0.1, Python ABI and runtime behavior are unchanged. Current conformance reports were rerun; earlier evidence was preserved separately without relabeling it.

## Local pilot history

2026-10-30: VLMP0.1/CustomModelProtocol was investigated with the actual Box5 loader. 2026-10-31: VLSP0.1 and protected offline/online prototypes were tested locally. Private libraries, test-host commands and platform source are excluded from the public project; historical results do not automatically apply to the new profile.
