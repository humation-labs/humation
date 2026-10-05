"""Row direction for fills, chosen per motif so the stitches read like the thing they depict.

Ink/Stitch's guided fill lays its rows parallel to a guide line, so each flow below is just a curve:

- arch:  rows circle the crown and fall down both sides (hair).
- wrap:  rows bow downwards across the shape, like latitude lines on a ball seen slightly from above (face).
- drape: rows hang vertically with a slight bow (clothes).
"""

from __future__ import annotations

import math

from shapely.geometry import LineString, Polygon

SAMPLES = 24
# Straight stitches cut across curved rows; tighter flows need shorter stitches so the chords do not leave gaps.
STITCH_LENGTH_MM = {"arch": 2.5, "wrap": 3.0, "drape": 3.5}


def guide_line(flow: str, part: Polygon, reference: tuple[float, float, float, float] | None = None) -> tuple[LineString, int] | None:
    """(guide, Ink/Stitch guided_fill_strategy) for one fill polygon. reference: bounds shared by every part
    of the motif (all hair), so separate pieces follow one flow instead of each arching on its own."""
    minx, miny, maxx, maxy = part.bounds
    if flow == "arch":
        rminx, rminy, rmaxx, rmaxy = reference or part.bounds
        r0 = (rmaxx - rminx) / 2
        cx, cy = rminx + r0, rminy + r0
        # The flow is a family of concentric crown arcs with straight legs. Each part gets the member that runs
        # through it (Ink/Stitch leaves a part empty when its guide misses it) and offsets it inwards (strategy 1),
        # so rows stay concentric without cusps: hair wraps round the head and falls down the sides.
        p = part.representative_point()
        r = abs(p.x - cx) if p.y > cy else math.hypot(p.x - cx, p.y - cy)
        r = max(r, 0.5)
        legs = max(maxy, rmaxy) - cy
        left = [(cx - r, cy + legs * (1 - i / 6)) for i in range(6)] if legs > 0 else []
        arc = [(cx - r * math.cos(math.pi * i / SAMPLES), cy - r * math.sin(math.pi * i / SAMPLES)) for i in range(SAMPLES + 1)]
        right = [(cx + r, cy + legs * i / 6) for i in range(1, 7)] if legs > 0 else []
        return LineString([(round(x, 4), round(y, 4)) for x, y in left + arc + right]), 1
    w, h = maxx - minx, maxy - miny
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    # Shallow curves are copied side by side (strategy 0) rather than offset, which would crease them.
    if flow == "wrap":
        sag = 0.3 * min(w, h)
        return _curve(lambda t: (cx + t * (w / 2 + 1), cy + sag * (0.5 - t * t))), 0
    if flow == "drape":
        bow = 0.12 * min(w, h)
        return _curve(lambda t: (cx + bow * (0.5 - t * t), cy + t * (h / 2 + 1))), 0
    return None


def _curve(point) -> LineString:
    return LineString([tuple(round(v, 4) for v in point(-1 + 2 * i / SAMPLES)) for i in range(SAMPLES + 1)])
