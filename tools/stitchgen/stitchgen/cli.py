"""stitchgen input.svg -o out/ [--size 60] [--border satin|none] [--palette palette.json] [--debug]"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import tempfile
import traceback
from pathlib import Path

from shapely.geometry import LineString, Polygon

from .attributes import FillPart, assign_attributes
from .border import cut_contour, make_border, make_patch
from .colors import ColorRegion, ReducedDrawing, reduce_colors
from .config import load_config, load_palette
from .export import Frame, inkstitch_svg, read_back, run_inkstitch, summarize, thread_for
from .geometry import clean, polygons, union
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
    p.add_argument("--border", choices=["outline", "satin", "none"], default="outline",
                   help="outline: thin outer line (default); satin: older patch style with a wide outer edge")
    p.add_argument("--patch", choices=["felt", "stitched", "none"], default="felt",
                   help="felt: embroider on felt and cut along cutline.svg (default); stitched: background + satin edge; none: direct embroidery")
    p.add_argument("--fill-pattern", choices=["random", "regular"], default=None,
                   help="random: random stitch lengths (default from config.toml); regular: classic tatami stagger")
    p.add_argument("--palette", type=Path, default=None)
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--debug", action="store_true", help="keep intermediate SVGs in out/debug/")
    return p


def run(args: argparse.Namespace) -> list[Warning]:
    if not args.input.is_file():
        raise StitchgenError(f"input not found: {args.input}")
    cfg = load_config(args.config)
    if args.fill_pattern:
        cfg = dataclasses.replace(cfg, fill=dataclasses.replace(cfg.fill, pattern=args.fill_pattern))
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
    # 5b. patch: background margin and heat-cut edge around the artwork (the satin border already is one)
    patch = patch_thread = cut = None
    if args.patch == "felt" and args.border != "satin":
        cut = cut_contour(shape, cfg.patch.cut_margin_mm, cfg.patch.smooth_mm)
    if args.patch == "stitched" and args.border != "satin":
        threads = {p.brother_number: p for p in palette}
        background_thread, patch_thread = threads.get(cfg.patch.background), threads.get(cfg.patch.edge)
        if background_thread is None or patch_thread is None:
            raise StitchgenError("patch background/edge colours must be palette brother_numbers")
        painted = union([*(r.geometry for r in stitched.regions), *([border.outline] if border else [])])
        patch = make_patch(painted, shape, cfg.patch.margin_mm, cfg.patch.edge_width_mm, cfg.patch.smooth_mm, cfg.fill.overlap_mm)
        for part in polygons(patch.background):
            if part.area >= cfg.detail.min_mm ** 2:
                attributed.fills.append(FillPart(background_thread, part, angle=cfg.fill.angles[0], role="background"))
    # 6. order
    items = sewing_order(attributed, border, border_thread, placement=args.border == "satin", patch=patch, patch_thread=patch_thread)

    edge = patch.outline if patch else cut if cut is not None else border.outline if border else shape
    frame = Frame.around(edge.bounds, margin=3.0 if cut is not None else 1.0)  # room for the felt's shadow
    if debug:
        _write_debug(debug, frame, drawing, reduced, shape, attributed, border, patch, cut)

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
    if cut is not None:
        (out / "cutline.svg").write_text(svg_document(frame.width, frame.height, [_cut_path(frame.place(cut))]), encoding="utf-8")
    felt = (frame.place(cut), cfg.patch.felt) if cut is not None else None
    preview = render_preview(pattern, frame, (minx, miny), out / "preview.png", felt=felt)
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
        "fill_pattern": cfg.fill.pattern,
        "patch": args.patch if args.border != "satin" else "satin-edge",
        **({"cut_size_mm": {"width": round(cut.bounds[2] - cut.bounds[0], 1), "height": round(cut.bounds[3] - cut.bounds[1], 1)}} if cut is not None else {}),
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


def _cut_path(cut) -> str:
    # Hairline red stroke: the usual convention for "cut here" in plotter and laser software.
    return f'<path id="cutline" d="{geom_to_path_d(cut.exterior)}" fill="none" stroke="#ff0000" stroke-width="0.1"/>'


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


def _write_debug(debug: Path, frame: Frame, drawing, reduced, shape, attributed, border, patch=None, cut=None) -> None:
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
    if patch is not None:
        body.append(satin(patch.edge, "#9a9a9a"))
    if cut is not None:
        body.append(line(cut.exterior, "#ff0000", 0.1))
    doc("05_border.svg", body)
