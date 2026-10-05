"""stitchgen input.svg -o out/ [--size 60] [--border satin|none] [--palette palette.json] [--debug]"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import traceback
from pathlib import Path

from shapely.geometry import LineString, Polygon

from .attributes import assign_attributes
from .border import make_border
from .colors import ColorRegion, ReducedDrawing, reduce_colors
from .config import load_config, load_palette
from .export import Frame, inkstitch_svg, read_back, run_inkstitch, summarize, thread_for
from .geometry import clean, union
from .normalize import normalize
from .order import sewing_order
from .outline import silhouette
from .preview import render_artwork, render_compare, render_preview
from .report import StitchgenError, Warning
from .svgio import filled_path_element, geom_to_path_d, svg_document

EXIT_OK, EXIT_WARN, EXIT_FAIL = 0, 1, 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        warnings = run(args)
    except StitchgenError as exc:
        print(f"stitchgen: error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except Exception:  # anything unexpected is still a failed conversion, never a silent partial output
        traceback.print_exc()
        return EXIT_FAIL
    for w in warnings:
        print(f"stitchgen: warning [{w.code}] {w.message}", file=sys.stderr)
    return EXIT_WARN if warnings else EXIT_OK


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stitchgen", description="Convert a Humation avatar SVG into patch embroidery data (PES/DST).")
    p.add_argument("input", type=Path)
    p.add_argument("-o", "--out", type=Path, required=True, help="output directory")
    p.add_argument("--size", type=float, default=None, help="avatar size in mm (longest side), default from config.toml")
    p.add_argument("--border", choices=["satin", "outline", "none"], default="satin",
                   help="satin: wide heat-cut patch edge; outline: thin outer line for direct embroidery")
    p.add_argument("--palette", type=Path, default=None)
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--debug", action="store_true", help="keep intermediate SVGs in out/debug/")
    return p


def run(args: argparse.Namespace) -> list[Warning]:
    if not args.input.is_file():
        raise StitchgenError(f"input not found: {args.input}")
    cfg = load_config(args.config)
    palette = load_palette(args.palette)
    size = args.size or cfg.size.default_mm
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    debug = out / "debug" if args.debug else None
    if debug:
        debug.mkdir(exist_ok=True)

    # 1. normalize
    drawing = normalize(args.input, size)
    warnings = list(drawing.warnings)
    # 2. reduce colours
    reduced = reduce_colors(drawing, palette, cfg.colors.max, cfg.colors.roles)
    warnings += reduced.warnings
    # 3. outline
    shape = silhouette([r.geometry for r in reduced.regions], cfg.outline.concavity_fill_mm)
    border_thread = next((p for p in palette if p.brother_number == cfg.border.color), None)
    if border_thread is None:
        raise StitchgenError(f"border colour {cfg.border.color} is not in the palette")
    # 5 (prepared early). The border takes over the avatar's own outer outline, so the artwork is cut back
    # to the area inside it before stitch types are chosen.
    border = None
    if args.border == "satin":
        border = make_border(shape, cfg.border.width_mm, cfg.border.inset_mm)
    elif args.border == "outline":
        border = make_border(shape, cfg.border.outline_width_mm, cfg.border.outline_width_mm)
    stitched = reduced
    if border is not None:
        clipped = [ColorRegion(r.thread, clean(r.geometry.intersection(border.inner))) for r in reduced.regions]
        stitched = ReducedDrawing([r for r in clipped if not r.geometry.is_empty], [], reduced.roles)
    # 4. stitch attributes (the border thread doubles as the line-art thread)
    attributed = assign_attributes(stitched, cfg, line_art_hex=border_thread.hex, canvas=shape)
    warnings += attributed.warnings
    # 6. order
    items = sewing_order(attributed, border, border_thread, placement=args.border == "satin")

    frame = Frame.around((border.outline if border else shape).bounds)
    if debug:
        _write_debug(debug, frame, drawing, reduced, shape, attributed, border)

    # 7. export
    try:
        pattern, dst = _export(items, frame, cfg, out, debug, guided=True)
    except StitchgenError as exc:
        # Guided fills are the only Ink/Stitch feature here that can fail on odd shapes; retry with straight rows.
        pattern, dst = _export(items, frame, cfg, out, debug, guided=False)
        warnings.append(Warning("flow_fallback", f"directional fills failed in Ink/Stitch ({exc}); all fills use straight rows"))
    summary = summarize(pattern)
    if summarize(dst).stitch_count == 0:
        raise StitchgenError("design.dst has no stitches")

    # Previews
    anchor_geom = union(_footprint(i) for i in items)
    minx, miny, _, _ = frame.place(anchor_geom).bounds
    preview = render_preview(pattern, frame, (minx, miny), out / "preview.png")
    artwork = render_artwork(reduced.regions, frame, preview.size)
    render_compare(artwork, preview, out / "compare.png")

    # Guardrails: report, never abort.
    g = cfg.guardrails
    if summary.stitch_count > g.max_stitches:
        warnings.append(Warning("too_many_stitches", f"{summary.stitch_count} stitches exceeds {g.max_stitches}"))
    if summary.color_changes > g.max_color_changes:
        warnings.append(Warning("too_many_color_changes", f"{summary.color_changes} colour changes exceeds {g.max_color_changes}"))
    dropped_ratio = attributed.dropped / max(drawing.input_element_count, 1)
    if dropped_ratio > g.max_dropped_ratio:
        warnings.append(Warning("too_much_detail_dropped", f"{attributed.dropped} tiny parts dropped ({dropped_ratio:.0%} of {drawing.input_element_count} elements)"))

    meta = {
        "input": args.input.name,
        "stitch_count": summary.stitch_count,
        "color_changes": summary.color_changes,
        "color_order": [_thread_json(h, palette) for h in summary.threads],
        "estimated_minutes": round(summary.stitch_count / cfg.export.stitches_per_minute, 1),
        "size_mm": {"width": summary.width_mm, "height": summary.height_mm},
        "avatar_size_mm": {"width": drawing.width_mm, "height": drawing.height_mm},
        "border": args.border,
        "warnings": [w.to_json() for w in warnings],
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return warnings


def _export(items, frame: Frame, cfg, out: Path, debug: Path | None, guided: bool):
    with tempfile.TemporaryDirectory() as tmp:
        svg_path = (debug or Path(tmp)) / "07_inkstitch.svg"
        svg_path.write_text(inkstitch_svg(items, frame, cfg, guided), encoding="utf-8")
        run_inkstitch(svg_path, "pes", out / "design.pes", cfg.export.timeout_s)
        run_inkstitch(svg_path, "dst", out / "design.dst", cfg.export.timeout_s)
    return read_back(out / "design.pes"), read_back(out / "design.dst")


def _footprint(item):
    if item.satin is not None:
        return Polygon(item.satin.rails[0] + item.satin.rails[1][::-1]).buffer(0)
    if isinstance(item.geometry, LineString):
        return item.geometry.buffer(0.05)
    return item.geometry


def _thread_json(hex_color: str, palette) -> dict[str, str]:
    thread = thread_for(hex_color, palette)
    if thread is None:
        return {"brother_number": "", "name": "unknown", "hex": hex_color}
    return {"brother_number": thread.brother_number, "name": thread.name, "hex": thread.hex}


def _write_debug(debug: Path, frame: Frame, drawing, reduced, shape, attributed, border) -> None:
    def doc(name: str, body: list[str]) -> None:
        (debug / name).write_text(svg_document(frame.width, frame.height, body), encoding="utf-8")

    def fill(geom, color: str, extra: str = "") -> str:
        el = filled_path_element(geom_to_path_d(frame.place(geom)), color)
        return el.replace("/>", f"{extra}/>") if extra else el

    def line(geom, color: str, width: float) -> str:
        return f'<path d="{geom_to_path_d(frame.place(geom))}" fill="none" stroke="{color}" stroke-width="{width:.3f}" stroke-linejoin="round"/>'

    def satin(s, color: str) -> str:
        return fill(Polygon(s.rails[0] + s.rails[1][::-1]).buffer(0), color)

    doc("01_normalized.svg", [fill(e.geometry, e.color) for e in drawing.elements])
    doc("02_reduced.svg", [fill(r.geometry, r.thread.hex) for r in reduced.regions])
    doc("03_outline.svg", [fill(r.geometry, r.thread.hex) for r in reduced.regions] + [line(shape.exterior, "#e0007a", 0.2)])
    doc("04_attributes.svg",
        [fill(f.geometry, f.thread.hex, f' opacity="0.8" data-angle="{f.angle:g}"') for f in attributed.fills]
        + [satin(s.satin, s.thread.hex) for s in attributed.satins]
        + [line(s.satin.centre, "#e0007a", 0.06) for s in attributed.satins])
    body = [fill(f.geometry, f.thread.hex) for f in attributed.fills] + [satin(s.satin, s.thread.hex) for s in attributed.satins]
    if border is not None:
        body.append(satin(border.satin, "#000000"))
    doc("05_border.svg", body)
