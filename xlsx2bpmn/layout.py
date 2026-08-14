# -*- coding: utf-8 -*-
"""
Сборка общего плана схемы из отдельно разложенных процессов.

Раскладчик работает с одним процессом за раз, поэтому здесь XML режется по
пулам, каждый процесс уходит в раскладчик отдельно, а планы сшиваются обратно:
достраиваются рамки пулов, полосы дорожек, значки документов и стрелки потоков
сообщений.

Наружу: apply_layout(xml, layout_fn) -> (xml_с_координатами, предупреждения)
где layout_fn: str -> str — любой раскладчик: на вход XML с одним процессом,
на выход он же с добавленным BPMNDiagram.
"""
from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .core import NS, TARGET_NS

# Геометрия. Значения совпадают с соглашениями bpmn.io.
DOC_W, DOC_H = 36, 50    # размер значка документа
DOC_GAP = 90             # насколько документ поднят над задачей
LABEL_BAND = 30      # ширина полосы с названием пула/дорожки
PAD = 40             # отступ от содержимого до рамки пула
POOL_GAP = 60        # зазор между пулами по вертикали

B = f"{{{NS['bpmn']}}}"
DI = f"{{{NS['bpmndi']}}}"
DC = f"{{{NS['dc']}}}"
D = f"{{{NS['di']}}}"


class LayoutError(Exception):
    """Раскладчик недоступен или вернул мусор."""


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


# --------------------------------------------------------------------------- #
# Разрезание
# --------------------------------------------------------------------------- #

def split_processes(xml: str) -> tuple[list[tuple[str, str]], dict]:
    """Делит документ на самостоятельные одно-процессные документы."""
    root = ET.fromstring(xml)

    participants: dict[str, tuple[str, str]] = {}
    message_flows: list[tuple[str, str, str]] = []
    collab = root.find(f"{B}collaboration")
    if collab is not None:
        for part in collab.findall(f"{B}participant"):
            participants[part.get("processRef", "")] = (part.get("id", ""),
                                                        part.get("name", "") or "")
        for mf in collab.findall(f"{B}messageFlow"):
            message_flows.append((mf.get("id", ""), mf.get("sourceRef", ""),
                                  mf.get("targetRef", "")))

    docs: list[tuple[str, str]] = []
    lanes: dict[str, list[tuple[str, str, list[str]]]] = {}
    data: dict[str, dict] = {}

    for proc in root.findall(f"{B}process"):
        pid = proc.get("id", "")
        clone = copy.deepcopy(proc)

        lane_set = clone.find(f"{B}laneSet")
        found: list[tuple[str, str, list[str]]] = []
        if lane_set is not None:
            for lane in lane_set.findall(f"{B}lane"):
                refs = [r.text or "" for r in lane.findall(f"{B}flowNodeRef")]
                found.append((lane.get("id", ""), lane.get("name", "") or "", refs))
        lanes[pid] = found              # сам laneSet остаётся в XML:
                                        # раскладчик строит по нему полосы

        refs = [(r.get("id", ""), r.get("name", "") or "")
                for r in proc.findall(f"{B}dataObjectReference")]
        assoc: list[tuple[str, str, str, str]] = []
        for act in proc.iter():
            act_id = act.get("id", "")
            for din in act.findall(f"{B}dataInputAssociation"):
                src = din.find(f"{B}sourceRef")
                if src is not None and src.text:
                    assoc.append((din.get("id", ""), "in", src.text.strip(), act_id))
            for dout in act.findall(f"{B}dataOutputAssociation"):
                tgt = dout.find(f"{B}targetRef")
                if tgt is not None and tgt.text:
                    assoc.append((dout.get("id", ""), "out", tgt.text.strip(), act_id))
        data[pid] = {"refs": refs, "assoc": assoc,
                     "objects": [o.get("id", "") for o in proc.findall(f"{B}dataObject")]}

        holder = ET.Element(f"{B}definitions", {
            "id": f"Defs_{pid}", "targetNamespace": TARGET_NS,
        })
        holder.append(clone)
        docs.append((pid, ET.tostring(holder, encoding="unicode", xml_declaration=True)))

    meta = {
        "participants": participants,
        "message_flows": message_flows,
        "lanes": lanes,
        "collaboration": collab.get("id") if collab is not None else None,
        "data": data,
    }
    return docs, meta


