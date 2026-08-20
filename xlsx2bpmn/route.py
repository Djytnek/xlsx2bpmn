# -*- coding: utf-8 -*-
"""
Развод стрелок в обход фигур.

Стрелка не имеет права проходить сквозь плашку — ни сквозь задачу, ни сквозь
событие, ни сквозь документ. Раскладчики этого не гарантируют: и внешний
bpmn-auto-layout, и встроенный иногда ведут линию напрямик. Здесь готовый план
проверяется, и каждая проткнутая стрелка перекладывается заново.

Как это устроено. Из границ самих фигур строится сетка: у каждой плашки своя
линия слева, справа и по центру, и так же по горизонтали. По этой сетке ищется
путь (A*), которому запрещено входить в любую чужую плашку, со штрафом за
поворот — чтобы линия шла прямо — и за отрезок, уже занятый другой стрелкой,
чтобы параллельные стрелки не сливались в одну. Стрелка, которая никого не
задевает, остаётся ровно такой, какой её провёл раскладчик.

Наружу: reroute(shapes, edges) -> (переложено, не удалось)
"""
from __future__ import annotations

import heapq
import xml.etree.ElementTree as ET
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from .core import NS

DC = f"{{{NS['dc']}}}"
D = f"{{{NS['di']}}}"

MARGIN = 15      # на столько стрелка отходит от плашки, огибая её
BEND = 45        # штраф за поворот: прямая линия дешевле ломаной
BUSY = 30        # штраф за отрезок, по которому уже идёт другая стрелка
SIDE = 40        # штраф за выход не с той стороны, куда стрелка идёт
EPS = 0.5
TOUCH = 8        # настолько стрелке можно войти в свою же плашку
SLACK = 140      # насколько шире концов стрелки смотрим сначала
LIMIT = 900      # предел числа линий сетки по оси: дальше не ищем
STEPS = 80_000   # предел шагов поиска на одну стрелку


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
# Мелочи
# --------------------------------------------------------------------------- #

def _num(value: float) -> str:
    return str(int(round(value)))


def _points(edge: ET.Element) -> list[tuple[float, float]]:
    return [(float(p.get("x", 0)), float(p.get("y", 0)))
            for p in edge.findall(f"{D}waypoint")]


def _set_points(edge: ET.Element, points: list[tuple[float, float]]) -> None:
    for old in edge.findall(f"{D}waypoint"):
        edge.remove(old)
    for i, (x, y) in enumerate(points):     # waypoint идут до BPMNLabel
        edge.insert(i, ET.Element(f"{D}waypoint", {"x": _num(x), "y": _num(y)}))


def _inside(p: tuple[float, float], q: tuple[float, float], box: Box,
            grow: float) -> float:
    """Длина куска отрезка внутри плашки. Отсечение Лианга — Барски."""
    left, right = box.x - grow + EPS, box.right + grow - EPS
    top, bottom = box.y - grow + EPS, box.bottom + grow - EPS
    if left >= right or top >= bottom:
        return 0.0
    dx, dy = q[0] - p[0], q[1] - p[1]
    lo, hi = 0.0, 1.0
    for near, far in ((-dx, p[0] - left), (dx, right - p[0]),
                      (-dy, p[1] - top), (dy, bottom - p[1])):
        if abs(near) < 1e-9:
            if far < 0:
                return 0.0
            continue
        cut = far / near
        if near < 0:
            if cut > hi:
                return 0.0
            lo = max(lo, cut)
        else:
            if cut < lo:
                return 0.0
            hi = min(hi, cut)
    if lo >= hi:
        return 0.0
    return (hi - lo) * (abs(dx) ** 2 + abs(dy) ** 2) ** 0.5


def _crosses(p: tuple[float, float], q: tuple[float, float], box: Box) -> bool:
    return _inside(p, q, box, MARGIN) > 0


def _shape_boxes(shapes: list[ET.Element]) -> list[Box]:
    boxes = []
    for shape in shapes:
        bounds = shape.find(f"{DC}Bounds")   # у самой фигуры, не у подписи
        if bounds is None:
            continue
        boxes.append(Box(float(bounds.get("x", 0)), float(bounds.get("y", 0)),
                         float(bounds.get("width", 0)),
                         float(bounds.get("height", 0))))
    return boxes


