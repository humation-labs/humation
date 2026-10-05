"""Step 4: split every thread region into tatami fills and satin lines."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import distance_transform_edt
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from skimage.morphology import skeletonize

from .colors import ReducedDrawing
from .config import Config, PaletteColor
from .geometry import clean, polygons, union
from .report import Warning

RES = 20.0  # raster resolution for centerlines, px per mm

Pixel = tuple[int, int]  # (row, col)


@dataclass
class FillPart:
    thread: PaletteColor
    geometry: Polygon
    angle: float = 0.0


@dataclass
class SatinLine:
    thread: PaletteColor
    line: LineString
    width: float
    closed: bool = False


@dataclass
class Attributed:
    fills: list[FillPart]
    satins: list[SatinLine]
    dropped: int
    warnings: list[Warning] = field(default_factory=list)


def assign_attributes(reduced: ReducedDrawing, cfg: Config, line_art_hex: str | None = None) -> Attributed:
    """line_art_hex: the outline thread. Its thin parts are always satin lines; other threads' thin parts
    that touch their own fill are narrow bits of that fill, and stay tatami instead of a web of satins."""
    t = cfg.thin.threshold_mm
    silhouette = union(r.geometry for r in reduced.regions)
    fills: list[FillPart] = []
    satins: list[SatinLine] = []
    dropped = 0
    widened = 0

    for region in reduced.regions:
        geom = region.geometry
        thick = clean(geom.buffer(-t / 2, quad_segs=8).buffer(t / 2, quad_segs=8).intersection(geom))
        thin_parts: list[Polygon] = []
        absorbed: list[Polygon] = []
        for part in polygons(clean(geom.difference(thick))):
            # Corner leftovers of an opening stay with the fill they belong to.
            touching = not thick.is_empty and part.distance(thick) < 0.05
            if touching and (part.area < t * t or region.thread.hex != line_art_hex):
                absorbed.append(part)
            elif _too_small(part, cfg.detail.min_mm):
                dropped += 1
            else:
                thin_parts.append(part)
        for part in polygons(union([thick, *absorbed])):
            if _too_small(part, cfg.detail.min_mm):
                dropped += 1
            else:
                fills.append(FillPart(region.thread, part))
        for line, half_width, closed in centerlines(thin_parts, cfg):
            if 2 * half_width < cfg.detail.min_mm:
                widened += 1
            satins.append(SatinLine(region.thread, line, satin_width(half_width, cfg), closed))

    _assign_angles(fills, cfg.fill.angles, t)
    if cfg.fill.overlap_mm > 0:
        for fill in fills:
            grown = clean(fill.geometry.buffer(cfg.fill.overlap_mm, quad_segs=8).intersection(silhouette))
            fill.geometry = polygons(grown)[0] if polygons(grown) else fill.geometry

    warnings = []
    if dropped:
        warnings.append(Warning("detail_dropped", f"{dropped} part(s) smaller than {cfg.detail.min_mm} mm were dropped"))
    if widened:
        warnings.append(Warning("detail_widened", f"{widened} line(s) narrower than {cfg.detail.min_mm} mm were widened to satin ≥ {cfg.thin.satin_min_mm} mm"))
    return Attributed(fills, satins, dropped, warnings)


def _too_small(part: Polygon, min_mm: float) -> bool:
    minx, miny, maxx, maxy = part.bounds
    return maxx - minx < min_mm and maxy - miny < min_mm


def _assign_angles(fills: list[FillPart], angles: tuple[float, ...], reach: float) -> None:
    """Greedy colouring of the 'touching' graph so neighbouring fills run in different directions."""
    order = sorted(range(len(fills)), key=lambda i: (-round(fills[i].geometry.area, 4), fills[i].geometry.bounds))
    done: list[int] = []
    for i in order:
        near = [fills[j].angle for j in done if fills[i].geometry.distance(fills[j].geometry) <= reach]
        free = [a for a in angles if a not in near]
        fills[i].angle = free[0] if free else min(angles, key=lambda a: (near.count(a), angles.index(a)))
        done.append(i)


# --- centerlines -------------------------------------------------------------------------------


def centerlines(parts: list[Polygon], cfg: Config) -> list[tuple[LineString, float, bool]]:
    """(centerline, median half-width mm, closed) for thin polygons."""
    if not parts:
        return []
    out: list[tuple[LineString, float, bool]] = []
    lines: list[Polygon] = []
    for part in parts:
        dot = _dot(part, cfg)
        if dot is not None:
            out.append(dot)
        else:
            lines.append(part)
    if not lines:
        return out

    geom = union(lines)
    minx, miny, maxx, maxy = geom.bounds
    ox, oy = minx - 0.5, miny - 0.5
    w = int(math.ceil((maxx - ox + 0.5) * RES))
    h = int(math.ceil((maxy - oy + 0.5) * RES))
    mask = _rasterize(geom, ox, oy, w, h)
    dist = distance_transform_edt(mask) / RES
    skel = skeletonize(mask.astype(bool))
    pixels = {(int(r), int(c)) for r, c in zip(*np.nonzero(skel))}
    pixels = _prune(pixels, cfg.thin.spur_min_mm * RES)

    for path, closed in _chain(_trace(pixels)):
        pts = [(ox + (c + 0.5) / RES, oy + (r + 0.5) / RES) for r, c in path]
        half = float(np.median([dist[r, c] for r, c in path]))
        if closed:
            pts.append(pts[0])
        line = LineString(pts).simplify(cfg.thin.simplify_mm, preserve_topology=False)
        if len(set(line.coords)) < (3 if closed else 2) or line.length < cfg.detail.min_mm:
            continue
        # Ink/Stitch puts a satin rung on every node; near-collinear nodes on a short line make it hang.
        line = _smooth(line, closed).simplify(0.03, preserve_topology=False)
        if len(set(line.coords)) < (3 if closed else 2):
            continue
        width = satin_width(half, cfg)
        # Ink/Stitch hangs on satins shorter than they are wide and on loops too small for their width:
        # those become a straight bar at least one width long.
        if closed and _short_side(Polygon(line.coords)) < 2 * width:
            bar = _bar(line.buffer(half), cfg)
        elif not closed and line.length < 2 * cfg.thin.satin_max_mm:
            bar = _bar(line.buffer(max(half, 0.05)), cfg)
        else:
            out.append((_round_line(line), half, closed))
            continue
        if bar is not None:
            out.append(bar)
    return out


def satin_width(half_width: float, cfg: Config) -> float:
    return round(min(max(2 * half_width, cfg.thin.satin_min_mm), cfg.thin.satin_max_mm), 3)


def _dot(part: Polygon, cfg: Config) -> tuple[LineString, float, bool] | None:
    """Compact blobs (eyes, small marks) become a short satin along their long axis."""
    rect = _rect_axes(part)
    if rect is None or rect[1] > 2 * cfg.thin.satin_max_mm:
        return None
    return _bar(part, cfg)


def _bar(shape: BaseGeometry, cfg: Config) -> tuple[LineString, float, bool] | None:
    """Straight satin through the shape's long axis, never shorter than its own width."""
    rect = _rect_axes(shape)
    if rect is None:
        return None
    (ux, uy), long_len, short_len = rect
    half_len = max(long_len, satin_width(short_len / 2, cfg)) / 2
    c = shape.centroid
    line = LineString([(c.x - ux * half_len, c.y - uy * half_len), (c.x + ux * half_len, c.y + uy * half_len)])
    return _round_line(line), short_len / 2, False


