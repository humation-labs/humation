from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from shapely.geometry import box

from stitchgen.attributes import assign_attributes
from stitchgen.colors import ColorRegion, ReducedDrawing, ciede2000, nearest, reduce_colors
from stitchgen.config import load_config, load_palette
from stitchgen.export import DEFAULT_INKSTITCH, Frame, inkstitch_svg
from stitchgen.normalize import normalize
from stitchgen.order import sewing_order
from stitchgen.border import make_border
from stitchgen.outline import patch_outline
from stitchgen.report import UnsupportedSvgError

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
CFG = load_config()
PALETTE = load_palette()
BLACK = next(p for p in PALETTE if p.brother_number == "900")


def svg(tmp_path: Path, body: str, attrs: str = 'viewBox="0 0 100 100"') -> Path:
    path = tmp_path / "in.svg"
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" {attrs}>{body}</svg>')
    return path


# --- step 1 -----------------------------------------------------------------------------------


def test_scales_bbox_to_size(tmp_path):
    d = normalize(svg(tmp_path, '<rect x="10" y="20" width="40" height="20" fill="#ff0000"/>'), 60)
    assert (d.width_mm, d.height_mm) == (60.0, 30.0)
    assert d.elements[0].geometry.bounds == pytest.approx((0, 0, 60, 30))


def test_bakes_nested_transforms(tmp_path):
    body = '<g transform="translate(10 0)"><g transform="scale(2)"><rect width="5" height="5" fill="#000"/></g></g><rect x="40" y="40" width="10" height="10" fill="#000"/>'
    d = normalize(svg(tmp_path, body), 50)
    # bbox (10,0)-(50,50) maps to (0,0)-(50,50); the scaled square is 10x10 at the origin.
    assert d.elements[0].geometry.bounds == pytest.approx((0, 0, 10, 10))


def test_evenodd_hole(tmp_path):
    d = normalize(svg(tmp_path, '<path fill-rule="evenodd" d="M0 0H10V10H0Z M3 3H7V7H3Z" fill="#000"/>'), 10)
    assert d.elements[0].geometry.area == pytest.approx(100 - 16, rel=1e-3)


def test_nonzero_opposite_winding_hole(tmp_path):
    d = normalize(svg(tmp_path, '<path d="M0 0H10V10H0Z M3 3V7H7V3Z" fill="#000"/>'), 10)
    assert d.elements[0].geometry.area == pytest.approx(84, rel=1e-3)


def test_occlusion_removes_covered_shapes(tmp_path):
    body = '<rect x="2" y="2" width="4" height="4" fill="#ff0000"/><rect width="10" height="10" fill="#0000ff"/>'
    d = normalize(svg(tmp_path, body, 'viewBox="-5 -5 20 20"'), 10)
    assert [e.color for e in d.elements] == ["#0000ff"]


def test_background_rect_removed(tmp_path):
    body = '<rect width="100" height="100" fill="#eee"/><circle cx="50" cy="50" r="20" fill="#000"/>'
    d = normalize(svg(tmp_path, body), 40)
    assert [w.code for w in d.warnings] == ["background_removed"]
    assert len(d.elements) == 1


def test_css_variables_resolved(tmp_path):
    body = '<rect width="10" height="10" fill="var(--hm-skin, #FFFFFF)"/><rect x="20" width="10" height="10" fill="var(--hm-missing, #ABCDEF)"/>'
    d = normalize(svg(tmp_path, body, 'viewBox="0 0 100 100" style="--hm-skin:#F6D7B8"'), 30)
    assert [e.color for e in d.elements] == ["#f6d7b8", "#abcdef"]


def test_simple_clip_path_is_applied(tmp_path):
    body = '<defs><clipPath id="c"><rect width="5" height="10"/></clipPath></defs><g clip-path="url(#c)"><rect width="10" height="10" fill="#000"/></g>'
    d = normalize(svg(tmp_path, body), 10)
    assert d.elements[0].geometry.bounds == pytest.approx((0, 0, 5, 10))


def test_content_outside_viewbox_is_cropped(tmp_path):
    d = normalize(svg(tmp_path, '<rect x="0" y="50" width="20" height="200" fill="#000"/>'), 50)
    assert (d.width_mm, d.height_mm) == (20.0, 50.0)


@pytest.mark.parametrize("body", [
    '<rect width="5" height="5" mask="url(#m)"/>',
    '<rect width="5" height="5" style="filter:url(#f)"/>',
    '<image href="x.png" width="5" height="5"/>',
])
def test_unsupported_features_fail(tmp_path, body):
    with pytest.raises(UnsupportedSvgError):
        normalize(svg(tmp_path, body), 10)


def test_gradient_flattened_with_warning(tmp_path):
    body = '<defs><linearGradient id="g"><stop offset="0" stop-color="#ff0000"/><stop offset="1" stop-color="#0000ff"/></linearGradient></defs><rect width="10" height="10" fill="url(#g)"/>'
    d = normalize(svg(tmp_path, body), 10)
    assert d.elements[0].color == "#ff0000"
    assert d.warnings[0].code == "gradient_flattened"


def test_stroke_becomes_polygon(tmp_path):
    body = '<rect width="1" height="1" fill="#000"/><path d="M0 50 H100" stroke="#000" stroke-width="4" fill="none"/>'
    d = normalize(svg(tmp_path, body), 100)
    assert d.elements[-1].geometry.area == pytest.approx(100 * 4, rel=0.02)


