# -*- coding: utf-8 -*-
"""
Отрисовка BPMN в SVG по готовым координатам.

Ни сети, ни зависимостей, ни браузера: координаты уже посчитаны раскладчиком
и лежат в BPMNDiagram, здесь они только превращаются в фигуры.

Единственная точка входа: to_svg(xml: str | bytes) -> str
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET

from .core import ConvertError, EVENT_DEFS, GATEWAY_TYPES, NS

B = f"{{{NS['bpmn']}}}"
DI = f"{{{NS['bpmndi']}}}"
DC = f"{{{NS['dc']}}}"
D = f"{{{NS['di']}}}"

PAD = 24
FONT = 12
LABEL_BAND = 30          # ширина полосы с названием пула/дорожки
CHAR_W = 0.55            # доля от кегля на символ, для переноса строк

STROKE = "#3a3a3a"
FILL = "#ffffff"
BAND = "#f4f4f5"
TEXT = "#1a1a1a"
PAPER = "#ffffff"

EVENT_TAGS = {"startEvent", "endEvent", "boundaryEvent",
              "intermediateCatchEvent", "intermediateThrowEvent"}
ACTIVITY_TAGS = {"task", "userTask", "serviceTask", "scriptTask", "manualTask",
                 "sendTask", "receiveTask", "businessRuleTask",
                 "subProcess", "callActivity"}
DEF_BY_TAG = {v: k for k, v in EVENT_DEFS.items()}


# --------------------------------------------------------------------------- #
# Мелочи
# --------------------------------------------------------------------------- #

def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def _n(value: float) -> str:
    return f"{value:.0f}"


def _wrap(text: str, width: float, font: int = FONT) -> list[str]:
    """Жадный перенос по словам под заданную ширину в пикселях."""
    if not text:
        return []
    limit = max(int(width / (font * CHAR_W)), 4)
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            lines.append(current)
        while len(word) > limit:                  # слово длиннее строки
            lines.append(word[:limit - 1] + "-")
            word = word[limit - 1:]
        current = word
    if current:
        lines.append(current)
    return lines[:4]


def _label(text: str, cx: float, cy: float, width: float,
           anchor: str = "middle", weight: str = "normal") -> str:
    lines = _wrap(text, width)
    if not lines:
        return ""
    top = cy - (len(lines) - 1) * (FONT + 2) / 2
    out = []
    for i, line in enumerate(lines):
        out.append(
            f"<text x='{_n(cx)}' y='{_n(top + i * (FONT + 2) + FONT * 0.35)}' "
            f"text-anchor='{anchor}' font-size='{FONT}' font-weight='{weight}' "
            f"fill='{TEXT}'>{_esc(line)}</text>")
    return "".join(out)


# --------------------------------------------------------------------------- #
# Разбор документа
# --------------------------------------------------------------------------- #

def _index(root: ET.Element) -> dict[str, dict]:
    """id -> сведения об элементе: тег, подпись, тип события, маркер."""
    out: dict[str, dict] = {}

    for collab in root.findall(f"{B}collaboration"):
        for part in collab.findall(f"{B}participant"):
            out[part.get("id", "")] = {"tag": "participant",
                                       "name": part.get("name", "")}
        for mf in collab.findall(f"{B}messageFlow"):
            out[mf.get("id", "")] = {"tag": "messageFlow", "name": mf.get("name", "")}

    for proc in root.findall(f"{B}process"):
        for el in proc.iter():
            eid = el.get("id")
            if not eid:
                continue
            tag = el.tag.split("}")[-1]
            if tag in ("process", "laneSet", "flowNodeRef"):
                continue
            info = {"tag": tag, "name": el.get("name", "")}
            for child in el:
                ctag = child.tag.split("}")[-1]
                if ctag in DEF_BY_TAG:
                    info["def"] = DEF_BY_TAG[ctag]
                elif ctag == "multiInstanceLoopCharacteristics":
                    info["marker"] = ("mi_sequential"
                                      if child.get("isSequential", "").lower() == "true"
                                      else "mi_parallel")
                elif ctag == "standardLoopCharacteristics":
                    info["marker"] = "loop"
            out[eid] = info
    return out


def _bounds(shape: ET.Element) -> tuple[float, float, float, float] | None:
    box = shape.find(f"{DC}Bounds")
    if box is None:
        return None
    return (float(box.get("x", 0)), float(box.get("y", 0)),
            float(box.get("width", 0)), float(box.get("height", 0)))


# --------------------------------------------------------------------------- #
# Фигуры
# --------------------------------------------------------------------------- #

def _band(x: float, y: float, w: float, h: float, name: str) -> str:
    """Рамка пула или дорожки с полосой названия слева."""
    parts = [f"<rect x='{_n(x)}' y='{_n(y)}' width='{_n(w)}' height='{_n(h)}' "
             f"fill='none' stroke='{STROKE}' stroke-width='1.6'/>",
             f"<rect x='{_n(x)}' y='{_n(y)}' width='{_n(LABEL_BAND)}' "
             f"height='{_n(h)}' fill='{BAND}' stroke='{STROKE}' stroke-width='1.6'/>"]
    if name:
        cx, cy = x + LABEL_BAND / 2, y + h / 2
        parts.append(
            f"<g transform='rotate(-90 {_n(cx)} {_n(cy)})'>"
            f"{_label(name, cx, cy, h - 8, weight='600')}</g>")
    return "".join(parts)


def _event(x: float, y: float, w: float, h: float, info: dict) -> str:
    cx, cy, r = x + w / 2, y + h / 2, min(w, h) / 2
    tag = info["tag"]
    width = 4 if tag == "endEvent" else 1.8
    parts = [f"<circle cx='{_n(cx)}' cy='{_n(cy)}' r='{_n(r)}' fill='{FILL}' "
             f"stroke='{STROKE}' stroke-width='{width}'/>"]
    if tag in ("boundaryEvent", "intermediateCatchEvent", "intermediateThrowEvent"):
        parts.append(f"<circle cx='{_n(cx)}' cy='{_n(cy)}' r='{_n(r - 3)}' "
                     f"fill='none' stroke='{STROKE}' stroke-width='1.6'/>")

    kind = info.get("def", "")
    if kind == "message":
        parts.append(
            f"<rect x='{_n(cx - 8)}' y='{_n(cy - 5)}' width='16' height='11' "
            f"fill='none' stroke='{STROKE}' stroke-width='1.3'/>"
            f"<path d='M{_n(cx - 8)},{_n(cy - 5)} L{_n(cx)},{_n(cy + 2)} "
            f"L{_n(cx + 8)},{_n(cy - 5)}' fill='none' stroke='{STROKE}' "
            f"stroke-width='1.3'/>")
    elif kind == "timer":
        parts.append(
            f"<circle cx='{_n(cx)}' cy='{_n(cy)}' r='8' fill='none' "
            f"stroke='{STROKE}' stroke-width='1.3'/>"
            f"<path d='M{_n(cx)},{_n(cy - 5)} L{_n(cx)},{_n(cy)} "
            f"L{_n(cx + 4)},{_n(cy + 3)}' fill='none' stroke='{STROKE}' "
            f"stroke-width='1.3'/>")
    elif kind == "terminate":
        parts.append(f"<circle cx='{_n(cx)}' cy='{_n(cy)}' r='7' fill='{STROKE}'/>")
    elif kind == "error":
        parts.append(
            f"<path d='M{_n(cx - 7)},{_n(cy + 6)} L{_n(cx - 2)},{_n(cy - 5)} "
            f"L{_n(cx + 2)},{_n(cy + 2)} L{_n(cx + 7)},{_n(cy - 6)}' fill='none' "
            f"stroke='{STROKE}' stroke-width='1.6'/>")
    elif kind == "signal":
        parts.append(
            f"<path d='M{_n(cx)},{_n(cy - 7)} L{_n(cx + 7)},{_n(cy + 5)} "
            f"L{_n(cx - 7)},{_n(cy + 5)} Z' fill='none' stroke='{STROKE}' "
            f"stroke-width='1.3'/>")
    return "".join(parts)


def _gateway(x: float, y: float, w: float, h: float, info: dict) -> str:
    cx, cy = x + w / 2, y + h / 2
    parts = [f"<path d='M{_n(cx)},{_n(y)} L{_n(x + w)},{_n(cy)} "
             f"L{_n(cx)},{_n(y + h)} L{_n(x)},{_n(cy)} Z' fill='{FILL}' "
             f"stroke='{STROKE}' stroke-width='1.6'/>"]
    tag = info["tag"]
    if tag == "exclusiveGateway":
        parts.append(f"<path d='M{_n(cx - 7)},{_n(cy - 7)} L{_n(cx + 7)},{_n(cy + 7)} "
                     f"M{_n(cx + 7)},{_n(cy - 7)} L{_n(cx - 7)},{_n(cy + 7)}' "
                     f"stroke='{STROKE}' stroke-width='2.4' fill='none'/>")
    elif tag == "parallelGateway":
        parts.append(f"<path d='M{_n(cx)},{_n(cy - 9)} L{_n(cx)},{_n(cy + 9)} "
                     f"M{_n(cx - 9)},{_n(cy)} L{_n(cx + 9)},{_n(cy)}' "
                     f"stroke='{STROKE}' stroke-width='2.4' fill='none'/>")
    elif tag == "inclusiveGateway":
        parts.append(f"<circle cx='{_n(cx)}' cy='{_n(cy)}' r='8' fill='none' "
                     f"stroke='{STROKE}' stroke-width='2.2'/>")
    elif tag == "eventBasedGateway":
        parts.append(f"<circle cx='{_n(cx)}' cy='{_n(cy)}' r='9' fill='none' "
                     f"stroke='{STROKE}' stroke-width='1.4'/>"
                     f"<circle cx='{_n(cx)}' cy='{_n(cy)}' r='6' fill='none' "
                     f"stroke='{STROKE}' stroke-width='1.4'/>")
    return "".join(parts)


def _task_icon(x: float, y: float, tag: str) -> str:
    """Значок типа задачи в левом верхнем углу, как принято в BPMN."""
    ax, ay = x + 6, y + 6                      # левый верхний угол значка
    thin = f"fill='none' stroke='{STROKE}' stroke-width='1.2'"

    if tag == "userTask":                      # человечек
        return (f"<circle cx='{_n(ax + 7)}' cy='{_n(ay + 4)}' r='3' {thin}/>"
                f"<path d='M{_n(ax + 1)},{_n(ay + 14)} a6,6 0 0,1 12,0' {thin}/>"
                f"<path d='M{_n(ax + 1)},{_n(ay + 14)} h12' {thin}/>")
    if tag == "serviceTask":                   # шестерёнка
        ticks = "".join(
            f"<path d='M{_n(ax + 7 + 4.5 * math.cos(a))},"
            f"{_n(ay + 7 + 4.5 * math.sin(a))} L"
            f"{_n(ax + 7 + 7 * math.cos(a))},{_n(ay + 7 + 7 * math.sin(a))}' {thin}/>"
            for a in (i * math.pi / 3 for i in range(6)))
        return (f"<circle cx='{_n(ax + 7)}' cy='{_n(ay + 7)}' r='4.5' {thin}/>"
                f"<circle cx='{_n(ax + 7)}' cy='{_n(ay + 7)}' r='1.6' {thin}/>{ticks}")
    if tag in ("sendTask", "receiveTask"):     # конверт: у отправки залитый
        fill = STROKE if tag == "sendTask" else "none"
        line = PAPER if tag == "sendTask" else STROKE
        return (f"<rect x='{_n(ax)}' y='{_n(ay + 2)}' width='14' height='10' "
                f"fill='{fill}' stroke='{STROKE}' stroke-width='1.2'/>"
                f"<path d='M{_n(ax)},{_n(ay + 2)} L{_n(ax + 7)},{_n(ay + 8)} "
                f"L{_n(ax + 14)},{_n(ay + 2)}' fill='none' stroke='{line}' "
                f"stroke-width='1.2'/>")
    if tag == "manualTask":                    # кисть руки
        return (f"<path d='M{_n(ax + 2)},{_n(ay + 13)} v-5 a2,2 0 0,1 4,0 v-3 "
                f"a2,2 0 0,1 4,0 v3 a2,2 0 0,1 4,0 v5 z' {thin}/>")
    if tag == "scriptTask":                    # свиток со строками
        return (f"<path d='M{_n(ax + 3)},{_n(ay + 1)} h9 a3,3 0 0,0 -3,4 v6 "
                f"a3,3 0 0,0 -3,4 h-9 a3,3 0 0,0 3,-4 v-6 a3,3 0 0,0 3,-4 z' {thin}/>"
                f"<path d='M{_n(ax + 4)},{_n(ay + 5)} h5 M{_n(ax + 3)},{_n(ay + 9)} h5' "
                f"{thin}/>")
    if tag == "businessRuleTask":              # табличка правил
        return (f"<rect x='{_n(ax)}' y='{_n(ay + 2)}' width='14' height='11' {thin}/>"
                f"<path d='M{_n(ax)},{_n(ay + 5.5)} h14 M{_n(ax + 5)},{_n(ay + 5.5)} "
                f"v7.5' {thin}/>")
    return ""


def _activity(x: float, y: float, w: float, h: float, info: dict) -> str:
    parts = [f"<rect x='{_n(x)}' y='{_n(y)}' width='{_n(w)}' height='{_n(h)}' "
             f"rx='8' ry='8' fill='{FILL}' stroke='{STROKE}' stroke-width='1.8'/>",
             _task_icon(x, y, info["tag"])]
    cx = x + w / 2
    marker = info.get("marker", "")
    icons = []
    if marker == "mi_parallel":
        icons.append(f"<path d='M{_n(cx - 5)},{_n(y + h - 14)} v10 M{_n(cx)},"
                     f"{_n(y + h - 14)} v10 M{_n(cx + 5)},{_n(y + h - 14)} v10' "
                     f"stroke='{STROKE}' stroke-width='2'/>")
    elif marker == "mi_sequential":
        icons.append(f"<path d='M{_n(cx - 6)},{_n(y + h - 13)} h12 M{_n(cx - 6)},"
                     f"{_n(y + h - 9)} h12 M{_n(cx - 6)},{_n(y + h - 5)} h12' "
                     f"stroke='{STROKE}' stroke-width='1.8'/>")
    elif marker == "loop":
        icons.append(f"<path d='M{_n(cx + 5)},{_n(y + h - 12)} a6,6 0 1,1 -5,-3' "
                     f"fill='none' stroke='{STROKE}' stroke-width='1.8'/>")
    if info["tag"] in ("subProcess", "callActivity"):
        icons.append(
            f"<rect x='{_n(cx - 7)}' y='{_n(y + h - 16)}' width='14' height='14' "
            f"fill='none' stroke='{STROKE}' stroke-width='1.4'/>"
            f"<path d='M{_n(cx)},{_n(y + h - 13)} v8 M{_n(cx - 4)},{_n(y + h - 9)} h8' "
            f"stroke='{STROKE}' stroke-width='1.4'/>")
    parts += icons
    offset = 8 if icons else 0
    parts.append(_label(info.get("name", ""), cx, y + h / 2 - offset + 6, w - 14))
    return "".join(parts)


def _data(x: float, y: float, w: float, h: float, info: dict) -> str:
    fold = 10
    parts = [f"<path d='M{_n(x)},{_n(y)} h{_n(w - fold)} l{_n(fold)},{_n(fold)} "
             f"v{_n(h - fold)} h{_n(-w)} Z' fill='{FILL}' stroke='{STROKE}' "
             f"stroke-width='1.6'/>",
             f"<path d='M{_n(x + w - fold)},{_n(y)} v{_n(fold)} h{_n(fold)}' "
             f"fill='none' stroke='{STROKE}' stroke-width='1.6'/>"]
    parts.append(_label(info.get("name", ""), x + w / 2, y + h + 12, 110))
    return "".join(parts)


def _midpoint(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Середина ломаной по длине — подпись не липнет к шлюзу-источнику."""
    lengths = [((points[i + 1][0] - points[i][0]) ** 2
                + (points[i + 1][1] - points[i][1]) ** 2) ** 0.5
               for i in range(len(points) - 1)]
    half = sum(lengths) / 2
    for i, length in enumerate(lengths):
        if half <= length or i == len(lengths) - 1:
            share = half / length if length else 0
            return (points[i][0] + (points[i + 1][0] - points[i][0]) * share,
                    points[i][1] + (points[i + 1][1] - points[i][1]) * share)
        half -= length
    return points[0]


