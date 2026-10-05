"""Step 5: the artwork's outer line, and the patch around it (background and heat-cut edge)."""

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


@dataclass
class Patch:
    edge: Satin  # heat-cut satin edge; its centre line doubles as the placement run
    background: BaseGeometry  # everything between the artwork and the edge
    outline: Polygon  # cut line (outer edge of the satin)


def make_patch(painted: BaseGeometry, shape: Polygon, margin_mm: float, edge_width_mm: float, smooth_mm: float, overlap_mm: float) -> Patch:
    """Sticker-style patch: a smooth contour margin_mm outside the artwork, finished with a satin edge.
    painted: everything the artwork stitches (fills, lines, outer line); the rest inside the edge is background."""
    r = smooth_mm
    # Grow past the margin, then close with a large radius so the cut line has no tight notches.
    body = polygons(clean(shape.buffer(margin_mm + edge_width_mm + r, quad_segs=16).buffer(-r, quad_segs=16)))[0]
    body = Polygon(body.exterior)
    centre = polygons(clean(body.buffer(-edge_width_mm, quad_segs=16).buffer(edge_width_mm / 2, quad_segs=16)))[0]
    inside_edge = body.buffer(-edge_width_mm + overlap_mm, quad_segs=16)  # background tucks under the edge
    background = clean(inside_edge.difference(painted.buffer(-overlap_mm, quad_segs=8)))
    return Patch(edge=band_satin(centre, edge_width_mm), background=background, outline=body)
