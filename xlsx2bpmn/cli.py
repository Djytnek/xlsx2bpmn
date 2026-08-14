# -*- coding: utf-8 -*-
"""
Консольная утилита. Работает офлайн: ни сети, ни сервера.

Одна программа, два имени — направление берётся из того, как её позвали:

    xlsx2bpmn process.xlsx                 # таблица  -> диаграмма
    bpmn2xlsx process.bpmn                 # диаграмма -> таблица
    xlsx2bpmn --template process.xlsx      # создать пустой шаблон

Перекрыть направление можно ключами --to-bpmn / --to-table.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from . import __version__
from .core import ConvertError, convert
from .layout import LayoutError, apply_layout
from .layout_native import layout_process
from .render_png import to_png
from .render_svg import to_svg
from .to_table import to_table, to_workbook

TEMPLATE = Path(__file__).parent / "template.xlsx"

BPMN_HEAD = re.compile(rb"<\s*(\w+:)?definitions", re.I)


def _looks_like_bpmn(data: bytes) -> bool:
    return data[:2] != b"PK" and bool(BPMN_HEAD.search(data[:8000]))


def _draw(xml: str, out: Path, args) -> str:
    """Рисует картинки рядом с результатом по ключам --svg и --png."""
    made = []
    for wanted, suffix, render in ((args.svg, ".svg", lambda: to_svg(xml).encode("utf-8")),
                                   (args.png, ".png", lambda: to_png(xml))):
        if not wanted:
            continue
        picture = out.with_suffix(suffix)
        try:
            picture.write_bytes(render())
        except (ConvertError, OSError) as exc:
            print(f"WARN {suffix} не нарисован: {exc}", file=sys.stderr)
            continue
        made.append(picture.name)
    return (" + " + " + ".join(made)) if made else ""


def _table_one(path: Path, out: Path, args) -> int:
    """Обратный ход: диаграмма -> таблица."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        print(f"{path.name}: ERROR {exc}", file=sys.stderr)
        return 2
    if not _looks_like_bpmn(data):
        print(f"{path.name}: ERROR это не BPMN-диаграмма. Таблицу в диаграмму "
              f"превращает xlsx2bpmn", file=sys.stderr)
        return 2

    try:
        result = to_table(data, full=out.suffix.lower() == ".xlsx")
    except ConvertError as exc:
        print(f"{path.name}: ERROR {exc}", file=sys.stderr)
        return 2

    lines = [str(i) for i in result.issues]
    if not args.quiet:
        for line in lines:
            print(f"{path.name}: {line}", file=sys.stderr)
    if args.report:
        Path(args.report).write_text("\n".join(lines), encoding="utf-8")

    try:
        if out.suffix.lower() == ".xlsx":
            out.write_bytes(to_workbook(result, TEMPLATE.read_bytes()))
        else:
            out.write_text(result.table + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"{path.name}: ERROR не удалось записать {out}: {exc}", file=sys.stderr)
        return 2

    # у присланной диаграммы координат может не быть — тогда рисуем по своей
    source = data.decode("utf-8-sig", "replace")
    if (args.svg or args.png) and "BPMNPlane" not in source:
        rebuilt = convert(result.table.encode("utf-8"))
        if rebuilt.ok:
            try:
                source, _ = apply_layout(rebuilt.xml, layout_process)
            except LayoutError:
                pass
    picture = _draw(source, out, args)
    if not args.quiet:
        s = result.stats
        print(f"OK -> {out}{picture}  элементов {s['nodes']}, потоков {s['flows']}, "
              f"сообщений {s['message_flows']}, пулов {s['pools']}, "
              f"колонок {s['columns']}, потерь {s['warnings']}")
    return 0