def _edge(edge: ET.Element, info: dict) -> str:
    points = [(float(p.get("x", 0)), float(p.get("y", 0)))
              for p in edge.findall(f"{D}waypoint")]
    if len(points) < 2:
        return ""
    path = " ".join(f"{_n(x)},{_n(y)}" for x, y in points)
    tag = info.get("tag", "sequenceFlow")

    if tag == "messageFlow":
        style = f"stroke-dasharray='8 5' marker-end='url(#msg)'"
    elif tag in ("dataInputAssociation", "dataOutputAssociation", "association"):
        style = f"stroke-dasharray='2 4' marker-end='url(#thin)'"
    else:
        style = "marker-end='url(#arrow)'"

    out = [f"<polyline points='{path}' fill='none' stroke='{STROKE}' "
           f"stroke-width='1.6' {style}/>"]
    name = info.get("name", "")
    if name:
        mx, my = _midpoint(points)
        my -= 6
        out.append(f"<rect x='{_n(mx - len(name) * FONT * CHAR_W / 2 - 3)}' "
                   f"y='{_n(my - FONT)}' width='{_n(len(name) * FONT * CHAR_W + 6)}' "
                   f"height='{_n(FONT + 4)}' fill='{PAPER}' opacity='0.9'/>")
        out.append(_label(name, mx, my - FONT / 2, 200))
    return "".join(out)


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def to_svg(xml: str | bytes, *, padding: int = PAD) -> str:
    """Превращает BPMN с координатами в самостоятельный SVG."""
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

    minx, miny = min(xs) - padding, min(ys) - padding
    width = max(xs) - min(xs) + 2 * padding
    height = max(ys) - min(ys) + 2 * padding

    # пулы и дорожки — под низ, остальное поверх
    lanes, nodes = [], []
    for shape in shapes:
        meta = info.get(shape.get("bpmnElement", ""), {"tag": "task", "name": ""})
        (lanes if meta["tag"] in ("participant", "lane") else nodes).append((shape, meta))

    body: list[str] = []
    for shape, meta in lanes:
        box = _bounds(shape)
        if box:
            body.append(_band(*box, meta.get("name", "")))
    for shape, meta in nodes:
        box = _bounds(shape)
        if not box:
            continue
        tag = meta["tag"]
        if tag in EVENT_TAGS:
            body.append(_event(*box, meta))
            body.append(_label(meta.get("name", ""), box[0] + box[2] / 2,
                               box[1] + box[3] + 12, 120))
        elif tag in GATEWAY_TYPES:
            body.append(_gateway(*box, meta))
            body.append(_label(meta.get("name", ""), box[0] + box[2] / 2,
                               box[1] + box[3] + 12, 120))
        elif tag == "dataObjectReference":
            body.append(_data(*box, meta))
        elif tag in ACTIVITY_TAGS:
            body.append(_activity(*box, meta))
        else:
            body.append(_activity(*box, meta))
    for edge in edges:
        body.append(_edge(edge, info.get(edge.get("bpmnElement", ""), {})))

    defs = (
        f"<defs>"
        f"<marker id='arrow' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' "
        f"markerHeight='7' orient='auto-start-reverse'>"
        f"<path d='M0,1 L10,5 L0,9 z' fill='{STROKE}'/></marker>"
        f"<marker id='msg' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='8' "
        f"markerHeight='8' orient='auto-start-reverse'>"
        f"<path d='M0,1 L10,5 L0,9 z' fill='{PAPER}' stroke='{STROKE}'/></marker>"
        f"<marker id='thin' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' "
        f"markerHeight='7' orient='auto-start-reverse'>"
        f"<path d='M0,1 L10,5 L0,9' fill='none' stroke='{STROKE}'/></marker>"
        f"</defs>")

    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{_n(width)}' "
        f"height='{_n(height)}' viewBox='{_n(minx)} {_n(miny)} {_n(width)} "
        f"{_n(height)}' font-family='-apple-system, Segoe UI, Roboto, Arial, "
        f"sans-serif'>{defs}"
        f"<rect x='{_n(minx)}' y='{_n(miny)}' width='{_n(width)}' "
        f"height='{_n(height)}' fill='{PAPER}'/>"
        + "".join(body) + "</svg>")
