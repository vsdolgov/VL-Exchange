# VLSP v0.2 — ошибки и жизненный цикл sync core

Этот документ нормативно уточняет `python-scenario-services-json-v1` версии VLSP 0.2.0. Он не переопределяет async capability и не требует изменения package ZIP. Реализация, соответствующая обновлённой редакции 0.2, возвращает `stage`, `code`, `message`, `fatal`, `retryable` по [error.schema.json](schemas/error.schema.json). `message` — непустая диагностическая строка, её формулировка, язык и содержимое не участвуют в сравнении. Не включать секреты, кадры и содержимое модели.

## Словарь

| stage | code | Условие |
|---|---|---|
| manifest | `manifest_invalid` | Manifest, schema или семантическая структура некорректны |
| manifest | `version_unsupported` | Версия или контракт не поддерживаются |
| requirements | `capability_missing` | Требуемая capability/extension не поддерживается |
| requirements | `dependency_missing` | Отсутствует обязательная модель, трекер или Python dependency |
| requirements | `dependency_mismatch` | Версия Python dependency или реализация/версия трекера несовместимы |
| requirements | `model_link_mismatch` | Kind, profile, manifest digest или callback модели несовместимы |
| requirements | `hardware_rejected` | Требуемая device capability недоступна до создания instance |
| integrity | `path_unsafe` | Небезопасный путь или symlink |
| integrity | `file_missing` | Отсутствует заявленный файл |
| integrity | `digest_mismatch` | Hash заявленного файла неверен |
| integrity | `inventory_mismatch` | Неперечисленные файлы или несогласованный inventory |
| parameters | `parameter_invalid` | Неизвестный или недопустимый параметр |
| input | `input_contract` | Невалидный input/context/reset или превышен входной лимит до process |
| input | `input_order` | Нарушен sequence, UTC time или уникальность frame_id |
| input | `input_mutation` | Код сценария изменил переданные input/context |
| output | `event_contract` | Невалидные candidates, event_type, payload, full envelope или лимит events |
| state | `state_contract` | Невалидное возвращённое состояние или превышен его лимит |
| execution | `service_contract` | Невалидный service request/response, корреляция, alias, объект, crop или лимит service calls |
| execution | `service_failed` | Backend модели/трекера отказал либо сценарий перехватил сервисную ошибку |
| execution | `scenario_exception` | Исключение кода сценария или его constructor/close, не связанное с сохранённой ошибкой сервиса |
| execution | `process_timeout` | Синхронный process вернулся после deadline |
| lifecycle | `lifecycle` | Операция недопустима для failed, closed или занятого instance |
| golden | `golden_mismatch` | Фактический результат не совпал с oracle golden sequence |
| security | `policy_rejected` | Режим доставки или trust configuration не разрешён |
| security | `authorization_failed` | Проверка прав или исходного ExecutionLease не удалась |

Все preflight проверки выполняются до import, но взаимный приоритет одновременно нарушенных manifest, security, inventory, requirements и parameters не нормирован: host возвращает одну применимую ошибку, а conformance vectors проверяют один дефект за раз. Для process проверяются lifecycle, input/ordering, авторизация до вызова, service/scenario, output/state, исходный lease, затем commit. Ошибка реального устройства во время inference — `service_failed`, а не `hardware_rejected`. Истечение либо отзыв исходного разрешения модели/устройства до commit — `authorization_failed`; это не замена на другую модель/device и не silent fallback.

## Flags и переходы

`fatal` сообщает, запрещены ли дальнейшие `process` и `reset` именно этому существующему instance из-за отказа; `retryable` разрешает повторить операцию после устранения внешнего условия и не обещает сетевой exactly-once или автоматический retry. Host назначает flags и code сам, не доверяя изменяемому исключению, возвращённому из Python-сценария.

| Событие | flags `(fatal,retryable)` | Действие |
|---|---|---|
| Ошибка до успешного создания instance, включая constructor и проверку прав | `(false,false)` | Instance не создан, import выполняется только после preflight; повтор — новый open |
| Невалидный input/context/order/reset на живом instance | `(false,false)` | State/order/seen/events не изменяются; instance остаётся живым |
| Service failure/contract, исключение сценария, mutation, output/state/лимит, deadline после начала process | `(true,false)` | Нет commit; instance failed, новый process/reset запрещены; требуется новый instance |
| Права недействительны до process или исходный lease истёк до commit | `(false,true)` | `authorization_failed`, нет commit и публикации; instance остаётся живым, повторная попытка снова проверяет права |
| process/reset на failed instance | `(true,false)` | `lifecycle`; state/order не меняются |
| process/reset на закрытом instance | `(false,false)` | `lifecycle`; требуется новый instance |
| Одновременный вызов process/reset, пока instance занят | `(false,false)` | `lifecycle`; выполняющийся вызов не отменяется |
| Успешный close / повторный close | ошибки нет | optional close вызывается не более одного раза; дальнейшие process/reset отклоняются |

Если после начала вызова возникла сервисная ошибка, она не становится recoverable оттого, что сценарий её перехватил. Код, stage и flags сохраняемой ошибки принадлежат host. Закрытие release state и private module resources не является checkpoint API. Неудачный close выдаёт `scenario_exception` с `(true,false)`, дальнейшие process/reset запрещены. Проверка lease и отказ авторизации не фиксируют state/order/seen/events; новый grant не продлевает тот lease, с которым начат вызов. После разрешённого исправления прав host может принять тот же sequence/frame_id заново, если он не был закоммичен; сетевые побочные эффекты не откатываются.

Для JSON byte limits `max_input_bytes` применяется отдельно к inputs и context; `max_state_bytes` применяется к tracker input/candidate JSON и отдельно к scenario state; `max_event_bytes` — к каждому полному host envelope после добавления event identity. Изменяемые input/context, незарегистрированные ссылки, неправильные координаты и invalid service response проверяются до commit. Tracker bbox может отличаться от detection bbox для сглаживания, но должен быть корректным и связан с detection текущего кадра; classification crop должен лежать внутри зарегистрированной области соответствующего объекта.

## Нормативные тестовые векторы

[error-vectors.json](error-vectors.json) фиксирует типовые stage/code/flags и ожидаемое состояние instance. Это данные для conformance harness, не сценарные golden fixtures и не утверждение об успешном прохождении тестов. В каждом тесте отдельно проверяется отсутствие state/order/event commit, сохранность source/run identity и результат следующего вызова. Ошибки, сформированные producer-кодом вручную, не могут отменить host policy; code в векторе относится к первопричине, а не к слову в diagnostic message.
