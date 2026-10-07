"""Step 7: write the attributed SVG, run Ink/Stitch headless and verify the result with pyembroidery."""

from __future__ import annotations

import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pyembroidery
from shapely import affinity

from .config import Config, PaletteColor
from .flow import STITCH_LENGTH_MM
from .order import Item
from .report import StitchgenError
from .svgio import fmt_coord, geom_to_path_d, svg_document

TRIM_GAP_MM = 1.0
DEFAULT_INKSTITCH = "/root/.config/inkscape/extensions/inkstitch/bin/inkstitch"


@dataclass(frozen=True)
class Frame:
    """Translation from pipeline millimetres to document millimetres (all coordinates positive)."""

    dx: float
    dy: float
    width: float
    height: float

    @staticmethod
    def around(bounds: tuple[float, float, float, float], margin: float = 1.0) -> Frame:
        minx, miny, maxx, maxy = bounds
        return Frame(round(margin - minx, 4), round(margin - miny, 4), round(maxx - minx + 2 * margin, 4), round(maxy - miny + 2 * margin, 4))

    def place(self, geom):
        return affinity.translate(geom, self.dx, self.dy)


def inkstitch_svg(items: list[Item], frame: Frame, cfg: Config, guided: bool = True) -> str:
    """guided=False lays every fill in straight rows (fallback when a guided fill fails in Ink/Stitch)."""
    body = []
    for n, item in enumerate(items, start=1):
        guide = None
        if item.kind == "fill":
            d = geom_to_path_d(frame.place(item.geometry))
            attrs = {
                "style": f"fill:{item.thread.hex};fill-rule:evenodd;stroke:none",
                "inkstitch:row_spacing_mm": fmt_coord(cfg.fill.row_spacing_mm),
                "inkstitch:max_stitch_length_mm": fmt_coord(cfg.fill.max_stitch_length_mm),
                "inkstitch:fill_underlay": str(cfg.fill.underlay),
                # Keep the underlay inside the top stitches so it never peeks out at the edges.
                "inkstitch:fill_underlay_inset_mm": fmt_coord(cfg.fill.underlay_inset_mm),
            }
            if cfg.fill.pattern == "random":
                # Random stitch lengths: no needle-point pattern, the surface reads as plain straight stitches.
                attrs["inkstitch:enable_random_stitch_length"] = "True"
                attrs["inkstitch:random_stitch_length_jitter_percent"] = fmt_coord(cfg.fill.random_jitter_percent)
            else:
                # Classic tatami: needle points shift row by row and repeat every `staggers` rows.
                attrs["inkstitch:staggers"] = str(cfg.fill.staggers)
            if item.guide is not None and guided:
                attrs["inkstitch:max_stitch_length_mm"] = fmt_coord(min(cfg.fill.max_stitch_length_mm, STITCH_LENGTH_MM.get(item.flow or "", 99)))
                attrs["inkstitch:fill_method"] = "guided_fill"
                attrs["inkstitch:guided_fill_strategy"] = str(item.guide_strategy)  # 0 copy, 1 parallel offset
                guide = geom_to_path_d(frame.place(item.guide))
            else:
                attrs["inkstitch:fill_method"] = "tatami_fill"
                attrs["inkstitch:angle"] = fmt_coord(item.angle)
        elif item.kind == "satin" and item.satin is not None and item.satin.run:
            d = geom_to_path_d(frame.place(item.satin.centre)) + (" Z" if item.satin.closed else "")
            attrs = {
                "style": f"fill:none;stroke:{item.thread.hex};stroke-width:0.1",
                "inkstitch:running_stitch_length_mm": fmt_coord(cfg.thin.running_stitch_length_mm),
                "inkstitch:bean_stitch_repeats": "1",  # each stitch sewn forward, back, forward: a bolder line
            }
        elif item.kind == "satin" and item.satin is not None:
            # Two rails with equal node counts: Ink/Stitch uses each node pair as a rung.
            d = " ".join(_polyline_d(rail, frame) for rail in item.satin.rails)
            attrs = {"style": f"fill:none;stroke:{item.thread.hex};stroke-width:0.1", "inkstitch:satin_column": "True",
                     **_satin_recipe(item.satin, cfg),
                     # stitchgen already chains satins tip to tip; Ink/Stitch's own nearest-point start/end would
                     # split small satins and leave travel and tie stitches on top of an eye.
                     "inkstitch:start_at_nearest_point": "False",
                     "inkstitch:end_at_nearest_point": "False"}
        else:
            d = geom_to_path_d(frame.place(item.geometry))
            attrs = {
                "style": f"fill:none;stroke:{item.thread.hex};stroke-width:0.1",
                "inkstitch:running_stitch_length_mm": fmt_coord(cfg.border.placement_stitch_length_mm),
            }
        nxt = items[n] if n < len(items) else None
        if nxt is not None and nxt.thread.hex == item.thread.hex and item.geometry.distance(nxt.geometry) > TRIM_GAP_MM:
            # Without a trim Ink/Stitch would sew short travels as visible stitches across other colours.
            attrs["inkstitch:trim_after"] = "True"
        rendered = " ".join(f'{k}="{v}"' for k, v in attrs.items())
        path = f'<path id="{item.role}-{n:03d}" d="{d}" {rendered}/>'
        if guide is not None and guided:
            # Ink/Stitch finds a guide line as a marked sibling in the same group.
            marker = "fill:none;stroke:#000000;stroke-width:0.1;marker-start:url(#inkstitch-guide-line-marker)"
            path = f'<g id="group-{n:03d}">\n  {path}\n  <path id="guide-{n:03d}" d="{guide}" style="{marker}"/>\n</g>'
        if item.kind == "satin" and item.satin is not None and item.satin.dot and not item.satin.run:
            # Lead into an eye from its middle: Ink/Stitch puts the tie-in on this short run, which the satin
            # rows then cover, instead of knotting it onto the visible tip row.
            lead = _lead_in(item.satin, frame)
            if lead:
                body.append(f'<path id="lead-{n:03d}" d="{lead}" style="fill:none;stroke:{item.thread.hex};stroke-width:0.1" '
                            f'inkstitch:running_stitch_length_mm="0.5000"/>')
        body.append(path)
    return svg_document(frame.width, frame.height, body)