# --------------------------------------------------------------------------- #
# Сшивка
# --------------------------------------------------------------------------- #

def _bounds(el: ET.Element) -> ET.Element | None:
    return el.find(f"{DC}Bounds")


def _shift(el: ET.Element, dx: float, dy: float) -> None:
    for bounds in el.iter(f"{DC}Bounds"):
        bounds.set("x", _num(float(bounds.get("x", 0)) + dx))
        bounds.set("y", _num(float(bounds.get("y", 0)) + dy))
    for wp in el.iter(f"{D}waypoint"):
        wp.set("x", _num(float(wp.get("x", 0)) + dx))
        wp.set("y", _num(float(wp.get("y", 0)) + dy))


def _num(value: float) -> str:
    return str(int(round(value)))


def _extract(laid_xml: str) -> tuple[list[ET.Element], list[ET.Element],
                                     list[ET.Element]]:
    """Фигуры и стрелки основного плана плюс отдельные планы субпроцессов.

    Планы субпроцессов — самостоятельные полотна: их координаты не связаны
    с основным, поэтому они переносятся как есть.
    """
    try:
        root = ET.fromstring(laid_xml)
    except ET.ParseError as exc:
        raise LayoutError(f"раскладчик вернул невалидный XML: {exc}") from exc

    diagrams = root.findall(f"{DI}BPMNDiagram")
    plane = root.find(f".//{DI}BPMNPlane")
    if plane is None:
        raise LayoutError("в ответе раскладчика нет BPMNPlane")
    shapes = [e for e in plane if e.tag == f"{DI}BPMNShape"]
    edges = [e for e in plane if e.tag == f"{DI}BPMNEdge"]
    if not shapes:
        raise LayoutError("раскладчик не вернул ни одной фигуры")
    return shapes, edges, diagrams[1:]


