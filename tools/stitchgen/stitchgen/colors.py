"""Step 2: map colours onto the thread palette and merge same-thread regions."""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry.base import BaseGeometry

from .config import PaletteColor
from .geometry import polygons, union
from .normalize import NormalizedDrawing
from .report import Warning


@dataclass
class ColorRegion:
    thread: PaletteColor
    geometry: BaseGeometry  # Polygon or MultiPolygon, all same-thread visible area merged


@dataclass
class ReducedDrawing:
    regions: list[ColorRegion]  # one per thread, largest total area first
    warnings: list[Warning]


def reduce_colors(drawing: NormalizedDrawing, palette: list[PaletteColor], max_colors: int) -> ReducedDrawing:
    warnings: list[Warning] = []
    by_hex = {p.hex: p for p in palette}
    mapping = {c: nearest(c, palette) for c in sorted({e.color for e in drawing.elements})}

    area: dict[str, float] = {}
    for element in drawing.elements:
        thread = mapping[element.color].hex
        area[thread] = area.get(thread, 0.0) + element.geometry.area

    # Fold the least-used thread into its closest surviving neighbour until within the limit.
    while len(area) > max_colors:
        smallest = min(area, key=lambda h: (area[h], h))
        others = [by_hex[h] for h in area if h != smallest]
        target = nearest(smallest, others).hex
        warnings.append(Warning("color_merged", f"{by_hex[smallest].name} merged into {by_hex[target].name} to stay within {max_colors} colours"))
        area[target] += area.pop(smallest)
        for source, thread in mapping.items():
            if thread.hex == smallest:
                mapping[source] = by_hex[target]

    grouped: dict[str, list[BaseGeometry]] = {}
    for element in drawing.elements:
        grouped.setdefault(mapping[element.color].hex, []).append(element.geometry)
    regions = [ColorRegion(by_hex[h], union(geoms)) for h, geoms in grouped.items()]
    regions = [r for r in regions if polygons(r.geometry)]
    regions.sort(key=lambda r: (-round(r.geometry.area, 4), r.thread.hex))
    return ReducedDrawing(regions, warnings)


def nearest(hex_color: str, palette: list[PaletteColor]) -> PaletteColor:
    lab = _lab(hex_color)
    return min(palette, key=lambda p: (round(ciede2000(lab, _lab(p.hex)), 6), p.hex))


def _lab(hex_color: str) -> tuple[float, float, float]:
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    x = (0.4124564 * lin[0] + 0.3575761 * lin[1] + 0.1804375 * lin[2]) / 0.95047
    y = 0.2126729 * lin[0] + 0.7151522 * lin[1] + 0.0721750 * lin[2]
    z = (0.0193339 * lin[0] + 0.1191920 * lin[1] + 0.9503041 * lin[2]) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def ciede2000(lab1: tuple[float, float, float], lab2: tuple[float, float, float]) -> float:
    """CIEDE2000 colour difference (Sharma et al. 2005)."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    c_bar = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(c_bar ** 7 / (c_bar ** 7 + 25 ** 7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0

    dL = L2 - L1
    dC = c2p - c1p
    if c1p * c2p == 0:
        dh = 0.0
    elif abs(h2p - h1p) <= 180:
        dh = h2p - h1p
    else:
        dh = h2p - h1p - 360 if h2p > h1p else h2p - h1p + 360
    dH = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dh / 2))

    L_bar = (L1 + L2) / 2
    c_bar_p = (c1p + c2p) / 2
    if c1p * c2p == 0:
        h_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        h_bar = (h1p + h2p) / 2
    else:
        h_bar = (h1p + h2p + 360) / 2 if h1p + h2p < 360 else (h1p + h2p - 360) / 2
    t = (1 - 0.17 * math.cos(math.radians(h_bar - 30)) + 0.24 * math.cos(math.radians(2 * h_bar))
         + 0.32 * math.cos(math.radians(3 * h_bar + 6)) - 0.20 * math.cos(math.radians(4 * h_bar - 63)))
    d_theta = 30 * math.exp(-(((h_bar - 275) / 25) ** 2))
    r_c = 2 * math.sqrt(c_bar_p ** 7 / (c_bar_p ** 7 + 25 ** 7))
    s_l = 1 + 0.015 * (L_bar - 50) ** 2 / math.sqrt(20 + (L_bar - 50) ** 2)
    s_c = 1 + 0.045 * c_bar_p
    s_h = 1 + 0.015 * c_bar_p * t
    r_t = -math.sin(math.radians(2 * d_theta)) * r_c
    return math.sqrt(
        (dL / s_l) ** 2 + (dC / s_c) ** 2 + (dH / s_h) ** 2 + r_t * (dC / s_c) * (dH / s_h)
    )
