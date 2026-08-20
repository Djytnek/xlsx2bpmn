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
import os
import shutil
import subprocess
from pathlib import Path
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .core import NS, SUBPROCESS_TYPES, TARGET_NS
from .route import reroute

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


def _owner(proc: ET.Element) -> dict[str, str]:
    """id элемента -> id субпроцесса, внутри которого он лежит. '' — верх."""
    out: dict[str, str] = {}

    def walk(container: ET.Element, parent: str) -> None:
        for el in container:
            eid = el.get("id", "")
            if el.tag.split("}")[-1] in SUBPROCESS_TYPES:
                out[eid] = parent
                walk(el, eid)
            elif eid:
                out[eid] = parent

    walk(proc, "")
    return out


def _nest(shapes: list[ET.Element], edges: list[ET.Element],
          owner: dict[str, str]) -> list[ET.Element]:
    """Уносит содержимое субпроцессов на их собственные полотна.

    Внешний bpmn-auto-layout вложенности не знает и раскладывает содержимое
    субпроцесса прямо поверх основного потока — элементы оказываются друг на
    друге. Разносим сами: у каждого субпроцесса своё полотно, как и требует
    BPMN. Раскладчику, который сделал это сам, здесь разносить уже нечего.
    """
    groups: dict[str, list[ET.Element]] = {}
    for el in shapes + edges:
        home = owner.get(el.get("bpmnElement", ""), "")
        if home:
            groups.setdefault(home, []).append(el)

    out: list[ET.Element] = []
    for i, (sub_id, members) in enumerate(groups.items(), start=1):
        for el in members:
            (shapes if el.tag == f"{DI}BPMNShape" else edges).remove(el)
        diagram = ET.Element(f"{DI}BPMNDiagram", {"id": f"BPMNDiagram_nest_{i}"})
        plane = ET.SubElement(diagram, f"{DI}BPMNPlane",
                              {"id": f"BPMNPlane_nest_{i}", "bpmnElement": sub_id})
        for el in members:
            plane.append(el)
        out.append(diagram)
    return out


def _plane_of(diagram: ET.Element):
    """Полотно и его содержимое списками, чтобы работать как с главным."""
    plane = diagram.find(f"{DI}BPMNPlane")
    if plane is None:
        return None, [], []
    return (plane,
            [e for e in plane if e.tag == f"{DI}BPMNShape"],
            [e for e in plane if e.tag == f"{DI}BPMNEdge"])


