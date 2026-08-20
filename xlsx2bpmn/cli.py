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
import subprocess
import sys
from pathlib import Path

from . import __version__
from .core import SUBPROCESS_TYPES, ConvertError, convert
from .layout import (LayoutError, apply_layout, engine_name, layout_hint,
                     layout_name, node_home, node_script, pick_layout)
from .layout_native import layout_process
from .render_png import to_png
from .render_svg import to_svg
from .to_table import to_table, to_workbook

TEMPLATE = Path(__file__).parent / "template.xlsx"

BPMN_HEAD = re.compile(rb"<\s*(\w+:)?definitions", re.I)
PICTURES = {".png", ".svg"}
NAME_BAD = re.compile(r'[\\/:*?"<>|]+')


def _setup_layout() -> int:
    """Запасной путь: доустановить раскладчик из сети.

    Обычно он не нужен — bpmn-auto-layout зашит в пакет одним файлом и
    работает сразу. Команда пригодится, если этот файл почему-то не подошёл.
    """
    if node_script() is not None:
        print(f"Раскладчик уже работает: {layout_name('node')}. "
              f"Ничего делать не нужно")
        return 0
    if shutil.which("node") is None:
        print("ERROR не найден Node.js. Поставьте версию 20 или новее: "
              "https://nodejs.org — после этого раскладчик заработает сам, "
              "он уже внутри пакета", file=sys.stderr)
        return 2
    source = node_home()
    if source is None:
        print("ERROR не найден файл раскладчика cli.mjs", file=sys.stderr)
        return 2
    if shutil.which("npm") is None:
        print("ERROR не найден npm. Нужен Node.js 20 или новее: "
              "https://nodejs.org", file=sys.stderr)
        return 2

    # ставим в домашнюю папку: site-packages бывает недоступен на запись,
    # а этот путь ищется первым
    home = Path.home() / ".xlsx2bpmn" / "layout-node"
    try:
        home.mkdir(parents=True, exist_ok=True)
        for name in ("cli.mjs", "package.json"):
            if (source / name).is_file():
                shutil.copyfile(source / name, home / name)
    except OSError as exc:
        print(f"ERROR не удалось подготовить {home}: {exc}", file=sys.stderr)
        return 2

    print(f"Ставлю bpmn-auto-layout в {home} ...")
    try:
        code = subprocess.call(["npm", "install", "--no-audit", "--no-fund"],
                               cwd=home)
    except OSError as exc:
        print(f"ERROR npm не запустился: {exc}", file=sys.stderr)
        return 2
    if code != 0:
        print("ERROR npm вернул ошибку. Проверьте интернет и версию Node",
              file=sys.stderr)
        return 2
    print(f"Готово. Раскладчик: {layout_name('auto')}")
    return 0


def _looks_like_bpmn(data: bytes) -> bool:
    return data[:2] != b"PK" and bool(BPMN_HEAD.search(data[:8000]))


def _draw(xml: str, out: Path, args) -> str:
    """Рисует картинки рядом с результатом по ключам --svg и --png."""
    level = getattr(args, "level", None)
    made = []
    for wanted, suffix, render in (
            (args.svg, ".svg", lambda: to_svg(xml, level=level).encode("utf-8")),
            (args.png, ".png", lambda: to_png(xml, level=level))):
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


def _diagram(path: Path, args) -> str:
    """Разложенная диаграмма, что бы ни дали на вход — таблицу или .bpmn."""
    data = path.read_bytes()
    if _looks_like_bpmn(data):
        xml = data.decode("utf-8-sig", "replace")
        if "BPMNPlane" in xml:
            return xml
        result = convert(to_table(data).table.encode("utf-8"))
    else:
        result = convert(data, sheet=args.sheet, strict=args.strict,
                         executable=args.executable, target=args.target)
    if not result.ok:
        raise ConvertError("\n".join(str(i) for i in result.errors))
    xml, _ = apply_layout(result.xml, pick_layout(args.layout))
    return xml


def _sublevels(rows: list[dict]) -> list[str]:
    """id субпроцессов с содержимым, в порядке появления в таблице.

    Пустой субпроцесс и callActivity уровнем не считаются: внутри них нечего
    показывать — вызываемый процесс живёт в своём файле.
    """
    filled = {r["parent_id"] for r in rows if r["parent_id"]}
    return [r["id"] for r in rows
            if r["type"] in SUBPROCESS_TYPES and r["id"] in filled]


def _folder_name(text: str, fallback: str) -> str:
    """Имя файла из названия уровня: без запрещённых знаков и без пробелов."""
    name = "-".join(NAME_BAD.sub("-", text).split()).strip("-. ")
    return name[:60] or fallback


