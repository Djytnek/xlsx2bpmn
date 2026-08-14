# -*- coding: utf-8 -*-
"""
Отрисовка схемы в PNG.

Рисуется теми же координатами, что и SVG, но растром через Pillow — без
системных библиотек и без внешних программ. Шрифт лежит внутри пакета,
поэтому кириллица работает в любом окружении, включая пустой контейнер.

Единственная точка входа: to_png(xml: str | bytes, scale: float) -> bytes
"""
from __future__ import annotations

import io
import math
import xml.etree.ElementTree as ET
from pathlib import Path

from .core import ConvertError
from .render_svg import (ACTIVITY_TAGS, DC, DI, D, EVENT_TAGS, LABEL_BAND,
                         _bounds, _index, _wrap)
from .core import GATEWAY_TYPES

FONT_FILE = Path(__file__).parent / "DejaVuSans.ttf"
FONT_FALLBACK = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)

PAPER = (255, 255, 255)
STROKE = (58, 58, 58)
FILL = (255, 255, 255)
BAND = (244, 244, 245)
TEXT = (26, 26, 26)

FONT_SIZE = 12
MARGIN = 24
DASH_ON, DASH_OFF = 8, 5


def _font(size: int):
    from PIL import ImageFont

    for candidate in (FONT_FILE, *map(Path, FONT_FALLBACK)):
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), size)
            except OSError:
                continue
    return ImageFont.load_default()


# --------------------------------------------------------------------------- #
# Примитивы
# --------------------------------------------------------------------------- #

def _dashed(draw, points, width: int) -> None:
    """Пунктир: Pillow его не умеет, режем отрезки руками."""
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        length = math.hypot(x2 - x1, y2 - y1)
        if length < 1:
            continue
        dx, dy = (x2 - x1) / length, (y2 - y1) / length
        position, draw_now = 0.0, True
        while position < length:
            step = min(DASH_ON if draw_now else DASH_OFF, length - position)
            if draw_now:
                draw.line([(x1 + dx * position, y1 + dy * position),
                           (x1 + dx * (position + step), y1 + dy * (position + step))],
                          fill=STROKE, width=width)
            position += step
            draw_now = not draw_now


def _arrow(draw, tail, head, size: float, filled: bool) -> None:
    angle = math.atan2(head[1] - tail[1], head[0] - tail[0])
    wing = math.radians(24)
    points = [head,
              (head[0] - size * math.cos(angle - wing),
               head[1] - size * math.sin(angle - wing)),
              (head[0] - size * math.cos(angle + wing),
               head[1] - size * math.sin(angle + wing))]
    draw.polygon(points, fill=STROKE if filled else PAPER, outline=STROKE)


