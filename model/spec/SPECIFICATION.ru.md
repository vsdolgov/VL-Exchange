# VLMP: публичный независимый профиль

**Предложение 0.2.0-draft.1 · Experimental · 01.11.2026 · [English](SPECIFICATION.en.md)**

Это дополнительный публичный профиль для обсуждения, а не замена испытанного VLMP 0.1.0 / CustomModelProtocol. Он устраняет зависимость демонстрации от приватного SDK, меняя ABI postprocess на обычный Python dict. Его совместимость с Box 5 не проверена. Схема исходного 0.1 сохранена отдельно в [историческом разделе](../../specs/vlmp/0.1/model-package.schema.json). MUST/ДОЛЖЕН и MUST NOT/НЕ ДОЛЖЕН — требования этого предлагаемого профиля.

## 1. Область и пакет

Формат `vl.model-package`, версия `0.2.0-draft.1`, профиль `python-numpy-classification-v1`. Поддерживается один CPU ONNX FP32 classifier со статическим batch 1 и входом `[1,3,2,2]`. Малый размер — намеренное ограничение публичного interoperability probe, не рекомендация промышленным моделям. GPU, dynamic shapes, другие типы результата, custom operators, external ONNX data и arbitrary Python portability не входят в профиль.

Пакет — неизменяемый каталог либо ZIP с корнем, содержащим `model-package.json`, `code/__init__.py`, `code/model.py`, ONNX-файл и `golden/*.npy`. ZIP перед исполнением безопасно извлекается принимающим host; демонстрационные модельные host принимают каталог. Структурные требования задаёт [JSON Schema](model-package.schema.json).

Все файлы, кроме корневого manifest, перечислены в `files: {relative_path: sha256}`. POSIX-пути относительные и канонические; absolute, backslash, пустые сегменты, `.`, `..`, symlink и glob запрещены. Неперечисленные, пропавшие и изменённые файлы отклоняются до импорта. При распаковке отдельно нужны ограничения количества/размера файлов и запрет zip-slip. Host не пишет bytecode, логи или временные файлы в пакет. Он обеспечивает отсутствие подмены после проверки; writable development directory не является защитой от другого процесса.

## 2. Поля manifest

| Поле | Смысл |
|---|---|
| `format`, `spec_version`, `profile` | Точный идентификатор формата, версии и ABI |
| `model_id`, `version` | Namespaced ID и семантическая версия поставки; одна пара не переиздаётся с другими байтами |
| `entrypoint` | `root` — Python package, `module="model.py"`, `class="Model"` |
| `runtime.python` | Непустое PEP 440 ограничение Python |
| `backend` | ONNX Runtime / CPU / FP32, точный файл весов, opset 13, входные/выходные имена и формы |
| `source_input` | BGR, uint8, HWC, три канала |
| `categories` | Упорядоченное отображение output index → category ID и label |
| `dependencies` | Distribution name → PEP 440 constraint, без автоматической установки |
| `required_capabilities`, `extensions` | Явное согласование обязательных возможностей |
| `golden` | Fixtures, эталонные результаты и зафиксированные допуски |
| `files` | Полный inventory и SHA-256 каждого файла |

Manifest — strict UTF-8 JSON без duplicate keys, comments и NaN/Infinity. Неизвестные core fields запрещены. Исходные байты manifest хешируются отдельно; изменение whitespace меняет identity. Номер версии без digest не идентифицирует конкретную поставку.

## 3. ABI

```python
class Model:
    def __init__(self): ...
    def preprocess(self, image): ...
    def postprocess(self, outputs, categories): ...
```

Конструктор без аргументов. `preprocess` получает один C-contiguous NumPy uint8 `[H,W,3]`, H/W>0, BGR, значения 0…255. Начало координат слева сверху. Alpha и неявное RGB-преобразование запрещены. Модель сама задаёт resize, BGR→RGB, нормализацию и прочую обработку в включённом коде.

`preprocess` возвращает dict ровно с `backend.input_name`; значение — NumPy float32 `[1,3,2,2]`, все числа конечны. Молчаливое исправление dtype или shape запрещено. Приведение к contiguous без изменения значений допустимо.

Host вызывает ONNX Runtime с `CPUExecutionProvider`; вход и выход по точным именам. Граф содержит один float32 вход заявленной формы и один float32 выход `[1,C]`; C равно числу categories. Opset — стандартный domain `""`, версия13; IR8 используется в demo. Host валидирует граф, actual I/O, dtype/shape, finite values и загрузку backend. Другие профили не выбираются автоматически.

