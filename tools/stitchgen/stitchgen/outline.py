"""Step 3: derive the patch silhouette from the avatar."""

from __future__ import annotations

from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from .geometry import clean, polygons, union


def silhouette(shapes: list[BaseGeometry], concavity_fill_mm: float) -> Polygon:
    """Outer shape of the avatar with notches narrower than concavity_fill_mm closed and holes dropped."""
    r = concavity_fill_mm / 2
    # Closing (grow then shrink) fills notches narrower than concavity_fill_mm.
    closed = clean(union(shapes).buffer(r, quad_segs=16).buffer(-r, quad_segs=16))
    # Floating parts (an item above the head) are bridged with the smallest closing that joins them.
    for gap in (0.5, 1.0, 2.0, 3.0, 5.0):
        if len(polygons(closed)) <= 1:
            break
        closed = clean(closed.buffer(gap, quad_segs=16).buffer(-gap, quad_segs=16))
    return Polygon(polygons(closed)[0].exterior).simplify(0.02, preserve_topology=True)
