"""Step 6: sewing order — placement run, fills, line satins, satin border."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from shapely.geometry import LineString, Polygon

from .attributes import Attributed, SatinLine
from .border import Border
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


def sewing_order(attributed: Attributed, border: Border | None, border_thread: PaletteColor | None) -> list[Item]:
    items: list[Item] = []
    if border is not None and border_thread is not None:
        items.append(Item("run", border_thread, border.satin.centre, "placement"))

    # Fills: thread groups by total area (largest first), regions by area within a thread.
    fill_area: dict[str, float] = {}
    for f in attributed.fills:
        fill_area[f.thread.hex] = fill_area.get(f.thread.hex, 0.0) + f.geometry.area
    fill_threads = sorted(fill_area, key=lambda h: (-round(fill_area[h], 4), h))
    if items and items[-1].thread.hex in fill_threads:
        # Sewing the placement thread's fills next saves a colour change.
        fill_threads.remove(items[-1].thread.hex)
        fill_threads.insert(0, items[-1].thread.hex)
    # Line-art satins (the border thread) go on top of everything; other threads' thin parts (hair
    # strands, stems) are sewn right after that thread's fills so the colour is not loaded twice.
    final = border_thread.hex if border is not None and border_thread is not None else None
    satin_threads = sorted({s.thread.hex for s in attributed.satins})
    line_art = final if final in satin_threads else None
    for hex_ in fill_threads:
        group = sorted((f for f in attributed.fills if f.thread.hex == hex_), key=lambda f: (-round(f.geometry.area, 4), f.geometry.bounds))
        items.extend(Item("fill", f.thread, f.geometry, "fill", angle=f.angle) for f in group)
        if hex_ != line_art and hex_ in satin_threads:
            _add_satins(items, attributed.satins, hex_)
    for hex_ in satin_threads:
        if hex_ != line_art and hex_ not in fill_threads:
            _add_satins(items, attributed.satins, hex_)
    if line_art is not None:
        _add_satins(items, attributed.satins, line_art)

    if border is not None and border_thread is not None:
        items.append(Item("satin", border_thread, border.satin.centre, "border", satin=border.satin))
    return items


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
