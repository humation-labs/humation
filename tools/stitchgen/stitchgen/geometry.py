"""Small shapely helpers shared by the pipeline steps."""

from __future__ import annotations

from collections.abc import Iterable

from shapely import make_valid, set_precision
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

GRID = 1e-4  # coordinate grid (mm) that keeps boolean ops stable and reproducible
SLIVER_AREA = 0.01  # mm²


def polygons(geom: BaseGeometry | None) -> list[Polygon]:
    """Every non-empty Polygon inside geom, in a stable order (largest first)."""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        found = [geom]
    elif isinstance(geom, (MultiPolygon, GeometryCollection)):
        found = [p for part in geom.geoms for p in polygons(part)]
    else:
        found = []
    return sorted(
        (p for p in found if p.area > 0),
        key=lambda p: (-round(p.area, 6), round(p.bounds[0], 4), round(p.bounds[1], 4)),
    )


def clean(geom: BaseGeometry | None, min_area: float = SLIVER_AREA) -> BaseGeometry:
    """Valid, grid-snapped polygonal geometry without slivers."""
    if geom is None or geom.is_empty:
        return Polygon()
    geom = set_precision(make_valid(geom), GRID)
    parts = [p for p in polygons(geom) if p.area >= min_area]
    if not parts:
        return Polygon()
    if len(parts) == 1:
        return parts[0]
    return MultiPolygon(parts)


def union(geoms: Iterable[BaseGeometry]) -> BaseGeometry:
    return clean(unary_union([g for g in geoms if g is not None and not g.is_empty]))