def _icon(draw, x: float, y: float, tag: str, s: float) -> None:
    """Значок типа задачи в левом верхнем углу, как принято в BPMN."""
    ax, ay = x + 6 * s, y + 6 * s
    w = max(int(1.2 * s), 1)

    if tag == "userTask":                              # человечек
        draw.ellipse([ax + 4 * s, ay + s, ax + 10 * s, ay + 7 * s],
                     outline=STROKE, width=w)
        draw.arc([ax + s, ay + 8 * s, ax + 13 * s, ay + 20 * s], 180, 360,
                 fill=STROKE, width=w)
        draw.line([(ax + s, ay + 14 * s), (ax + 13 * s, ay + 14 * s)],
                  fill=STROKE, width=w)
    elif tag == "serviceTask":                         # шестерёнка
        cx, cy = ax + 7 * s, ay + 7 * s
        draw.ellipse([cx - 4.5 * s, cy - 4.5 * s, cx + 4.5 * s, cy + 4.5 * s],
                     outline=STROKE, width=w)
        draw.ellipse([cx - 1.6 * s, cy - 1.6 * s, cx + 1.6 * s, cy + 1.6 * s],
                     outline=STROKE, width=w)
        for i in range(6):
            angle = i * math.pi / 3
            draw.line([(cx + 4.5 * s * math.cos(angle), cy + 4.5 * s * math.sin(angle)),
                       (cx + 7 * s * math.cos(angle), cy + 7 * s * math.sin(angle))],
                      fill=STROKE, width=w)
    elif tag in ("sendTask", "receiveTask"):           # конверт
        box = [ax, ay + 2 * s, ax + 14 * s, ay + 12 * s]
        draw.rectangle(box, fill=STROKE if tag == "sendTask" else None,
                       outline=STROKE, width=w)
        draw.line([(ax, ay + 2 * s), (ax + 7 * s, ay + 8 * s),
                   (ax + 14 * s, ay + 2 * s)],
                  fill=PAPER if tag == "sendTask" else STROKE, width=w)
    elif tag == "manualTask":                          # кисть руки
        draw.rounded_rectangle([ax + 2 * s, ay + 5 * s, ax + 14 * s, ay + 13 * s],
                               radius=2 * s, outline=STROKE, width=w)
        for offset in (5, 8, 11):
            draw.line([(ax + offset * s, ay + 5 * s), (ax + offset * s, ay + 2 * s)],
                      fill=STROKE, width=w)
    elif tag == "scriptTask":                          # свиток со строками
        draw.rectangle([ax + 2 * s, ay + s, ax + 12 * s, ay + 13 * s],
                       outline=STROKE, width=w)
        for offset in (4, 7, 10):
            draw.line([(ax + 4 * s, ay + offset * s), (ax + 10 * s, ay + offset * s)],
                      fill=STROKE, width=w)
    elif tag == "businessRuleTask":                    # табличка правил
        draw.rectangle([ax, ay + 2 * s, ax + 14 * s, ay + 13 * s],
                       outline=STROKE, width=w)
        draw.line([(ax, ay + 5.5 * s), (ax + 14 * s, ay + 5.5 * s)], fill=STROKE, width=w)
        draw.line([(ax + 5 * s, ay + 5.5 * s), (ax + 5 * s, ay + 13 * s)],
                  fill=STROKE, width=w)


