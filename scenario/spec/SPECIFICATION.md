# VL Exchange — VL Scenario Package (VLSP) v0.1

**Версия: 0.1.0. Статус: открытый нормативный проект интерфейса для пилота, 31.10.2026.** MUST / ДОЛЖЕН, MUST NOT / НЕ ДОЛЖЕН и MAY / МОЖЕТ обозначают требование, запрет и возможность. Документ не утверждает поддержку формата сторонними производителями или готовность промышленного развёртывания. Проверенные результаты хранятся отдельно.

## Экспериментальное расширение атрибуции

Издатель может добавить необязательный элемент `org.vl-exchange.attribution` в `extensions` с `required: false`. Исполнитель, поддерживающий отдельный [профиль Attribution & Branding](../../docs/ru/ATTRIBUTION.md), может проверить и передавать сведения об издателе и авторах. Исполнитель без такой поддержки может проигнорировать расширение. Оно не меняет базовую схему события и существующие golden-примеры: для передачи атрибуции платформа может использовать отдельную оболочку экспорта. Отображение и передача требуют поддержки платформы.

## 1. Решение и область применения

VLSP — самостоятельная исполняемая поставка **Python-кода сценария** с открытым описанием его внешних входов, параметров, состояния, событий, зависимостей и ошибок. Она является межвендорной частью **VL Exchange** вместе с **VL Model Package (VLMP)**. Код эксплуатационной поставки не публикуется автоматически: открыта спецификация интерфейса, а исполнение лицензируется.

Внешнему получателю не нужны VLBP, граф блоков, редактор VL, NRI, proprietary models_manager или SDK VL. Пакет сценария и его потребитель не должны импортировать VLBP или физически зависеть от его файлов. Внутренний маркетплейс компонентов VL — отдельная область. Host реализует описанный ниже небольшой Python API и связывает с ним свою систему.

Первый профиль `python-scenario-json-v1` ограничен одним Classification input, упорядоченными синхронными вызовами, JSON state по источнику и логическому запуску, и JSON event candidates. Он не обещает переносимость произвольного Python, всех сценариев видеоаналитики или GPU. Видеофреймы, треки, геометрия, самостоятельное декодирование видео, графы, асинхронные callback и автономные таймеры потребуют отдельных профилей.

## 2. Пакет и manifest

```text
package-root/
├── scenario-package.json
├── code/
│   ├── __init__.py
│   └── scenario.py
└── golden/
    └── basic.json
```

`scenario-package.json` — strict UTF-8 JSON по [scenario-package.schema.json](scenario-package.schema.json). `format="vl.scenario-package"`, `spec_version="0.1.0"`. Duplicate keys, comments, NaN/Infinity и unknown core fields запрещены. Все строки содержат Unicode scalar values; unpaired surrogates отклоняются, UTF-8 кодируется strict, implicit Unicode normalization не выполняется.

`scenario_id` — namespaced ID, `version` — версия поставки/поведения. Издатель НЕ ДОЛЖЕН повторно публиковать ту же пару scenario_id/version с другими байтами. Точная идентичность — SHA-256 исходного manifest и каждого файла из `files`; имён/номеров без digest недостаточно.

| Поле | Интерфейс |
|---|---|
| `entrypoint` | Python package root, scenario.py, класс Scenario, версия ABI |
| `files` | Полный inventory `relative path → SHA-256` |
| `runtime` | PEP 440 требование к Python |
| `input_contract` | Имя входа, message type, clock domain |
| `event_contract` | Алгоритм identity, разрешённые event types и schemas их payload |
| `parameters` | Типы/defaults/ограничения внешних параметров |
| `state`, `ordering`, `limits` | Владение состоянием, порядок, deadline/size/count пределы |
| `dependencies`, `model_links` | Python packages и явные VLMP dependencies |
| `required_capabilities`, `extensions` | Negotiated возможности без silent fallback |
| `golden` | Проверочные последовательности и ожидаемые host events/state/errors |
| `delivery` | Production требует общего protected envelope; plaintext — только development |

