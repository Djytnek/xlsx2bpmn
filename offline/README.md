# Установка без интернета

Здесь лежит всё, что нужно, чтобы поставить утилиту на машину, у которой
нет выхода в сеть. Порядок действий по шагам — в [ИНСТРУКЦИЯ.md](../ИНСТРУКЦИЯ.md),
часть 6.

| Файл | Что это | Размер |
|---|---|---|
| `xlsx2bpmn-1.14.0-py3-none-any.whl` | сама утилита, уже собранная | 505 КБ |
| `markitdown-offline.zip` | markitdown и все 36 его зависимостей | 92 МБ |

**Утилита подойдёт к любому Python 3.10+** — она собрана как `py3-none-any`,
то есть без привязки к версии и к железу.

**markitdown — нет.** Колёса в архиве скачаны под **Python 3.12** и **Linux
x86_64**. Если на сервере другая версия Python, архив не подойдёт, его надо
пересобрать — как, написано ниже.

## Зачем это, если можно поставить обычным способом

Обычная установка идёт в интернет за зависимостями. На закрытом сервере это
не работает, а собрать пакет на месте нельзя: для сборки нужен `setuptools`,
которого там может не быть. Поэтому здесь лежит **уже собранное** — pip такое
просто распаковывает, ничего не скачивая и не собирая.

## Как это пересобрать

Понадобится, когда выйдет новая версия утилиты или когда на сервере
окажется другой Python. Обе команды выполняются на машине **с интернетом**.

**Утилита** — из корня проекта:

```bash
python3 -m pip wheel . -w offline --no-deps
```

Получится новый `.whl`. Старый удалите, чтобы не путать.

**markitdown** — подставьте версию Python вашего сервера вместо `3.12`
(узнать её там: `python3 --version`):

```bash
python3 -m pip download "markitdown[docx,pdf,pptx,xlsx]==0.1.7" \
  --only-binary=:all: \
  --python-version 3.12 --abi cp312 --abi abi3 --abi none \
  --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 --platform any \
  -d markitdown-wheels
zip -r markitdown-offline.zip markitdown-wheels
```

Три вещи в этой команде неочевидны, и без каждой она ломается:

1. **`==0.1.7` обязателен.** Без точной версии pip выберет `markitdown 0.0.2` —
   древнюю, с которой утилита не работает, и молча скачает не то.
2. **Три набора `--abi`.** С одним только `cp312` подбор падает
   с `ResolutionImpossible`: `pypdfium2`, нужный для PDF, собран с тегом `none`.
3. **`--only-binary=:all:`** — иначе приедут исходники, которые на сервере
   нечем собирать.

## Что внутри архива

36 пакетов, из них тяжёлые — `magika` и `onnxruntime` (это ~90 МБ из 92).
Они обязательны: markitdown требует `magika` и без неё не ставится вовсе.

```
markitdown  magika  onnxruntime  numpy  protobuf  flatbuffers
mammoth  cobble  lxml  python_pptx  xlsxwriter  openpyxl  et_xmlfile
pdfplumber  pdfminer_six  pypdfium2  cryptography  cffi  pycparser
pandas  python_dateutil  six  beautifulsoup4  soupsieve  markdownify
requests  urllib3  certifi  idna  charset_normalizer  defusedxml
click  packaging  pillow  python_dotenv  typing_extensions
```

Одного файла `markitdown-0.1.7-py3-none-any.whl`, скачанного с сайта пакета,
**недостаточно** — в нём нет ни одной зависимости. Как и архива с исходниками
markitdown: его пришлось бы собирать.
