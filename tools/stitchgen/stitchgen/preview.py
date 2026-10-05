"""preview.png (stitch simulation) and compare.png (reduced artwork | simulation)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyembroidery
from PIL import Image, ImageDraw, ImageFilter

from .colors import ColorRegion
from .export import Frame
from .geometry import polygons

FABRIC = (240, 238, 233)
THREAD_MM = 0.42  # visual width of a laid 40 wt thread
SUPERSAMPLE = 2


def render_preview(pattern: pyembroidery.EmbPattern, frame: Frame, anchor: tuple[float, float], path: Path, min_px: int = 2000) -> Image.Image:
    """Render the stitches as real thread: round, glossy strands that cast a soft shadow on the fabric.
    anchor = document position (mm) of the design's top-left bound."""
    final = min_px / min(frame.width, frame.height)  # px per mm; the short side gets min_px
    scale = final * SUPERSAMPLE
    size = (round(frame.width * scale), round(frame.height * scale))
    minx, miny, _, _ = pattern.bounds()

    def px(x: float, y: float) -> tuple[float, float]:
        return ((anchor[0] + (x - minx) / 10) * scale, (anchor[1] + (y - miny) / 10) * scale)

    colors = [_rgb(t.hex_color()) for t in pattern.threadlist] or [(0, 0, 0)]
    segments: list[tuple[tuple[float, float], tuple[float, float], tuple[int, int, int]]] = []
    block, prev = 0, None
    for x, y, command in pattern.stitches:
        cmd = command & pyembroidery.COMMAND_MASK
        if cmd == pyembroidery.COLOR_CHANGE:
            block, prev = min(block + 1, len(colors) - 1), None
        elif cmd == pyembroidery.STITCH:
            point = px(x, y)
            if prev is not None and prev != point:
                segments.append((prev, point, colors[block]))
            prev = point
        else:  # jump, trim, stop, end
            prev = None

    width = max(3, round(THREAD_MM * scale))
    image = _fabric(size)

    # Soft shadow: thread stands proud of the fabric, light comes from the top left.
    shadow = Image.new("L", size, 0)
    shadow_draw = ImageDraw.Draw(shadow)
    dx, dy = 0.1 * scale, 0.16 * scale
    for a, b, _ in segments:
        shadow_draw.line([(a[0] + dx, a[1] + dy), (b[0] + dx, b[1] + dy)], fill=255, width=width)
    shadow = shadow.filter(ImageFilter.GaussianBlur(0.18 * scale))
    image = Image.composite(Image.new("RGB", size, (150, 146, 138)), image, shadow.point(lambda v: v * 0.45))

    # Each strand: a dark groove at its edges, the body, and a highlight on the side facing the light.
    draw = ImageDraw.Draw(image)
    body_w, glint_w = max(2, round(width * 0.72)), max(1, round(width * 0.26))
    for a, b, color in segments:
        sheen = _sheen(a, b)
        nx, ny = _light_side(a, b)
        off = width * 0.14
        draw.line([a, b], fill=_shade(color, 0.6 * sheen), width=width)
        draw.line([a, b], fill=_shade(color, 0.96 * sheen), width=body_w)
        draw.line([(a[0] + nx * off, a[1] + ny * off), (b[0] + nx * off, b[1] + ny * off)], fill=_shade(color, 1.22 * sheen), width=glint_w)

    image = image.resize((round(frame.width * final), round(frame.height * final)), Image.LANCZOS)
    image.save(path, optimize=False)
    return image


def _fabric(size: tuple[int, int]) -> Image.Image:
    """Plain fabric with a faint, fixed grain so the preview reads as cloth."""
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 3.0, (size[1] // 4 + 1, size[0] // 4 + 1))
    grain = Image.fromarray(np.clip(128 + noise, 0, 255).astype(np.uint8)).resize(size, Image.BILINEAR)
    base = np.asarray(Image.new("RGB", size, FABRIC), dtype=np.int16)
    out = base + (np.asarray(grain, dtype=np.int16)[..., None] - 128)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


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


def _light_side(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    """Unit normal of the strand pointing towards the light."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = (dx * dx + dy * dy) ** 0.5 or 1.0
    nx, ny = -dy / length, dx / length
    return (nx, ny) if nx * LIGHT[0] + ny * LIGHT[1] >= 0 else (-nx, -ny)


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _shade(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    if factor < 1:
        return tuple(round(c * factor) for c in color)
    # Lighten towards white, gently: dark threads keep their depth instead of turning grey.
    return tuple(min(255, round(c + (255 - c) * (factor - 1) * (0.35 + 0.9 * c / 255))) for c in color)
