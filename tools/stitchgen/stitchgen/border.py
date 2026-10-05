"""Step 5: satin edge and positioning run along the patch outline."""

from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient

from .geometry import polygons


@dataclass
class Border:
    centerline: LineString  # closed; the satin is centred on it so its outer edge is the patch edge
    width: float


def make_border(outline: Polygon, width_mm: float) -> Border:
    inner = polygons(outline.buffer(-width_mm / 2, quad_segs=16))
    ring = orient(inner[0] if inner else outline, sign=1.0).exterior
    coords = [(round(x, 4), round(y, 4)) for x, y in list(ring.coords)[:-1]]
    # Start at the bottom-most point (bottom centre of a bust), a predictable place for the machine to begin.
    start = max(range(len(coords)), key=lambda i: (coords[i][1], -coords[i][0]))
    coords = coords[start:] + coords[:start]
    return Border(LineString([*coords, coords[0]]), width_mm)