def _rect_axes(shape: BaseGeometry) -> tuple[tuple[float, float], float, float] | None:
    """(long-axis unit vector, long side, short side) of the minimum rotated rectangle."""
    with np.errstate(all="ignore"):  # GEOS warns on collinear hull edges; the result is still usable
        rect = shape.minimum_rotated_rectangle
    if not isinstance(rect, Polygon):
        return None
    coords = list(rect.exterior.coords)[:4]
    if len(coords) < 4 or any(math.isnan(v) for xy in coords for v in xy):
        return None
    edges = [(coords[i], coords[(i + 1) % 4]) for i in range(2)]
    lengths = [math.dist(a, b) for a, b in edges]
    i = 0 if lengths[0] >= lengths[1] else 1
    if lengths[i] == 0:
        return None
    (ax, ay), (bx, by) = edges[i]
    return ((bx - ax) / lengths[i], (by - ay) / lengths[i]), lengths[i], lengths[1 - i]


def _short_side(shape: BaseGeometry) -> float:
    rect = _rect_axes(shape)
    return rect[2] if rect else 0.0


def _smooth(line: LineString, closed: bool, iterations: int = 2) -> LineString:
    """Chaikin corner cutting: turns the pixel-derived polyline into a fair curve for the satin."""
    # Densify first so a long straight run only loses its last ~0.4 mm at a real corner.
    pts = list(line.segmentize(1.0).coords)
    if closed:
        pts = pts[:-1]
    for _ in range(iterations):
        if len(pts) < 3:
            break
        pairs = zip(pts, pts[1:] + pts[:1]) if closed else zip(pts, pts[1:])
        cut = []
        for (x0, y0), (x1, y1) in pairs:
            cut += [(0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1), (0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1)]
        pts = cut if closed else [pts[0], *cut[1:-1], pts[-1]]
    return LineString([*pts, pts[0]] if closed else pts)


