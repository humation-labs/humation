"""Step 5: the artwork's outer line, and the patch around it (background and heat-cut edge)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient
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
    body = cut_contour(shape, margin_mm + edge_width_mm, smooth_mm)
    centre = polygons(clean(body.buffer(-edge_width_mm, quad_segs=16).buffer(edge_width_mm / 2, quad_segs=16)))[0]
    inside_edge = body.buffer(-edge_width_mm + overlap_mm, quad_segs=16)  # background tucks under the edge
    background = clean(inside_edge.difference(painted.buffer(-overlap_mm, quad_segs=8)))
    return Patch(edge=band_satin(centre, edge_width_mm), background=background, outline=body)


def cut_contour(shape: Polygon, margin_mm: float, smooth_mm: float) -> Polygon:
    """Smooth contour margin_mm outside the artwork: grow past the margin, then shrink back with a large radius
    so the line has no tight notches to cut around."""
    r = smooth_mm
    body = polygons(clean(shape.buffer(margin_mm + r, quad_segs=16).buffer(-r, quad_segs=16)))[0]
    return Polygon(body.exterior).simplify(0.02, preserve_topology=True)


def outline_bridges(shape: Polygon, line_art: BaseGeometry, width_mm: float, max_gap_mm: float,
                    tolerance_mm: float = 0.25, step_mm: float = 0.1, crop: BaseGeometry | None = None) -> list[LineString]:
    """Centre lines of satins that close short gaps in the drawn outer line (--border outline).

    The drawn outer line is sewn as it is, like every other line. Only where the silhouette edge runs without
    a drawn line for at most max_gap_mm (a break in the hand-drawn stroke) is a satin of width_mm added, just
    inside the edge, overlapping the drawn line on both sides. Longer undrawn edges (an item drawn without an
    outline, the straight crop at the bottom) are left as the artwork has them."""
    ring = orient(shape, 1.0).exterior
    n = max(16, int(ring.length / step_mm))
    pts = np.array([ring.interpolate(i * ring.length / n).coords[0] for i in range(n)])
    covered = shapely.contains_xy(line_art.buffer(tolerance_mm, quad_segs=8), pts[:, 0], pts[:, 1])
    if crop is not None:
        # The straight crop is where the artwork is cut, not a break in a drawn line: never bridge it.
        covered |= shapely.distance(shapely.points(pts), crop.boundary) < 0.05
    if covered.all() or not covered.any():
        return []
    # Inward normals: for a counter-clockwise ring the inside is on the left of the direction of travel.
    tangent = np.roll(pts, -1, axis=0) - np.roll(pts, 1, axis=0)
    tangent /= np.maximum(np.hypot(*tangent.T), 1e-9)[:, None]
    inward = np.stack([-tangent[:, 1], tangent[:, 0]], axis=1)
    centre = pts + inward * (width_mm / 2)

    start = int(np.argmax(covered))  # walk the ring from a covered point so no gap wraps around the start
    order = np.roll(np.arange(n), -start)
    bridges, run = [], []
    for i in [*order, order[0]]:
        if not covered[i]:
            run.append(i)
            continue
        if run:
            length = len(run) * ring.length / n
            if length <= max_gap_mm:
                # Overlap the drawn line by one width on each side so the bridge joins it without a seam.
                pad = max(1, int(width_mm / (ring.length / n)))
                idx = [order[(np.where(order == run[0])[0][0] - k) % n] for k in range(pad, 0, -1)] + run + \
                      [order[(np.where(order == run[-1])[0][0] + k) % n] for k in range(1, pad + 1)]
                line = LineString(centre[idx]).simplify(0.03, preserve_topology=False)
                if line.length > width_mm:
                    bridges.append(line)
            run = []
    return bridges