def _bbox(shapes: list[ET.Element], edges: list[ET.Element]) -> Box:
    xs: list[float] = []
    ys: list[float] = []
    for el in shapes + edges:
        for bounds in el.iter(f"{DC}Bounds"):
            x, y = float(bounds.get("x", 0)), float(bounds.get("y", 0))
            xs += [x, x + float(bounds.get("width", 0))]
            ys += [y, y + float(bounds.get("height", 0))]
        for wp in el.iter(f"{D}waypoint"):
            xs.append(float(wp.get("x", 0)))
            ys.append(float(wp.get("y", 0)))
    return Box(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _lane_bands(lane_defs: list[tuple[str, str, list[str]]],
                boxes: dict[str, Box], pool: Box,
                warnings: list[str]) -> list[tuple[str, str, Box]]:
    """Режет пул на непересекающиеся горизонтальные полосы."""
    placed: list[tuple[str, str, float, float]] = []
    for lane_id, name, refs in lane_defs:
        members = [boxes[r] for r in refs if r in boxes]
        if not members:
            warnings.append(f"дорожка {name or lane_id!r}: ни один элемент не разложен, "
                            f"полоса не построена")
            continue
        placed.append((lane_id, name,
                       min(m.y for m in members), max(m.bottom for m in members)))
    if not placed:
        return []

    placed.sort(key=lambda t: (t[2] + t[3]) / 2)
    if any(placed[i][3] > placed[i + 1][2] for i in range(len(placed) - 1)):
        warnings.append("элементы разных дорожек перемешаны по вертикали: раскладчик "
                        "не учитывает дорожки, границы полос проведены приблизительно")

    x = pool.x + LABEL_BAND
    width = pool.w - LABEL_BAND
    out: list[tuple[str, str, Box]] = []
    for i, (lane_id, name, top, bottom) in enumerate(placed):
        upper = pool.y if i == 0 else (placed[i - 1][3] + top) / 2
        lower = pool.bottom if i == len(placed) - 1 else (bottom + placed[i + 1][2]) / 2
        out.append((lane_id, name, Box(x, upper, width, max(lower - upper, 40))))
    return out


def _place_data(shapes: list[ET.Element], edges: list[ET.Element],
                info: dict, warnings: list[str]) -> None:
    """Ставит значки документов над задачами и рисует ассоциации.

    Раскладчик может не поставить фигуру документа и не развести ассоциации —
    обрабатываются оба случая.
    """
    if not info or not info["refs"]:
        return

    # у самого dataObject фигуры быть не должно — рисуется только ссылка
    for shape in [x for x in shapes
                  if x.get("bpmnElement", "") in set(info.get("objects", []))]:
        shapes.remove(shape)

    boxes: dict[str, Box] = {}
    for shape in shapes:
        bounds = _bounds(shape)
        if bounds is not None:
            boxes[shape.get("bpmnElement", "")] = Box(
                float(bounds.get("x", 0)), float(bounds.get("y", 0)),
                float(bounds.get("width", 0)), float(bounds.get("height", 0)))

    def _free(box: Box, taken: list[Box]) -> bool:
        return not any(box.x < t.right + 12 and t.x < box.right + 12
                       and box.y < t.bottom + 10 and t.y < box.bottom + 10
                       for t in taken)

    taken: list[Box] = list(boxes.values())
    for ref_id, name in info["refs"]:
        if ref_id in boxes:                      # раскладчик уже поставил
            taken.append(boxes[ref_id])
            continue
        linked = [boxes[a] for _, _, r, a in info["assoc"] if r == ref_id and a in boxes]
        if not linked:
            warnings.append(f"документ {name or ref_id!r} не привязан ни к одной "
                            f"разложенной задаче, значок не поставлен")
            continue
        x = sum(b.cx for b in linked) / len(linked) - DOC_W / 2
        above = min(b.y for b in linked) - DOC_GAP
        below = max(b.bottom for b in linked) + DOC_GAP - DOC_H

        # сперва над задачей, потом под ней, потом в стороны — но не поверх фигур
        box = None
        for base_y in (above, below):
            for step in range(6):
                for sign in (0, 1, -1) if step else (0,):
                    candidate = Box(x + sign * step * (DOC_W + 24), base_y,
                                    DOC_W, DOC_H)
                    if _free(candidate, taken):
                        box = candidate
                        break
                if box:
                    break
            if box:
                break
        if box is None:
            box = Box(x, above, DOC_W, DOC_H)
            warnings.append(f"документ {name or ref_id!r} поставлен поверх других "
                            f"фигур: свободного места рядом с задачей не нашлось")
        taken.append(box)
        boxes[ref_id] = box

        shape = ET.Element(f"{DI}BPMNShape", {"id": f"{ref_id}_di", "bpmnElement": ref_id})
        ET.SubElement(shape, f"{DC}Bounds", {
            "x": _num(box.x), "y": _num(box.y),
            "width": _num(DOC_W), "height": _num(DOC_H)})
        shapes.append(shape)

    for assoc_id, direction, ref_id, act_id in info["assoc"]:
        if ref_id not in boxes or act_id not in boxes:
            continue
        doc, act = boxes[ref_id], boxes[act_id]
        pts = ([(doc.cx, doc.bottom), (act.cx, act.y)] if direction == "in"
               else [(act.cx, act.y), (doc.cx, doc.bottom)])
        edge = ET.Element(f"{DI}BPMNEdge", {"id": f"{assoc_id}_di", "bpmnElement": assoc_id})
        for x, y in pts:
            ET.SubElement(edge, f"{D}waypoint", {"x": _num(x), "y": _num(y)})
        edges.append(edge)


def _message_waypoints(src: Box, tgt: Box) -> list[tuple[float, float]]:
    if src.bottom <= tgt.y:
        return [(src.cx, src.bottom), (src.cx, tgt.y - 20), (tgt.cx, tgt.y - 20), (tgt.cx, tgt.y)]
    if tgt.bottom <= src.y:
        return [(src.cx, src.y), (src.cx, tgt.bottom + 20), (tgt.cx, tgt.bottom + 20), (tgt.cx, tgt.bottom)]
    if src.right <= tgt.x:
        return [(src.right, src.cy), (tgt.x, tgt.cy)]
    return [(src.x, src.cy), (tgt.right, tgt.cy)]


def merge_layout(xml: str, laid: dict[str, str], meta: dict) -> tuple[str, list[str]]:
    """Собирает единый BPMNPlane из отдельно разложенных процессов."""
    warnings: list[str] = []
    root = ET.fromstring(xml)
    order = [p.get("id", "") for p in root.findall(f"{B}process")]

    parsed: dict[str, tuple[list[ET.Element], list[ET.Element], Box]] = {}
    nested: list[ET.Element] = []
    for pid in order:
        if pid not in laid:
            raise LayoutError(f"нет результата раскладки для процесса {pid!r}")
        shapes, edges, extra = _extract(laid[pid])
        _place_data(shapes, edges, meta.get("data", {}).get(pid, {}), warnings)
        parsed[pid] = (shapes, edges, _bbox(shapes, edges))
        nested += extra

    has_pools = bool(meta["participants"])
    inner_x = LABEL_BAND + PAD if has_pools else PAD
    width = max(box.w for _, _, box in parsed.values()) + inner_x + PAD

    all_shapes: list[ET.Element] = []
    all_edges: list[ET.Element] = []
    pool_shapes: list[ET.Element] = []
    lane_shapes: list[ET.Element] = []
    boxes: dict[str, Box] = {}

    cursor = 0.0
    for pid in order:
        shapes, edges, box = parsed[pid]
        dx = inner_x - box.x
        dy = cursor + PAD - box.y
        for el in shapes + edges:
            _shift(el, dx, dy)

        pool = Box(0, cursor, width, box.h + 2 * PAD)

        for shape in shapes:
            bounds = _bounds(shape)
            if bounds is None:
                continue
            boxes[shape.get("bpmnElement", "")] = Box(
                float(bounds.get("x", 0)), float(bounds.get("y", 0)),
                float(bounds.get("width", 0)), float(bounds.get("height", 0)),
            )

        if has_pools:
            part_id, part_name = meta["participants"].get(pid, ("", ""))
            if part_id:
                shape = ET.Element(f"{DI}BPMNShape", {
                    "id": f"{part_id}_di", "bpmnElement": part_id, "isHorizontal": "true",
                })
                ET.SubElement(shape, f"{DC}Bounds", {
                    "x": _num(pool.x), "y": _num(pool.y),
                    "width": _num(pool.w), "height": _num(pool.h),
                })
                pool_shapes.append(shape)
                boxes[part_id] = pool

        for lane_id, _, band in _lane_bands(meta["lanes"].get(pid, []), boxes,
                                               pool, warnings):
            shape = ET.Element(f"{DI}BPMNShape", {
                "id": f"{lane_id}_di", "bpmnElement": lane_id, "isHorizontal": "true",
            })
            ET.SubElement(shape, f"{DC}Bounds", {
                "x": _num(band.x), "y": _num(band.y),
                "width": _num(band.w), "height": _num(band.h),
            })
            lane_shapes.append(shape)

        all_shapes += shapes
        all_edges += edges
        cursor = pool.bottom + POOL_GAP

    for mf_id, src, tgt in meta["message_flows"]:
        if src not in boxes or tgt not in boxes:
            warnings.append(f"поток сообщений {mf_id!r} не отрисован: "
                            f"один из концов не получил координат")
            continue
        edge = ET.Element(f"{DI}BPMNEdge", {"id": f"{mf_id}_di", "bpmnElement": mf_id})
        for x, y in _message_waypoints(boxes[src], boxes[tgt]):
            ET.SubElement(edge, f"{D}waypoint", {"x": _num(x), "y": _num(y)})
        all_edges.append(edge)

    for old in root.findall(f"{DI}BPMNDiagram"):
        root.remove(old)

    diagram = ET.SubElement(root, f"{DI}BPMNDiagram", {"id": "BPMNDiagram_1"})
    plane_ref = meta["collaboration"] or (order[0] if order else "")
    plane = ET.SubElement(diagram, f"{DI}BPMNPlane",
                          {"id": "BPMNPlane_1", "bpmnElement": plane_ref})
    for el in pool_shapes + lane_shapes + all_shapes + all_edges:
        plane.append(el)

    for i, extra in enumerate(nested, start=1):     # полотна субпроцессов
        extra.set("id", f"BPMNDiagram_sub_{i}")
        root.append(extra)

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True), warnings


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def apply_layout(xml: str, layout_fn) -> tuple[str, list[str]]:
    docs, meta = split_processes(xml)
    if not docs:
        raise LayoutError("в документе нет ни одного процесса")

    laid: dict[str, str] = {}
    for pid, single in docs:
        laid[pid] = layout_fn(single)

    return merge_layout(xml, laid, meta)
