from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from shapely.geometry import Polygon, box

from stitchgen.attributes import assign_attributes
from stitchgen.colors import ColorRegion, ReducedDrawing, ciede2000, nearest, reduce_colors
from stitchgen.config import load_config, load_palette
from stitchgen.export import DEFAULT_INKSTITCH, Frame, inkstitch_svg
from stitchgen.normalize import normalize
from stitchgen.order import sewing_order
from stitchgen.border import make_border
from stitchgen.outline import silhouette
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


def test_silhouette_closes_narrow_notch_and_bridges_islands():
    notched = box(0, 0, 20, 20).difference(box(9.5, 10, 10.5, 20))  # 1 mm slot
    assert silhouette([notched], 2.5).contains(box(9.6, 12, 10.4, 19))
    shape = silhouette([box(0, 0, 10, 10), box(11, 0, 21, 10)], 2.5)  # item floating 1 mm away
    assert shape.contains(box(1, 1, 20, 9))


def test_border_covers_drawn_outline_and_reaches_outside():
    shape = box(0, 0, 30, 30)
    border = make_border(shape, CFG.border.width_mm, CFG.border.inset_mm)
    outside = CFG.border.width_mm - CFG.border.inset_mm
    assert border.outline.bounds == pytest.approx((-outside, -outside, 30 + outside, 30 + outside), abs=0.01)
    assert border.inner.bounds == pytest.approx((CFG.border.inset_mm,) * 2 + (30 - CFG.border.inset_mm,) * 2, abs=0.01)
    a, b = border.satin.rails
    assert len(a) == len(b) and a[0] == a[-1] and b[0] == b[-1]


def test_thin_ring_becomes_closed_satin_following_its_width():
    ring = box(0, 0, 30, 30).difference(box(1.2, 1.2, 28.8, 28.8))  # 1.2 mm outline
    face = box(1.2, 1.2, 28.8, 28.8)
    reduced = ReducedDrawing([ColorRegion(PALETTE[2], face), ColorRegion(BLACK, ring)], [])
    attributed = assign_attributes(reduced, CFG, line_art_hex=BLACK.hex)
    assert [f.thread.hex for f in attributed.fills] == [PALETTE[2].hex]
    assert len(attributed.satins) == 1
    satin = attributed.satins[0].satin
    assert satin.closed
    assert satin.width == pytest.approx(1.2, abs=0.15)
    assert len(satin.rails[0]) == len(satin.rails[1])


