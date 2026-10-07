"""Step 6: sewing order — placement run, fills, line satins, satin border."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from shapely.geometry import LineString, Polygon

from .attributes import Attributed, FillPart, SatinLine
from .border import Border, Patch
from .config import PaletteColor
from .satin import Satin


Kind = Literal["run", "fill", "satin"]


@dataclass
class Item:
    kind: Kind
    thread: PaletteColor
    geometry: Polygon | LineString
    role: str  # placement | fill | line | border
    angle: float = 0.0
    satin: Satin | None = None
    guide: LineString | None = None
    guide_strategy: int = 0
    flow: str | None = None


def sewing_order(attributed: Attributed, border: Border | None, border_thread: PaletteColor | None, placement: bool = True,
                 patch: Patch | None = None, patch_thread: PaletteColor | None = None) -> list[Item]:
    """placement run -> fill layer (colour by colour) -> small marks of the fill layer -> line layer (Humation
    line art) -> outer line -> patch edge. The line art is drawn over the fills in the artwork, so it is sewn
    over them too and covers every seam."""
    items: list[Item] = []
    if patch is not None and patch_thread is not None:
        items.append(Item("run", patch_thread, patch.edge.centre, "placement"))
    elif placement and border is not None and border_thread is not None:
        items.append(Item("run", border_thread, border.satin.centre, "placement"))

    for kind, hex_, group in sewing_plan(attributed.fills, attributed.satins, items[-1].thread.hex if items else None):
        if kind == "fill":
            items.extend(Item("fill", f.thread, f.geometry, "fill", angle=f.angle, guide=f.guide,
                              guide_strategy=f.guide_strategy, flow=f.flow) for f in group)
        else:
            _add_satins(items, group, hex_)

    if border is not None and border_thread is not None:
        items.append(Item("satin", border_thread, border.satin.centre, "border", satin=border.satin))
    if patch is not None and patch_thread is not None:
        items.append(Item("satin", patch_thread, patch.edge.centre, "edge", satin=patch.edge))
    return items


def _fill_threads(fills: list[FillPart], satins: list[SatinLine], line_threads: list[str], first: str | None = None) -> list[str]:
    """Fill-layer thread order: largest total fill area first, then threads that only have satins; the
    placement thread first (one colour change fewer); a thread in the line colour (black hair) last, so it runs
    straight into the line layer."""
    area: dict[str, float] = {}
    for f in fills:
        area[f.thread.hex] = area.get(f.thread.hex, 0.0) + f.geometry.area
    threads = sorted(area, key=lambda h: (-round(area[h], 4), h))
    threads += sorted({s.thread.hex for s in satins} - set(threads))
    if first in threads:
        threads.remove(first)
        threads.insert(0, first)
    return [h for h in threads if h not in line_threads] + [h for h in threads if h in line_threads]


def _within_thread(fills: list[FillPart], hex_: str) -> list[FillPart]:
    return sorted((f for f in fills if f.thread.hex == hex_), key=lambda f: (-round(f.geometry.area, 4), f.geometry.bounds))


def sewing_plan(fills: list[FillPart], satins: list[SatinLine], first: str | None = None) -> list[tuple[str, str, list]]:
    """The one sewing order, as (kind, thread, items) groups; sewing_order and the underlap rule both use it.
    1. fill layer, thread by thread: the thread's fills, then its narrow satins;
    2. the fill layer's small marks (catchlights, polka dots), after every fill, so no later fill buries them;
    3. line layer: solid line-art shapes, then the lines."""
    layer_fills = [f for f in fills if f.layer == "fill"]
    layer_satins = [s for s in satins if s.layer == "fill"]
    body = [s for s in layer_satins if not s.satin.dot]
    marks = [s for s in layer_satins if s.satin.dot]
    line_fills = [f for f in fills if f.layer == "line"]
    line_satins = [s for s in satins if s.layer == "line"]
    line_threads = sorted({x.thread.hex for x in [*line_fills, *line_satins]})
    plan: list[tuple[str, str, list]] = []
    threads = _fill_threads(layer_fills, layer_satins, line_threads, first)
    for hex_ in threads:
        plan.append(("fill", hex_, _within_thread(layer_fills, hex_)))
        plan.append(("satin", hex_, [s for s in body if s.thread.hex == hex_]))
    mark_threads = [h for h in threads if any(s.thread.hex == h for s in marks)]
    # Marks in the thread sewn last so far go first: no extra colour change for them.
    mark_threads.sort(key=lambda h: (h != threads[-1] if threads else True, threads.index(h)))
    for hex_ in mark_threads:
        plan.append(("satin", hex_, [s for s in marks if s.thread.hex == hex_]))
    for hex_ in line_threads:
        plan.append(("fill", hex_, _within_thread(line_fills, hex_)))
        plan.append(("satin", hex_, [s for s in line_satins if s.thread.hex == hex_]))
    return [g for g in plan if g[2]]


def sewing_sequence(fills: list[FillPart], satins: list[SatinLine], first: str | None = None) -> list[tuple[str, object]]:
    """Every fill and satin as ("fill"|"satin", item) in sewing order (satin order inside a group aside)."""
    return [(kind, x) for kind, _, group in sewing_plan(fills, satins, first) for x in group]


def _add_satins(items: list[Item], satins: list[SatinLine], hex_: str) -> None:
    position = _end_point(items[-1]) if items else (0.0, 0.0)
    for satin in _nearest_path([s for s in satins if s.thread.hex == hex_], position):
        items.append(Item("satin", satin.thread, satin.satin.centre, "line", satin=satin.satin))


def _end_point(item: Item) -> tuple[float, float]:
    if isinstance(item.geometry, LineString):
        return item.geometry.coords[-1]
    c = item.geometry.centroid
    return (c.x, c.y)


def _nearest_path(satins: list[SatinLine], start: tuple[float, float]) -> list[SatinLine]:
    """Greedy nearest-neighbour tour; open satins are flipped to start at the nearer end."""
    remaining = sorted(satins, key=lambda s: tuple(s.satin.centre.coords[0]))
    ordered: list[SatinLine] = []
    position = start
    while remaining:
        best_i, best_d, flip = 0, math.inf, False
        for i, s in enumerate(remaining):
            a, b = s.satin.centre.coords[0], s.satin.centre.coords[-1]
            da, db = math.dist(position, a), math.dist(position, b)
            if da < best_d:
                best_i, best_d, flip = i, da, False
            if not s.satin.closed and db < best_d:
                best_i, best_d, flip = i, db, True
        s = remaining.pop(best_i)
        if flip and not s.satin.dot:  # marks keep their canonical direction, so a pair of eyes matches
            s = SatinLine(s.thread, s.satin.reversed())
        ordered.append(s)
        position = s.satin.centre.coords[-1]
    return ordered