def test_samples_normalize():
    for name in ("simple", "standard", "complex"):
        d = normalize(SAMPLES / f"{name}.svg", 60)
        assert len(d.elements) > 10
        assert max(d.width_mm, d.height_mm) == pytest.approx(60, abs=0.01)


# --- step 2 -----------------------------------------------------------------------------------


def test_ciede2000_reference_pairs():
    # Sharma, Wu & Dalal (2005) test data, pairs 1 and 17.
    assert ciede2000((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485)) == pytest.approx(2.0425, abs=1e-4)
    assert ciede2000((50.0, 2.5, 0.0), (73.0, 25.0, -18.0)) == pytest.approx(27.1492, abs=1e-4)


def test_nearest_palette_colour():
    assert nearest("#111111", PALETTE).brother_number == "900"
    assert nearest("#fefefe", PALETTE).brother_number == "001"


def test_reduce_merges_down_to_max_colours():
    d = normalize(SAMPLES / "standard.svg", 60)
    reduced = reduce_colors(d, PALETTE, 3)
    assert len(reduced.regions) == 3
    assert any(w.code == "color_merged" for w in reduced.warnings)


# --- steps 3-6 --------------------------------------------------------------------------------


def test_outline_contains_avatar_with_offset():
    shape = box(0, 0, 20, 40)
    outline = patch_outline([shape], 2.5, 2.5)
    assert outline.contains(shape)
    assert outline.exterior.distance(shape) == pytest.approx(2.5, abs=0.05)


def test_outline_closes_narrow_notch():
    notched = box(0, 0, 20, 20).difference(box(9.5, 10, 10.5, 20))  # 1 mm slot
    assert patch_outline([notched], 2.5, 0.01).contains(box(9.6, 12, 10.4, 19))


def test_thin_region_becomes_satin_and_thick_becomes_fill():
    ring = box(0, 0, 30, 30).difference(box(1, 1, 29, 29))  # 1 mm outline
    face = box(1, 1, 29, 29)
    reduced = ReducedDrawing([ColorRegion(PALETTE[2], face), ColorRegion(BLACK, ring)], [])
    attributed = assign_attributes(reduced, CFG, line_art_hex=BLACK.hex)
    assert [f.thread.hex for f in attributed.fills] == [PALETTE[2].hex]
    assert len(attributed.satins) == 1
    satin = attributed.satins[0]
    assert satin.closed and satin.width == CFG.thin.satin_min_mm
    assert satin.line.length == pytest.approx(4 * 29, rel=0.05)


def test_neighbouring_fills_alternate_angles():
    a, b = box(0, 0, 10, 10), box(11, 0, 21, 10)
    reduced = ReducedDrawing([ColorRegion(PALETTE[2], a), ColorRegion(PALETTE[3], b)], [])
    angles = {f.thread.hex: f.angle for f in assign_attributes(reduced, CFG).fills}
    assert sorted(angles.values()) == sorted(CFG.fill.angles)


def test_sewing_order_placement_fills_lines_border():
    ring = box(0, 0, 30, 30).difference(box(1, 1, 29, 29))
    reduced = ReducedDrawing([ColorRegion(PALETTE[2], box(1, 1, 29, 29)), ColorRegion(BLACK, ring)], [])
    attributed = assign_attributes(reduced, CFG, line_art_hex=BLACK.hex)
    border = make_border(patch_outline([box(0, 0, 30, 30)], 2.5, 2.5), 3.0)
    items = sewing_order(attributed, border, BLACK)
    assert [i.role for i in items] == ["placement", "fill", "line", "border"]


def test_inkstitch_svg_is_deterministic_and_versioned():
    d = normalize(SAMPLES / "simple.svg", 60)
    reduced = reduce_colors(d, PALETTE, CFG.colors.max)
    outline = patch_outline([r.geometry for r in reduced.regions], 2.5, 2.5)

    def build() -> str:
        attributed = assign_attributes(reduced, CFG, line_art_hex=BLACK.hex)
        items = sewing_order(attributed, make_border(outline, 3.0), BLACK)
        return inkstitch_svg(items, Frame.around(outline.bounds), CFG)

    first = build()
    assert first == build()
    assert "<inkstitch:inkstitch_svg_version>4</inkstitch:inkstitch_svg_version>" in first


# --- end to end (Docker image only) -----------------------------------------------------------


@pytest.mark.inkstitch
@pytest.mark.skipif(not Path(os.environ.get("INKSTITCH_BIN", DEFAULT_INKSTITCH)).exists(), reason="Ink/Stitch not installed")
@pytest.mark.parametrize("name", ["simple", "standard", "complex"])
def test_end_to_end(tmp_path, name):
    import pyembroidery

    from stitchgen.cli import main
    from stitchgen.export import summarize

    out = tmp_path / "out"
    assert main([str(SAMPLES / f"{name}.svg"), "-o", str(out)]) in (0, 1)
    meta = json.loads((out / "meta.json").read_text())
    summary = summarize(pyembroidery.read(str(out / "design.pes")))
    assert summary.stitch_count == meta["stitch_count"]
    assert summary.color_changes == meta["color_changes"] == len(meta["color_order"]) - 1
    assert meta["stitch_count"] <= CFG.guardrails.max_stitches
    assert meta["color_changes"] <= CFG.guardrails.max_color_changes

    again = tmp_path / "again"
    main([str(SAMPLES / f"{name}.svg"), "-o", str(again)])
    assert (out / "design.pes").read_bytes() == (again / "design.pes").read_bytes()
