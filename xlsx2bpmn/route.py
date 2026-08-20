# -*- coding: utf-8 -*-
"""
Выбор маршрута для каждой стрелки.

Стрелка не имеет права проходить сквозь плашку — ни сквозь задачу, ни сквозь
событие, ни сквозь документ. Но одного этого мало: линия должна ещё и идти
разумно — коротко, без лишних изломов, не пересекая чужие стрелки и не
сливаясь с ними в одну.

Поэтому для каждой стрелки собирается несколько вариантов: тот, что провёл
раскладчик, простые обходы (прямая, угол, скобка) и, если простых не хватило,
путь поиском по сетке из границ самих фигур. Все варианты оцениваются по
одной мерке — длина, изломы, пересечения с чужими, участки внахлёст, — и
берётся лучший. Действующему маршруту дана скидка: без выигрыша его не трогаем.

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
BEND = 60        # цена излома в пикселях пути: прямая дешевле ломаной
CROSS = 250      # цена пересечения с чужой стрелкой
OVER = 2.0       # цена каждого пикселя, пройденного по чужой линии
KEEP = 60        # скидка действующему маршруту: без пользы не дёргаем
SIDE = 40        # штраф за выход не с той стороны, куда стрелка идёт
BUSY = 30        # штраф поиску за линию сетки, уже кем-то занятую
GAP = 6          # ближе этого две параллельные линии сливаются в одну
CELL = 200       # клетка индекса соседей, в пикселях
TOUCH = 8        # настолько стрелке можно войти в свою же плашку
SLACK = 140      # насколько шире концов стрелки смотрим сначала
LIMIT = 900      # предел числа линий сетки по оси: дальше не ищем
STEPS = 80_000   # предел шагов поиска на одну стрелку
PASSES = 2       # проходов подбора: второй видит уже улучшенных соседей
HEAVY = 90       # выше этого числа стрелок реже зовём поиск: он дорогой
EPS = 0.5


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


Point = tuple[float, float]
Path = list[Point]


# --------------------------------------------------------------------------- #
# Мелочи
# --------------------------------------------------------------------------- #

def _num(value: float) -> str:
    return str(int(round(value)))


def _points(edge: ET.Element) -> Path:
    return [(float(p.get("x", 0)), float(p.get("y", 0)))
            for p in edge.findall(f"{D}waypoint")]


def _set_points(edge: ET.Element, points: Path) -> None:
    for old in edge.findall(f"{D}waypoint"):
        edge.remove(old)
    for i, (x, y) in enumerate(points):     # waypoint идут до BPMNLabel
        edge.insert(i, ET.Element(f"{D}waypoint", {"x": _num(x), "y": _num(y)}))


def _inside(p: Point, q: Point, box: Box, grow: float) -> float:
    """Длина куска отрезка внутри плашки. Отсечение Лианга — Барски."""
    left, right = box.x - grow + EPS, box.right + grow - EPS
    top, bottom = box.y - grow + EPS, box.bottom + grow - EPS
    if left >= right or top >= bottom:
        return 0.0
    if max(p[0], q[0]) <= left or min(p[0], q[0]) >= right:
        return 0.0
    if max(p[1], q[1]) <= top or min(p[1], q[1]) >= bottom:
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
    return (hi - lo) * (dx * dx + dy * dy) ** 0.5


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


def _ends(points: Path, boxes: list[Box]) -> set[int]:
    """Плашки, на которых висят концы стрелки: их касаться разрешено.

    Граничное событие сидит на краю своей задачи, поэтому концом стрелки
    считается каждая плашка, накрывающая крайнюю точку.
    """
    out: set[int] = set()
    for x, y in (points[0], points[-1]):
        for i, b in enumerate(boxes):
            if b.x - 2 <= x <= b.right + 2 and b.y - 2 <= y <= b.bottom + 2:
                out.add(i)
    return out


def _pierced(points: Path, boxes: list[Box], ends: set[int]) -> bool:
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


def _end_box(point: Point, boxes: list[Box], ends: set[int]) -> Box:
    """Плашка, с которой стрелка стартует. Из вложенных берём меньшую."""
    on = [boxes[i] for i in ends
          if boxes[i].x - 2 <= point[0] <= boxes[i].right + 2
          and boxes[i].y - 2 <= point[1] <= boxes[i].bottom + 2]
    if not on:                       # конец висит в воздухе — цепляемся за точку
        return Box(point[0], point[1], 0, 0)
    return min(on, key=lambda b: b.w * b.h)


def _tidy(points: Path) -> Path:
    """Выбрасывает повторы и точки посреди прямой."""
    out: Path = []
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
# Мерка качества
# --------------------------------------------------------------------------- #

def _length(points: Path) -> float:
    return sum(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
               for a, b in zip(points, points[1:]))


def _bends(points: Path) -> int:
    return max(len(points) - 2, 0)


def _turn(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _crossing(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    """Настоящее пересечение: касания в общей точке не считаются."""
    d1, d2 = _turn(p3, p4, p1), _turn(p3, p4, p2)
    d3, d4 = _turn(p1, p2, p3), _turn(p1, p2, p4)
    return (((d1 > EPS and d2 < -EPS) or (d1 < -EPS and d2 > EPS))
            and ((d3 > EPS and d4 < -EPS) or (d3 < -EPS and d4 > EPS)))


def _shared(a: Point, b: Point, c: Point, d: Point) -> float:
    """Длина куска, который два параллельных отрезка проходят вместе."""
    if abs(a[1] - b[1]) < EPS and abs(c[1] - d[1]) < EPS and abs(a[1] - c[1]) < GAP:
        lo = max(min(a[0], b[0]), min(c[0], d[0]))
        hi = min(max(a[0], b[0]), max(c[0], d[0]))
        return max(hi - lo, 0.0)
    if abs(a[0] - b[0]) < EPS and abs(c[0] - d[0]) < EPS and abs(a[0] - c[0]) < GAP:
        lo = max(min(a[1], b[1]), min(c[1], d[1]))
        hi = min(max(a[1], b[1]), max(c[1], d[1]))
        return max(hi - lo, 0.0)
    return 0.0


def _cells(a: Point, b: Point):
    """Клетки, которые задевает отрезок. По ним ищутся соседи."""
    x1, x2 = sorted((a[0], b[0]))
    y1, y2 = sorted((a[1], b[1]))
    for cx in range(int((x1 - GAP) // CELL), int((x2 + GAP) // CELL) + 1):
        for cy in range(int((y1 - GAP) // CELL), int((y2 + GAP) // CELL) + 1):
            yield cx, cy


def _add(index: dict, owner: int, points: Path) -> None:
    for k, (a, b) in enumerate(zip(points, points[1:])):
        for key in _cells(a, b):
            index.setdefault(key, {})[(owner, k)] = (a, b)


def _drop(index: dict, owner: int, points: Path) -> None:
    for k, (a, b) in enumerate(zip(points, points[1:])):
        for key in _cells(a, b):
            index.get(key, {}).pop((owner, k), None)


def _clash(points: Path, index: dict, mine: int) -> tuple[int, float]:
    """Сколько раз маршрут пересекает чужие стрелки и сколько идёт по ним.

    Соседи берутся из клеточного индекса, а не перебором всех со всеми:
    на густых схемах это разница между секундами и минутами.
    """
    cross, along = 0, 0.0
    for a, b in zip(points, points[1:]):
        near: dict = {}
        for key in _cells(a, b):
            for who, seg in index.get(key, {}).items():
                if who[0] != mine:
                    near[who] = seg
        for c, d in near.values():
            if _crossing(a, b, c, d):
                cross += 1
            else:
                along += _shared(a, b, c, d)
    return cross, along


def _cost(points: Path) -> float:
    """Дешёвая мерка: длина плюс изломы. По ней варианты выстраиваются в ряд."""
    return _length(points) + BEND * _bends(points)


# --------------------------------------------------------------------------- #
# Простые обходы — то, как линию провёл бы человек
# --------------------------------------------------------------------------- #

def _gates(box: Box, other: Box) -> dict:
    """Причалы плашки: сторона -> (точка на плашке, точка отхода, ось, штраф)."""
    natural = set()
    if other.cx > box.right:
        natural.add("r")
    if other.cx < box.x:
        natural.add("l")
    if other.cy > box.bottom:
        natural.add("b")
    if other.cy < box.y:
        natural.add("t")
    if not natural:                           # плашки стоят друг на друге
        natural = {"r", "l", "t", "b"}

    sides = {
        "r": ((box.right, box.cy), (box.right + MARGIN, box.cy), 0),
        "l": ((box.x, box.cy), (box.x - MARGIN, box.cy), 0),
        "t": ((box.cx, box.y), (box.cx, box.y - MARGIN), 1),
        "b": ((box.cx, box.bottom), (box.cx, box.bottom + MARGIN), 1),
    }
    return {name: (dock, stub, axis, 0 if name in natural else SIDE)
            for name, (dock, stub, axis) in sides.items()}


def _simple(src: Box, tgt: Box) -> list[Path]:
    """Прямая, угол и скобка на всех разумных причалах."""
    out: list[Path] = []
    here, there = _gates(src, tgt), _gates(tgt, src)
    for _sn, (sdock, sstub, saxis, spen) in here.items():
        if spen:
            continue                          # только естественные стороны
        for _tn, (tdock, tstub, taxis, tpen) in there.items():
            if tpen:
                continue
            if saxis == taxis == 0:
                if abs(sstub[1] - tstub[1]) < EPS:
                    out.append([sdock, tdock])
                middle = (sstub[0] + tstub[0]) / 2
                for shift in (0, 30, -30, 60, -60):
                    x = middle + shift
                    out.append([sdock, sstub, (x, sstub[1]),
                                (x, tstub[1]), tstub, tdock])
                for y in (min(src.y, tgt.y) - MARGIN - 20,
                          max(src.bottom, tgt.bottom) + MARGIN + 20):
                    out.append([sdock, sstub, (sstub[0], y),
                                (tstub[0], y), tstub, tdock])
            elif saxis == taxis == 1:
                if abs(sstub[0] - tstub[0]) < EPS:
                    out.append([sdock, tdock])
                middle = (sstub[1] + tstub[1]) / 2
                for shift in (0, 30, -30, 60, -60):
                    y = middle + shift
                    out.append([sdock, sstub, (sstub[0], y),
                                (tstub[0], y), tstub, tdock])
                for x in (min(src.x, tgt.x) - MARGIN - 20,
                          max(src.right, tgt.right) + MARGIN + 20):
                    out.append([sdock, sstub, (x, sstub[1]),
                                (x, tstub[1]), tstub, tdock])
            elif saxis == 0:
                out.append([sdock, sstub, (tstub[0], sstub[1]), tstub, tdock])
            else:
                out.append([sdock, sstub, (sstub[0], tstub[1]), tstub, tdock])
    return [_tidy(path) for path in out]


# --------------------------------------------------------------------------- #
# Сетка и поиск — для случаев, где простого обхода нет
# --------------------------------------------------------------------------- #

def _grid(boxes: list[Box], extra: list[Point]):
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


def _occupy(points: Path, xs: list[int], ys: list[int], used: set) -> None:
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


def _docks(box: Box, other: Box, index_x: dict, index_y: dict) -> dict:
    """Причалы, переведённые в узлы сетки: узел -> (ось, штраф, точка)."""
    out: dict = {}
    for _name, (dock, stub, axis, penalty) in _gates(box, other).items():
        i = index_x.get(round(stub[0]))
        j = index_y.get(round(stub[1]))
        if i is None or j is None:
            continue
        if (i, j) not in out or penalty < out[(i, j)][1]:
            out[(i, j)] = (axis, penalty, dock)
    return out


def _search(xs, ys, horizontal, vertical, starts, goals, used, window=None):
    """A* по сетке: длина пути плюс штрафы за изломы и занятые линии.

    Обходятся все плашки без исключения, включая свои собственные: причалы
    стоят ровно на границе обходного коридора, поэтому выйти из плашки это
    не мешает, а вернуться в неё посреди пути — не даёт.
    """
    marks = [(xs[gi], ys[gj]) for gi, gj in goals]
    seen_rest: dict[tuple[int, int], float] = {}

    def rest(i: int, j: int) -> float:
        """Оценка остатка пути. Считается один раз на узел — вызовов миллионы."""
        got = seen_rest.get((i, j))
        if got is None:
            x, y = xs[i], ys[j]
            got = min(abs(x - gx) + abs(y - gy) for gx, gy in marks)
            seen_rest[(i, j)] = got
        return got

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
            out: Path = []
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


def _found(src: Box, tgt: Box, mesh: dict, used: set) -> Path | None:
    """Путь по сетке от одной плашки к другой или None."""
    xs, ys = mesh["xs"], mesh["ys"]
    starts = _docks(src, tgt, mesh["ix"], mesh["iy"])
    goals = _docks(tgt, src, mesh["ix"], mesh["iy"])
    if not starts or not goals:
        return None

    # сначала ищем в окрестности самой стрелки — так на порядок быстрее,
    # и только если пути там нет, разворачиваем всю сетку
    near = Box(min(src.x, tgt.x), min(src.y, tgt.y),
               abs(src.cx - tgt.cx) + src.w + tgt.w,
               abs(src.cy - tgt.cy) + src.h + tgt.h)
    window = (bisect_left(xs, near.x - SLACK), bisect_right(xs, near.right + SLACK) - 1,
              bisect_left(ys, near.y - SLACK), bisect_right(ys, near.bottom + SLACK) - 1)
    path, hit = _search(xs, ys, mesh["h"], mesh["v"], starts, goals, used, window)
    if path is None:
        path, hit = _search(xs, ys, mesh["h"], mesh["v"], starts, goals, used)
    if path is None:
        return None
    dock_in = starts[(mesh["ix"][round(path[0][0])],
                      mesh["iy"][round(path[0][1])])][2]
    return _tidy([dock_in] + path + [goals[hit][2]])


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def reroute(shapes: list[ET.Element], edges: list[ET.Element]) -> tuple[int, int]:
    """Подбирает каждой стрелке лучший маршрут. Меняет edges на месте."""
    boxes = _shape_boxes(shapes)
    if len(boxes) < 2:
        return 0, 0

    routes = [_points(e) for e in edges]
    live = [i for i, points in enumerate(routes) if len(points) >= 2]
    if not live:
        return 0, 0

    extra = [end for i in live for end in (routes[i][0], routes[i][-1])]
    xs, ys = _grid(boxes, extra)
    mesh = None
    if len(xs) <= LIMIT and len(ys) <= LIMIT:
        horizontal, vertical = _blocked(xs, ys, boxes)
        mesh = {"xs": xs, "ys": ys, "h": horizontal, "v": vertical,
                "ix": {v: i for i, v in enumerate(xs)},
                "iy": {v: i for i, v in enumerate(ys)}}
    nearby: dict = {}
    for i in live:
        _add(nearby, i, routes[i])

    moved: set[int] = set()
    failed = 0
    # второй проход выбирает маршрут, уже зная улучшенных соседей: так
    # пересечений заметно меньше, а третий проход почти ничего не добавляет
    for _round in range(PASSES):
        failed = 0
        used: set = set()
        for i in live:
            failed += _one(i, routes, nearby, boxes, edges, mesh, used, moved,
                           xs, ys)
    return len(moved), failed


def _one(index: int, routes: list[Path], nearby: dict, boxes: list[Box],
         edges: list[ET.Element], mesh: dict | None, used: set,
         moved: set[int], xs: list[int], ys: list[int]) -> int:
    """Подбирает маршрут одной стрелке. Возвращает 1, если не вышло."""
    old = routes[index]
    ends = _ends(old, boxes)
    src = _end_box(old[0], boxes, ends)
    tgt = _end_box(old[-1], boxes, ends)

    # сначала выстраиваем варианты по дешёвой мерке и проверяем на плашки
    # только лучших: перебирать все два десятка целиком незачем
    ranked: list[tuple[float, int, Path]] = []
    if not _pierced(old, boxes, ends):
        ranked.append((_cost(old) - KEEP, 0, old))
    for n, path in enumerate(_simple(src, tgt), start=1):
        if len(path) >= 2:
            ranked.append((_cost(path), n, path))
    ranked.sort(key=lambda item: (item[0], item[1]))

    # на густой схеме поиск зовём только к совсем ломаным: он дорогой
    limit = 3 if len(routes) > HEAVY else 2

    clean: list[Path] = []
    for _score, _n, path in ranked:
        if _pierced(path, boxes, ends):
            continue
        clean.append(path)
        if len(clean) >= 4:
            break

    # поиск по сетке — когда простого обхода нет или лучший из них ломаный
    if mesh and (not clean or _bends(clean[0]) >= limit):
        found = _found(src, tgt, mesh, used)
        if found and not _pierced(found, boxes, ends):
            clean.append(found)

    if not clean:
        return 1

    best, best_score = clean[0], None
    for path in clean:
        cross, along = _clash(path, nearby, index)
        score = (_cost(path) + CROSS * cross + OVER * along
                 - (KEEP if path is old else 0))
        if best_score is None or score < best_score:
            best, best_score = path, score

    if best is not old:
        _drop(nearby, index, old)
        _add(nearby, index, best)
        _set_points(edges[index], best)
        routes[index] = best
        moved.add(index)
    if mesh:
        _occupy(best, xs, ys, used)
    return 0
