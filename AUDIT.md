# Publication candidate audit

English | [Русский](#русский)

**2026-11-01 · GitHub publication candidate / кандидат публикации на GitHub · Draft / Experimental**

## English

The candidate contains openly described VLMP/VLSP interfaces, synthetic data and minimal examples. Deployed platform source, private SDKs, customer footage and commercial weights are excluded. The threat model trusts the publisher and administrator; the Python runtime is not a sandbox. Shared encryption, signatures and online/offline licensing do not establish hostile-administrator protection or signed-code safety.

Schemas, tests, two local execution paths and actual ONNX output feeding a protected VLSP have been checked. Current counts and exit codes are in the [machine report](reports/summary.json), [pytest](reports/pytest.txt) and [compatibility matrix](docs/en/COMPATIBILITY.md). Historical SDK-based VLMP0.1/Box5 and proposed public0.2.0-draft.1 are different profiles. Box5 evidence does not transfer to the new profile. A third-party platform, production TLS/HA, GPU and process isolation have not been verified.

A known-pattern secret/private-source-path scan found no matches in the candidate. This limited static check is neither an independent security review nor legal clearance. [PROVENANCE](PROVENANCE.md) records component origins. Test keys are ephemeral; persistent secrets are not distributed.

Publication of the file set in [vsdolgov/VL-Exchange](https://github.com/vsdolgov/VL-Exchange) has been approved; the public repository exists with main as its default branch. This audit covers the prepared payload and does not establish its upload or a GitHub Actions run. Final licenses and the essential-patent policy are unapproved; there is no effective LICENSE or patent commitment. The [licensing proposal](docs/governance/LICENSE-PROPOSAL.en.md) grants no license. Commercial migration rights must be explicitly included in the solution's own terms.

## Русский

Подготовленный проект содержит открыто описанные интерфейсы VLMP/VLSP, синтетические данные и минимальные примеры. Исходники действующих платформ, приватный SDK, клиентские видео и коммерческие веса исключены. Политика угроз предполагает доверенных издателя и администратора; Python runtime не является sandbox. Общий профиль шифрования, подписи и online/offline лицензирования не гарантирует защиту от привилегированного администратора или безопасность подписанного кода.

Проверены схемы, тесты, два локальных пути исполнения и сквозной пример настоящего ONNX результата с защищённым VLSP. Актуальные числа и коды завершения находятся в [машинном отчёте](reports/summary.json), [pytest](reports/pytest.txt) и [матрице совместимости](docs/ru/COMPATIBILITY.md). Исторический VLMP0.1 с Box5 и предложенный public0.2.0-draft.1 — разные профили. Результат Box5 не переносится на новый профиль. Проверки на платформе стороннего вендора, production TLS/HA, GPU и процессная изоляция не выполнены.

Аудит известных шаблонов секретов и путей к приватным исходникам не обнаружил совпадений в кандидате. Это ограниченная статическая проверка, не независимый security review и не подтверждение юридических прав. Происхождение компонентов описано в [PROVENANCE](PROVENANCE.md). Ключи тестов создаются временно; поставка постоянных секретов не предусмотрена.

Состав распространяемых файлов одобрен к публикации в [vsdolgov/VL-Exchange](https://github.com/vsdolgov/VL-Exchange); публичный репозиторий создан, основная ветка — main. Этот аудит относится к подготовленному payload и не подтверждает его загрузку или запуск GitHub Actions. Итоговые лицензии и essential-patent policy не утверждены; действующего LICENSE и патентного обязательства нет. [Предложение по лицензиям](docs/governance/LICENSE-PROPOSAL.ru.md) не является выдачей лицензии. Коммерческие права переноса должны быть отдельно закреплены в условиях лицензии решения.