def _round_line(line: LineString) -> LineString:
    return LineString([(round(x, 4), round(y, 4)) for x, y in line.coords])


def _rasterize(geom: BaseGeometry, ox: float, oy: float, w: int, h: int) -> np.ndarray:
    image = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(image)

    def px(coords):
        return [((x - ox) * RES, (y - oy) * RES) for x, y in coords]

    for poly in polygons(geom):
        draw.polygon(px(poly.exterior.coords), fill=1)
        for hole in poly.interiors:
            draw.polygon(px(hole.coords), fill=0)
    return np.array(image, dtype=np.uint8)


def _neighbours(p: Pixel, pixels: set[Pixel]) -> list[Pixel]:
    r, c = p
    out = []
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == dc == 0:
                continue
            q = (r + dr, c + dc)
            if q not in pixels:
                continue
            # A diagonal step that can also be made through an orthogonal pixel is redundant.
            if dr and dc and ((r + dr, c) in pixels or (r, c + dc) in pixels):
                continue
            out.append(q)
    return out


@dataclass
class _Edge:
    a: int  # node id at path[0]
    b: int  # node id at path[-1] (== a for loops)
    path: list[Pixel]
    cycle: bool = False


def _trace(pixels: set[Pixel]) -> list[_Edge]:
    nbrs = {p: _neighbours(p, pixels) for p in pixels}
    node_of: dict[Pixel, int] = {}
    next_id = 0
    for p in sorted(pixels):
        if len(nbrs[p]) == 2 or p in node_of:
            continue
        # Flood adjacent junction pixels into one node.
        stack = [p]
        node_of[p] = next_id
        while stack:
            q = stack.pop()
            for n in nbrs[q]:
                if n not in node_of and len(nbrs[n]) > 2 and len(nbrs[q]) > 2:
                    node_of[n] = next_id
                    stack.append(n)
        next_id += 1

    edges: list[_Edge] = []
    used: set[tuple[Pixel, Pixel]] = set()
    seen: set[Pixel] = set(node_of)
    for start in sorted(node_of):
        for first in sorted(nbrs[start]):
            if (start, first) in used or node_of.get(first) == node_of[start]:
                continue
            path = [start, first]
            prev, cur = start, first
            while cur not in node_of:
                seen.add(cur)
                nxt = [n for n in nbrs[cur] if n != prev]
                if not nxt:
                    break
                prev, cur = cur, nxt[0]
                path.append(cur)
            used.add((start, first))
            used.add((path[-1], path[-2]))
            end_node = node_of.get(path[-1], node_of[start])
            edges.append(_Edge(node_of[start], end_node, path))

    for p in sorted(pixels - seen):
        if p in seen:
            continue
        path = [p]
        seen.add(p)
        prev, cur = p, nbrs[p][0]
        while cur != p and cur not in seen:
            seen.add(cur)
            path.append(cur)
            nxt = [n for n in nbrs[cur] if n != prev]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
        edges.append(_Edge(-1, -1, path, cycle=True))
    return edges


