"""preview.png (stitch simulation) and compare.png (reduced artwork | simulation)."""

from __future__ import annotations

from pathlib import Path

import pyembroidery
from PIL import Image, ImageDraw

from .colors import ColorRegion
from .export import Frame
from .geometry import polygons

FABRIC = (244, 241, 234)
THREAD_MM = 0.35  # drawn thread thickness


def render_preview(pattern: pyembroidery.EmbPattern, frame: Frame, anchor: tuple[float, float], path: Path, min_px: int = 2000) -> Image.Image:
    """Draw every stitch as a shaded thread. anchor = document position (mm) of the design's top-left bound."""
    scale = min_px / min(frame.width, frame.height)  # px per mm; the short side gets min_px
    image = Image.new("RGB", (round(frame.width * scale), round(frame.height * scale)), FABRIC)
    draw = ImageDraw.Draw(image)
    width = max(2, round(THREAD_MM * scale))
    core = max(1, round(width * 0.45))
    minx, miny, _, _ = pattern.bounds()

    def px(x: float, y: float) -> tuple[float, float]:
        return ((anchor[0] + (x - minx) / 10) * scale, (anchor[1] + (y - miny) / 10) * scale)

    colors = [_rgb(t.hex_color()) for t in pattern.threadlist] or [(0, 0, 0)]
    block = 0
    prev = None
    for x, y, command in pattern.stitches:
        cmd = command & pyembroidery.COMMAND_MASK
        if cmd == pyembroidery.COLOR_CHANGE:
            block = min(block + 1, len(colors) - 1)
            prev = None
        elif cmd == pyembroidery.STITCH:
            point = px(x, y)
            if prev is not None:
                sheen = _sheen(prev, point)
                draw.line([prev, point], fill=_shade(colors[block], 0.72 * sheen), width=width)
                draw.line([prev, point], fill=_shade(colors[block], 1.12 * sheen), width=core)
            prev = point
        else:  # jump, trim, stop, end
            prev = None
    image.save(path, optimize=False)
    return image


def render_artwork(regions: list[ColorRegion], frame: Frame, size: tuple[int, int]) -> Image.Image:
    scale = size[0] / frame.width
    image = Image.new("RGB", size, FABRIC)
    for region in regions:
        # Regions never overlap, so each one is cut out with its own mask (holes stay untouched).
        mask = Image.new("L", size, 0)
        mask_draw = ImageDraw.Draw(mask)
        for poly in polygons(frame.place(region.geometry)):
            mask_draw.polygon([(x * scale, y * scale) for x, y in poly.exterior.coords], fill=255)
            for hole in poly.interiors:
                mask_draw.polygon([(x * scale, y * scale) for x, y in hole.coords], fill=0)
        image.paste(_rgb(region.thread.hex), mask=mask)
    return image


def render_compare(artwork: Image.Image, preview: Image.Image, path: Path) -> None:
    gap = 40
    canvas = Image.new("RGB", (artwork.width + preview.width + gap, max(artwork.height, preview.height)), (255, 255, 255))
    canvas.paste(artwork, (0, 0))
    canvas.paste(preview, (artwork.width + gap, 0))
    canvas.save(path, optimize=False)


LIGHT = (-0.7071, -0.7071)  # light from the top left


def _sheen(a: tuple[float, float], b: tuple[float, float]) -> float:
    """A thread is a glossy cylinder: it catches the light most when it lies across the light direction.
    This is what makes stitch direction read as form in real embroidery."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = (dx * dx + dy * dy) ** 0.5
    if length == 0:
        return 1.0
    along = (dx * LIGHT[0] + dy * LIGHT[1]) / length
    return 0.8 + 0.32 * (1 - along * along)


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _shade(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    if factor < 1:
        return tuple(round(c * factor) for c in color)
    return tuple(min(255, round(c + (255 - c) * (factor - 1) * 1.5)) for c in color)
