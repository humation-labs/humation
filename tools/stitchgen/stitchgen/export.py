"""Step 7: write the attributed SVG, run Ink/Stitch headless and verify the result with pyembroidery."""

from __future__ import annotations

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
                # Random stitch lengths: straight stitches without the tatami brick pattern.
                "inkstitch:enable_random_stitch_length": "True",
                "inkstitch:random_stitch_length_jitter_percent": fmt_coord(cfg.fill.random_jitter_percent),
                "inkstitch:fill_underlay": str(cfg.fill.underlay),
            }
            if item.guide is not None and guided:
                attrs["inkstitch:max_stitch_length_mm"] = fmt_coord(min(cfg.fill.max_stitch_length_mm, STITCH_LENGTH_MM.get(item.flow or "", 99)))
                attrs["inkstitch:fill_method"] = "guided_fill"
                attrs["inkstitch:guided_fill_strategy"] = str(item.guide_strategy)  # 0 copy, 1 parallel offset
                guide = geom_to_path_d(frame.place(item.guide))
            else:
                attrs["inkstitch:fill_method"] = "tatami_fill"
                attrs["inkstitch:angle"] = fmt_coord(item.angle)
        elif item.kind == "satin" and item.satin is not None:
            # Two rails with equal node counts: Ink/Stitch uses each node pair as a rung.
            d = " ".join(_polyline_d(rail, frame) for rail in item.satin.rails)
            border = item.role == "border"
            narrow = item.satin.width < 1.2
            attrs = {
                "style": f"fill:none;stroke:{item.thread.hex};stroke-width:0.1",
                "inkstitch:satin_column": "True",
                "inkstitch:zigzag_spacing_mm": fmt_coord(cfg.border.zigzag_spacing_mm if border else cfg.thin.zigzag_spacing_mm),
                "inkstitch:center_walk_underlay": "False" if border or narrow else "True",
                "inkstitch:contour_underlay": "True" if border else "False",
                "inkstitch:zigzag_underlay": "True" if border else "False",
            }
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
        body.append(path)
    return svg_document(frame.width, frame.height, body)


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
