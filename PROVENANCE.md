# Provenance / Происхождение материалов

**Draft, 2026-11-01. Publication of this file set is approved for vsdolgov/VL-Exchange; final project licenses and patent policy remain pending. / Состав файлов одобрен к публикации в vsdolgov/VL-Exchange; итоговые лицензии и патентная политика остаются на согласовании.**

| Component | Origin / Происхождение | Publication boundary / Граница |
|---|---|---|
| Public VLMP0.2 proposal | New independent NumPy ABI, reference loader and separate integration host created for this public draft / Новые ABI и два загрузчика для публичного черновика | No private SDK, no claim of Box5 compatibility / Без private SDK и заявления Box5-совместимости |
| Synthetic ONNX | Programmatically generated ReduceMean+Softmax graph; no training or downloaded weights / Сгенерированный граф без обучения и скачанных весов | Rebuild with model/tools/build_demo.py |
| Synthetic inputs | In-memory color arrays written as `.npy`, no customer images / Цветовые массивы без клиентских данных | Five fixtures and analytical expected outputs / Пять входов с аналитическим oracle |
| VLSP0.1 | Self-contained local prototype curated into public scenario directory / Самостоятельный локальный прототип | No platform source, no block/editor dependency / Нет исходников платформы или редактора |
| Common security runtime | Reusable local protected-delivery prototype / Общий прототип защиты | Default manifest changed to scenario; all fixture keys generated at runtime / Default scenario manifest, ключи только временные |
| Historical VLMP0.1 specification/schema | Full RU/EN historical interface text and unchanged schema, with private filesystem provenance omitted / Полный исторический текст RU/EN и неизменённая схема, без приватных путей происхождения | Private SDK, deployed sources, host commands and raw historical logs excluded / Приватные SDK, исходники стенда, команды и логи исключены |
| Documentation | New RU/EN draft descriptions and translated public interface contracts / Новые RU/EN материалы | Experimental proposals, not third-party adoption or granted commercial rights / Не принятие отраслью и не выдача прав |

Third-party dependencies are installed from their public distributions, not vendored. Their respective licenses remain applicable; the proposed project license does not replace them. / Сторонние зависимости не скопированы в репозиторий; устанавливаются из публичных distributions и сохраняют собственные лицензии.

GitHub workflow uses current official examples: [actions/checkout](https://github.com/actions/checkout), [actions/setup-python](https://github.com/actions/setup-python). The prepared workflow has not run on GitHub. / Workflow подготовлен по официальным примерам, удалённый запуск GitHub ещё не выполнен.