Каждый обычный файл, кроме manifest, перечисляется в `files`, включая helpers, документацию и golden fixtures. Все относительные POSIX-пути разрешаются внутри package root; symlinks, absolute paths, `..`, `.`, пустые сегменты и glob запрещены. Entrypoint root содержит `__init__.py` и `scenario.py`. Неперечисленные/пропавшие/изменённые файлы отклоняются до импорта.

Исполнитель не изменяет поставку и не пишет в неё bytecode, временные данные или отчёты. Hashes проверяются до Python import. Изменение manifest/fixtures/tolerances — новая идентичность поставки. Digest — контроль целостности; авторство и право запуска определяются отдельно security profile.

Принимающая система ДОЛЖНА исключить изменение проверенных файлов между проверкой и импортом и на срок использования: например, разместить частную копию в каталоге, недоступном посторонним процессам, и применить подходящие права или read-only mount. Раздельные операции чтения хеша и импорта сами по себе не дают защиты от подмены между ними. У эталонного security-компонента есть приватный временный каталог; доверенные администратор и издатель находятся вне заявленной модели атакующего. Development-каталог, доступный на запись постороннему процессу, такой гарантии не даёт.

## 3. Python API и самодостаточность

```python
class Scenario:
    def __init__(self, **parameters): ...
    def process(self, inputs: dict, context: dict, state: dict) -> dict: ...
    # close(self) — необязательное освобождение ресурсов
```

`process` возвращает **кандидаты**, а не готовые внешние envelopes:

```json
{"events":[{"event_type":"org.vlexchange.target-observed.v1","payload":{"label":"red","score":0.5,"count":2}}],"state":{"count":2}}
```

Точная schema — [raw-return.schema.json](schemas/raw-return.schema.json); candidate — [event-candidate.schema.json](schemas/event-candidate.schema.json). Кандидат содержит только event_type и payload. Нельзя подставить event_id/source/time: эти поля создаёт и проверяет host. Пустой events list — нормальный успешный результат без события.

Самодостаточность означает, что реализация сценарной логики включена в поставку, а все внешние зависимости объявлены. Она не означает, что пакет сам устанавливает Python, драйверы или модели. Код не импортирует внутренности VLBP/NRI/VL; допустимы standard library и объявленные нейтральные Python dependencies. Helper modules входят в inventory, их импорты относительные. Runtime pip/apt, sys.path hacks и скрытая загрузка кода не входят в профиль.

`runtime.python_version_specifier` и зависимости проверяются по фактической среде. Диапазон >=3.12,<4 в примере — декларация, а не свидетельство проверки всех Python/OS из диапазона. Конкретные Python/backend/OS версии записываются в отчёт.

Параметры имеют type string/number/integer/boolean и default; constraints/enum проверяются до constructor. Boolean не является integer, все numbers конечны. Host берёт все defaults и применяет только известные валидные overrides, затем передаёт ровно этот набор в Scenario(**parameters). Неявные device, SDK/context, secrets и paths не добавляются. Изменение параметров требует нового instance.

## 4. Вход и host mapping

В `inputs` ровно один ключ `input_contract.name`; его значение — один [Classification](schemas/classification.schema.json): label, score в [0,1], category_id и output_index. Никакой list/batch/SDK object/дополнительный payload не принимается. Один Classification совпадает с элементом канонического VLMP `results`; host явно извлекает его, сохраняя ID/index и score.

Принимающая система сама связывает свои камеры/модели/потоки с входом: извлекает Classification, назначает source_id/run_id/sequence и корректное UTC event time. Host не угадывает source по label и не меняет label/score для прохождения сценария. Mapping является частью интеграции и тестируется отдельно; получение валидного JSON не доказывает фактический inference или качество модели.

## 5. Контекст, логический запуск и clock domain

Контекст по [context.schema.json](schemas/context.schema.json): source_id, run_id, неотрицательные целые sequence и event_time_ms. Дополнительные поля запрещены.

- **source_id** — логический источник host.
- **run_id** назначает host уникально для независимого логического запуска. Это не installation_id лицензии и не автоматически UUID процесса.
- В рамках пары source_id/run_id sequence строго возрастает; gaps допустимы, duplicate/backward sequence — ошибка до process.
- В той же паре event_time_ms не убывает; одинаковые timestamps допустимы. Между sources/runs глобальной сортировки нет.
- Clock domain v0.1 — **UTC Unix milliseconds**, без leap-second отдельного представления. Source-relative PTS нельзя передавать как Unix time без явного преобразования; иная clock domain требует другого профиля.