def _prune(pixels: set[Pixel], spur_px: float) -> set[Pixel]:
    """Remove short branches that end in a free endpoint and hang off a junction."""
    for _ in range(4):
        edges = _trace(pixels)
        degree: dict[int, int] = {}
        for e in edges:
            if not e.cycle:
                degree[e.a] = degree.get(e.a, 0) + 1
                degree[e.b] = degree.get(e.b, 0) + 1
        removed = False
        for e in edges:
            if e.cycle or _px_length(e.path) >= spur_px:
                continue
            da, db = degree.get(e.a, 0), degree.get(e.b, 0)
            if da == 1 and db >= 3:
                drop = e.path[:-1]
            elif db == 1 and da >= 3:
                drop = e.path[1:]
            else:
                continue
            pixels = pixels - set(drop)
            removed = True
        if not removed:
            break
    return pixels


def _px_length(path: list[Pixel]) -> float:
    return sum(math.dist(a, b) for a, b in zip(path, path[1:]))


def _chain(edges: list[_Edge]) -> list[tuple[list[Pixel], bool]]:
    """Join edges through junctions, always continuing along the straightest branch."""
    out: list[tuple[list[Pixel], bool]] = []
    at_node: dict[int, list[int]] = {}
    for i, e in enumerate(edges):
        if e.cycle:
            out.append((e.path, True))
            continue
        at_node.setdefault(e.a, []).append(i)
        if e.b != e.a:
            at_node.setdefault(e.b, []).append(i)
    used: set[int] = set()

    def oriented(i: int, from_node: int) -> list[Pixel]:
        e = edges[i]
        return e.path if e.a == from_node else e.path[::-1]

    def direction(path: list[Pixel], at_end: bool) -> tuple[float, float]:
        k = min(len(path) - 1, 8)
        a, b = (path[-1 - k], path[-1]) if at_end else (path[0], path[k])
        d = math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0
        return ((b[0] - a[0]) / d, (b[1] - a[1]) / d)

    # Endpoints first so chains run end to end, then whatever is left (loops through junctions).
    starts = sorted(at_node, key=lambda n: (len(at_node[n]) != 1, n))
    for node in starts:
        for i in sorted(at_node[node]):
            if i in used:
                continue
            used.add(i)
            path = list(oriented(i, node))
            end = edges[i].b if edges[i].a == node else edges[i].a
            while True:
                candidates = [j for j in at_node.get(end, []) if j not in used]
                if not candidates:
                    break
                d_in = direction(path, True)
                best = max(candidates, key=lambda j: (_dot2(d_in, direction(oriented(j, end), False)), -j))
                if _dot2(d_in, direction(oriented(best, end), False)) < 0.5:  # sharper than 60°
                    break
                used.add(best)
                nxt = oriented(best, end)
                path.extend(nxt[1:])
                end = edges[best].b if edges[best].a == end else edges[best].a
            closed = path[0] == path[-1] and len(path) > 3
            if len(path) >= 2:
                out.append((path[:-1] if closed else path, closed))
    return out


def _dot2(u: tuple[float, float], v: tuple[float, float]) -> float:
    return u[0] * v[0] + u[1] * v[1]