def _refill(plane: ET.Element, shapes: list[ET.Element],
            edges: list[ET.Element]) -> None:
    for el in list(plane):
        plane.remove(el)
    for el in shapes + edges:
        plane.append(el)


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
                info: dict, warnings: list[str], mark: str = "") -> set[str]:
    """Ставит значки документов над задачами и рисует ассоциации.

    Вызывается для каждого полотна отдельно: документ рисуется там, где лежит
    его задача, — в том числе внутри субпроцесса. Возвращает документы,
    которые на этом полотне поставлены.
    """
    if not info or not info.get("refs"):
        return set()

    # У самого dataObject фигуры быть не должно — рисуется только ссылка.
    # Заодно выбрасываем фигуры документов, которые расставил раскладчик:
    # bpmn-auto-layout сваливает их столбиком в стороне, а мы ставим каждый
    # документ вплотную к своей задаче.
    drop = set(info.get("objects", [])) | {ref for ref, _ in info["refs"]}
    for shape in [x for x in shapes if x.get("bpmnElement", "") in drop]:
        shapes.remove(shape)

    # стрелки ассоциаций тоже перерисуем сами — от новых мест
    for edge in [e for e in edges
                 if e.get("bpmnElement", "") in {a for a, _, _, _ in info["assoc"]}]:
        edges.remove(edge)

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
    placed: set[str] = set()
    for ref_id, name in info["refs"]:
        if ref_id in boxes:                      # раскладчик уже поставил
            taken.append(boxes[ref_id])
            placed.add(ref_id)
            continue
        # документ ставим над той задачей, которая его создаёт; если создателя
        # нет — над первой, которая его читает
        makers = [boxes[a] for _, d, r, a in info["assoc"]
                  if r == ref_id and d == "out" and a in boxes]
        readers = [boxes[a] for _, d, r, a in info["assoc"]
                   if r == ref_id and d == "in" and a in boxes]
        anchor = (makers or readers)[:1]
        if not anchor:
            continue                         # его задача на другом полотне
        x = anchor[0].cx - DOC_W / 2
        above = anchor[0].y - DOC_GAP
        below = anchor[0].bottom + DOC_GAP - DOC_H

        # Держимся строго над задачей, пока получается: сначала прямо над,
        # потом прямо под, и только если и там занято — вбок.
        box = None
        for step in range(6):
            shifts = (0,) if not step else (step, -step)
            for base_y in (above, below):
                for sign in shifts:
                    candidate = Box(x + sign * (DOC_W + 24), base_y, DOC_W, DOC_H)
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
        placed.add(ref_id)

        # документ, нужный сразу двум уровням, рисуется на каждом полотне,
        # поэтому id фигуры помечается полотном — они обязаны быть разными
        shape = ET.Element(f"{DI}BPMNShape",
                           {"id": f"{ref_id}_di{mark}", "bpmnElement": ref_id})
        ET.SubElement(shape, f"{DC}Bounds", {
            "x": _num(box.x), "y": _num(box.y),
            "width": _num(DOC_W), "height": _num(DOC_H)})
        shapes.append(shape)

    for assoc_id, direction, ref_id, act_id in info["assoc"]:
        if ref_id not in boxes or act_id not in boxes:
            continue
        doc, act = boxes[ref_id], boxes[act_id]
        # причаливаем с той стороны, где документ на самом деле стоит,
        # иначе линия к документу под задачей прошивает саму задачу
        if doc.cy <= act.cy:
            at_doc, at_act = (doc.cx, doc.bottom), (act.cx, act.y)
        else:
            at_doc, at_act = (doc.cx, doc.y), (act.cx, act.bottom)
        pts = [at_doc, at_act] if direction == "in" else [at_act, at_doc]
        edge = ET.Element(f"{DI}BPMNEdge", {"id": f"{assoc_id}_di", "bpmnElement": assoc_id})
        for x, y in pts:
            ET.SubElement(edge, f"{D}waypoint", {"x": _num(x), "y": _num(y)})
        edges.append(edge)

    return placed


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

    owners = {p.get("id", ""): _owner(p) for p in root.findall(f"{B}process")}

    parsed: dict[str, tuple[list[ET.Element], list[ET.Element], Box]] = {}
    nested: list[ET.Element] = []
    for pid in order:
        if pid not in laid:
            raise LayoutError(f"нет результата раскладки для процесса {pid!r}")
        shapes, edges, extra = _extract(laid[pid])
        extra += _nest(shapes, edges, owners.get(pid, {}))

        info = meta.get("data", {}).get(pid, {})
        placed = _place_data(shapes, edges, info, warnings)
        for diagram in extra:
            plane, in_shapes, in_edges = _plane_of(diagram)
            if plane is None:
                continue
            placed |= _place_data(in_shapes, in_edges, info, warnings,
                                  mark=f"_{plane.get('bpmnElement', '')}")
            _refill(plane, in_shapes, in_edges)
        for ref_id, name in info.get("refs", []):
            if ref_id not in placed:
                warnings.append(f"документ {name or ref_id!r} не привязан ни к одной "
                                f"разложенной задаче, значок не поставлен")

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

    # ни одна стрелка не имеет права идти сквозь плашку
    _, stuck = reroute(all_shapes, all_edges)
    if stuck:
        warnings.append(f"стрелок не удалось провести в обход фигур: {stuck}; "
                        f"схема слишком плотная, они оставлены как были")

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
        inner = extra.find(f"{DI}BPMNPlane")
        if inner is not None:
            reroute([e for e in inner if e.tag == f"{DI}BPMNShape"],
                    [e for e in inner if e.tag == f"{DI}BPMNEdge"])
        root.append(extra)

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True), warnings


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def node_places() -> list[Path]:
    """Где может лежать раскладчик на Node — в порядке предпочтения.

    Первым идёт bundle.mjs: это тот же bpmn-auto-layout, собранный в один
    файл вместе с зависимостями. Он едет вместе с пакетом и работает сразу,
    без npm install и без сети. Остальные пути — на случай, если кто-то
    поставил раскладчик сам.
    """
    found = [Path(p) for p in (os.getenv("XLSX2BPMN_NODE_SCRIPT"),) if p]
    here = Path(__file__).parent / "layout-node"
    return found + [
        here / "bundle.mjs",                        # зашит в пакет
        Path.home() / ".xlsx2bpmn" / "layout-node" / "cli.mjs",
        here / "cli.mjs",
        Path.cwd() / "layout-node" / "cli.mjs",
        Path(__file__).parent.parent / "layout-node" / "cli.mjs",
    ]


