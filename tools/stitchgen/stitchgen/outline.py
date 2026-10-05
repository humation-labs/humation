"""Step 3: derive the patch silhouette from the avatar."""

from __future__ import annotations

from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from .geometry import clean, polygons, union


def patch_outline(shapes: list[BaseGeometry], concavity_fill_mm: float, offset_mm: float) -> Polygon:
    """Outer edge of the patch: the silhouette with narrow notches closed, holes dropped, offset outward."""
    silhouette = union(shapes)
    r = concavity_fill_mm / 2
    # Closing (grow then shrink) fills notches narrower than concavity_fill_mm.
    closed = clean(silhouette.buffer(r, quad_segs=16).buffer(-r, quad_segs=16))
    # Separate islands (e.g. a floating item) are bridged by the offset, otherwise keep the largest.
    outer = clean(union(Polygon(p.exterior) for p in polygons(closed)).buffer(offset_mm, quad_segs=16))
    parts = polygons(outer)
    return Polygon(parts[0].exterior).simplify(0.02, preserve_topology=True)
