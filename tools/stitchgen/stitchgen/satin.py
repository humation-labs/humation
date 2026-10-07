"""Two-rail satin columns that follow the artwork's own shapes.

Ink/Stitch pairs the nodes of two rails with the same node count as rungs, so every satin here is two
equally long point lists: the stitches run from rails[0][i] to rails[1][i]. Line widths therefore follow the
source line (tapering, round ends) instead of a constant stroke width.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace

import numpy as np
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient

from .geometry import polygons

STEP = 0.25  # mm between rungs
MIN_HALF = 0.08  # never let a rail touch the centre line, Ink/Stitch dislikes zero-width rungs
DOT_TIP_INSET = 0.20  # mm: a mark's first and last rows lie half a thread inside its tips
DOT_ROW = 0.18  # mm between a mark's chords
DOT_ROUND_ZONE = 0.45  # mm from a tip within which a mark's rows are shortened a little to round the end
DOT_ROUND_CUT = 0.08  # mm taken off each side of the tip row
FLAT_END_ZONE_MM = 0.6  # near an end cut flat at a junction, widths come from the inscribed radius

Point = tuple[float, float]


@dataclass
class Satin:
    rails: tuple[list[Point], list[Point]]
    centre: LineString  # used for sewing order and debug output
    width: float  # median width, mm
    closed: bool = False
    run: bool = False  # too fine for satin: sewn as a triple running stitch along the centre line instead
    dot: bool = False  # compact mark (eye, dash): its own dense, underlay-free recipe

    def reversed(self) -> Satin:
        return replace(self, rails=(self.rails[0][::-1], self.rails[1][::-1]), centre=LineString(list(self.centre.coords)[::-1]))


Sides = Callable[[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]


def line_satin(line: LineString, sides: Sides, closed: bool, min_half: float, max_half: float,
               caps: tuple[bool, bool] = (True, True), limit: Sides | None = None,
               edt: Callable[[np.ndarray], np.ndarray] | None = None) -> Satin | None:
    """Satin along a centre line. sides(points, normals) gives the distance from each point to the drawn edge on
    the left and right, so each rail follows its own edge (a line that bulges on one side keeps that bulge).
    caps: True = a free end, rounded like the brush stroke; False = an end at a junction, cut flat (the caller
    trims it to tuck under the line it meets). limit(points, normals, left, right) may cap the sides further."""
    pts = _resample(line, closed)
    if len(pts) < 2:
        return None
    arr = np.asarray(pts, dtype=float)
    normal = _normals(arr, closed)
    left, right = sides(arr, normal)
    if edt is not None and not closed and not all(caps):
        # Near an end cut flat at a junction the normal runs along the line being met, so the rays read that
        # line's length. There the inscribed radius, which has no direction, is the honest width.
        s0 = _arc_lengths(pts)
        near = np.zeros(len(pts), dtype=bool)
        if not caps[0]:
            near |= s0 < FLAT_END_ZONE_MM
        if not caps[1]:
            near |= (s0[-1] - s0) < FLAT_END_ZONE_MM
        r = edt(arr)
        left = np.where(near, np.minimum(left, r), left)
        right = np.where(near, np.minimum(right, r), right)
    # A short median only steadies the reading; real corners and bumps (about 1 mm) must survive it.
    left, right = _rolling_median(left, 3, closed), _rolling_median(right, 3, closed)
    short = np.maximum(2 * min_half - (left + right), 0) / 2
    left, right = np.minimum(left + short, max_half), np.minimum(right + short, max_half)
    if limit is not None:
        left, right = limit(arr, normal, left, right)

    if not closed:
        s = _arc_lengths(pts)
        for at_start, cap in ((True, caps[0]), (False, caps[1])):
            if not cap:
                continue
            # Round end: taper both sides like a circle whose radius is the line's half-width there.
            i = 0 if at_start else -1
            r = max((left[i] + right[i]) / 2, MIN_HALF)
            u = s if at_start else s[-1] - s
            zone = u < r
            f = np.sqrt(np.maximum(r * r - (r - u[zone]) ** 2, 0)) / r
            left[zone] *= f
            right[zone] *= f
    left, right = np.maximum(left, MIN_HALF), np.maximum(right, MIN_HALF)
    a, b = _offset_rails(arr, normal, left, right, closed)
    widths = np.sort(left + right)
    return Satin((a, b), LineString(pts + ([pts[0]] if closed else [])), round(float(np.median(widths)), 3), closed)


def dot_satin(shape: BaseGeometry, min_width: float) -> Satin | None:
    """Compact mark (eye, dash). Rails are the ends of chords cut straight across the mark's axis, so every
    stitch is a full row (no diagonal stitch at a zero-width tip) and the outline is followed exactly. A nearly
    round mark is always stitched horizontally, and every mark is sewn in the same direction (top to bottom,
    or left to right), so the two eyes of a face come out alike."""
    axes = pca_axes(shape)
    if axes is None:
        return None
    if axes[2] < min_width:
        shape = shape.buffer((min_width - axes[2]) / 2, quad_segs=8)
        axes = pca_axes(shape)
    parts = polygons(shape)
    if not parts or axes is None:
        return None
    poly = parts[0]
    (ux, uy), long_len, short_len = axes
    if long_len < 1.15 * short_len and long_len <= 5.0:
        ux, uy = 0.0, 1.0
    c = poly.centroid
    along = [(x - c.x) * ux + (y - c.y) * uy for x, y in poly.exterior.coords]
    lo, hi = min(along), max(along)
    # The first and last rows lie half a thread inside the tips, so their strands end on the drawn tip.
    inset = min(DOT_TIP_INSET, (hi - lo) / 4)
    count = max(5, math.ceil((hi - lo - 2 * inset) / DOT_ROW) + 1)
    a: list[Point] = []
    b: list[Point] = []
    reach = long_len + short_len
    for k in range(count):
        t = lo + inset + (hi - lo - 2 * inset) * k / (count - 1)
        mx, my = c.x + ux * t, c.y + uy * t
        chord = poly.intersection(LineString([(mx + uy * reach, my - ux * reach), (mx - uy * reach, my + ux * reach)]))
        if chord.geom_type != "LineString" or chord.is_empty:
            # Not convex across the axis (a crescent): fall back to splitting the contour at the tips.
            return strip_satin(poly, (c.x - ux * long_len, c.y - uy * long_len), (c.x + ux * long_len, c.y + uy * long_len), dot=True)
        p, q = chord.coords[0], chord.coords[-1]
        # Keep the left/right sense constant: the endpoint on the +perpendicular side goes on rail a.
        if (p[0] - mx) * uy - (p[1] - my) * ux < (q[0] - mx) * uy - (q[1] - my) * ux:
            p, q = q, p
        # Round the end rows off a little: a full-length chord at the tip squares the end.
        d = min(hi - t, t - lo)
        if d < DOT_ROUND_ZONE:
            cut = min(DOT_ROUND_CUT * (DOT_ROUND_ZONE - d) / max(DOT_ROUND_ZONE - inset, 1e-6), max((math.dist(p, q) - 0.3) / 2, 0.0))
            vx, vy = (q[0] - p[0]), (q[1] - p[1])
            n = math.hypot(vx, vy) or 1.0
            p = (p[0] + vx / n * cut, p[1] + vy / n * cut)
            q = (q[0] - vx / n * cut, q[1] - vy / n * cut)
        a.append((round(p[0], 4), round(p[1], 4)))
        b.append((round(q[0], 4), round(q[1], 4)))
    centre = LineString([(c.x + ux * lo, c.y + uy * lo), (c.x + ux * hi, c.y + uy * hi)])
    width = max(math.dist(p, q) for p, q in zip(a, b))
    return Satin((a, b), centre, round(width, 3), False, dot=True)


def mark_run(shape: BaseGeometry) -> Satin | None:
    """A hairline mark (narrower than a satin can be) as a triple run along its own axis."""
    axes = pca_axes(shape)
    parts = polygons(shape)
    if axes is None or not parts:
        return None
    (ux, uy), long_len, short_len = axes
    c = parts[0].centroid
    half = max(long_len / 2 - min(0.2, long_len / 4), 0.05)
    line = LineString([(c.x - ux * half, c.y - uy * half), (c.x + ux * half, c.y + uy * half)])
    return Satin(([], []), line, round(short_len, 3), False, run=True)


def pca_axes(shape: BaseGeometry) -> tuple[Point, float, float] | None:
    """(long-axis unit vector, extent along it, extent across) from the shape's second moments of area. Unlike
    the minimum rotated rectangle this is unique for an ellipse, so a level eye is stitched level. The axis
    sign is canonical: pointing down (or right for a level mark)."""
    parts = polygons(shape)
    if not parts:
        return None
    poly = parts[0]
    pts = np.asarray(poly.exterior.coords)
    c = poly.centroid
    x, y = pts[:, 0] - c.x, pts[:, 1] - c.y
    cross = x[:-1] * y[1:] - x[1:] * y[:-1]
    area = cross.sum() / 2
    if abs(area) < 1e-9:
        return None
    sxx = ((x[:-1] ** 2 + x[:-1] * x[1:] + x[1:] ** 2) * cross).sum() / 12
    syy = ((y[:-1] ** 2 + y[:-1] * y[1:] + y[1:] ** 2) * cross).sum() / 12
    sxy = ((x[:-1] * y[1:] + 2 * x[:-1] * y[:-1] + 2 * x[1:] * y[1:] + x[1:] * y[:-1]) * cross).sum() / 24
    sign = 1.0 if area > 0 else -1.0
    vals, vecs = np.linalg.eigh(np.array([[sxx * sign, sxy * sign], [sxy * sign, syy * sign]]))
    ux, uy = float(vecs[0, 1]), float(vecs[1, 1])  # eigenvector of the largest eigenvalue: the long axis
    if abs(ux) < 1e-9:
        ux = 0.0
    if abs(uy) < 1e-9:
        uy = 0.0
    if (abs(uy) >= abs(ux) and uy < 0) or (abs(uy) < abs(ux) and ux < 0):
        ux, uy = -ux, -uy
    along = x * ux + y * uy
    across = -x * uy + y * ux
    return (ux, uy), float(along.max() - along.min()), float(across.max() - across.min())


def strip_satin(shape: Polygon, end_a: Point, end_b: Point, dot: bool = False) -> Satin | None:
    """Satin whose rails are the shape's own contour, split at the boundary points nearest end_a and end_b.
    Fits any strip without branches (petal, leaf, stem, a strand of hair), however it curves."""
    ring = list(orient(shape, 1.0).exterior.segmentize(STEP).coords)[:-1]
    if len(ring) < 4:
        return None
    i0 = min(range(len(ring)), key=lambda i: (math.dist(ring[i], end_a), i))
    i1 = min(range(len(ring)), key=lambda i: (math.dist(ring[i], end_b), i))
    if i0 == i1:
        return None
    n = len(ring)
    forward = [ring[(i0 + k) % n] for k in range((i1 - i0) % n + 1)]
    backward = [ring[(i0 - k) % n] for k in range((i0 - i1) % n + 1)]
    length = max(LineString(forward).length, LineString(backward).length)
    count = max(5, math.ceil(length / (STEP * 0.8)) + 1)
    a, b = _resample_count(forward, count), _resample_count(backward, count)
    centre = LineString([((p[0] + q[0]) / 2, (p[1] + q[1]) / 2) for p, q in zip(a, b)])
    widths = sorted(math.dist(p, q) for p, q in zip(a, b))
    # The rails meet at the tips, so the median rung under-reads the width; the 90th percentile is stable.
    return Satin((a, b), centre, round(widths[int(len(widths) * 0.9)], 3), False, dot=dot)


def band_satin(centre: Polygon, width: float) -> Satin:
    """Closed satin of constant width centred on a polygon's boundary (the patch border)."""
    ring = orient(centre, 1.0).exterior
    coords = list(ring.coords)[:-1]
    start = max(range(len(coords)), key=lambda i: (coords[i][1], -coords[i][0]))
    line = LineString(coords[start:] + coords[:start] + [coords[start]])
    pts = _resample(line, closed=True)
    arr = np.asarray(pts, dtype=float)
    half = np.full(len(pts), width / 2)
    a, b = _offset_rails(arr, _normals(arr, True), half, half, closed=True)
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


