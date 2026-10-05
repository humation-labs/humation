"""Two-rail satin columns that follow the artwork's own shapes.

Ink/Stitch pairs the nodes of two rails with the same node count as rungs, so every satin here is two
equally long point lists: the stitches run from rails[0][i] to rails[1][i]. Line widths therefore follow the
source line (tapering, round ends) instead of a constant stroke width.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient

from .geometry import polygons

STEP = 0.25  # mm between rungs
MIN_HALF = 0.08  # never let a rail touch the centre line, Ink/Stitch dislikes zero-width rungs

Point = tuple[float, float]


@dataclass
class Satin:
    rails: tuple[list[Point], list[Point]]
    centre: LineString  # used for sewing order and debug output
    width: float  # median width, mm
    closed: bool = False

    def reversed(self) -> Satin:
        return Satin((self.rails[0][::-1], self.rails[1][::-1]), LineString(list(self.centre.coords)[::-1]), self.width, self.closed)


def line_satin(line: LineString, half_at: Callable[[float, float], float], closed: bool, min_half: float, max_half: float) -> Satin | None:
    """Satin along a centre line whose half-width is read from the artwork (half_at) and clamped."""
    pts = _resample(line, closed)
    if len(pts) < 2:
        return None
    raw = np.array([half_at(x, y) for x, y in pts])
    half = np.clip(_moving_average(raw, 5, closed), min_half, max_half)

    if not closed:
        # Round ends: carry on past the skeleton end for as far as the artwork goes, then taper like a circle.
        pts, half = _extend(pts, half, raw[0], at_start=True)
        pts, half = _extend(pts, half, raw[-1], at_start=False)
        s = _arc_lengths(pts)
        for end_dist, r in ((s, half[0]), (s[-1] - s, half[-1])):
            cap = end_dist < r
            half[cap] = np.minimum(half[cap], np.sqrt(np.maximum(r * r - (r - end_dist[cap]) ** 2, 0)))
    half = np.maximum(half, MIN_HALF)
    a, b = _offset_rails(pts, half, closed)
    return Satin((a, b), LineString(pts + ([pts[0]] if closed else [])), round(float(np.median(half)) * 2, 3), closed)


def dot_satin(shape: BaseGeometry, min_width: float) -> Satin | None:
    """Compact mark (eye, dash): its own contour, split at the two ends of the long axis, becomes the rails."""
    axes = rect_axes(shape)
    if axes is None:
        return None
    if axes[2] < min_width:
        shape = shape.buffer((min_width - axes[2]) / 2, quad_segs=8)
        axes = rect_axes(shape)
    parts = polygons(shape)
    if not parts or axes is None:
        return None
    (ux, uy), long_len, short_len = axes
    ring = list(orient(parts[0], 1.0).exterior.coords)[:-1]
    c = parts[0].centroid
    proj = [(x - c.x) * ux + (y - c.y) * uy for x, y in ring]
    i0, i1 = int(np.argmin(proj)), int(np.argmax(proj))
    n = len(ring)
    forward = [ring[(i0 + k) % n] for k in range((i1 - i0) % n + 1)]
    backward = [ring[(i0 - k) % n] for k in range((i0 - i1) % n + 1)]
    count = max(5, math.ceil(long_len / (STEP * 0.8)) + 1)
    a = _resample_count(forward, count)
    b = _resample_count(backward, count)
    centre = LineString([(c.x - ux * long_len / 2, c.y - uy * long_len / 2), (c.x + ux * long_len / 2, c.y + uy * long_len / 2)])
    return Satin((a, b), centre, round(short_len, 3), False)


def band_satin(centre: Polygon, width: float) -> Satin:
    """Closed satin of constant width centred on a polygon's boundary (the patch border)."""
    ring = orient(centre, 1.0).exterior
    coords = list(ring.coords)[:-1]
    start = max(range(len(coords)), key=lambda i: (coords[i][1], -coords[i][0]))
    line = LineString(coords[start:] + coords[:start] + [coords[start]])
    pts = _resample(line, closed=True)
    a, b = _offset_rails(pts, np.full(len(pts), width / 2), closed=True)
    # Closed rails: repeat the first node so both rails end where they began.
    return Satin((a + [a[0]], b + [b[0]]), LineString(pts + [pts[0]]), width, True)