def _ends(points: list[tuple[float, float]], boxes: list[Box]) -> set[int]:
    """Плашки, на которых висят концы стрелки: их протыкать не запрещено.

    Граничное событие сидит на краю своей задачи, поэтому концом стрелки
    считается каждая плашка, накрывающая крайнюю точку.
    """
    out: set[int] = set()
    for x, y in (points[0], points[-1]):
        for i, b in enumerate(boxes):
            if b.x - 2 <= x <= b.right + 2 and b.y - 2 <= y <= b.bottom + 2:
                out.add(i)
    return out


def _pierced(points: list[tuple[float, float]], boxes: list[Box],
             ends: set[int]) -> bool:
    """Задевает ли стрелка хоть одну плашку.

    Своей плашки стрелка касается по определению — она из неё выходит. Но
    пройти сквозь неё насквозь нельзя и своей: именно так линия к документу
    прошивала задачу, когда документ оказывался под ней.
    """
    for a, b in zip(points, points[1:]):
        for i, box in enumerate(boxes):
            if i in ends:
                if _inside(a, b, box, 0) > TOUCH:
                    return True
            elif _inside(a, b, box, MARGIN) > 0:
                return True
    return False


def _end_box(point: tuple[float, float], boxes: list[Box],
             ends: set[int]) -> Box:
    """Плашка, с которой стрелка стартует. Из вложенных берём меньшую."""
    on = [boxes[i] for i in ends
          if boxes[i].x - 2 <= point[0] <= boxes[i].right + 2
          and boxes[i].y - 2 <= point[1] <= boxes[i].bottom + 2]
    if not on:                       # конец висит в воздухе — цепляемся за точку
        return Box(point[0], point[1], 0, 0)
    return min(on, key=lambda b: b.w * b.h)


# --------------------------------------------------------------------------- #
# Сетка
# --------------------------------------------------------------------------- #

def _grid(boxes: list[Box], extra: list[tuple[float, float]]):
    xs: set[int] = set()
    ys: set[int] = set()
    for b in boxes:
        xs.update((round(b.x - MARGIN), round(b.cx), round(b.right + MARGIN)))
        ys.update((round(b.y - MARGIN), round(b.cy), round(b.bottom + MARGIN)))
    for x, y in extra:
        xs.update((round(x - MARGIN), round(x), round(x + MARGIN)))
        ys.update((round(y - MARGIN), round(y), round(y + MARGIN)))
    return sorted(xs), sorted(ys)


def _blocked(xs: list[int], ys: list[int], boxes: list[Box]):
    """Для каждого отрезка сетки — какие плашки его перекрывают."""
    horizontal: dict[tuple[int, int], set[int]] = {}
    vertical: dict[tuple[int, int], set[int]] = {}
    for n, b in enumerate(boxes):
        left, right = b.x - MARGIN, b.right + MARGIN
        top, bottom = b.y - MARGIN, b.bottom + MARGIN

        rows = range(bisect_right(ys, top + EPS), bisect_left(ys, bottom - EPS))
        cols = range(max(bisect_right(xs, left + EPS) - 1, 0),
                     min(bisect_left(xs, right - EPS), len(xs) - 1))
        for j in rows:
            for i in cols:
                horizontal.setdefault((i, j), set()).add(n)

        cols2 = range(bisect_right(xs, left + EPS), bisect_left(xs, right - EPS))
        rows2 = range(max(bisect_right(ys, top + EPS) - 1, 0),
                      min(bisect_left(ys, bottom - EPS), len(ys) - 1))
        for i in cols2:
            for j in rows2:
                vertical.setdefault((i, j), set()).add(n)
    return horizontal, vertical