def _bundle(path: Path, out: Path, args) -> int:
    """Раскладывает каждый уровень процесса тремя файлами в одну папку."""
    try:
        master = _diagram(path, args)
    except (ConvertError, LayoutError, OSError) as exc:
        print(f"{path.name}: ERROR {exc}", file=sys.stderr)
        return 2

    rows = to_table(master).rows
    names = {r["id"]: r["name"] or r["id"] for r in rows}
    levels = [("", out.name)] + [(i, names[i]) for i in _sublevels(rows)]

    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR не удалось создать папку {out}: {exc}", file=sys.stderr)
        return 2

    template = TEMPLATE.read_bytes()
    if not args.quiet:
        print(f"Папка: {out}")
    made, worst = 0, 0
    for number, (level, title) in enumerate(levels, start=1):
        stem = f"{number:02d}-{_folder_name(title, level or 'process')}"
        # верхний уровень отдаём целиком: и вложенное содержимое, и все полотна
        try:
            table = to_table(master, full=True, level=None if not level else level)
            xml = master
            if level:
                rebuilt = convert(table.table.encode("utf-8"))
                if not rebuilt.ok:
                    raise ConvertError("\n".join(str(i) for i in rebuilt.errors))
                xml, _ = apply_layout(rebuilt.xml, pick_layout(args.layout))
            book = to_workbook(table, template, split=not level)
        except (ConvertError, LayoutError) as exc:
            print(f"  {stem}: ERROR {exc}", file=sys.stderr)
            worst = max(worst, 1)
            continue

        wrote = []
        try:
            for suffix, blob in ((".xlsx", book), (".bpmn", xml.encode("utf-8"))):
                (out / (stem + suffix)).write_bytes(blob)
                wrote.append(suffix)
        except OSError as exc:
            print(f"  {stem}: ERROR не удалось записать: {exc}", file=sys.stderr)
            worst = max(worst, 2)
            continue
        # xml здесь — уже отдельная схема этого уровня, целиться в полотно не надо
        for suffix, render in ((".png", lambda: to_png(xml)),
                               (".svg", lambda: to_svg(xml).encode("utf-8"))):
            if suffix == ".svg" and not args.svg:
                continue
            try:
                (out / (stem + suffix)).write_bytes(render())
                wrote.append(suffix)
            except (ConvertError, OSError) as exc:
                print(f"  {stem}{suffix} не нарисован: {exc}", file=sys.stderr)
                worst = max(worst, 3)

        made += len(wrote)
        if not args.quiet:
            what = "весь процесс" if not level else level
            print(f"  {stem}{' + '.join(wrote)}   {what}, "
                  f"элементов {len(table.rows)}")

    if not args.quiet:
        print(f"Готово: уровней {len(levels)}, файлов {made}")
    return worst


