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
    """placement run -> fill layer (colour by colour) -> line layer (Humation line art) -> outer line -> patch edge.
    The line art is drawn over the fills in the artwork, so it is sewn over them too and covers every seam."""
    items: list[Item] = []
    if patch is not None and patch_thread is not None:
        items.append(Item("run", patch_thread, patch.edge.centre, "placement"))
    elif placement and border is not None and border_thread is not None:
        items.append(Item("run", border_thread, border.satin.centre, "placement"))

    fills = [f for f in attributed.fills if f.layer == "fill"]
    satins = [s for s in attributed.satins if s.layer == "fill"]
    line_fills = [f for f in attributed.fills if f.layer == "line"]
    line_satins = [s for s in attributed.satins if s.layer == "line"]
    line_threads = sorted({x.thread.hex for x in [*line_fills, *line_satins]})

    # Fill layer: thread groups by total area (largest first). Each thread's narrow satins follow its fills.
    area: dict[str, float] = {}
    for f in fills:
        area[f.thread.hex] = area.get(f.thread.hex, 0.0) + f.geometry.area
    threads = sorted(area, key=lambda h: (-round(area[h], 4), h))
    threads += sorted({s.thread.hex for s in satins} - set(threads))
    if items and items[-1].thread.hex in threads:
        threads.remove(items[-1].thread.hex)  # the placement thread's fills go first: one colour change fewer
        threads.insert(0, items[-1].thread.hex)
    for hex_ in [h for h in threads if h in line_threads]:
        threads.remove(hex_)  # a fill in the line colour (black hair) goes last, straight into the line layer
        threads.append(hex_)
    for hex_ in threads:
        _add_fills(items, fills, hex_)
        _add_satins(items, satins, hex_)

    # Line layer: solid line-art shapes, then the lines, nearest first.
    for hex_ in line_threads:
        _add_fills(items, line_fills, hex_)
        _add_satins(items, line_satins, hex_)

    if border is not None and border_thread is not None:
        items.append(Item("satin", border_thread, border.satin.centre, "border", satin=border.satin))
    if patch is not None and patch_thread is not None:
        items.append(Item("satin", patch_thread, patch.edge.centre, "edge", satin=patch.edge))
    return items


def _add_fills(items: list[Item], fills: list[FillPart], hex_: str) -> None:
    group = sorted((f for f in fills if f.thread.hex == hex_), key=lambda f: (-round(f.geometry.area, 4), f.geometry.bounds))
    items.extend(Item("fill", f.thread, f.geometry, "fill", angle=f.angle, guide=f.guide, guide_strategy=f.guide_strategy, flow=f.flow)
                 for f in group)


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
        if flip:
            s = SatinLine(s.thread, s.satin.reversed())
        ordered.append(s)
        position = s.satin.centre.coords[-1]
    return ordered