`postprocess` получает `{output_name: NumPy_float32_[1,C]}` и tuple labels в исходном порядке categories. Возврат — ровно `{"label": str, "score": number}`. Label должен иметь единственное отображение в categories; score конечен и в[0,1], bool не число. Выбор класса, softmax, tie-breaking и пороги принадлежат коду модели, а не host.

## 4. Категории и результат

`output_index` последовательно равен 0…C−1 в порядке массива. `category_id` и label уникальны; ID не обязан совпадать с index и по нему нельзя сортировать. Host дополняет результат до `{label, score, category_id, output_index}`. Для связи с VLSP он помещает этот объект под ключ `classification`; публичный host не передаёт приватный SDK object.

В demo ReduceMean усредняет нормализованные RGB каналы, Softmax формирует вероятности; NumPy argmax выбирает первый index при равенстве. Входы с различными каналами обнаруживают BGR/RGB ошибки. Это классификация синтетических цветов, не промышленный детектор.

## 5. Зависимости и import

До import проверяются manifest/schema, inventory/digests, capability/extension, ссылки на файлы, ONNX graph и зависимости. Неизвестная required capability или required extension приводит к отказу; optional extension сохраняется и не меняет core behavior. Обязательные capabilities: `python-numpy-classification-v1`, `onnx-cpu-fp32`.

Host проверяет фактические Python/distribution версии по PEP440. Диапазон декларации не доказывает испытание всех версий. Runtime не устанавливает зависимости и не скачивает код/модели. Helpers включены в inventory и импортируются относительными imports. Import выполняется под отдельным namespace; это предотвращает конфликт имён, но не является sandbox.

## 6. Жизненный цикл и ошибки

Порядок: validate → import → Model() → CPU backend → последовательные preprocess/inference/postprocess → освобождение модели/backend/namespace. Один instance не принимает параллельные вызовы. В этом профиле результат не зависит от предыдущих изображений. Ошибка не превращается в пустой или устаревший успешный результат; CLI возвращает ненулевой status с type/message. Для повторного исполнения после runtime failure создаётся новый instance.

Host отклоняет неверный input, tensor/result contract, label/score, digest, path, dependency или required feature. Проверка статическая и исполнение должны быть различимы в отчёте: schema validation не доказывает inference. Минимальный независимый host демонстрирует отдельный load/inference путь для данного проверенного demo; не выдаётся за полный production validator недоверенных моделей.

## 7. Golden и независимость

Минимум три различных `.npy` uint8 BGR inputs, `allow_pickle=False`. Каждый входит в inventory. Fixture ID и path уникальны. Минимум три массива различаются содержимым по совокупности dtype, формы и значений; разные имена файлов, ID или заголовки `.npy` сами по себе не делают изображения различными. Label, category ID и index совпадают точно. Score удовлетворяет `abs(actual-expected) <= atol + rtol*abs(expected)`, 0≤atol,rtol≤0.001; tolerance фиксируется до запуска в manifest. Demo включает пять изображений, прямой и обратный порядок на новых instance и повтор первого после другого — 13 вызовов на host.

Для заявления о двух независимых системах нужны одинаковые manifest/file/ZIP digests, отсутствие модификаций, отдельные load/orchestration implementations, все fixtures в допусках, зафиксированные версии ОС/Python/backend и ограничения. Два CLI над одним исполнителем не являются двумя реализациями. Общая ONNX Runtime допустима, но независимость inference engines не заявляется. Текущие два host локальные; сторонний вендор и Box5 для этого профиля не испытаны.

## 8. Защита и лицензия

SHA-256 по известному эталону проверяет целостность, не автора. Подпись издателя, право execute, конфиденциальность весов и защита от администратора — разные свойства. Общий [security envelope](../../docs/security/SPECIFICATION.ru.md) может защищать точный ZIP и ссылаться на `model-package.json`; защищённый модельный runner для данного нового профиля пока не интегрирован и не заявляется проверенным.

Открытый demo исполняется как доверенный разработческий код. Коммерческий автор не обязан публиковать веса/код, но принимающая платформа должна знать открытые интерфейсы. Администратор конечной системы доверен; доступ к памяти/plaintext позволяет извлечь веса. Отдельная лицензия не становится переносимой автоматически: перенос установки требует договорного права и разрешённого перевыпуска. [Матрица](../../docs/ru/COMPATIBILITY.md) отделяет этот план от проверенного VLSP protected execution.
