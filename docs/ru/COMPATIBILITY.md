# Совместимость и доказательства

**01.11.2026 · Draft / Experimental · [English](../en/COMPATIBILITY.md)**

Нормативная спецификация описывает требование, отчёт — реально выполненный запуск. «Подготовлено» не означает «проверено», а два локальных host не являются платформами двух производителей.

| Компонент | Проверяемый объём | Состояние |
|---|---|---|
| VLMP public proposal0.2.0-draft.1 | Синтетическая классификация, CPU FP32 ONNX13/IR8, NumPy ABI, 5 входов, 13 вызовов на каждый host | Локальные reference и independent host; общий ONNX Runtime; отчёты ниже |
| VLSP0.1 | Один Classification, deterministic JSON state/events, 15 golden шагов | Reference и independent host; independent development/offline/online, идентичность полного event/state |
| Security0.1 | Подпись и шифрование, recipient binding, expiry, offline entitlement, online nonce/grant/revocation/lease, negative paths | Автотесты с временными ключами, online реальный loopback HTTP |
| Pipeline | Настоящий ONNX result → protected VLSP, offline/online | Явное mapping Classification; не standardized callback, не model_links public0.2 |
| CI | Schema, local Markdown targets, tests, два host, pipeline | До первой публикации GitHub Actions ещё не запускался. Текущие запуски — в [GitHub Actions](https://github.com/vsdolgov/VL-Exchange/actions). |
| Сторонняя платформа | Партнёрский импорт неизменённого ZIP | План пилота; NOT TESTED |
| GPU/production sandbox/TLS deployment | Эксплуатационные свойства | NOT TESTED |

## Воспроизведение и файлы

Из корня репозитория после установки README-зависимостей: `python tools/verify.py`. Все проверки обязаны завершиться успешно. Online тестам нужен loopback socket; sandbox запрет bind — ограничение окружения, а не успешная проверка. Текущий [машинный итог](../../reports/summary.json) содержит коды каждой проверки и версии ОС/Python. [Результат pytest](../../reports/pytest.txt) фиксирует фактическое число тестов.

Модель: [reference](../../reports/vlmp-reference.json), [independent](../../reports/vlmp-independent.json), [ZIP](../../model/reports/vlmp-public-channel-probe.zip). Один manifest SHA256 `abcb2f3b91250490bdf4716c48f0e05ef1c9df11c48baf2d463865bd1413b315`, ZIP SHA256 `b004c02c6f12839007389c71ec60e6bd77b2824a72c27d9bd7ddc71be04752ff`. [Archive-roundtrip отчёт](../../reports/vlmp-archive-roundtrip.json) дополнительно фиксирует фактическое исполнение этого ZIP: две отдельные безопасные распаковки в приватные временные каталоги, 13 вызовов на каждый host, сверку всех полей результатов между host и с golden oracle, SHA-256 архива/manifest/каждого файла до и после. Расшифрованных секретов здесь нет; временные каталоги удаляются. Независимость относится к загрузке и orchestration, а не к inference engine.

Новая независимая интеграция не импортирует reference runtime; оба используют ONNX Runtime и один model.py, что явно ограничивает заявление независимости.

Сценарий: [reference](../../reports/vlsp-reference.json), [independent](../../reports/vlsp-independent.json), [ZIP](../../scenario/reports/vlsp-target-scenario.zip). ZIP SHA256 `eb9ea96fda4bbae5b6db2da859b78d4a897858dff1ffc7dc90a07b077e876e35`. Independent host реализует свой load/process/state/event-ID путь и разделяет только schema/test vectors и общий crypto-компонент. Три режима независимого host проверяют один ZIP без правок; ephemeral encryption envelopes различаются по recipient keys и случайному шифрованию, а исходный payload ZIP остаётся тем же.

[Сквозной отчёт](../../reports/pipeline.json) отдельно фиксирует actual_inference, действительный score и результат protected scenario. Подготовленные Classification fixtures в golden test не выдаются за inference.

Валидатор также проверяет минимум три действительно различных golden массива по dtype, форме и значениям, а не только по ID/path. После добавления этой проверки выполнен отдельный [модельный прогон](../../reports/model-tests.txt): **14 passed**; отрицательный случай сохраняет разные ID/path и обновляет file digests, поэтому проверяется именно различие содержимого. Этот результат не является новым заявлением об общем числе тестов.

Общий итоговый прогон после правки: **55 passed**, все **9 проверок** `tools/verify.py` завершились успешно; актуальные результаты — в [summary](../../reports/summary.json) и [pytest](../../reports/pytest.txt).

Синтетическая поставка сценария имеет версию решения **1.0.1**: отображаемое название компании в docstring заменено на **VL**. Поведение сценария, формат VLSP0.1 и Python ABI не изменились. Новые bytes имеют новую идентичность manifest/ZIP; актуальные отчёты получены повторным прогоном этой поставки. Прежние результаты1.0.0 не выдаются за испытание1.0.1.

При очистке буквальных упоминаний из обоих README удалены ссылки на домен компании. Независимый host сохраняет denylist прежнего vendor namespace через точное сравнение digest корневого имени. Отрицательный тест проверяет отклонение импорта корня и подмодуля до исполнения и допустимость похожих имён. Bytes синтетических пакетов модели и сценария в этой очистке не менялись.

## Исторический VLMP0.1

Исходный локальный пилот30.10 проверял другой manifest/ABI: CustomModelProtocol, `vlmodels-types1.8.1`, настоящий CustomModel Box5 и независимый loader. Его private SDK и стендовые материалы исключены из этого проекта. [Полная историческая спецификация0.1](../../specs/vlmp/0.1/SPECIFICATION.ru.md) описывает интерфейс для независимого адаптера; внешний SDK не распространяется. Исходная [JSON Schema0.1](../../specs/vlmp/0.1/model-package.schema.json) сохранена как исторический интерфейс; public0.2 не переименован в0.1 и не наследует Box5 compatibility. Для публикации воспроизводимого Box5 адаптера необходим разрешённый публичный SDK или отдельная реализация адаптера и новый прогон.

## Ограничения

Нет проверки промышленной точности, стороннего внедрения, любой модели/сценария/GPU. Python trusted; namespace не sandbox. Лицензионный сервер loopback тестовый, production TLS/HA/key storage не испытаны. Envelope не защищает внешнюю модель автоматически, а public VLMP protected runner не интегрирован. Offline expiry зависит от доверенных часов, перенос установки — от согласованного reissue; мгновенный offline отзыв и защита от администратора не обещаются. Состав материалов одобрен к публикации; лицензии самого проекта и патентная политика требуют финального решения.
