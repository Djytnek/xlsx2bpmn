# -*- coding: utf-8 -*-
"""
Раскладка координат на чистом Python. Ни сети, ни зависимостей.

Контракт: на вход XML с одним процессом, на выход тот же XML
с добавленным BPMNDiagram.

Алгоритм послойный: по горизонтали узел ставится в колонку по длиннейшему пути
от старта, по вертикали — в полосу своей дорожки. Стрелки не оптимизируются,
зато результат предсказуем и не зависит ни от чего внешнего.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict, deque

from .core import NS

B = f"{{{NS['bpmn']}}}"
DI = f"{{{NS['bpmndi']}}}"
DC = f"{{{NS['dc']}}}"
D = f"{{{NS['di']}}}"

SIZES = {
    "startEvent": (36, 36), "endEvent": (36, 36), "boundaryEvent": (36, 36),
    "intermediateCatchEvent": (36, 36), "intermediateThrowEvent": (36, 36),
    "exclusiveGateway": (50, 50), "parallelGateway": (50, 50),
    "inclusiveGateway": (50, 50), "eventBasedGateway": (50, 50),
}
TASK_SIZE = (100, 80)
COL_GAP = 60          # зазор между колонками
ROW_GAP = 40          # зазор между строками
LANE_PAD = 24         # отступ от содержимого дорожки до её границы
LANE_MIN = 90         # высота пустой дорожки
DETOUR_GAP = 16       # расстояние между параллельными обходами
SKIP = {"laneSet", "sequenceFlow", "dataObject", "dataObjectReference",
        "documentation", "extensionElements", "property", "ioSpecification",
        "dataInputAssociation", "dataOutputAssociation", "incoming", "outgoing",
        "standardLoopCharacteristics", "multiInstanceLoopCharacteristics",
        "textAnnotation", "association", "group"}


def _size(tag: str) -> tuple[int, int]:
    return SIZES.get(tag, TASK_SIZE)


def _order(cell: dict, columns: dict, lanes: int, in_edges: dict) -> None:
    """Упорядочивает узлы внутри клетки так, чтобы стрелки меньше пересекались.

    Идём колонками слева направо; внутри клетки узел встаёт тем выше, чем выше
    стояли его предшественники. Классическая barycenter-эвристика.
    """
    row: dict[str, float] = {}
    for col in sorted(columns):
        for lane in range(lanes):
            members = cell.get((lane, col))
            if not members:
                continue
            if len(members) > 1:
                def height(node: str) -> float:
                    known = [row[p] for p in in_edges.get(node, []) if p in row]
                    return sum(known) / len(known) if known else 0.0
                members.sort(key=height)
            for index, node in enumerate(members):
                row[node] = lane * 100 + index


def _place(proc: ET.Element) -> tuple[dict, dict, dict, list]:
    """Расставляет координаты внутри одного контейнера — процесса или субпроцесса.

    Возвращает (nodes, attached, boxes, flows).
    """
    nodes: dict[str, str] = {}          # id -> имя тега
    attached: dict[str, str] = {}       # boundary id -> id хозяина
    for child in proc:
        tag = child.tag.split("}")[1]
        if tag in SKIP:
            continue
        nid = child.get("id")
        if not nid:
            continue
        nodes[nid] = tag
        if tag == "boundaryEvent":
            attached[nid] = child.get("attachedToRef", "")

    flows: list[tuple[str, str, str]] = []
    out_edges: dict[str, list[str]] = defaultdict(list)
    in_edges: dict[str, list[str]] = defaultdict(list)
    in_count: dict[str, int] = defaultdict(int)
    for flow in proc.findall(f"{B}sequenceFlow"):
        src, tgt = flow.get("sourceRef", ""), flow.get("targetRef", "")
        if src in nodes and tgt in nodes:
            flows.append((flow.get("id", ""), src, tgt))
            out_edges[src].append(tgt)
            in_edges[tgt].append(src)
            in_count[tgt] += 1

    # --- дорожки: узел -> номер полосы ---------------------------------------
    lane_names: list[str] = []
    lane_of: dict[str, int] = {}
    lane_set = proc.find(f"{B}laneSet")
    if lane_set is not None:
        for index, lane in enumerate(lane_set.findall(f"{B}lane")):
            lane_names.append(lane.get("name") or lane.get("id", ""))
            for ref in lane.findall(f"{B}flowNodeRef"):
                nid = (ref.text or "").strip()
                if nid in nodes:
                    lane_of[nid] = index

    # --- слои: длиннейший путь от источников, с защитой от циклов -----------
    # Граничные события сами не занимают колонку, но слои через них проходят:
    # иначе задача, запускаемая только по таймеру, уезжает в самое начало.
    layout_nodes = [n for n in nodes if n not in attached]
    layer: dict[str, int] = {n: 0 for n in nodes}

    steps: dict[str, list[tuple[str, int]]] = defaultdict(list)
    reach: dict[str, int] = defaultdict(int)
    for src, targets in out_edges.items():
        for tgt in targets:
            steps[src].append((tgt, 1))
            reach[tgt] += 1
    for boundary, host in attached.items():      # событие живёт в слое хозяина
        if host in nodes:
            steps[host].append((boundary, 0))
            reach[boundary] += 1

    sources = [n for n in nodes if not reach[n]] or layout_nodes[:1]

    queue = deque(sources)
    seen_order: list[str] = []
    guard = 0
    limit = len(nodes) * len(nodes) + 100
    while queue and guard < limit:
        guard += 1
        node = queue.popleft()
        if node not in seen_order and node not in attached:
            seen_order.append(node)
        for nxt, weight in steps.get(node, []):
            if layer[nxt] < layer[node] + weight:
                layer[nxt] = layer[node] + weight
                queue.append(nxt)

    for node in layout_nodes:                    # недостижимые — в свою колонку
        if node not in seen_order:
            seen_order.append(node)

    # --- порядок внутри колонки: по обходу от старта -------------------------
    columns: dict[int, list[str]] = defaultdict(list)
    for node in seen_order:
        columns[layer[node]].append(node)

    # узлы без дорожки наследуют её у предшественника
    if lane_names:
        for node in seen_order:
            if node in lane_of or node in attached:
                continue
            source = next((s for s in in_edges.get(node, []) if s in lane_of), None)
            lane_of[node] = lane_of[source] if source else 0

    # --- размеры колонок -----------------------------------------------------
    col_x: dict[int, float] = {}
    col_w: dict[int, int] = {}
    x_cursor = 0.0
    for col in sorted(columns):
        col_w[col] = max(_size(nodes[n])[0] for n in columns[col])
        col_x[col] = x_cursor
        x_cursor += col_w[col] + COL_GAP

    def _stack(members: list[str]) -> float:
        return (sum(_size(nodes[n])[1] for n in members)
                + ROW_GAP * (len(members) - 1)) if members else 0.0

    # --- координаты ----------------------------------------------------------
    boxes: dict[str, tuple[float, float, int, int]] = {}

    if lane_names:
        # клетка (дорожка, колонка): узлы стопкой, дорожка задаёт полосу по Y
        cell: dict[tuple[int, int], list[str]] = defaultdict(list)
        for node in layout_nodes:
            cell[(lane_of.get(node, 0), layer[node])].append(node)
        _order(cell, columns, len(lane_names), in_edges)

        y_cursor = 0.0
        for index in range(len(lane_names)):
            tallest = max((_stack(cell[(index, col)]) for col in columns),
                          default=0.0)
            band = max(tallest + 2 * LANE_PAD, LANE_MIN)
            for col in sorted(columns):
                members = cell[(index, col)]
                if not members:
                    continue
                y = y_cursor + (band - _stack(members)) / 2
                for node in members:
                    w, h = _size(nodes[node])
                    boxes[node] = (col_x[col] + (col_w[col] - w) / 2, y, w, h)
                    y += h + ROW_GAP
            y_cursor += band
    else:
        for col in sorted(columns):
            members = columns[col]
            y = -_stack(members) / 2
            for node in members:
                w, h = _size(nodes[node])
                boxes[node] = (col_x[col] + (col_w[col] - w) / 2, y, w, h)
                y += h + ROW_GAP

    # --- граничные события: на нижней кромке хозяина -------------------------
    per_host: dict[str, int] = defaultdict(int)
    for bid, host in attached.items():
        if host not in boxes:
            continue
        hx, hy, hw, hh = boxes[host]
        w, h = _size("boundaryEvent")
        index = per_host[host]
        per_host[host] += 1
        boxes[bid] = (hx + hw * 0.7 - w / 2 + index * (w + 6), hy + hh - h / 2, w, h)

    # --- сдвиг в положительные координаты ------------------------------------
    if boxes:
        min_x = min(b[0] for b in boxes.values())
        min_y = min(b[1] for b in boxes.values())
        dx, dy = 60 - min_x, 60 - min_y
        boxes = {k: (v[0] + dx, v[1] + dy, v[2], v[3]) for k, v in boxes.items()}

    return nodes, attached, boxes, flows


def _draw(plane: ET.Element, nodes: dict, attached: dict, boxes: dict,
          flows: list) -> None:
    """Переносит посчитанные координаты в BPMNPlane."""
    for node, (x, y, w, h) in boxes.items():
        attrs = {"id": f"{node}_di", "bpmnElement": node}
        if nodes.get(node) == "subProcess":
            attrs["isExpanded"] = "false"
        if nodes.get(node) == "exclusiveGateway":
            attrs["isMarkerVisible"] = "true"
        shape = ET.SubElement(plane, f"{DI}BPMNShape", attrs)
        ET.SubElement(shape, f"{DC}Bounds", {
            "x": _n(x), "y": _n(y), "width": _n(w), "height": _n(h)})

    slots = _slots(boxes, flows, attached)
    for fid, src, tgt in flows:
        if src not in boxes or tgt not in boxes:
            continue
        edge = ET.SubElement(plane, f"{DI}BPMNEdge",
                             {"id": f"{fid}_di", "bpmnElement": fid})
        for x, y in _route(boxes[src], boxes[tgt], src in attached, slots.get(fid, 0)):
            ET.SubElement(edge, f"{D}waypoint", {"x": _n(x), "y": _n(y)})


def layout_process(xml: str) -> str:
    """Единственная публичная функция. Идемпотентна по входу.

    Кроме основного плана строит по отдельному плану на каждый субпроцесс —
    так редакторы показывают его содержимое при раскрытии.
    """
    root = ET.fromstring(xml)
    proc = root.find(f"{B}process")
    if proc is None:
        raise ValueError("в документе нет process")

    for old in root.findall(f"{DI}BPMNDiagram"):
        root.remove(old)

    diagram = ET.SubElement(root, f"{DI}BPMNDiagram", {"id": "BPMNDiagram_1"})
    plane = ET.SubElement(diagram, f"{DI}BPMNPlane", {
        "id": "BPMNPlane_1", "bpmnElement": proc.get("id", ""),
    })
    _draw(plane, *_place(proc))

    # содержимое субпроцессов: каждому своё полотно, чтобы редактор показал
    # его при раскрытии
    for index, sub in enumerate(proc.iter(f"{B}subProcess"), start=1):
        nodes, attached, boxes, flows = _place(sub)
        if not boxes:
            continue
        sub_diagram = ET.SubElement(root, f"{DI}BPMNDiagram",
                                    {"id": f"BPMNDiagram_sub_{index}"})
        sub_plane = ET.SubElement(sub_diagram, f"{DI}BPMNPlane", {
            "id": f"BPMNPlane_sub_{index}", "bpmnElement": sub.get("id", ""),
        })
        _draw(sub_plane, nodes, attached, boxes, flows)

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _n(value: float) -> str:
    return str(int(round(value)))


def _detour(src: tuple[float, float, int, int], tgt: tuple[float, float, int, int],
            from_boundary: bool) -> bool:
    """Пойдёт ли стрелка обходом понизу — тогда ей нужна своя дорожка."""
    sx, sy, sw, sh = src
    tx, ty, tw, th = tgt
    if from_boundary or tx + tw < sx:
        return True
    return tx > sx + sw and abs((sy + sh / 2) - (ty + th / 2)) < 1 \
        and tx - (sx + sw) > COL_GAP * 1.6


def _slots(boxes: dict, flows: list, attached: dict) -> dict[str, int]:
    """Разводит обходные стрелки по параллельным дорожкам, чтобы не слипались.

    Две стрелки могут делить дорожку, только если не пересекаются по X.
    """
    spans: list[tuple[float, float, str]] = []
    for fid, src, tgt in flows:
        if src not in boxes or tgt not in boxes:
            continue
        if not _detour(boxes[src], boxes[tgt], src in attached):
            continue
        sx, _, sw, _ = boxes[src]
        tx, _, tw, _ = boxes[tgt]
        left, right = sorted((sx + sw / 2, tx + tw / 2))
        spans.append((left, right, fid))

    spans.sort()
    busy: list[list[tuple[float, float]]] = []
    out: dict[str, int] = {}
    for left, right, fid in spans:
        for slot, taken in enumerate(busy):
            if all(right <= a or left >= b for a, b in taken):
                taken.append((left, right))
                out[fid] = slot
                break
        else:
            busy.append([(left, right)])
            out[fid] = len(busy) - 1
    return out


def _route(src: tuple[float, float, int, int], tgt: tuple[float, float, int, int],
           from_boundary: bool, slot: int = 0) -> list[tuple[float, float]]:
    sx, sy, sw, sh = src
    tx, ty, tw, th = tgt
    s_cx, s_cy = sx + sw / 2, sy + sh / 2
    t_cx, t_cy = tx + tw / 2, ty + th / 2

    lane = slot * DETOUR_GAP

    if from_boundary:                                   # из граничного события — вниз
        drop = max(sy + sh, ty + th) + 40 + lane
        return [(s_cx, sy + sh), (s_cx, drop), (t_cx, drop), (t_cx, ty + th)]

    if tx > sx + sw:                                    # вперёд
        if abs(s_cy - t_cy) < 1:
            if tx - (sx + sw) > COL_GAP * 1.6:
                # перепрыгивает через колонку: между ними стоят другие узлы,
                # поэтому обходим понизу, а не режем их насквозь
                drop = max(sy + sh, ty + th) + 20 + lane
                return [(s_cx, sy + sh), (s_cx, drop), (t_cx, drop), (t_cx, ty + th)]
            return [(sx + sw, s_cy), (tx, t_cy)]
        mid = (sx + sw + tx) / 2
        return [(sx + sw, s_cy), (mid, s_cy), (mid, t_cy), (tx, t_cy)]

    if tx + tw < sx:                                    # назад — обходим снизу
        drop = max(sy + sh, ty + th) + 50 + lane
        return [(s_cx, sy + sh), (s_cx, drop), (t_cx, drop), (t_cx, ty + th)]

    if t_cy > s_cy:                                     # вниз в той же колонке
        return [(s_cx, sy + sh), (t_cx, ty)]
    return [(s_cx, sy), (t_cx, ty + th)]