def _levels(path: Path, args) -> int:
    """Печатает уровни схемы: сам процесс и каждый субпроцесс."""
    try:
        xml = _diagram(path, args)
    except (ConvertError, LayoutError, OSError) as exc:
        print(f"{path.name}: ERROR {exc}", file=sys.stderr)
        return 2

    rows = to_table(xml).rows
    inside: dict[str, int] = {}
    for row in rows:
        inside[row["parent_id"]] = inside.get(row["parent_id"], 0) + 1
    names = {row["id"]: row["name"] or row["id"] for row in rows}

    print(f"{'уровень':<22}{'название':<34}элементов")
    print(f"{'(весь процесс)':<22}{'':<34}{inside.get('', 0)}")
    for row in rows:
        if row["type"] in ("subProcess", "callActivity"):
            print(f"{row['id']:<22}{names[row['id']][:32]:<34}"
                  f"{inside.get(row['id'], 0)}")
    print()
    print("Взять один уровень:  --level ID")
    return 0


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

    picture_only = out.suffix.lower() in PICTURES
    try:
        result = to_table(data, full=out.suffix.lower() == ".xlsx",
                          level=args.level)
    except ConvertError as exc:
        print(f"{path.name}: ERROR {exc}", file=sys.stderr)
        return 2

    lines = [str(i) for i in result.issues]
    if not args.quiet:
        for line in lines:
            print(f"{path.name}: {line}", file=sys.stderr)
    if args.report:
        Path(args.report).write_text("\n".join(lines), encoding="utf-8")

    if picture_only:
        args.png = out.suffix.lower() == ".png"
        args.svg = out.suffix.lower() == ".svg"
    else:
        try:
            if out.suffix.lower() == ".xlsx":
                out.write_bytes(to_workbook(result, TEMPLATE.read_bytes(),
                                            split=args.level is None))
            else:
                out.write_text(result.table + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"{path.name}: ERROR не удалось записать {out}: {exc}",
                  file=sys.stderr)
            return 2

    # у присланной диаграммы координат может не быть — тогда рисуем по своей
    source = data.decode("utf-8-sig", "replace")
    if (args.svg or args.png) and "BPMNPlane" not in source:
        rebuilt = convert(result.table.encode("utf-8"))
        if rebuilt.ok:
            try:
                source, _ = apply_layout(rebuilt.xml, pick_layout(args.layout))
            except LayoutError:
                pass
    picture = _draw(source, out, args)
    if picture_only and not picture:
        return 3
    if not args.quiet:
        s = result.stats
        where = picture[3:] if picture_only else f"{out}{picture}"
        print(f"OK -> {where}  элементов {s['nodes']}, потоков {s['flows']}, "
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
    engine = pick_layout(args.layout) if not args.no_layout else None
    if engine is not None:
        try:
            xml, extra = apply_layout(xml, engine)
            lines += [f"[файл] WARN  {w}" for w in extra]
        except LayoutError as exc:
            print(f"{path.name}: ERROR раскладка не выполнена: {exc}", file=sys.stderr)
            return 3

    if not args.quiet:
        for line in lines:
            print(f"{path.name}: {line}", file=sys.stderr)
    if args.report:
        Path(args.report).write_text("\n".join(lines), encoding="utf-8")

    if out.suffix.lower() in PICTURES:          # попросили только картинку
        args.png = out.suffix.lower() == ".png"
        args.svg = out.suffix.lower() == ".svg"
        made = _draw(xml, out, args)
        if not made:
            return 3
        if not args.quiet:
            s = result.stats
            print(f"OK -> {made[3:]}  элементов {s['nodes']}, потоков {s['flows']}, "
                  f"сообщений {s['message_flows']}, пулов {s['pools']}, "
                  f"раскладка: {engine_name(engine, args.layout)}"
                  f"{layout_hint(args.layout)}")
        return 0

    out.write_text(xml, encoding="utf-8")
    picture = _draw(xml, out, args)
    if not args.quiet:
        s = result.stats
        name = "нет" if engine is None else engine_name(engine, args.layout)
        print(f"OK -> {out}{picture}  элементов {s['nodes']}, потоков {s['flows']}, "
              f"сообщений {s['message_flows']}, пулов {s['pools']}, "
              f"раскладка: {name}"
              f"{'' if args.no_layout else layout_hint(args.layout)}")
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
    ap.add_argument("--level", metavar="ID",
                    help="работать не со всем процессом, а с содержимым "
                         "субпроцесса: и картинка, и таблица будут только его")
    ap.add_argument("--levels", action="store_true",
                    help="показать уровни схемы и выйти")
    ap.add_argument("--all-levels", action="store_true",
                    help="сложить в папку все уровни сразу: каждому — таблица, "
                         ".bpmn и картинка. Имя папки берётся от файла, "
                         "перекрывается ключом -o")
    ap.add_argument("--png", action="store_true",
                    help="то же, но растром .png")
    ap.add_argument("--setup-layout", action="store_true",
                    help="проверить раскладчик и, если нужно, доустановить его "
                         "из сети. Обычно не требуется: он уже внутри пакета")
    ap.add_argument("--template", metavar="PATH",
                    help="создать пустой шаблон таблицы по этому пути и выйти")
    ap.add_argument("--sheet", default=None, help="имя листа (по умолчанию Process)")
    ap.add_argument("--target", choices=["plain", "camunda8"], default="plain")
    ap.add_argument("--no-layout", action="store_true",
                    help="не расставлять координаты: только структура BPMN")
    ap.add_argument("--layout", choices=["auto", "native", "node"], default="auto",
                    help="auto (по умолчанию) — по схеме: с дорожками встроенный, "
                         "он единственный их понимает, без дорожек "
                         "bpmn-auto-layout, он ведёт процесс ровнее. "
                         "native и node — взять один и тот же принудительно")
    ap.add_argument("--strict", action="store_true",
                    help="предупреждения блокируют генерацию")
    ap.add_argument("--executable", action="store_true")
    ap.add_argument("--report", help="файл для отчёта")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    if args.setup_layout:
        return _setup_layout()

    if args.template:
        dest = Path(args.template)
        if dest.exists():
            print(f"ERROR {dest} уже существует", file=sys.stderr)
            return 2
        shutil.copyfile(TEMPLATE, dest)
        print(f"Шаблон создан: {dest}")
        return 0

    if args.levels:
        if not args.input:
            ap.error("укажите файл, уровни которого показать")
        return _levels(Path(args.input[0]), args)

    if args.all_levels:
        if not args.input:
            ap.error("укажите файл, уровни которого разложить по папке")
        if args.output and len(args.input) > 1:
            ap.error("-o работает только с одним файлом")
        worst = 0
        for name in args.input:
            path = Path(name)
            folder = Path(args.output) if args.output else path.with_suffix("")
            worst = max(worst, _bundle(path, folder, args))
        return worst

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
