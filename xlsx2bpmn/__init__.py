# -*- coding: utf-8 -*-
"""
Таблица процесса -> BPMN 2.0 XML с координатами.

Работает офлайн: ни сети, ни сервера, ни внешних сервисов.

    from xlsx2bpmn import convert, apply_layout, layout_process

    result = convert(open("process.xlsx", "rb").read())
    if result.ok:
        xml, warnings = apply_layout(result.xml, layout_process)

Обратный ход — to_table: разбирает готовую диаграмму обратно в таблицу.

    from xlsx2bpmn import to_table

    print(to_table(open("process.bpmn").read()).table)
"""
from .core import ConvertError, Issue, Result, convert
from .layout import LayoutError, apply_layout
from .layout_native import layout_process
from .read_doc import read_document
from .render_png import to_png
from .render_svg import to_svg
from .to_table import TableResult, to_table, to_workbook

__version__ = "1.14.0"
__all__ = ["convert", "apply_layout", "layout_process", "to_table", "to_workbook",
           "read_document", "to_svg", "to_png",
           "Result", "TableResult", "Issue", "ConvertError", "LayoutError"]