Невалидный вызов не продвигает ordering. Event time не используется как часы лицензии или deadline. Значения времени в golden fixtures синтетические; они не выдаются за дату реального события. Новая физическая инсталляция, restart процесса или сетевой retry сами по себе не определяют новый логический run: это решает host с учётом replay/deduplication.

## 6. Формальная граница события

Каждый event_type объявлен в `event_contract.types` с self-contained JSON Schema payload. Unknown type/extra candidate fields/несоответствующий payload — fatal output error. Schema не должна содержать $ref/$dynamicRef/$id. Типы event_type уникальны.

Издатель обязан описать смысл типа, единицы payload, условия возникновения и смысл времени в нормативном профиле или включённой в inventory документации. Одна shape schema не описывает бизнес-смысл. Смена смысла события требует новой версии сценария/типа; события с совпадающими именами не считаются эквивалентными без этого интерфейса.

Для демонстрационного `org.vlexchange.target-observed.v1`: на входе проверяется label==target_label и score>=min_score; считаются последовательные **полученные observations** в пределах source/run. Gaps sequence не заполняются воображаемыми кадрами. Несовпадение сбрасывает count в0, совпадение увеличивает count до1_000_000. Кандидат возникает один раз при переходе `previous_count < min_count <= count`; третий consecutive match после default threshold2 не создаёт нового события. Payload label/score взяты из triggering observation, count — новое значение. Это не dwell time, не доказательство физического присутствия и не ground-truth качество модели.

Host строит полный внешний event:

```json
{"event_id":"sha256:…","event_type":"org.vlexchange.target-observed.v1","source_id":"camera-a","run_id":"run-1","sequence":2,"event_time_ms":1700000000000,"payload":{"label":"red","score":0.5,"count":2}}
```

Полная schema — [event.schema.json](schemas/event.schema.json). Source/run/sequence/time точно соответствуют принятому context. В этом профиле событие относится ко времени текущего observation; отдельные interval start/end, ретроспективное время и lifecycle события требуют расширения. Кандидаты сохраняют детерминированный порядок, их ordinal — индекс в списке с0.

### Алгоритм event_id: sha256-scenario-run-v1

1. Взять поля в порядке **scenario_id, version, source_id, run_id, decimal sequence, event_type, decimal ordinal**.
2. Строки состоят из Unicode scalar values и кодируются UTF-8 strict без normalization. Sequence/ordinal — неотрицательные integers; decimal без leading zeros, zero представлен `0`.
3. Начальные байты — ASCII `VLSP-EVENT-V1` и нулевой байт 00.
4. Для каждого поля добавить uint32 big-endian длину его UTF-8 bytes, затем эти bytes. Значение длины не включает свои4байта; поле с длиной >=2^32 отклоняется.
5. Посчитать SHA-256 всех полученных байтов и вернуть `sha256:` +64 lowercase hex.

Языконезависимый Unicode vector: [event-id-test-vector.json](event-id-test-vector.json), включая exact hash_input_hex. Ни JSON serialization, ни platform encoding в алгоритме не используются. Manifest digest не включён: golden events входят в inventory, поэтому такое включение создало бы цикл.

Повтор тех же scenario_id/version/source/run/sequence/type/ordinal при replay даёт тот же ID. Новый независимый run_id даёт иной ID. Этот ID помогает потребителю дедуплицировать событие, но возвращённый объект не гарантирует exactly-once сетевую доставку или durable outbox. Издатель должен соблюдать неизменность scenario_id/version, чтобы один ID не обозначал разные реализации.

## 7. Состояние, reset и ошибки

State принадлежит host в namespace `(manifest digest, instance identity, source_id, run_id)`. Каждый новый namespace получает deep copy `state.initial`. State — finite JSON object по self-contained draft2020-12 schema без ref/id. Inputs/context/state передаются как независимые копии. Изменение inputs/context запрещено.

