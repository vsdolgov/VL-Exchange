# VL Exchange — VLSP v0.1: checklist внешнего сценария

Статусы PASS / FAIL / NOT TESTED. Требование не является результатом испытания. [Спецификация](SPECIFICATION.md) и [общий security profile](../../docs/security/SPECIFICATION.ru.md) применяются отдельно.

## A. Самостоятельность и integrity

- **A01** Manifest strict JSON0.1.0/format vl.scenario-package; schema, duplicate/nonfinite/scalar-Unicode проверки пройдены.
- **A02** Exact manifest/file hashes совпадают; inventory полный; unsafe paths/symlinks/unlisted/modified files отклонены до импорта.
- **A03** Включены scenario.py/helpers; пакет и executor не требуют VLBP, NRI, block editor или proprietary SDK.
- **A04** Scenario ID/version неизменны для опубликованных bytes; runtime Python/dependencies constraints проверены и actual versions записаны.
- **A05** Input и event declarations/semantic meanings/schema согласованы; state/payload schemas self-contained без ref/id.
- **A06** Required VLMP links проверены по integrity/declared contract; graph/inference false явно указаны, если они не исполнялись.

## B. Process и host event boundary

- **B01** Constructor получает только validated params/defaults/overrides, без скрытых kwargs/model manager.
- **B02** Inputs/context/state deep-copied; inputs/context mutation запрещена; raw return содержит только candidates+state.
- **B03** Candidate type/payload/extras проверены; host сам формирует event_id/source/run/sequence/time и full envelope.
- **B04** Context использует UTC Unix ms; sequence/time валидируются per source/run; invalid call не меняет ordering/state.
- **B05** States источников/runs/instances независимы; reset одного namespace не сбрасывает другой; fatal instance не оживляется reset.
- **B06** Limits проверены: input/state/event bytes, event count, measured process deadline. Hard preemption/sandbox не заявлены без реализации.
- **B07** No events/state commit при exception, bad output/state, timeout или expired lease; stale output и implicit fallback отсутствуют.

## C. Детерминированность и события

- **C01** Threshold boundary, третий matching input, разрыв серии и несколько sources выполнены по golden.
- **C02** Один source с разными run_id создаёт независимые state и event IDs.
- **C03** Reset/replay того же run_id даёт тот же event ID; новый логический run выбирает новый host run_id.
- **C04** Length-prefixed UTF-8 algorithm совпал с event-id-test-vector.json, включая Unicode, strict encoding/no normalization и integer decimal representation.
- **C05** Два независимых host совпали по full events и state при тех же inputs/run_id; общий crypto допустим, общий scenario executor не считается независимым.
- **C06** Event objects/IDs не выданы за exactly-once доставку, durable outbox или бизнес-ground-truth.
- **C07** Unknown event/required extension/capability, duplicate order, bad parameters и отрицательные integrity tests выполнены.

## D. Production protection

- **D01** Plaintext допускается только explicit development policy; production path требует protected envelope.
- **D02** Offline entitlement и online grant положительные проверки выполнены с admin-provided trust/installation keys.
- **D03** Неверные подписи/ключ/subject/digests/rights/сроки и неподдерживаемый licensing component не доходят до code import.
- **D04** Online nonce/request/grant bindings, server refusal/revocation/network failure/TLS policy проверены, нет downgrade в offline.
- **D05** ExecutionLease фиксируется перед вызовом и тот же lease проверяется перед commit; renewal не авторизует завершившийся после expiry вызов.
- **D06** Trusted publisher/admin модель описана; нет Python sandbox утверждения; untrusted scripts не разделяют ключи/plaintext/process identity.
- **D07** Внешние VLMP bytes/лицензии не считаются защищёнными автоматически ZIP сценария; секретов в артефактах нет.

## E. Доказательства и границы

- **E01** Настоящий VLMP вызван отдельно и его фактический Classification передан защищённому VLSP; fixtures не выданы за inference.
- **E02** До/после независимых запусков пакет неизменён; reports содержат package/host/security versions и digests.
- **E03** Source analysis, execution и production deployment различены; NOT TESTED не превращён в PASS.
- **E04** Не заявлены поддержка конкурентами, универсальная GPU переносимость или готовность всех сценариев.

Заявление о результате ограничивается реально проверенными пунктами/профилями. Сам по себе этот checklist не сертифицирует реализацию.