def test_round_dot_keeps_round_contour():
    from shapely.geometry import Point

    dot = Point(10, 10).buffer(0.6, quad_segs=16)  # 1.2 mm eye
    reduced = ReducedDrawing([ColorRegion(BLACK, dot)], [])
    satin = assign_attributes(reduced, CFG, line_art_hex=BLACK.hex).satins[0].satin
    a, b = satin.rails
    assert len(a) == len(b) >= 5
    # Rails are the two halves of the circle, so rung lengths shrink towards both ends.
    rungs = [((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 for (ax, ay), (bx, by) in zip(a, b)]
    assert max(rungs) == pytest.approx(1.2, abs=0.1) and rungs[0] < 0.2 and rungs[-1] < 0.2


def test_neighbouring_fills_alternate_angles():
    a, b = box(0, 0, 10, 10), box(11, 0, 21, 10)
    reduced = ReducedDrawing([ColorRegion(PALETTE[2], a), ColorRegion(PALETTE[3], b)], [])
    angles = {f.thread.hex: f.angle for f in assign_attributes(reduced, CFG).fills}
    assert sorted(angles.values()) == sorted(CFG.fill.angles)


def test_sewing_order_placement_fills_lines_border():
    ring = box(0, 0, 30, 30).difference(box(1, 1, 29, 29))
    inner_line = box(10, 5, 11, 25)
    black = ring.union(inner_line)
    reduced = ReducedDrawing([ColorRegion(PALETTE[2], box(1, 1, 29, 29).difference(inner_line)), ColorRegion(BLACK, black)], [])
    border = make_border(box(0, 0, 30, 30), CFG.border.width_mm, CFG.border.inset_mm)
    clipped = ReducedDrawing([ColorRegion(r.thread, r.geometry.intersection(border.inner)) for r in reduced.regions], [])
    attributed = assign_attributes(clipped, CFG, line_art_hex=BLACK.hex)
    items = sewing_order(attributed, border, BLACK)
    assert [i.role for i in items] == ["placement", "fill", "line", "border"]


def test_inkstitch_svg_is_deterministic_and_versioned():
    d = normalize(SAMPLES / "simple.svg", 60)
    reduced = reduce_colors(d, PALETTE, CFG.colors.max)
    shape = silhouette([r.geometry for r in reduced.regions], 2.5)

    def build() -> str:
        border = make_border(shape, CFG.border.width_mm, CFG.border.inset_mm)
        attributed = assign_attributes(reduced, CFG, line_art_hex=BLACK.hex, canvas=shape)
        items = sewing_order(attributed, border, BLACK)
        return inkstitch_svg(items, Frame.around(border.outline.bounds), CFG)

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
    assert meta["border"] == "outline" and meta["patch"] == "felt"
    assert 'id="cutline"' in (out / "cutline.svg").read_text()
    assert meta["cut_size_mm"]["height"] > meta["size_mm"]["height"]
    summary = summarize(pyembroidery.read(str(out / "design.pes")))
    assert summary.stitch_count == meta["stitch_count"]
    assert summary.color_changes == meta["color_changes"] == len(meta["color_order"]) - 1
    assert meta["stitch_count"] <= CFG.guardrails.max_stitches
    assert meta["color_changes"] <= CFG.guardrails.max_color_changes

    again = tmp_path / "again"
    main([str(SAMPLES / f"{name}.svg"), "-o", str(again)])
    assert (out / "design.pes").read_bytes() == (again / "design.pes").read_bytes()


# --- motif flows and role colours ---------------------------------------------------------------


def test_skin_role_is_stitched_white():
    d = normalize(SAMPLES / "standard.svg", 60)
    reduced = reduce_colors(d, PALETTE, CFG.colors.max, CFG.colors.roles)
    white = next(r for r in reduced.regions if r.thread.brother_number == "001")
    assert white.geometry.contains(reduced.roles["skin"].representative_point())


def test_arch_guide_runs_through_every_hair_part():
    from stitchgen.flow import guide_line

    crown = box(10, 0, 30, 40)
    for part in (box(10, 30, 13, 40), box(18, 0, 22, 3), box(27, 20, 30, 25)):
        guide, strategy = guide_line("arch", part, crown.bounds)
        assert guide.intersects(part) and strategy == 1


def test_fills_follow_motif_flows_in_inkstitch_svg():
    d = normalize(SAMPLES / "standard.svg", 60)
    reduced = reduce_colors(d, PALETTE, CFG.colors.max, CFG.colors.roles)
    shape = silhouette([r.geometry for r in reduced.regions], 2.5)
    attributed = assign_attributes(reduced, CFG, line_art_hex=BLACK.hex, canvas=shape)
    flows = {f.flow for f in attributed.fills}
    assert {"arch", "wrap", "drape"} <= flows
    svg = inkstitch_svg(sewing_order(attributed, None, BLACK), Frame.around(shape.bounds), CFG)
    assert 'inkstitch:fill_method="guided_fill"' in svg
    assert "marker-start:url(#inkstitch-guide-line-marker)" in svg
    assert 'inkstitch:enable_random_stitch_length="True"' in svg
    straight = inkstitch_svg(sewing_order(attributed, None, BLACK), Frame.around(shape.bounds), CFG, guided=False)
    assert "guided_fill" not in straight and "guide-line" not in straight


def test_outline_border_mode_is_thin_and_has_no_placement_run():
    shape = box(0, 0, 30, 30)
    border = make_border(shape, CFG.border.outline_width_mm, CFG.border.outline_width_mm)
    assert border.outline.bounds == pytest.approx((0, 0, 30, 30), abs=0.01)  # nothing added outside the artwork
    assert border.satin.width == CFG.border.outline_width_mm
    reduced = ReducedDrawing([ColorRegion(PALETTE[2], border.inner)], [])
    items = sewing_order(assign_attributes(reduced, CFG), border, BLACK, placement=False)
    assert [i.role for i in items] == ["fill", "border"]


@pytest.mark.inkstitch
@pytest.mark.skipif(not Path(os.environ.get("INKSTITCH_BIN", DEFAULT_INKSTITCH)).exists(), reason="Ink/Stitch not installed")
def test_end_to_end_direct_embroidery(tmp_path):
    from stitchgen.cli import main

    out = tmp_path / "out"
    assert main([str(SAMPLES / "standard.svg"), "-o", str(out), "--patch", "none"]) in (0, 1)
    meta = json.loads((out / "meta.json").read_text())
    assert meta["border"] == "outline" and meta["patch"] == "none" and meta["size_mm"]["height"] < 61.5
    assert not (out / "cutline.svg").exists()


def test_patch_adds_background_margin_and_edge():
    from stitchgen.border import make_patch

    art = box(0, 0, 20, 30)
    patch = make_patch(art, art, CFG.patch.margin_mm, CFG.patch.edge_width_mm, CFG.patch.smooth_mm, CFG.fill.overlap_mm)
    grow = CFG.patch.margin_mm + CFG.patch.edge_width_mm
    assert patch.outline.bounds == pytest.approx((-grow, -grow, 20 + grow, 30 + grow), abs=0.05)
    assert not patch.background.intersects(box(1, 1, 19, 29))  # artwork is not covered by background
    assert patch.background.contains(box(-1.5, 5, -0.5, 25))  # the margin is
    border = make_border(art, CFG.border.outline_width_mm, CFG.border.outline_width_mm)
    reduced = ReducedDrawing([ColorRegion(PALETTE[2], border.inner)], [])
    attributed = assign_attributes(reduced, CFG)
    white = next(p for p in PALETTE if p.brother_number == CFG.patch.background)
    attributed.fills.append(type(attributed.fills[0])(white, polygons_of(patch.background)[0], role="background"))
    items = sewing_order(attributed, border, BLACK, placement=False, patch=patch, patch_thread=white)
    assert [i.role for i in items][0] == "placement" and [i.role for i in items][-2:] == ["border", "edge"]


def polygons_of(geom):
    from stitchgen.geometry import polygons

    return polygons(geom)


def test_felt_cut_line_is_a_smooth_margin_round_the_artwork():
    from stitchgen.border import cut_contour

    art = box(0, 0, 20, 30).union(box(9, 30, 11, 34))  # a 2 mm stem on top
    cut = cut_contour(art, CFG.patch.cut_margin_mm, CFG.patch.smooth_mm)
    assert cut.contains(art.buffer(CFG.patch.cut_margin_mm - 0.05))
    assert cut.exterior.distance(art) == pytest.approx(CFG.patch.cut_margin_mm, abs=0.05)
    assert not cut.interiors


# --- satin for narrow parts of fills -----------------------------------------------------------------


def test_narrow_protrusion_becomes_satin_but_tapering_edge_stays_tatami():
    from stitchgen.attributes import _split_narrow

    body = box(0, 0, 20, 20)
    stem = box(9, 20, 12, 30)  # 3 mm wide, 10 mm long: meets the body at a 3 mm neck
    wide, narrow = _split_narrow(body.union(stem), CFG.fill.satin_max_width_mm)
    assert len(narrow) == 1 and narrow[0].intersection(stem).area > 0.9 * stem.area
    taper = Polygon([(0, 0), (20, 0), (20, 10), (0, 12), (0, 0)]).union(Polygon([(0, 12), (20, 10), (20, 14)]))
    wide, narrow = _split_narrow(taper, CFG.fill.satin_max_width_mm)
    assert not narrow  # a sliver along the side of a wide area is part of it


def test_leaf_shaped_fill_is_one_satin_between_its_own_sides():
    from shapely.geometry import Point

    leaf = Point(0, 0).buffer(1, quad_segs=16)
    leaf = Polygon([(x * 6, y * 2) for x, y in leaf.exterior.coords])  # 12 x 4 mm lens
    reduced = ReducedDrawing([ColorRegion(PALETTE[9], leaf)], [])
    attributed = assign_attributes(reduced, CFG)
    assert not attributed.fills and len(attributed.satins) == 1
    satin = attributed.satins[0].satin
    rungs = [((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 for (ax, ay), (bx, by) in zip(*satin.rails)]
    assert max(rungs) == pytest.approx(4.0, abs=0.2) and rungs[0] < 0.5 and rungs[-1] < 0.5  # pointed ends
    assert satin.centre.length == pytest.approx(12, abs=1.0)


def test_regular_fill_pattern_uses_tatami_stagger():
    import dataclasses

    cfg = dataclasses.replace(CFG, fill=dataclasses.replace(CFG.fill, pattern="regular"))
    reduced = ReducedDrawing([ColorRegion(PALETTE[2], box(0, 0, 20, 20))], [])
    svg = inkstitch_svg(sewing_order(assign_attributes(reduced, cfg), None, BLACK), Frame.around((0, 0, 20, 20)), cfg)
    assert 'inkstitch:staggers="4"' in svg and "enable_random_stitch_length" not in svg