def node_home() -> Path | None:
    """Папка раскладчика, даже если npm install в ней ещё не делали."""
    for path in node_places():
        if path.is_file():
            return path.parent
    return None


def _runnable(path: Path) -> bool:
    """Готов ли скрипт к запуску: бандлу зависимости не нужны, cli.mjs — нужны."""
    if not path.is_file():
        return False
    return path.name == "bundle.mjs" or (path.parent / "node_modules").is_dir()


def has_node() -> bool:
    """Есть ли в системе сам Node. Без него любой bundle бесполезен."""
    return shutil.which("node") is not None


def node_script() -> Path | None:
    """Ищет готовый к работе раскладчик. Сеть при этом не трогается."""
    if not has_node():
        return None
    for path in node_places():
        if _runnable(path):
            return path
    return None


def main_first(single_xml: str) -> str:
    """Ставит главную ветку первой — по ней bpmn-auto-layout ведёт прямую линию.

    Он берёт первую исходящую как продолжение строки, а остальные уводит вниз
    (в его же исходнике на этом месте стоит «TODO: sort by priority»). Если
    первой в таблице оказалась короткая ветка вроде «Нет → Конец», главной
    линией станет она, а вся работа уедет вниз и вбок. Порядок исходящих на
    смысл не влияет, поэтому переставляем: первой идёт та ветка, за которой
    стоит больше процесса.
    """
    try:
        root = ET.fromstring(single_xml)
    except ET.ParseError:
        return single_xml

    flows: dict[str, str] = {}                  # id потока -> куда ведёт
    after: dict[str, list[str]] = {}
    for flow in root.iter(f"{B}sequenceFlow"):
        source, target = flow.get("sourceRef", ""), flow.get("targetRef", "")
        flows[flow.get("id", "")] = target
        after.setdefault(source, []).append(target)

    known: dict[str, int] = {}

    def weight(start: str) -> int:
        """Сколько элементов стоит за этой веткой."""
        if start in known:
            return known[start]
        seen, stack = {start}, [start]
        while stack:
            for nxt in after.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        known[start] = len(seen)
        return known[start]

    changed = False
    for node in root.iter():
        outs = node.findall(f"{B}outgoing")
        if len(outs) < 2:
            continue
        ids = [(o.text or "").strip() for o in outs]
        order = sorted(range(len(ids)),
                       key=lambda i: (-weight(flows.get(ids[i], "")), i))
        if order != list(range(len(ids))):
            changed = True
            for element, i in zip(outs, order):
                element.text = ids[i]

    if not changed:
        return single_xml
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def node_layout(script: Path):
    """Обёртка над bpmn-auto-layout: XML внутрь, XML с координатами наружу."""
    def run(single_xml: str) -> str:
        ready = strip_lanes(main_first(single_xml))
        try:
            done = subprocess.run(
                ["node", str(script)], input=ready.encode("utf-8"),
                capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LayoutError(f"не удалось запустить node: {exc}") from exc
        if done.returncode != 0:
            raise LayoutError("bpmn-auto-layout: "
                              f"{done.stderr.decode('utf-8', 'replace')[:300]}")
        return done.stdout.decode("utf-8")
    return run


LANE_GAP = 40        # зазор между полосами ролей
ROW_GAP = 24         # зазор между строками внутри одной полосы
COL_GAP = 40         # на столько элементы расходятся по горизонтали


def lane_layout(single_xml: str, run) -> str:
    """Процесс ведёт bpmn-auto-layout, а по дорожкам раскладываем мы.

    Он про дорожки не знает и выстраивает всё одной строкой, из-за чего роли
    перестают совпадать с полосами. Но столбцы — кто за кем идёт — он считает
    лучше нашего. Поэтому берём у него X, а Y расставляем сами: каждый
    элемент опускается в полосу своей роли, а внутри полосы то, что налезает
    друг на друга по горизонтали, разъезжается на отдельные строки.
    """
    source = ET.fromstring(single_xml)
    lanes = [[(r.text or "").strip() for r in lane.findall(f"{B}flowNodeRef")]
             for lane in source.iter(f"{B}lane")]
    if not any(lanes):
        return run(single_xml)

    root = ET.fromstring(run(single_xml))
    plane = root.find(f".//{DI}BPMNPlane")
    if plane is None:
        raise LayoutError("bpmn-auto-layout не вернул полотно")

    boxes: dict[str, ET.Element] = {}
    for shape in plane:
        if shape.tag == f"{DI}BPMNShape":
            bounds = shape.find(f"{DC}Bounds")
            if bounds is not None:
                boxes[shape.get("bpmnElement", "")] = bounds

    # граничные события не расставляем: они едут на краю своей задачи
    hosts = {b.get("id", ""): b.get("attachedToRef", "")
             for b in source.iter(f"{B}boundaryEvent")}

    top = 0.0
    for refs in lanes:
        members = [boxes[rid] for rid in refs if rid in boxes and rid not in hosts]
        if not members:
            continue
        members.sort(key=lambda b: (float(b.get("x", 0)), float(b.get("y", 0))))

        rows: list[list[tuple[float, float]]] = []
        where: list[int] = []
        for bounds in members:
            left = float(bounds.get("x", 0))
            right = left + float(bounds.get("width", 0))
            for i, row in enumerate(rows):
                if all(left >= b + COL_GAP or right <= a - COL_GAP for a, b in row):
                    row.append((left, right))
                    where.append(i)
                    break
            else:
                rows.append([(left, right)])
                where.append(len(rows) - 1)

        highest = [0.0] * len(rows)
        for bounds, i in zip(members, where):
            highest[i] = max(highest[i], float(bounds.get("height", 0)))
        starts, y = [], top
        for height in highest:
            starts.append(y)
            y += height + ROW_GAP
        for bounds, i in zip(members, where):
            own = float(bounds.get("height", 0))
            bounds.set("y", _num(starts[i] + (highest[i] - own) / 2))
        top = y - ROW_GAP + LANE_GAP

    for event, host in hosts.items():
        if event in boxes and host in boxes:
            near, mine = boxes[host], boxes[event]
            mine.set("x", _num(float(near.get("x", 0))
                               + float(near.get("width", 0)) * 0.7
                               - float(mine.get("width", 0)) / 2))
            mine.set("y", _num(float(near.get("y", 0)) + float(near.get("height", 0))
                               - float(mine.get("height", 0)) / 2))

    # стрелки после переезда никуда не годятся — сводим концы, остальное
    # доделает развод маршрутов
    for edge in [e for e in plane if e.tag == f"{DI}BPMNEdge"]:
        plane.remove(edge)
    for flow in source.iter(f"{B}sequenceFlow"):
        ends = [boxes.get(flow.get("sourceRef", "")), boxes.get(flow.get("targetRef", ""))]
        if any(e is None for e in ends):
            continue
        edge = ET.SubElement(plane, f"{DI}BPMNEdge",
                             {"id": f"{flow.get('id', '')}_di",
                              "bpmnElement": flow.get("id", "")})
        for bounds in ends:
            ET.SubElement(edge, f"{D}waypoint", {
                "x": _num(float(bounds.get("x", 0)) + float(bounds.get("width", 0)) / 2),
                "y": _num(float(bounds.get("y", 0)) + float(bounds.get("height", 0)) / 2)})

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def layout_name(choice: str = "auto") -> str:
    """Какой раскладчик будет использован — чтобы писать это в вывод."""
    if choice == "native":
        return "встроенный"
    if choice == "node":
        return "bpmn-auto-layout" if node_script() is not None else "недоступен"
    return "bpmn-auto-layout" if node_script() is not None else "встроенный"


def has_lanes(single_xml: str) -> bool:
    """Есть ли в процессе дорожки. По ним и выбирается раскладчик."""
    try:
        return ET.fromstring(single_xml).find(f".//{B}laneSet") is not None
    except ET.ParseError:
        return False


def smart_layout(script: Path | None):
    """Процесс ведёт bpmn-auto-layout, дорожки достраиваем мы.

    Он считает столбцы — кто за кем идёт — лучше нашего, но про дорожки не
    знает и валит всё одной строкой. Поэтому схему с дорожками отдаём ему
    без laneSet, а потом сами опускаем каждый элемент в полосу его роли.

    Встроенный раскладчик остаётся на случай, когда Node в системе нет.
    """
    from .layout_native import layout_process

    node = node_layout(script) if script else None
    used: set[str] = set()

    def run(single_xml: str) -> str:
        if node is not None:
            try:
                used.add("bpmn-auto-layout")
                if has_lanes(single_xml):
                    return lane_layout(single_xml, node)
                return node(single_xml)
            except LayoutError:
                used.discard("bpmn-auto-layout")
        used.add("встроенный")
        return layout_process(single_xml)

    run.used = used                     # чтобы вывод показывал, что вышло
    return run


def engine_name(layout_fn, choice: str = "auto") -> str:
    """Каким раскладчиком схема разложена на самом деле."""
    used = getattr(layout_fn, "used", None)
    if used:
        return " + ".join(sorted(used))
    return layout_name(choice)


def layout_hint(choice: str = "auto") -> str:
    """Подсказка, если схему разложил запасной раскладчик, а лучший не стоит."""
    if choice == "auto" and node_script() is None:
        if not has_node():
            return ("  (схему можно вести заметно ровнее, но для этого нужен "
                    "Node.js 20+: https://nodejs.org)")
        return ("  (схему можно вести ровнее: "
                "поставьте раскладчик командой  xlsx2bpmn --setup-layout)")
    return ""


def pick_layout(choice: str = "auto"):
    """Раскладчик по имени.

    node — bpmn-auto-layout: он ведёт процесс, дорожки достраиваем мы.
    native — встроенный, на чистом Python: нужен там, где нет Node.
    auto — bpmn-auto-layout, если Node есть, иначе встроенный.
    """
    from .layout_native import layout_process

    if choice == "native":
        return layout_process
    script = node_script()
    if choice == "node":
        if script is None:
            raise LayoutError(
                "Node-раскладчик не установлен. Поставьте его: "
                f"cd {node_home() or 'layout-node'} && npm install. "
                "Или используйте --layout native")
        return node_layout(script)
    return smart_layout(script)


def strip_lanes(xml: str) -> str:
    """Убирает laneSet. Нужен внешним раскладчикам, которые про дорожки не знают."""
    root = ET.fromstring(xml)
    for proc in root.findall(f"{B}process"):
        for lane_set in proc.findall(f"{B}laneSet"):
            proc.remove(lane_set)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def apply_layout(xml: str, layout_fn) -> tuple[str, list[str]]:
    docs, meta = split_processes(xml)
    if not docs:
        raise LayoutError("в документе нет ни одного процесса")

    laid: dict[str, str] = {}
    for pid, single in docs:
        laid[pid] = layout_fn(single)

    return merge_layout(xml, laid, meta)