def _occupy(points: list[tuple[float, float]], xs: list[int], ys: list[int],
            used: set) -> None:
    """Отмечает линии сетки, по которым уже проложена стрелка."""
    index_x = {v: i for i, v in enumerate(xs)}
    index_y = {v: i for i, v in enumerate(ys)}
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if abs(y1 - y2) < EPS and round(y1) in index_y:
            j = index_y[round(y1)]
            lo, hi = sorted((x1, x2))
            for i in range(max(bisect_right(xs, lo + EPS) - 1, 0),
                           min(bisect_left(xs, hi - EPS), len(xs) - 1)):
                used.add((0, i, j))
        elif abs(x1 - x2) < EPS and round(x1) in index_x:
            i = index_x[round(x1)]
            lo, hi = sorted((y1, y2))
            for j in range(max(bisect_right(ys, lo + EPS) - 1, 0),
                           min(bisect_left(ys, hi - EPS), len(ys) - 1)):
                used.add((1, i, j))


# --------------------------------------------------------------------------- #
# Поиск пути
# --------------------------------------------------------------------------- #

def _docks(box: Box, other: Box, index_x: dict, index_y: dict) -> dict:
    """Куда стрелка может выйти из плашки: узел сетки -> (ось, штраф, точка)."""
    natural = set()
    if other.cx > box.right:
        natural.add("r")
    if other.cx < box.x:
        natural.add("l")
    if other.cy > box.bottom:
        natural.add("b")
    if other.cy < box.y:
        natural.add("t")
    if not natural:                       # плашки стоят друг на друге
        natural = {"r", "l", "t", "b"}

    sides = {
        "r": ((box.right, box.cy), (box.right + MARGIN, box.cy), 0),
        "l": ((box.x, box.cy), (box.x - MARGIN, box.cy), 0),
        "t": ((box.cx, box.y), (box.cx, box.y - MARGIN), 1),
        "b": ((box.cx, box.bottom), (box.cx, box.bottom + MARGIN), 1),
    }
    out: dict = {}
    for name, (dock, stub, axis) in sides.items():
        i = index_x.get(round(stub[0]))
        j = index_y.get(round(stub[1]))
        if i is None or j is None:
            continue
        cost = 0 if name in natural else SIDE
        if (i, j) not in out or cost < out[(i, j)][1]:
            out[(i, j)] = (axis, cost, dock)
    return out


def _search(xs, ys, horizontal, vertical, starts, goals, used, window=None):
    """A* по сетке: длина пути плюс штрафы за повороты и занятые линии.

    Обходятся все плашки без исключения, включая свои собственные: причалы
    стоят ровно на границе обходного коридора, поэтому выйти из плашки это
    не мешает, а вернуться в неё посреди пути — не даёт."""
    keys = list(goals)

    def rest(i: int, j: int) -> float:
        return min(abs(xs[i] - xs[gi]) + abs(ys[j] - ys[gj]) for gi, gj in keys)

    i0, i1, j0, j1 = window or (0, len(xs) - 1, 0, len(ys) - 1)

    best: dict = {}
    came: dict = {}
    heap: list = []
    for (i, j), (axis, cost, _dock) in starts.items():
        state = (i, j, axis)
        best[state] = cost
        came[state] = None
        heapq.heappush(heap, (cost + rest(i, j), cost, state))

    steps = 0
    while heap:
        _, cost, state = heapq.heappop(heap)
        if cost > best.get(state, float("inf")):
            continue
        steps += 1
        if steps > STEPS:                 # схема-монстр: сдаёмся, но не зависаем
            return None, None
        i, j, axis = state
        if (i, j) in goals and goals[(i, j)][0] == axis:
            out = []
            while state is not None:
                out.append((xs[state[0]], ys[state[1]]))
                state = came[state]
            out.reverse()
            return out, (i, j)

        for di, dj, naxis in ((1, 0, 0), (-1, 0, 0), (0, 1, 1), (0, -1, 1)):
            ni, nj = i + di, j + dj
            if not (i0 <= ni <= i1 and j0 <= nj <= j1):
                continue
            if naxis == 0:
                cell = (min(i, ni), j)
                wall = horizontal.get(cell)
                step = abs(xs[ni] - xs[i])
            else:
                cell = (i, min(j, nj))
                wall = vertical.get(cell)
                step = abs(ys[nj] - ys[j])
            if wall:
                continue
            price = (cost + step
                     + (0 if naxis == axis else BEND)
                     + (BUSY if (naxis, cell[0], cell[1]) in used else 0))
            reach = goals.get((ni, nj))
            if reach and reach[0] == naxis:
                price += reach[1]
            nstate = (ni, nj, naxis)
            if price < best.get(nstate, float("inf")):
                best[nstate] = price
                came[nstate] = (i, j, axis)
                heapq.heappush(heap, (price + rest(ni, nj), price, nstate))
    return None, None