Всё влияющее на будущие events состояние хранится в возвращаемом state. Hidden globals/self state/общий vendor GraphContext не допускаются. Технический cache допустим, если результат от него не зависит. Один instance не обрабатывает concurrent process calls.

Host валидирует raw candidates, state, limits, ordering и действующий execution lease, создаёт envelopes и IDs, затем атомарно публикует events и заменяет state/последние sequence/time. При ошибке нет commit и нет публикации. Атомарность здесь относится к локальной операции; crash recovery/доставка во внешнюю систему/outbox не специфицированы.

`reset(source_id,run_id)` явно очищает state и ordering указанной пары; reset одного source без run очищает все его runs; reset без аргументов очищает весь instance. Run без source недопустим. Reset не выполняется автоматически по дням/таймаутам. Повтор того же logical run после reset — replay с теми же event IDs; для новой логической сессии host выбирает новый run_id. Наличие JSON state не гарантирует миграцию между версиями.

Ошибки содержат stage/code/message/fatal/retryable. Невалидные input/context/order до process — input_contract/input_order с fatal=false,retryable=false, state/order не меняются. Exception, mutation inputs/context, bad candidate/event/state, превышение лимита/deadline после process — fatal=true,retryable=false, дальнейший process/reset этого instance запрещён; необходим новый instance. Optional close вызывается на cleanup, а не служит автоматическим исправлением ошибки. Нет implicit retry, stale output или fallback на иной сценарий.

## 8. Ресурсы и детерминизм

`max_process_ms` измеряет синхронный process монотонными wall-clock часами. Late return отклоняется без commit; это не hard preemption и не гарантия остановки зависшего Python. Worker termination, CPU/GPU/RAM/network isolation — отдельные возможности host. Import time в process budget не входит.

`max_input_bytes`, `max_state_bytes`, `max_event_bytes`, `max_events_per_call` проверяются до/после вызова в подходящей стадии. Размер — UTF-8 compact JSON: sort_keys, ensure_ascii=false, allow_nan=false, separators(',',':'). Event limit применяется к полному envelope после host construction. Это не ограничение transient allocations внутри Python.

Обязательный профиль детерминирован по params/inputs/context/state. Wall clock, random UUID и незаявленные внешние ответы не определяют events/state. Базовый сценарий не делает прямых сетевых уведомлений, записи в БД или иной необратимой бизнес-операции: он возвращает candidates. Это позволяет не публиковать результат при license/deadline failure. Host routing выполняется после commit по своей политике.

## 9. Модели и dependencies

`model_links`: alias, exact VLMP manifest_sha256, profile, required. Путь или registry resolution задаёт host вне сценария. До импорта требуемой зависимости проверяются exact manifest bytes hash, format/spec/profile/result и полный inventory файлов с digest. Неверная required ссылка — отказ, optional unavailable явно отражается. Эта lean проверка имеет onnx_graph_validated=false и inference_executed=false; runtime не обязан импортировать ONNX ради ссылки.

Перед настоящим inference VLMP host отдельно выполняет полную schema/graph/backend validation. Модельные зависимости не упаковываются/не скачиваются автоматически; права на веса не возникают из ссылки. Common encrypted envelope защищает только включённые в него bytes; внешняя модель лицензируется/защищается отдельно.

Classification-only сценарий, не вызывающий модель напрямую, имеет model_links=[]: host может предоставить результат подходящей модели. Наличие type Classification само по себе не подтверждает, какой моделью он получен. Если нужен конкретный provenance, его проверяет интеграция или отдельный явно объявленный профиль. API внутренних model invocation callbacks в v0.1 не стандартизован и не инжектируется скрыто в Scenario.

Текущая проверка model_links принимает исходную форму VLMP0.1; предлагаемый public0.2 соединяется с host явно в демонстрационном pipeline, а не маскируется под ссылку0.1.

Реальный сквозной тест должен исполнить VLMP, затем передать его фактический result защищённому VLSP. Prepared Classification JSON — fixture, а не доказательство inference. [Предшествующий VLMP пилот](../../docs/ru/COMPATIBILITY.md) проверял классификатор и два загрузчика, а не этот сценарный интерфейс или стороннего конкурента.

## 10. Обязательная защищённая эксплуатационная выдача

