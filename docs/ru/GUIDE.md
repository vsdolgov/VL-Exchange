# Практическое руководство VL Exchange

**Draft / Experimental · [English](../en/GUIDE.md)**

## Экспорт модели

Сначала выберите поддерживаемый профиль, а не только формат весов. Исходный VLMP0.1 требует CustomModelProtocol/SDK; новый публичный профиль0.2.0-draft.1 — отдельное предложение без приватного SDK. Их нельзя подменять друг другом. Для первого публичного опыта используйте [синтетический пример](../../model/examples/channel-classifier) и [точный интерфейс](../../model/spec/SPECIFICATION.ru.md).

Реализуйте Model.preprocess/postprocess; включите все helpers. Зафиксируйте BGR uint8 HWC источник, RGB/resize/normalization в коде, имена и формы ONNX, выходные labels и category_id. Запишите публичные зависимости, версию Python и возможности. Добавьте минимум три различных `.npy` входа без pickle, эталонные label/score/ID/index и допуски. В `files` перечислите SHA-256 всех файлов, кроме manifest. Не добавляйте клиентские данные без разрешения.

Для воспроизводимого построения собственного учебного примера из корня репозитория:

```sh
python model/tools/build_demo.py
python model/src/public_model.py validate model/examples/channel-classifier
python model/src/public_model.py golden model/examples/channel-classifier
python model/tools/independent_host.py model/examples/channel-classifier
python tools/model_archive_roundtrip.py
```

Пересборка нужна до freeze. Между двумя host используйте одну неизменённую поставку. Сохраните manifest/file/ZIP digests и версии окружения. Совпадение top1 без проверки score недостаточно.

`model_archive_roundtrip.py` берёт один неизменённый ZIP, проверяет безопасные пути, отсутствие symlink и case-collision, лимиты и полный inventory, распаковывает его отдельно для двух host в приватные временные каталоги и запускает их через subprocess. Все 13 результатов каждого host сверяются с golden oracle и друг с другом; проверяются SHA-256 архива/manifest/каждого файла до и после, затем временные каталоги удаляются. Это реальное испытание архивной поставки на локальных host; запуска платформы другого производителя оно не заменяет.

## Экспорт сценария

Реализуйте `Scenario(**parameters)` и `process(inputs,context,state)`, возвращающий только `{events:[{event_type,payload}],state}`. Вынесите всё влияющее на будущие события состояние в JSON state. Код не обращается к внутреннему графу VL и не выполняет внешние бизнес-операции сам. Интерфейс должен описывать смысл события, пороги, единицы и временную семантику. Подготовьте schema/defaults/limits и последовательности golden с полными host events.

```sh
python scenario/tools/build_demo.py
python -m vl_scenario_package validate scenario/examples/target-scenario --policy development --check-environment
python -m vl_scenario_package golden scenario/examples/target-scenario --policy development
python scenario/tools/independent_host.py scenario/reports/vlsp-target-scenario.zip
```

Последняя команда испытывает тот же ZIP в независимом host, в development/offline/online режимах; временный loopback HTTP нужен только для тестового online сервера. Никаких постоянных ключей команда не сохраняет. Полный [VLSP интерфейс](../../scenario/spec/SPECIFICATION.md) определяет события, состояние, reset и ошибки.

## Адаптер принимающей платформы

1. Объявите точные format/version/profile/capabilities. Неизвестные required элементы отклоняйте до code import. Проверьте inventory, граф, зависимости и права отдельно.
2. Явно сопоставьте камеру/поток с source_id, логическую сессию с run_id; назначайте строго растущую sequence и неубывающее UTC Unix event_time_ms. Source PTS требует преобразования.
3. Получите настоящий результат модели; передайте `{classification:{label,score,category_id,output_index}}`. JSON fixture не является inference. VLSP0.1 не задаёт скрытый model callback; new public VLMP0.2 связывается явным mapping, а original model_links остаются0.1.
4. Host владеет JSON state по manifest/instance/source/run, создаёт event envelope/ID и только после всех проверок атомарно commits events/state. Доставку/outbox/дедупликацию обеспечьте своей интеграцией.
5. Обеспечьте процессную изоляцию, права на filesystem/network и лимиты. Namespace import и timeout после возврата не являются sandbox или preemption. Оба демонстрационных host исполняют доверенный Python.
6. Для commercial поставки отделите issuer trust от package contents, проверяйте offline entitlement либо online grant до decrypt/import и тот же lease до commit. Администратор доверен; protected runner модели нового public профиля пока требует отдельной интеграции.

## Защищённый сквозной пример

```sh
python tools/test_pipeline.py
```

Команда реально вызывает ONNX classifier, затем передаёт его Classification защищённому VLSP offline и online. Выпускаются временные recipient/installation/issuer/server keys; envelope и plaintext удаляются после теста. Протокол защищённой выдачи описан [отдельно](../security/SPECIFICATION.ru.md). Это интеграционный тест доверенных прототипов, не готовая инструкция промышленного управления ключами.

## Роль ИИ при переносе

ИИ-помощники могут упростить написание адаптеров, разбор чужого API и перенос прикладной логики. Наша позиция: общий интерфейс позволяет направить эту помощь на проверяемый результат и экономить ресурсы на повторной интеграции, сохраняя конкуренцию платформ и разработчиков. ИИ не заменяет согласование смысла события, условий лицензии и испытание одного неизменённого пакета в независимых системах; обещаний конкретного процента экономии здесь нет.

## FAQ

**Обязательно пользоваться платформой или облаком VL?** Нет. Получатель реализует открытые интерфейсы и свой адаптер; offline выдача не требует облака.

**Зачем это при наличии ONNX/API/ONVIF?** Они решают свои задачи. Перенос бизнес-результата требует ещё определить processing, state, event semantics и golden tests. VL Exchange дополняет существующие технологии.

**Нужно открыть исходники и веса?** Нет. Открыт интерфейс. Коммерческие права и защищённая поставка отдельны; публичные примеры синтетические. Финальные лицензии самого проекта пока согласуются.

**Оплаченная лицензия автоматически заработает на другом host?** Нет. Это целевой коммерческий принцип при соответствующих условиях правообладателя. Текущая установка привязана к recipient key; перенос требует согласованного reissue, а offline revocation не мгновенный.

**Можно доверять любой подписанной модели?** Нет. Подпись удостоверяет ключ, а не безопасность кода. Доверенные publisher/admin — предпосылка текущего runner; недоверенный код требует изоляции принимающей платформой.

**Что проверено сейчас?** Смотрите [матрицу](COMPATIBILITY.md); два локальных host не означают поддержку конкурента или GPU.