def _convert_one(path: Path, out: Path, args) -> int:
    try:
        data = path.read_bytes()
    except OSError as exc:
        print(f"{path.name}: ERROR {exc}", file=sys.stderr)
        return 2
    if _looks_like_bpmn(data):
        print(f"{path.name}: ERROR это уже BPMN-диаграмма. Обратно в таблицу "
              f"её превращает bpmn2xlsx", file=sys.stderr)
        return 2

    try:
        result = convert(data, sheet=args.sheet, strict=args.strict,
                         executable=args.executable, target=args.target)
    except (ConvertError, OSError) as exc:
        print(f"{path.name}: ERROR {exc}", file=sys.stderr)
        return 2

    lines = [str(i) for i in result.issues]
    if not result.ok:
        for line in lines:
            print(f"{path.name}: {line}", file=sys.stderr)
        if args.report:
            Path(args.report).write_text("\n".join(lines), encoding="utf-8")
        print(f"{path.name}: не сконвертировано — ошибок "
              f"{result.stats['errors']}", file=sys.stderr)
        return 1

    xml = result.xml
    if not args.no_layout:
        try:
            xml, extra = apply_layout(xml, layout_process)
            lines += [f"[файл] WARN  {w}" for w in extra]
        except LayoutError as exc:
            print(f"{path.name}: ERROR раскладка не выполнена: {exc}", file=sys.stderr)
            return 3

    if not args.quiet:
        for line in lines:
            print(f"{path.name}: {line}", file=sys.stderr)
    if args.report:
        Path(args.report).write_text("\n".join(lines), encoding="utf-8")

    out.write_text(xml, encoding="utf-8")
    picture = _draw(xml, out, args)
    if not args.quiet:
        s = result.stats
        print(f"OK -> {out}{picture}  элементов {s['nodes']}, потоков {s['flows']}, "
              f"сообщений {s['message_flows']}, пулов {s['pools']}")
    return 0


def main() -> int:
    prog = Path(sys.argv[0]).name
    reverse = "bpmn2" in prog
    ap = argparse.ArgumentParser(
        prog=prog or "xlsx2bpmn",
        description="Таблица процесса <-> BPMN 2.0 XML. Работает офлайн. "
                    "xlsx2bpmn собирает диаграмму, bpmn2xlsx разбирает её обратно.")
    ap.add_argument("--version", action="version",
                    version=f"xlsx2bpmn {__version__}")
    ap.add_argument("input", nargs="*", help="один или несколько файлов")
    ap.add_argument("-o", "--output", help="путь к результату (только для одного файла)")
    direction = ap.add_mutually_exclusive_group()
    direction.add_argument("--to-bpmn", action="store_true",
                           help="таблица -> диаграмма (по умолчанию для xlsx2bpmn)")
    direction.add_argument("--to-table", action="store_true",
                           help="диаграмма -> таблица (по умолчанию для bpmn2xlsx)")
    ap.add_argument("--txt", action="store_true",
                    help="при разборе писать текстовую таблицу, а не .xlsx")
    ap.add_argument("--svg", action="store_true",
                    help="дополнительно нарисовать картинку .svg рядом с результатом")
    ap.add_argument("--png", action="store_true",
                    help="то же, но растром .png")
    ap.add_argument("--template", metavar="PATH",
                    help="создать пустой шаблон таблицы по этому пути и выйти")
    ap.add_argument("--sheet", default=None, help="имя листа (по умолчанию Process)")
    ap.add_argument("--target", choices=["plain", "camunda8"], default="plain")
    ap.add_argument("--no-layout", action="store_true",
                    help="не расставлять координаты: только структура BPMN")
    ap.add_argument("--strict", action="store_true",
                    help="предупреждения блокируют генерацию")
    ap.add_argument("--executable", action="store_true")
    ap.add_argument("--report", help="файл для отчёта")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    if args.template:
        dest = Path(args.template)
        if dest.exists():
            print(f"ERROR {dest} уже существует", file=sys.stderr)
            return 2
        shutil.copyfile(TEMPLATE, dest)
        print(f"Шаблон создан: {dest}")
        return 0

    if args.to_table:
        reverse = True
    elif args.to_bpmn:
        reverse = False

    if not args.input:
        ap.error("укажите файл .bpmn" if reverse
                 else "укажите файл .xlsx или используйте --template")
    if args.output and len(args.input) > 1:
        ap.error("-o работает только с одним файлом")

    if reverse:
        suffix = ".txt" if args.txt else ".xlsx"
        worst = 0
        for name in args.input:
            path = Path(name)
            out = Path(args.output) if args.output else path.with_suffix(suffix)
            worst = max(worst, _table_one(path, out, args))
        return worst

    worst = 0
    for name in args.input:
        path = Path(name)
        out = Path(args.output) if args.output else path.with_suffix(".bpmn")
        worst = max(worst, _convert_one(path, out, args))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
