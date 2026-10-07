"""Step 4: split every thread region into tatami fills and satin lines."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import distance_transform_edt
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polylabel, substring
from shapely.geometry.base import BaseGeometry
from skimage.morphology import skeletonize

from .colors import ColorRegion, ReducedDrawing
from .normalize import LINE_ROLE
from .config import Config, PaletteColor
from .geometry import clean, polygons, union
from .flow import guide_line
from .report import Warning
from .satin import Satin, dot_satin, line_satin, mark_run, rect_axes, strip_satin

RES = 20.0  # raster resolution for centerlines, px per mm
JUNCTION_TUCK_MM = 0.3  # a line ending at a junction reaches this far under the line it meets
CROP_OVERRUN_MM = 0.6  # a line cut by the crop is carried past it, then its rails are clipped flush
MIN_SIDE_MM = 0.25  # a rail pulled in to keep a gap never comes closer than this to the centre line

Pixel = tuple[int, int]  # (row, col)


@dataclass
class FillPart:
    thread: PaletteColor
    geometry: Polygon
    angle: float = 0.0
    role: str | None = None
    guide: LineString | None = None  # rows follow this curve (guided fill); None = straight rows at angle
    guide_strategy: int = 0
    flow: str | None = None
    layer: str = "fill"  # fill: sewn first; line: Humation line art, sewn last on top


@dataclass
class SatinLine:
    thread: PaletteColor
    satin: Satin
    layer: str = "fill"


@dataclass
class Attributed:
    fills: list[FillPart]
    satins: list[SatinLine]
    dropped: int
    warnings: list[Warning] = field(default_factory=list)


def assign_attributes(reduced: ReducedDrawing, cfg: Config, canvas: BaseGeometry | None = None,
                      crop: BaseGeometry | None = None, first: str | None = None) -> Attributed:
    """Fill layer (reduced.regions) and line layer (reduced.line_art, sewn last on top).
    canvas: the patch silhouette; fills never grow beyond it. crop: the artwork's crop rectangle.
    first: the thread sewn first (placement), so the underlap follows the real sewing order."""
    silhouette = canvas if canvas is not None else union(r.geometry for r in [*reduced.regions, *reduced.line_art])
    fills: list[FillPart] = []
    satins: list[SatinLine] = []
    dropped = widened = 0
    for layer, regions in (("fill", reduced.regions), ("line", reduced.line_art)):
        for region in regions:
            f, s, d, w = _region_stitches(region, cfg, layer, crop)
            fills += f
            satins += s
            dropped += d
            widened += w

    fills = [*_split_by_role([f for f in fills if f.layer == "fill"], reduced.roles), *[f for f in fills if f.layer != "fill"]]
    _assign_angles(fills, cfg.fill.angles, cfg.thin.threshold_mm)
    if cfg.fill.overlap_mm > 0:
        _grow_under_later(fills, satins, cfg.fill.overlap_mm, silhouette, first)
    _assign_flows([f for f in fills if f.layer == "fill"], reduced.roles, cfg.fill.flow)

    warnings = []
    if dropped:
        warnings.append(Warning("detail_dropped", f"{dropped} part(s) smaller than {cfg.detail.min_mm} mm were dropped"))
    if widened:
        warnings.append(Warning("detail_widened", f"{widened} line(s) narrower than {cfg.detail.min_mm} mm were widened to satin ≥ {cfg.thin.satin_min_mm} mm"))
    return Attributed(fills, satins, dropped, warnings)


def _split_by_role(fills: list[FillPart], roles: dict[str, BaseGeometry], merge_below: float = 2.0) -> list[FillPart]:
    """One thread can carry several motifs (White skin next to a white tee). Each gets its own fill, so the face
    is laid with the skin flow and the tee with the cloth flow. Crumbs under merge_below mm² stay with the
    biggest piece."""
    motifs = [(r, g) for r, g in roles.items() if r != LINE_ROLE]
    out: list[FillPart] = []
    for fill in fills:
        pieces: list[tuple[str | None, Polygon]] = []
        rest: BaseGeometry = fill.geometry
        for role, area in motifs:
            for p in polygons(clean(fill.geometry.intersection(area))):
                pieces.append((role, p))
            rest = rest.difference(area)
        pieces += [(None, p) for p in polygons(clean(rest))]
        big = [(r, p) for r, p in pieces if p.area >= merge_below]
        if len(big) <= 1:
            out.append(fill)
            continue
        crumbs = [p for r, p in pieces if p.area < merge_below]
        groups = [[role, p] for role, p in sorted(big, key=lambda rp: -rp[1].area)]
        while crumbs:  # nearest crumb first, so a chain of crumbs grows outward from the piece it hangs on
            crumb = min(crumbs, key=lambda c: min(c.distance(g[1]) for g in groups))
            group = min(groups, key=lambda g: crumb.distance(g[1]))
            group[1] = union([group[1], crumb])
            crumbs.remove(crumb)
        for role, geometry in groups:
            for q in polygons(geometry):
                out.append(FillPart(fill.thread, q, role=role, layer=fill.layer))
    return out


def _grow_under_later(fills: list[FillPart], satins: list[SatinLine], overlap: float, silhouette: BaseGeometry,
                      first: str | None = None) -> None:
    """Underlap the way a digitiser does it: a fill reaches overlap mm under whatever of the fill layer is sewn
    after it (later fills, and the narrow satins and marks of the fill layer), which covers the seam. Against
    things sewn before it and against bare felt it keeps its drawn edge, so no thread of the wrong colour sits
    on top of a seam and nothing pokes out past the artwork. Uses the exact sewing order of order.py."""
    from .order import sewing_sequence  # local import: order imports this module

    sequence = sewing_sequence(fills, satins, first)
    later: BaseGeometry = Polygon()
    grown_by_id: dict[int, BaseGeometry] = {}
    for kind, obj in reversed(sequence):
        if kind == "fill":
            shape = obj.geometry
            grown = clean(union([shape, shape.buffer(overlap, quad_segs=8).intersection(later).intersection(silhouette)]))
            grown_by_id[id(obj)] = polygons(grown)[0] if polygons(grown) else shape
            later = union([later, shape])
        elif obj.layer == "fill":
            later = union([later, satin_footprint(obj.satin)])
    for fill in fills:
        fill.geometry = grown_by_id.get(id(fill), fill.geometry)


def _region_stitches(region: ColorRegion, cfg: Config, layer: str, crop: BaseGeometry | None = None,
                     ) -> tuple[list[FillPart], list[SatinLine], int, int]:
    """Tatami for wide parts, satin for narrow ones. In the line layer every thin part is a line; in the fill
    layer thin bits touching their own fill are narrow ends of that fill and stay with it."""
    t = cfg.thin.threshold_mm
    geom = region.geometry
    thick = clean(geom.buffer(-t / 2, quad_segs=8).buffer(t / 2, quad_segs=8).intersection(geom))
    fills: list[FillPart] = []
    satins: list[SatinLine] = []
    dropped = widened = 0
    thin_parts: list[Polygon] = []
    absorbed: list[Polygon] = []
    for part in polygons(clean(geom.difference(thick))):
        # Corner leftovers of an opening stay with the fill they belong to.
        touching = not thick.is_empty and part.distance(thick) < 0.05
        if touching and (part.area < t * t or layer == "fill"):
            absorbed.append(part)
        elif _too_small(part, cfg.detail.min_mm):
            dropped += 1
        else:
            thin_parts.append(part)
    for part in polygons(union([thick, *absorbed])):
        if _too_small(part, cfg.detail.min_mm):
            dropped += 1
            continue
        wide, narrow = _split_narrow(part, cfg.fill.satin_max_width_mm)
        fills.extend(FillPart(region.thread, p, layer=layer) for p in wide)
        for piece in narrow:
            satin = _narrow_satin(piece, cfg)
            if satin is None:
                fills.append(FillPart(region.thread, piece, layer=layer))  # no clean satin fits: keep it tatami
            else:
                satins.append(SatinLine(region.thread, satin, layer))
    if layer == "line":
        found = centerlines(thin_parts, cfg, keep_apart=True, crop=crop)
    else:
        # A thin colour patch is sewn as one satin between its own sides when that fits, as skeleton satins
        # when they really cover it, and as tatami otherwise, never as a token mark that leaves it half bare.
        found = []
        for part in thin_parts:
            contour = _narrow_satin(part, cfg)
            if contour is not None:
                found.append(contour)
                continue
            lines = centerlines([part], cfg, crop=crop)
            cover = union(satin_footprint(x) for x in lines).intersection(part).area if lines else 0.0
            if cover >= 0.9 * part.area:
                found += lines
            else:
                fills.append(FillPart(region.thread, part, layer=layer))
    for satin in found:
        if satin.width < cfg.detail.min_mm and not satin.run:
            widened += 1
        satins.append(SatinLine(region.thread, satin, layer))
    return fills, satins, dropped, widened


def _assign_flows(fills: list[FillPart], roles: dict[str, BaseGeometry], flows: dict[str, str]) -> None:
    """Give each fill its motif's guide line. Fills split by role already know their motif; others take the
    Humation slot covering most of them."""
    for fill in fills:
        best = fill.role
        if best is None:
            best_area = 0.0
            for role, area in roles.items():
                overlap = fill.geometry.intersection(area).area
                if overlap > best_area:
                    best, best_area = role, overlap
            if best is None or best_area < 0.5 * fill.geometry.area:
                continue
        fill.role = best
        guide = guide_line(flows[best], fill.geometry, roles[best].bounds) if best in flows and best in roles else None
        if guide is not None:
            fill.guide, fill.guide_strategy = guide
            fill.flow = flows[best]


def satin_footprint(satin: Satin) -> BaseGeometry:
    """Area a satin covers (its rails' outline; a run covers a thin line)."""
    if satin.run or not satin.rails[0]:
        return satin.centre.buffer(0.2)
    return Polygon([*satin.rails[0], *satin.rails[1][::-1]]).buffer(0)


def _split_narrow(part: Polygon, max_width: float) -> tuple[list[Polygon], list[Polygon]]:
    """Digitising rule of thumb: tatami for wide areas, satin for anything narrower than max_width.
    Returns (wide parts for tatami, narrow parts for satin)."""
    r = max_width / 2
    wide = clean(part.buffer(-r, quad_segs=8).buffer(r, quad_segs=8).intersection(part))
    if wide.is_empty:
        return [], [part]
    narrow, absorbed = [], []
    for piece in polygons(clean(part.difference(wide))):
        # Rounded-off corners and tapering edges of the wide area are not shapes of their own: they meet it
        # along a long seam (or are tiny) and stay tatami. Petals, stems and strands meet it at a short neck.
        seam = piece.boundary.intersection(wide.buffer(0.05)).length
        if piece.area < r * r or seam > max_width or _short(piece, max_width) and piece.distance(wide) < 0.05:
            absorbed.append(piece)
        else:
            narrow.append(piece)
    return polygons(union([wide, *absorbed])), narrow


def _short(piece: Polygon, max_width: float) -> bool:
    axes = rect_axes(piece)
    return axes is None or axes[1] < max_width


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


def centerlines(parts: list[Polygon], cfg: Config, min_width: float | None = None, max_width: float | None = None,
                width_from: BaseGeometry | None = None, keep_apart: bool = False,
                crop: BaseGeometry | None = None) -> list[Satin]:
    """Satins for thin polygons: compact marks keep their own contour, lines follow their skeleton.
    width_from: measure the satin width against this larger shape (a narrow part cut off a fill keeps its real
    width where it joins the fill, instead of pinching to a point at the cut).
    keep_apart: thin each line on the side that faces another stroke closer than thin.min_gap_mm, so strokes
    drawn almost touching (a double collar) stay two strokes.
    crop: the artwork's crop rectangle; a line cut by it ends flat on it instead of in a rounded brush tip."""
    if not parts:
        return []
    min_width = cfg.thin.satin_min_mm if min_width is None else min_width
    max_width = cfg.thin.satin_max_mm if max_width is None else max_width
    min_half, max_half = min_width / 2, max_width / 2
    out: list[Satin] = []
    lines: list[Polygon] = []
    for part in parts:
        axes = rect_axes(part)
        if axes is not None and axes[1] <= 2 * max_width:
            if width_from is None and _inscribed_width(part) < cfg.thin.running_max_mm:
                mark = mark_run(part)  # a hairline mark: a fine triple run, not a satin blown up to 1 mm
            else:
                mark = dot_satin(part, min_width)
            if mark is not None:
                out.append(mark)
                continue
        if width_from is not None and not part.interiors:
            strip = _unbranched_strip(part, cfg)
            if strip is not None:
                out.append(strip)
                continue
        lines.append(part)
    if not lines:
        return out

    geom = union(lines)
    measure = union([geom, width_from]) if width_from is not None else geom
    minx, miny, maxx, maxy = measure.bounds
    ox, oy = minx - 0.5, miny - 0.5
    w = int(math.ceil((maxx - ox + 0.5) * RES))
    h = int(math.ceil((maxy - oy + 0.5) * RES))
    mask = _rasterize(geom, ox, oy, w, h)
    measure_mask = _rasterize(measure, ox, oy, w, h) if width_from is not None else mask
    dist = distance_transform_edt(measure_mask) / RES
    skel = skeletonize(mask.astype(bool))
    pixels = {(int(r), int(c)) for r, c in zip(*np.nonzero(skel))}
    pixels = _prune(pixels, cfg.thin.spur_min_mm * RES, dist * RES, cfg.thin.spur_reach_mm * RES)
    sides = _side_reader(measure_mask, ox, oy, max_half)
    limit = _gap_keeper(mask, ox, oy, cfg) if keep_apart else None
    crop_line = crop.boundary if crop is not None else None

    def edt(arr: np.ndarray) -> np.ndarray:
        cols = np.clip(np.floor((arr[:, 0] - ox) * RES).astype(int), 0, w - 1)
        rows = np.clip(np.floor((arr[:, 1] - oy) * RES).astype(int), 0, h - 1)
        return dist[rows, cols]

    chains = _chain(_trace(pixels))
    # Where a sewn line actually passes: an end may tuck under a junction only if one does.
    passing: dict[Pixel, int] = {}
    for k, (path, closed, _, _) in enumerate(chains):
        if closed or _px_length(path) / RES >= cfg.detail.min_mm:
            for px in (path if closed else path[4:-4]):
                passing[px] = k

    def covered_by_other(px: Pixel, k: int) -> bool:
        r, c = px
        return any(passing.get((r + dr, c + dc), k) != k for dr in (-2, -1, 0, 1, 2) for dc in (-2, -1, 0, 1, 2))

    for k, (path, closed, free_start, free_end) in enumerate(chains):
        pts = [(ox + (c + 0.5) / RES, oy + (r + 0.5) / RES) for r, c in path]
        if closed:
            pts.append(pts[0])
        line = LineString(pts).simplify(cfg.thin.simplify_mm, preserve_topology=False)
        if len(set(line.coords)) < (3 if closed else 2) or line.length < cfg.detail.min_mm:
            continue
        line = _smooth(line, closed)
        # Width is read on the skeleton itself: the smoothed curve cuts corners and would under-measure.
        median_half = float(np.median([dist[r, c] for r, c in path]))
        if 2 * median_half < cfg.thin.running_max_mm and width_from is None:
            # Hairline details (a mouth, a whisker) read better as a fine triple run than a satin widened to 1 mm.
            centre = line.simplify(0.03, preserve_topology=False)
            out.append(Satin(([], []), LineString([*centre.coords]), round(2 * median_half, 3), closed, run=True))
            continue
        width = 2 * min(max(median_half, min_half), max_half)
        caps = (True, True)
        if not closed:
            ends = []
            for px, free, xy in ((path[0], free_start, pts[0]), (path[-1], free_end, pts[-1])):
                radius = float(dist[px])
                if crop_line is not None and crop_line.distance(Point(xy)) <= radius + 0.2:
                    ends.append(("crop", radius))  # cut by the crop: run on to it and end flat
                elif free or not covered_by_other(px, k):
                    ends.append(("tip", radius))  # a brush tip (or a corner no other line covers): round
                else:
                    ends.append(("tuck", radius))  # tucks just under the line passing through the junction
            start_cut = max(ends[0][1] - JUNCTION_TUCK_MM, 0.0) if ends[0][0] == "tuck" else 0.0
            end_cut = max(ends[1][1] - JUNCTION_TUCK_MM, 0.0) if ends[1][0] == "tuck" else 0.0
            if line.length - start_cut - end_cut < cfg.detail.min_mm / 2:
                continue
            line = substring(line, start_cut, line.length - end_cut)
            reach = [r + (CROP_OVERRUN_MM if kind == "crop" else 0.0) if kind != "tuck" else 0.0 for kind, r in ends]
            line = _extend_line(line, reach[0], reach[1])
            caps = (ends[0][0] == "tip", ends[1][0] == "tip")
        # Loops too small to have an inside, and stubs barely longer than wide, are stitched as one mark.
        if closed and Polygon(line.coords).buffer(-width / 2).is_empty:
            satin = dot_satin(Polygon(line.coords).buffer(median_half), min_width)
        elif not closed and line.length < 1.5 * width and all(caps):
            satin = dot_satin(line.buffer(median_half), min_width)
        else:
            satin = line_satin(line, sides, closed, min_half, max_half, caps=caps, limit=limit, edt=edt)
            if satin is not None and crop is not None and not closed and any(kind == "crop" for kind, _ in ends):
                satin = _clip_to_crop(satin, crop)
        if satin is not None:
            out.append(satin)
    return out


def _clip_to_crop(satin: Satin, crop: BaseGeometry) -> Satin:
    """A line cut by the crop ends flush on the crop line: rail points beyond it are pulled back onto it."""
    edge = crop.boundary

    def inside(pt: tuple[float, float]) -> tuple[float, float]:
        p = Point(pt)
        if crop.contains(p):
            return pt
        q = edge.interpolate(edge.project(p))
        return (round(q.x, 4), round(q.y, 4))

    a, b = satin.rails
    return replace(satin, rails=([inside(p) for p in a], [inside(p) for p in b]))


def _inscribed_width(part: Polygon) -> float:
    """Diameter of the largest circle inside the part (its true stroke width, unlike a bounding box)."""
    centre = polylabel(part, tolerance=0.01)
    return 2 * part.exterior.distance(centre) if part.contains(centre) else 0.0


def _extend_line(line: LineString, start: float, end: float) -> LineString:
    """Carry an open line straight on past either end (to reach the drawn tip of a round stroke end)."""
    coords = list(line.coords)
    if start > 0 and len(coords) >= 2:
        (x0, y0), (x1, y1) = coords[0], coords[1]
        d = math.hypot(x0 - x1, y0 - y1) or 1.0
        coords.insert(0, (x0 + (x0 - x1) / d * start, y0 + (y0 - y1) / d * start))
    if end > 0 and len(coords) >= 2:
        (x0, y0), (x1, y1) = coords[-1], coords[-2]
        d = math.hypot(x0 - x1, y0 - y1) or 1.0
        coords.append((x0 + (x0 - x1) / d * end, y0 + (y0 - y1) / d * end))
    return LineString(coords)


def _side_reader(mask: np.ndarray, ox: float, oy: float, max_half: float):
    """sides(points, normals): distance from each point to the drawn edge along +normal and -normal, read off
    the raster. A ray that never leaves the stroke runs along a crossing line, not across this one: it is no
    reading. Points without a reading (and the round-end extension outside the stroke) are interpolated from
    their neighbours."""
    h, w = mask.shape
    steps = np.arange(0.0, max_half + 0.35, 0.025)

    def sides(arr: np.ndarray, normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        out = []
        saturated = np.zeros(len(arr), dtype=bool)
        for sign in (1.0, -1.0):
            pos = arr[:, None, :] + sign * normal[:, None, :] * steps[None, :, None]
            cols = np.floor((pos[..., 0] - ox) * RES).astype(int)
            rows = np.floor((pos[..., 1] - oy) * RES).astype(int)
            ok = (rows >= 0) & (rows < h) & (cols >= 0) & (cols < w)
            inside = np.zeros(ok.shape, dtype=bool)
            inside[ok] = mask[rows[ok], cols[ok]] > 0
            full = inside.all(axis=1)
            saturated |= full
            out.append(steps[np.where(full, len(steps) - 1, np.argmax(~inside, axis=1))])
        left, right = out
        valid = ((left + right) > 0.05) & ~saturated
        if valid.any() and not valid.all():
            idx = np.arange(len(valid))
            left = np.interp(idx, idx[valid], left[valid])
            right = np.interp(idx, idx[valid], right[valid])
        elif not valid.any():
            left = right = np.full(len(arr), max_half / 2)
        return np.minimum(left, max_half), np.minimum(right, max_half)

    return sides


def _gap_keeper(mask: np.ndarray, ox: float, oy: float, cfg: Config):
    """limit(points, normals, left, right): look past each rail's drawn edge; if another stroke (or another part
    of the same, joined stroke) starts again closer than thin.min_gap_mm, pull that rail in so the gap between
    the needles stays open. Both strokes give way by half."""
    h, w = mask.shape
    gap, pc = cfg.thin.min_gap_mm, cfg.thin.pull_compensation_mm
    steps = np.arange(0.0, cfg.thin.satin_max_mm / 2 + gap + 0.6, 0.025)

    def limit(arr: np.ndarray, normal: np.ndarray, left: np.ndarray, right: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        result = []
        for sign, side in ((1.0, left), (-1.0, right)):
            pos = arr[:, None, :] + sign * normal[:, None, :] * steps[None, :, None]
            cols = np.floor((pos[..., 0] - ox) * RES).astype(int)
            rows = np.floor((pos[..., 1] - oy) * RES).astype(int)
            ok = (rows >= 0) & (rows < h) & (cols >= 0) & (cols < w)
            inside = np.zeros(ok.shape, dtype=bool)
            inside[ok] = mask[rows[ok], cols[ok]] > 0
            exit_i = np.argmax(~inside, axis=1)
            after = inside & (np.arange(len(steps))[None, :] > exit_i[:, None])
            has_neighbour = after.any(axis=1) & (~inside).any(axis=1) & inside[:, 0]
            back_i = np.argmax(after, axis=1)
            edge = steps[exit_i]
            drawn_gap = steps[back_i] - edge
            # The needle may reach the midline of the drawn gap minus half the gap to keep.
            cap = edge + drawn_gap / 2 - gap / 2 - pc
            squeeze = has_neighbour & (drawn_gap < gap + 2 * pc) & (cap < side)
            result.append(np.where(squeeze, np.maximum(cap, MIN_SIDE_MM), side))
        return result[0], result[1]

    return limit


def _narrow_satin(part: Polygon, cfg: Config) -> Satin | None:
    """A narrow part of a fill (petal, leaf, stem, strand) as one satin between its own two sides, pointed at
    the tips like the shape itself. None when no such satin is clean: a ring, a fan, or rungs that leave the
    shape. The part then stays tatami rather than becoming a satin that misrepresents it."""
    if part.interiors:
        return None
    max_width = cfg.fill.satin_max_width_mm
    axes = rect_axes(part)
    satin = None
    if axes is not None and axes[1] <= 2 * max_width:
        (ux, uy), long_len, _ = axes
        c = part.centroid
        satin = strip_satin(part, (c.x - ux * long_len, c.y - uy * long_len), (c.x + ux * long_len, c.y + uy * long_len))
    if satin is None:
        satin = _unbranched_strip(part, cfg)
    if satin is None:
        return None
    a, b = satin.rails
    rails = sorted([LineString(a).length, LineString(b).length])
    rungs = [LineString([p, q]) for p, q in zip(a, b) if p != q]
    if not rungs or rails[0] < 0.35 * rails[1]:
        return None
    inside = part.buffer(0.15)
    if sum(r.intersection(inside).length for r in rungs) < 0.98 * sum(r.length for r in rungs):
        return None
    if max(r.length for r in rungs) > 1.1 * max_width:
        return None
    return satin


def _unbranched_strip(part: Polygon, cfg: Config) -> Satin | None:
    """A narrow fill part whose skeleton is one open path is sewn as one satin between its own two sides."""
    minx, miny, maxx, maxy = part.bounds
    ox, oy = minx - 0.5, miny - 0.5
    mask = _rasterize(part, ox, oy, int(math.ceil((maxx - ox + 0.5) * RES)), int(math.ceil((maxy - oy + 0.5) * RES)))
    pixels = _prune({(int(r), int(c)) for r, c in zip(*np.nonzero(skeletonize(mask.astype(bool))))}, cfg.thin.spur_min_mm * RES)
    chains = _chain(_trace(pixels))
    if len(chains) != 1 or chains[0][1]:
        return None
    path = chains[0][0]
    ends = [(ox + (c + 0.5) / RES, oy + (r + 0.5) / RES) for r, c in (path[0], path[-1])]
    return strip_satin(part, ends[0], ends[1])


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


def _prune(pixels: set[Pixel], spur_px: float, dist_px: np.ndarray | None = None, reach_px: float = 0.0) -> set[Pixel]:
    """Remove short branches that end in a free endpoint and hang off a junction.
    With dist_px, a branch is judged by how far its DRAWN tip sticks out past the stroke it hangs off:
    length + its tip's radius - the junction's radius. Skeleton whiskers inside a thick stroke stick out by
    nothing and go; a hook, a curl tip or a toe sticks out by its own size and stays (reach_px)."""
    for _ in range(4 if dist_px is None else 8):
        edges = _trace(pixels)
        degree: dict[int, int] = {}
        for e in edges:
            if not e.cycle:
                degree[e.a] = degree.get(e.a, 0) + 1
                degree[e.b] = degree.get(e.b, 0) + 1
        removed = False
        for e in edges:
            if e.cycle:
                continue
            da, db = degree.get(e.a, 0), degree.get(e.b, 0)
            if da == 1 and db >= 3:
                drop, junction, tip = e.path[:-1], e.path[-1], e.path[0]
            elif db == 1 and da >= 3:
                drop, junction, tip = e.path[1:], e.path[0], e.path[-1]
            else:
                continue
            if dist_px is not None:
                if _px_length(e.path) + float(dist_px[tip]) - float(dist_px[junction]) >= reach_px:
                    continue
            elif _px_length(e.path) >= spur_px:
                continue
            pixels = pixels - set(drop)
            removed = True
        if not removed:
            break
    return pixels


def _px_length(path: list[Pixel]) -> float:
    return sum(math.dist(a, b) for a, b in zip(path, path[1:]))


def _chain(edges: list[_Edge]) -> list[tuple[list[Pixel], bool, bool, bool]]:
    """Join edges through junctions, always continuing along the straightest branch (forwards from the start,
    then backwards through it). Returns (path, closed, start is a free end, end is a free end); an end that
    stops at a junction is not free."""
    out: list[tuple[list[Pixel], bool, bool, bool]] = []
    at_node: dict[int, list[int]] = {}
    for i, e in enumerate(edges):
        if e.cycle:
            out.append((e.path, True, False, False))
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
            start = node
            # Grow backwards through the start node too, so a network without free ends (a petal outline, a
            # goggle frame) is not left as fragments that all stop at the same junction.
            while not (path[0] == path[-1] and len(path) > 3):
                candidates = [j for j in at_node.get(start, []) if j not in used]
                if not candidates:
                    break
                d_out = direction(path, False)
                best = max(candidates, key=lambda j: (_dot2(direction(oriented(j, start)[::-1], True), d_out), -j))
                if _dot2(direction(oriented(best, start)[::-1], True), d_out) < 0.5:
                    break
                used.add(best)
                path = oriented(best, start)[::-1][:-1] + path
                start = edges[best].b if edges[best].a == start else edges[best].a
            closed = path[0] == path[-1] and len(path) > 3
            if len(path) >= 2:
                out.append((path[:-1] if closed else path, closed, len(at_node[start]) == 1, len(at_node.get(end, [])) == 1))
    return out


def _dot2(u: tuple[float, float], v: tuple[float, float]) -> float:
    return u[0] * v[0] + u[1] * v[1]
