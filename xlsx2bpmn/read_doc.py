# -*- coding: utf-8 -*-
"""
Чтение описания процесса из документа любого формата.

Простой текст и markdown читаются своими силами. Для docx, pdf, pptx, xlsx,
html и прочего используется markitdown — библиотека необязательная: без неё
работают только текстовые форматы, о чём приходит внятное сообщение.

Единственная точка входа: read_document(data: bytes, name: str) -> str
"""
from __future__ import annotations

import io
from pathlib import Path

from .core import ConvertError

# Эти форматы разбираем сами, markitdown для них не нужен
PLAIN = {"", ".txt", ".md", ".markdown", ".text"}

# Что умеет markitdown при установленных дополнениях
RICH = {".docx", ".doc", ".pdf", ".pptx", ".ppt", ".xlsx", ".xls",
        ".html", ".htm", ".csv", ".json", ".xml", ".epub", ".rtf", ".odt"}

NO_LIBRARY = (
    "Для файлов {ext} нужна библиотека markitdown, а она не установлена.\n"
    "Поставьте её командой:  pip install \"markitdown[docx,pdf,pptx,xlsx]\"\n"
    "Либо пришлите описание процесса обычным текстом или файлом .txt/.md."
)


def _plain(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ConvertError("Не удалось определить кодировку файла")


def read_document(data: bytes, name: str = "") -> str:
    """Возвращает текст документа. Формат определяется по расширению имени."""
    if not data:
        raise ConvertError("Файл пустой")

    ext = Path(name).suffix.lower()

    # текст и markdown — без посредников
    if ext in PLAIN and data[:2] != b"PK" and not data.startswith(b"%PDF"):
        text = _plain(data).strip()
        if not text:
            raise ConvertError("Файл пустой")
        return text

    try:
        from markitdown import MarkItDown
    except ImportError:
        raise ConvertError(NO_LIBRARY.format(ext=ext or "этого формата")) from None

    try:
        result = MarkItDown().convert_stream(io.BytesIO(data),
                                             file_extension=ext or None)
    except Exception as exc:                                   # noqa: BLE001
        raise ConvertError(f"Не удалось прочитать документ {name or ''}: "
                           f"{type(exc).__name__}: {exc}") from exc

    text = (result.text_content or "").strip()
    if not text:
        raise ConvertError(
            f"Из файла {name or ''} не удалось извлечь текст. "
            f"Возможно, это скан или картинка — тогда описание процесса "
            f"придётся набрать вручную.")
    return text


def supported() -> str:
    """Список форматов для сообщений пользователю."""
    return ", ".join(sorted(e.lstrip(".") for e in RICH | {".txt", ".md"}))