def _tidy(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Выбрасывает повторы и точки посреди прямой."""
    out: list[tuple[float, float]] = []
    for point in points:
        if out and abs(out[-1][0] - point[0]) < EPS and abs(out[-1][1] - point[1]) < EPS:
            continue
        out.append(point)
    i = 1
    while i < len(out) - 1:
        (x0, y0), (x1, y1), (x2, y2) = out[i - 1], out[i], out[i + 1]
        if ((abs(x0 - x1) < EPS and abs(x1 - x2) < EPS)
                or (abs(y0 - y1) < EPS and abs(y1 - y2) < EPS)):
            del out[i]
        else:
            i += 1
    return out


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def reroute(shapes: list[ET.Element], edges: list[ET.Element]) -> tuple[int, int]:
    """Перекладывает стрелки, проходящие сквозь плашки. Меняет edges на месте."""
    boxes = _shape_boxes(shapes)
    if len(boxes) < 2:
        return 0, 0

    jobs = []
    calm = []
    for edge in edges:
        points = _points(edge)
        if len(points) < 2:
            continue
        ends = _ends(points, boxes)
        if _pierced(points, boxes, ends):
            jobs.append((edge, points, ends))
        else:
            calm.append(points)
    if not jobs:
        return 0, 0

    extra = [p for _, points, _ in jobs for p in (points[0], points[-1])]
    xs, ys = _grid(boxes, extra)
    if len(xs) > LIMIT or len(ys) > LIMIT:
        return 0, len(jobs)

    horizontal, vertical = _blocked(xs, ys, boxes)
    index_x = {v: i for i, v in enumerate(xs)}
    index_y = {v: i for i, v in enumerate(ys)}

    used: set = set()
    for points in calm:                 # чужие линии тоже заняты
        _occupy(points, xs, ys, used)

    done = failed = 0
    for edge, points, ends in jobs:
        src = _end_box(points[0], boxes, ends)
        tgt = _end_box(points[-1], boxes, ends)
        starts = _docks(src, tgt, index_x, index_y)
        goals = _docks(tgt, src, index_x, index_y)
        if not starts or not goals:
            failed += 1
            continue

        # сначала ищем в окрестности самой стрелки — так на порядок быстрее,
        # и только если пути там нет, разворачиваем всю сетку
        near = Box(min(src.x, tgt.x), min(src.y, tgt.y),
                   abs(src.cx - tgt.cx) + src.w + tgt.w,
                   abs(src.cy - tgt.cy) + src.h + tgt.h)
        window = (bisect_left(xs, near.x - SLACK), bisect_right(xs, near.right + SLACK) - 1,
                  bisect_left(ys, near.y - SLACK), bisect_right(ys, near.bottom + SLACK) - 1)
        path, hit = _search(xs, ys, horizontal, vertical, starts, goals, used, window)
        if path is None:
            path, hit = _search(xs, ys, horizontal, vertical, starts, goals, used)
        if path is None:
            failed += 1
            continue

        dock_in = starts[(index_x[round(path[0][0])], index_y[round(path[0][1])])][2]
        dock_out = goals[hit][2]
        fresh = _tidy([dock_in] + path + [dock_out])
        if len(fresh) < 2 or _pierced(fresh, boxes, ends):
            failed += 1               # хуже не делаем: оставляем как было
            continue
        _set_points(edge, fresh)
        _occupy(fresh, xs, ys, used)
        done += 1
    return done, failed
