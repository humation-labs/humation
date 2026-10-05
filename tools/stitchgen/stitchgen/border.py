"""Step 5: satin edge along the artwork's own outer outline, plus a positioning run."""

from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from .geometry import clean, polygons
from .satin import Satin, band_satin


@dataclass
class Border:
    satin: Satin  # its centre line doubles as the placement run
    outline: Polygon  # finished patch edge (outer edge of the satin)
    inner: BaseGeometry  # area left for the artwork; everything outside it is covered by the border


def make_border(shape: Polygon, width_mm: float, inset_mm: float) -> Border:
    """The border replaces the avatar's outermost drawn line: it reaches inset_mm inside the silhouette,
    just over that line, and the rest of its width lies outside, so small protrusions keep their colour."""
    inset = inset_mm
    offset_mm = width_mm - inset_mm
    # Shrink-then-grow rounds convex corners so the inner rail can never fold over itself.
    centre = polygons(clean(shape.buffer(-inset, quad_segs=16).buffer(width_mm / 2, quad_segs=16)))[0]
    return Border(
        satin=band_satin(centre, width_mm),
        outline=polygons(shape.buffer(offset_mm, quad_segs=16))[0],
        inner=clean(shape.buffer(-inset, quad_segs=16)),
    )