`delivery` фиксирует production `vl.exchange.protected` v0.1.0 и plaintext `development-only`. Production host не принимает прямой plaintext path как лицензию. Для разработки нужен явный development режим; plain execution не выдаётся за protected. Secret, trust anchor и самозаявленный `authorized=true` из package не дают прав.

Применяется отдельный общий компонент [VL Exchange Security](../../docs/security/SPECIFICATION.ru.md), физически не зависящий от VLBP. Поддерживаются оба режима: **offline signed entitlement** и **online signed short-lived grant**. Режим явно подписан в лицензии; silent fallback online→offline запрещён. Keys/trust/installation identity/server endpoint задаёт доверенный администратор, private keys не входят в пакет.

Подписи, issuer/audience/subject, rights, package/archive/manifest digests, issued/not-before/expires и ключ установки проверяются до decrypt/import согласно security profile. Online дополнительно связывает nonce/license/package/installation и проверяется по admin-provided server key; HTTPS обязателен, кроме явно включённого test loopback. Отказ/отзыв/network failure/unsupported licensing component приводит к fail closed.

Перед каждым process получается неизменяемый **ExecutionLease**; после возврата проверяется **тот же lease** до commit. Параллельное renewal не продлевает задним числом срок начатого вызова. Если permission истекло, events/state не фиксируются. Это не обещание принудительно остановить уже запущенный Python. Offline expiry зависит от доверенных локальных часов/rollback policy; мгновенный offline revocation без канала не обещается.

Издатель и его Python-code считаются доверенными, администратор host также доверен. **Референсный runner не является Python sandbox.** Нельзя исполнять произвольные недоверенные скрипты в том же процессе/OS identity с ключами, plaintext или памятью trusted code. Принимающая платформа отдельно обеспечивает права ОС, filesystem/network/process isolation, защиту временных файлов/logs/dumps и конфиденциальность входных данных.

Посторонний без recipient private key не может запустить скопированный encrypted delivery. Копирование секретного host key, полный клон доверенной системы, подмена runtime либо утечка plaintext в обход trusted authorization находятся вне этой гарантии. Абсолютная DRM или защита скомпрометированного plain runner не заявляется.

## 11. Golden, расширения и утверждение результата

Golden files по [sequence.schema.json](schemas/sequence.schema.json) содержат calls с inputs/context/expected или explicit reset. Expected events — **полные host envelopes**, не raw candidates; state — committed state; ошибки — code/fatal/retryable без побайтового сравнения diagnostic message. Каждая sequence начинается новым instance, а её шаги исполняются последовательно.

Keysets/strings/IDs/flags/integers сравниваются точно. Number payload использует `abs(actual-expected) <= atol + rtol*abs(expected)`, 0≤atol,rtol≤0.001. Tolerances определены до запуска и входят в manifest digest. В примере они равны0, потому что scenario только переносит scores из входа, не делает floating-point inference.

Проверяются: threshold boundary/третий hit/разрыв серии, два source, два run одного source, equal time/gapped sequence, error no-commit, reset и replay same ID, Unicode vector и отсутствие normalization, limits/unknown events/dependencies/capabilities, offline/online положительные и отрицательные пути. Оба независимых host обязаны совпасть по IDs на одинаковых inputs/run_id, включая Unicode; два CLI над одним load/process кодом не доказывают независимость.

Required capabilities: python-scenario-json-v1, classification-v1, event-json-v1, source-run-state-v1, event-id-sha256-v1. Unknown required capability/extension отклоняется до импорта. Unknown optional extension не меняет core behavior. Иная clock domain, input type, side effects, model callback, event lifecycle или state migration — отдельная negotiated capability/новая версия, не silent approximation.

Применяется [CONFORMANCE.md](CONFORMANCE.md). [Пример manifest](example-manifest.json) с нулевыми SHA — placeholder, не исполняемая поставка. [Пример sequence](example-sequence.json) — синтетические события, не результат работы промышленной видеоаналитики.

Отчёт явно разделяет specification, static validation, independent host execution, protected offline/online execution, actual VLMP→VLSP chain и deployment. Успех пилота не означает, что сторонние производители уже приняли VLSP или что все сценарии/модели/GPU переносимы.