def _lead_in(satin, frame: Frame) -> str:
    a, b = satin.rails
    start = ((a[0][0] + b[0][0]) / 2, (a[0][1] + b[0][1]) / 2)
    mid = satin.centre.interpolate(0.5, normalized=True)
    if math.dist(start, (mid.x, mid.y)) < 0.3:
        return ""
    return _polyline_d([(mid.x, mid.y), start], frame)


def _satin_recipe(satin, cfg: Config) -> dict[str, str]:
    """Density, underlay and compensation by kind of satin."""
    if satin.dot:
        # Eyes and small marks: dense, no underlay, no pull compensation (it would round and enlarge them),
        # no short stitches (on a 1.5 mm dot every row is a "tight curve").
        return {
            "inkstitch:zigzag_spacing_mm": fmt_coord(cfg.thin.dot_zigzag_spacing_mm),
            "inkstitch:center_walk_underlay": "False",
            "inkstitch:contour_underlay": "False",
            "inkstitch:zigzag_underlay": "False",
            "inkstitch:pull_compensation_mm": "0.0000",
            "inkstitch:short_stitch_inset": "0.0000",
        }
    if satin.width >= 2.0:
        # Wide satins (the heat-cut edge, broad petals) need zigzag + contour underlay to stand up.
        return {
            "inkstitch:zigzag_spacing_mm": fmt_coord(cfg.border.zigzag_spacing_mm),
            "inkstitch:center_walk_underlay": "False",
            "inkstitch:contour_underlay": "True",
            "inkstitch:zigzag_underlay": "True",
            "inkstitch:pull_compensation_mm": fmt_coord(cfg.border.pull_compensation_mm),
            "inkstitch:short_stitch_inset": fmt_coord(cfg.border.short_stitch_inset_percent),
            "inkstitch:short_stitch_distance_mm": fmt_coord(cfg.thin.short_stitch_distance_mm),
        }
    return {
        "inkstitch:zigzag_spacing_mm": fmt_coord(cfg.thin.zigzag_spacing_mm),
        "inkstitch:center_walk_underlay": "True" if satin.width >= 1.2 else "False",
        "inkstitch:contour_underlay": "False",
        "inkstitch:zigzag_underlay": "False",
        "inkstitch:pull_compensation_mm": fmt_coord(cfg.thin.pull_compensation_mm),
        # On tight curves the inside of a satin crowds; alternate stitches stop short there. Kept mild: a deep
        # inset notches the visible edge and lets the fill below show through.
        "inkstitch:short_stitch_inset": fmt_coord(cfg.thin.short_stitch_inset_percent),
        "inkstitch:short_stitch_distance_mm": fmt_coord(cfg.thin.short_stitch_distance_mm),
    }


def _polyline_d(points, frame: Frame) -> str:
    coords = [(x + frame.dx, y + frame.dy) for x, y in points]
    return "M " + " L ".join(f"{fmt_coord(x)} {fmt_coord(y)}" for x, y in coords)


def run_inkstitch(svg_path: Path, fmt: str, out_path: Path, timeout_s: int) -> None:
    binary = os.environ.get("INKSTITCH_BIN", DEFAULT_INKSTITCH)
    if not Path(binary).exists():
        raise StitchgenError(f"Ink/Stitch not found at {binary} (set INKSTITCH_BIN or run inside the Docker image)")
    cmd = [binary, "--extension=output", f"--format={fmt}", str(svg_path)]
    if not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a", *cmd]  # Ink/Stitch imports wxPython and needs an X display even headless
    try:
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        raise StitchgenError(f"Ink/Stitch timed out after {timeout_s}s exporting {fmt}") from exc
    if result.returncode != 0 or not result.stdout:
        tail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-5:]
        raise StitchgenError(f"Ink/Stitch failed exporting {fmt} (exit {result.returncode}): {' | '.join(tail)}")
    out_path.write_bytes(result.stdout)


@dataclass(frozen=True)
class Summary:
    stitch_count: int
    color_changes: int
    threads: list[str]  # hex per colour block, in sewing order
    width_mm: float
    height_mm: float


def read_back(path: Path) -> pyembroidery.EmbPattern:
    try:
        pattern = pyembroidery.read(str(path))
    except Exception as exc:  # pyembroidery raises assorted errors on corrupt files
        raise StitchgenError(f"{path.name} could not be read back: {exc}") from exc
    if pattern is None or summarize(pattern).stitch_count == 0:
        raise StitchgenError(f"{path.name} could not be read back or contains no stitches")
    return pattern


def summarize(pattern: pyembroidery.EmbPattern) -> Summary:
    stitches = sum(1 for _, _, cmd in pattern.stitches if cmd & pyembroidery.COMMAND_MASK == pyembroidery.STITCH)
    minx, miny, maxx, maxy = pattern.bounds()
    return Summary(
        stitch_count=stitches,
        color_changes=pattern.count_color_changes(),
        threads=[t.hex_color().lower() for t in pattern.threadlist],
        width_mm=round((maxx - minx) / 10, 1),
        height_mm=round((maxy - miny) / 10, 1),
    )


def thread_for(hex_color: str, palette: list[PaletteColor]) -> PaletteColor | None:
    return next((p for p in palette if p.hex == hex_color.lower()), None)