def _rolling_median(values: np.ndarray, window: int, closed: bool) -> np.ndarray:
    if len(values) < window:
        return values.copy()
    pad = window // 2
    padded = np.concatenate([values[-pad:], values, values[:pad]]) if closed else np.pad(values, pad, mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(padded, window), axis=1)


def _normals(arr: np.ndarray, closed: bool) -> np.ndarray:
    if closed:
        nxt, prv = np.roll(arr, -1, axis=0), np.roll(arr, 1, axis=0)
    else:
        nxt = np.vstack([arr[1:], arr[-1:]])
        prv = np.vstack([arr[:1], arr[:-1]])
    tangent = nxt - prv
    tangent /= np.maximum(np.hypot(*tangent.T), 1e-9)[:, None]
    return np.stack([-tangent[:, 1], tangent[:, 0]], axis=1)


def _offset_rails(arr: np.ndarray, normal: np.ndarray, left: np.ndarray, right: np.ndarray, closed: bool) -> tuple[list[Point], list[Point]]:
    n = len(arr)
    if closed:
        nxt, prv = np.roll(arr, -1, axis=0), np.roll(arr, 1, axis=0)
    else:
        nxt = np.vstack([arr[1:], arr[-1:]])
        prv = np.vstack([arr[:1], arr[:-1]])
    # Curvature (positive = turning towards +normal). The rail on the inside of a bend is pulled in so it
    # never folds back on itself, which would knot the satin.
    tangent = np.stack([normal[:, 1], -normal[:, 0]], axis=1)
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
    left = np.maximum(np.where(kappa > 0, np.minimum(left, limit), left), MIN_HALF)
    right = np.maximum(np.where(kappa < 0, np.minimum(right, limit), right), MIN_HALF)

    a = arr + normal * left[:, None]
    b = arr - normal * right[:, None]
    return [(round(x, 4), round(y, 4)) for x, y in a], [(round(x, 4), round(y, 4)) for x, y in b]
