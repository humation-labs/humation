from __future__ import annotations

from collections.abc import Iterable, Sequence

from shapely.geometry.base import BaseGeometry

INKSTITCH_NS = "http://inkstitch.org/namespace"
SVG_NS = "http://www.w3.org/2000/svg"


def fmt_coord(value: float) -> str:
    return f"{value:.4f}"


def _ring_to_d(coords: Sequence[Sequence[float]]) -> str:
    pts = [(float(x), float(y)) for x, y, *_ in coords]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if not pts:
        return ""
    parts = [f"M {fmt_coord(pts[0][0])} {fmt_coord(pts[0][1])}"]
    for x, y in pts[1:]:
        parts.append(f"L {fmt_coord(x)} {fmt_coord(y)}")
    parts.append("Z")
    return " ".join(parts)


def geom_to_path_d(geom: BaseGeometry) -> str:
    """Polygon/MultiPolygon → filled path d (evenodd); LineString → open path d."""
    if geom is None or geom.is_empty:
        return ""
    kind = geom.geom_type
    if kind == "Polygon":
        rings = [_ring_to_d(geom.exterior.coords)]
        for interior in geom.interiors:
            rings.append(_ring_to_d(interior.coords))
        return " ".join(r for r in rings if r)
    if kind == "MultiPolygon":
        return " ".join(geom_to_path_d(poly) for poly in geom.geoms if not poly.is_empty)
    if kind == "GeometryCollection":
        return " ".join(
            geom_to_path_d(part)
            for part in geom.geoms
            if not part.is_empty
        )
    if kind in ("LineString", "LinearRing"):
        coords = list(geom.coords)
        if not coords:
            return ""
        parts = [f"M {fmt_coord(coords[0][0])} {fmt_coord(coords[0][1])}"]
        for x, y in coords[1:]:
            parts.append(f"L {fmt_coord(x)} {fmt_coord(y)}")
        return " ".join(parts)
    if kind == "MultiLineString":
        return " ".join(geom_to_path_d(line) for line in geom.geoms if not line.is_empty)
    return ""


def svg_document(width_mm: float, height_mm: float, body: Iterable[str]) -> str:
    w = fmt_coord(width_mm)
    h = fmt_coord(height_mm)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="{SVG_NS}" xmlns:inkstitch="{INKSTITCH_NS}" '
            f'width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}">'
        ),
        "  <metadata>",
        "    <inkstitch:inkstitch_svg_version>4</inkstitch:inkstitch_svg_version>",
        "  </metadata>",
    ]
    for item in body:
        for line in str(item).splitlines() or [""]:
            lines.append(f"  {line}")
    lines.append("</svg>")
    lines.append("")
    return "\n".join(lines)


def filled_path_element(
    d: str,
    fill: str,
    *,
    element_id: str | None = None,
    fill_rule: str = "evenodd",
) -> str:
    attrs = []
    if element_id:
        attrs.append(f'id="{_escape_attr(element_id)}"')
    attrs.append(f'd="{_escape_attr(d)}"')
    attrs.append(f'fill="{_escape_attr(fill)}"')
    attrs.append(f'fill-rule="{_escape_attr(fill_rule)}"')
    attrs.append('stroke="none"')
    return f'<path {" ".join(attrs)}/>'


def _escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