def _text(draw, lines, cx, cy, font, leading) -> None:
    top = cy - (len(lines) - 1) * leading / 2
    for i, line in enumerate(lines):
        draw.text((cx, top + i * leading), line, font=font, fill=TEXT, anchor="mm")


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def to_png(xml: str | bytes, scale: float = 2.0) -> bytes:
    """Схема с координатами -> PNG. scale=2 даёт удвоенное разрешение."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise ConvertError(
            "Для PNG нужна библиотека Pillow. Переустановите пакет: "
            "pip install xlsx2bpmn") from None

    if isinstance(xml, bytes):
        xml = xml.decode("utf-8-sig", "replace")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ConvertError(f"Не удалось разобрать XML: {exc}") from exc

    plane = root.find(f".//{DI}BPMNPlane")
    if plane is None:
        raise ConvertError("в документе нет координат (BPMNPlane); "
                           "сначала разложите схему")

    info = _index(root)
    shapes = [e for e in plane if e.tag == f"{DI}BPMNShape"]
    edges = [e for e in plane if e.tag == f"{DI}BPMNEdge"]

    xs: list[float] = []
    ys: list[float] = []
    for shape in shapes:
        box = _bounds(shape)
        if box:
            xs += [box[0], box[0] + box[2]]
            ys += [box[1], box[1] + box[3]]
    for edge in edges:
        for point in edge.findall(f"{D}waypoint"):
            xs.append(float(point.get("x", 0)))
            ys.append(float(point.get("y", 0)))
    if not xs:
        raise ConvertError("в документе нет ни одной фигуры с координатами")

    minx, miny = min(xs) - MARGIN, min(ys) - MARGIN
    width = int((max(xs) - min(xs) + 2 * MARGIN) * scale)
    height = int((max(ys) - min(ys) + 2 * MARGIN) * scale)

    def sx(value: float) -> float:
        return (value - minx) * scale

    def sy(value: float) -> float:
        return (value - miny) * scale

    line_w = max(int(1.6 * scale), 1)
    font = _font(int(FONT_SIZE * scale))
    leading = (FONT_SIZE + 2) * scale

    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image)

    lanes, nodes = [], []
    for shape in shapes:
        meta = info.get(shape.get("bpmnElement", ""), {"tag": "task", "name": ""})
        (lanes if meta["tag"] in ("participant", "lane") else nodes).append((shape, meta))

    # --- рамки пулов и полосы дорожек ---------------------------------------
    for shape, meta in lanes:
        box = _bounds(shape)
        if not box:
            continue
        x, y, w, h = box
        draw.rectangle([sx(x), sy(y), sx(x + w), sy(y + h)], outline=STROKE, width=line_w)
        draw.rectangle([sx(x), sy(y), sx(x + LABEL_BAND), sy(y + h)],
                       fill=BAND, outline=STROKE, width=line_w)
        name = meta.get("name", "")
        if name:
            strip = Image.new("RGB", (int(h * scale), int(LABEL_BAND * scale)), BAND)
            ImageDraw.Draw(strip).text(
                (strip.width / 2, strip.height / 2), name[:60],
                font=font, fill=TEXT, anchor="mm")
            image.paste(strip.rotate(90, expand=True), (int(sx(x)) + 1, int(sy(y)) + 1))

    # --- узлы ----------------------------------------------------------------
    for shape, meta in nodes:
        box = _bounds(shape)
        if not box:
            continue
        x, y, w, h = box
        tag, name = meta["tag"], meta.get("name", "")
        left, top, right, bottom = sx(x), sy(y), sx(x + w), sy(y + h)
        cx, cy = (left + right) / 2, (top + bottom) / 2

        if tag in EVENT_TAGS:
            thick = max(int(4 * scale), 2) if tag == "endEvent" else line_w
            draw.ellipse([left, top, right, bottom], fill=FILL, outline=STROKE, width=thick)
            if tag in ("boundaryEvent", "intermediateCatchEvent",
                       "intermediateThrowEvent"):
                pad = 3 * scale
                draw.ellipse([left + pad, top + pad, right - pad, bottom - pad],
                             outline=STROKE, width=line_w)
            kind = meta.get("def", "")
            if kind == "terminate":
                pad = (right - left) / 2 - 7 * scale
                draw.ellipse([left + pad, top + pad, right - pad, bottom - pad], fill=STROKE)
            elif kind == "message":
                draw.rectangle([cx - 8 * scale, cy - 5 * scale,
                                cx + 8 * scale, cy + 6 * scale],
                               outline=STROKE, width=max(int(scale), 1))
                draw.line([(cx - 8 * scale, cy - 5 * scale), (cx, cy + 2 * scale),
                           (cx + 8 * scale, cy - 5 * scale)],
                          fill=STROKE, width=max(int(scale), 1))
            elif kind == "timer":
                pad = (right - left) / 2 - 8 * scale
                draw.ellipse([left + pad, top + pad, right - pad, bottom - pad],
                             outline=STROKE, width=max(int(scale), 1))
                draw.line([(cx, cy - 5 * scale), (cx, cy), (cx + 4 * scale, cy + 3 * scale)],
                          fill=STROKE, width=max(int(scale), 1))
            if name:
                _text(draw, _wrap(name, 120), cx, bottom + 10 * scale, font, leading)

        elif tag in GATEWAY_TYPES:
            draw.polygon([(cx, top), (right, cy), (cx, bottom), (left, cy)],
                         fill=FILL, outline=STROKE, width=line_w)
            mark = max(int(2.4 * scale), 2)
            if tag == "exclusiveGateway":
                draw.line([(cx - 7 * scale, cy - 7 * scale),
                           (cx + 7 * scale, cy + 7 * scale)], fill=STROKE, width=mark)
                draw.line([(cx + 7 * scale, cy - 7 * scale),
                           (cx - 7 * scale, cy + 7 * scale)], fill=STROKE, width=mark)
            elif tag == "parallelGateway":
                draw.line([(cx, cy - 9 * scale), (cx, cy + 9 * scale)], fill=STROKE, width=mark)
                draw.line([(cx - 9 * scale, cy), (cx + 9 * scale, cy)], fill=STROKE, width=mark)
            elif tag in ("inclusiveGateway", "eventBasedGateway"):
                pad = (right - left) / 2 - 8 * scale
                draw.ellipse([left + pad, top + pad, right - pad, bottom - pad],
                             outline=STROKE, width=mark)
            if name:
                _text(draw, _wrap(name, 120), cx, bottom + 10 * scale, font, leading)

        elif tag == "dataObjectReference":
            fold = 10 * scale
            draw.polygon([(left, top), (right - fold, top), (right, top + fold),
                          (right, bottom), (left, bottom)],
                         fill=FILL, outline=STROKE, width=line_w)
            draw.line([(right - fold, top), (right - fold, top + fold),
                       (right, top + fold)], fill=STROKE, width=line_w)
            if name:
                _text(draw, _wrap(name, 110), cx, bottom + 10 * scale, font, leading)

        else:                                        # задачи и субпроцессы
            draw.rounded_rectangle([left, top, right, bottom], radius=8 * scale,
                                   fill=FILL, outline=STROKE, width=line_w)
            _icon(draw, left, top, tag, scale)
            marker = meta.get("marker", "")
            shift = 0
            if marker or tag in ("subProcess", "callActivity"):
                shift = 8 * scale
                base = bottom - 12 * scale
                if marker == "mi_parallel":
                    for offset in (-5, 0, 5):
                        draw.line([(cx + offset * scale, base - 5 * scale),
                                   (cx + offset * scale, base + 5 * scale)],
                                  fill=STROKE, width=max(int(2 * scale), 1))
                elif marker == "mi_sequential":
                    for offset in (-4, 0, 4):
                        draw.line([(cx - 6 * scale, base + offset * scale),
                                   (cx + 6 * scale, base + offset * scale)],
                                  fill=STROKE, width=max(int(1.8 * scale), 1))
                elif marker == "loop":
                    draw.arc([cx - 6 * scale, base - 6 * scale,
                              cx + 6 * scale, base + 6 * scale],
                             30, 330, fill=STROKE, width=max(int(1.8 * scale), 1))
                if tag in ("subProcess", "callActivity"):
                    draw.rectangle([cx - 7 * scale, base - 7 * scale,
                                    cx + 7 * scale, base + 7 * scale],
                                   outline=STROKE, width=max(int(scale), 1))
                    draw.line([(cx, base - 4 * scale), (cx, base + 4 * scale)],
                              fill=STROKE, width=max(int(scale), 1))
                    draw.line([(cx - 4 * scale, base), (cx + 4 * scale, base)],
                              fill=STROKE, width=max(int(scale), 1))
            if name:
                _text(draw, _wrap(name, w - 14), cx, cy - shift + 6 * scale,
                      font, leading)

    # --- стрелки --------------------------------------------------------------
    for edge in edges:
        points = [(sx(float(p.get("x", 0))), sy(float(p.get("y", 0))))
                  for p in edge.findall(f"{D}waypoint")]
        if len(points) < 2:
            continue
        meta = info.get(edge.get("bpmnElement", ""), {})
        tag = meta.get("tag", "sequenceFlow")

        if tag == "messageFlow":
            _dashed(draw, points, line_w)
            _arrow(draw, points[-2], points[-1], 9 * scale, filled=False)
        elif tag in ("dataInputAssociation", "dataOutputAssociation", "association"):
            _dashed(draw, points, max(line_w - 1, 1))
            _arrow(draw, points[-2], points[-1], 7 * scale, filled=False)
        else:
            draw.line(points, fill=STROKE, width=line_w, joint="curve")
            _arrow(draw, points[-2], points[-1], 8 * scale, filled=True)

        name = meta.get("name", "")
        if name:
            mx, my = _mid(points)
            box = draw.textbbox((mx, my - 7 * scale), name, font=font, anchor="mm")
            draw.rectangle([box[0] - 3, box[1] - 1, box[2] + 3, box[3] + 1], fill=PAPER)
            draw.text((mx, my - 7 * scale), name, font=font, fill=TEXT, anchor="mm")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _mid(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Середина ломаной по длине — подпись не липнет к шлюзу-источнику."""
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1])
               for a, b in zip(points, points[1:])]
    half = sum(lengths) / 2
    for i, length in enumerate(lengths):
        if half <= length or i == len(lengths) - 1:
            share = half / length if length else 0
            return (points[i][0] + (points[i + 1][0] - points[i][0]) * share,
                    points[i][1] + (points[i + 1][1] - points[i][1]) * share)
        half -= length
    return points[0]