def rect_axes(shape: BaseGeometry) -> tuple[Point, float, float] | None:
    """(long-axis unit vector, long side, short side) of the minimum rotated rectangle."""
    with np.errstate(all="ignore"):  # GEOS warns on collinear hull edges; the result is still usable
        rect = shape.minimum_rotated_rectangle
    if not isinstance(rect, Polygon):
        return None
    coords = list(rect.exterior.coords)[:4]
    if len(coords) < 4 or any(math.isnan(v) for xy in coords for v in xy):
        return None
    edges = [(coords[i], coords[(i + 1) % 4]) for i in range(2)]
    lengths = [math.dist(p, q) for p, q in edges]
    i = 0 if lengths[0] >= lengths[1] else 1
    if lengths[i] == 0:
        return None
    (ax, ay), (bx, by) = edges[i]
    return ((bx - ax) / lengths[i], (by - ay) / lengths[i]), lengths[i], lengths[1 - i]


# --- helpers ----------------------------------------------------------------------------------


def _resample(line: LineString, closed: bool) -> list[Point]:
    length = line.length
    if length == 0:
        return []
    n = max(2, math.ceil(length / STEP))
    count = n if closed else n + 1
    return [_rounded(line.interpolate(i * length / n)) for i in range(count)]


def _resample_count(chain: list[Point], count: int) -> list[Point]:
    line = LineString(chain)
    return [_rounded(line.interpolate(i / (count - 1), normalized=True)) for i in range(count)]


def _rounded(p) -> Point:
    return (round(p.x, 4), round(p.y, 4))


def _arc_lengths(pts: list[Point]) -> np.ndarray:
    arr = np.asarray(pts)
    return np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(arr, axis=0).T))])


def _moving_average(values: np.ndarray, window: int, closed: bool) -> np.ndarray:
    if len(values) < window:
        return values.copy()
    pad = window // 2
    padded = np.concatenate([values[-pad:], values, values[:pad]]) if closed else np.pad(values, pad, mode="edge")
    return np.convolve(padded, np.ones(window) / window, mode="valid")


def _extend(pts: list[Point], half: np.ndarray, reach: float, at_start: bool) -> tuple[list[Point], np.ndarray]:
    if reach < STEP / 2 or len(pts) < 2:
        return pts, half
    end, prev = (pts[0], pts[1]) if at_start else (pts[-1], pts[-2])
    dx, dy = end[0] - prev[0], end[1] - prev[1]
    d = math.hypot(dx, dy) or 1.0
    steps = max(1, round(reach / STEP))
    extra = [(round(end[0] + dx / d * reach * k / steps, 4), round(end[1] + dy / d * reach * k / steps, 4)) for k in range(1, steps + 1)]
    h = np.full(steps, half[0] if at_start else half[-1])
    if at_start:
        return extra[::-1] + pts, np.concatenate([h, half])
    return pts + extra, np.concatenate([half, h])


def _offset_rails(pts: list[Point], half: np.ndarray, closed: bool) -> tuple[list[Point], list[Point]]:
    arr = np.asarray(pts, dtype=float)
    n = len(arr)
    if closed:
        nxt, prv = np.roll(arr, -1, axis=0), np.roll(arr, 1, axis=0)
    else:
        nxt = np.vstack([arr[1:], arr[-1:]])
        prv = np.vstack([arr[:1], arr[:-1]])
    tangent = nxt - prv
    tangent /= np.maximum(np.hypot(*tangent.T), 1e-9)[:, None]
    normal = np.stack([-tangent[:, 1], tangent[:, 0]], axis=1)

    # Curvature (positive = turning towards +normal). The rail on the inside of a bend is pulled in so it
    # never folds back on itself, which would knot the satin.
    theta = np.arctan2(tangent[:, 1], tangent[:, 0])
    if closed:
        dtheta = np.roll(theta, -1) - np.roll(theta, 1)
        ds = np.hypot(*(nxt - prv).T)
    else:
        dtheta = np.concatenate([[0.0], theta[2:] - theta[:-2], [0.0]])
        ds = np.concatenate([[1.0], np.hypot(*(arr[2:] - arr[:-2]).T), [1.0]])
    dtheta = (dtheta + np.pi) % (2 * np.pi) - np.pi
    kappa = _moving_average(dtheta / np.maximum(ds, 1e-9), 3, closed) if n >= 3 else np.zeros(n)
    limit = 0.8 / np.maximum(np.abs(kappa), 1e-9)
    left = np.where(kappa > 0, np.minimum(half, limit), half)
    right = np.where(kappa < 0, np.minimum(half, limit), half)
    left, right = np.maximum(left, MIN_HALF), np.maximum(right, MIN_HALF)

    a = arr + normal * left[:, None]
    b = arr - normal * right[:, None]
    return [(round(x, 4), round(y, 4)) for x, y in a], [(round(x, 4), round(y, 4)) for x, y in b]
