"""Step 1: flatten a Humation avatar SVG into coloured, visible polygons in millimetres."""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from lxml import etree
from PIL import ImageColor
from shapely import affinity
from shapely.geometry import LineString, Polygon, box
from shapely.geometry.base import BaseGeometry
from svgpathtools import CubicBezier, Line, QuadraticBezier, parse_path

from .geometry import clean, union
from .report import UnsupportedSvgError, Warning

SVG = "{http://www.w3.org/2000/svg}"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

# Elements whose children are definitions, never painted directly.
NON_RENDERED = {
    "defs", "clipPath", "mask", "linearGradient", "radialGradient", "pattern", "marker",
    "symbol", "metadata", "title", "desc", "style", "filter",
}
SHAPES = {"path", "rect", "circle", "ellipse", "polygon", "polyline", "line"}
UNSUPPORTED = {"image", "use", "foreignObject", "switch"}
INHERITED = {"fill", "stroke", "stroke-width", "fill-rule", "stroke-linejoin", "stroke-linecap", "visibility", "color"}


@dataclass
class Element:
    source_id: str
    color: str  # "#rrggbb"
    geometry: BaseGeometry  # visible region in output millimetres
    role: str | None = None  # Humation colour slot the paint came from (hair, skin, clothes, bottom, stroke)


@dataclass
class NormalizedDrawing:
    elements: list[Element]
    width_mm: float
    height_mm: float
    input_element_count: int
    warnings: list[Warning] = field(default_factory=list)


@dataclass
class _Raw:
    source_id: str
    color: str
    geometry: BaseGeometry  # in SVG user units
    role: str | None = None


def normalize(svg_path: str | Path, size_mm: float) -> NormalizedDrawing:
    svg_path = Path(svg_path)
    warnings: list[Warning] = []
    data = svg_path.read_bytes()
    root = _parse(data)
    if any(_local(el.tag) == "text" for el in root.iter(etree.Element)):
        data = _text_to_path(svg_path)
        root = _parse(data)
        warnings.append(Warning("text_converted", "text elements were converted to paths with Inkscape"))

    walker = _Walker(root, warnings)
    raw = walker.collect()

    viewport = walker.viewport_polygon()
    if viewport is not None:
        # Humation draws whole bodies and lets the crop viewBox hide the rest; embroider only what is shown.
        raw = [_Raw(r.source_id, r.color, g, r.role) for r in raw if not (g := clean(r.geometry.intersection(viewport), min_area=0)).is_empty]
    while raw and viewport is not None and _covers(raw[0].geometry, viewport):
        warnings.append(Warning("background_removed", f"dropped full-canvas background ({raw[0].source_id})"))
        raw.pop(0)
    if not raw:
        raise UnsupportedSvgError("the SVG contains nothing to embroider")

    minx, miny, maxx, maxy = union(r.geometry for r in raw).bounds
    scale = size_mm / max(maxx - minx, maxy - miny)
    scaled = [
        _Raw(r.source_id, r.color, clean(affinity.affine_transform(r.geometry, [scale, 0, 0, scale, -minx * scale, -miny * scale])), r.role)
        for r in raw
    ]

    # Painter's order: whatever is drawn later hides what lies beneath it.
    elements: list[Element] = []
    above: BaseGeometry = Polygon()
    for r in reversed(scaled):
        visible = clean(r.geometry.difference(above))
        if not visible.is_empty:
            elements.append(Element(r.source_id, r.color, visible, r.role))
        above = union([above, r.geometry])
    elements.reverse()

    return NormalizedDrawing(
        elements=elements,
        width_mm=round((maxx - minx) * scale, 4),
        height_mm=round((maxy - miny) * scale, 4),
        input_element_count=len(raw),
        warnings=warnings,
    )


def _parse(data: bytes) -> etree._Element:
    parser = etree.XMLParser(remove_comments=True, huge_tree=True, resolve_entities=False, no_network=True)
    return etree.fromstring(data, parser)


def _local(tag: object) -> str:
    return tag.split("}", 1)[-1] if isinstance(tag, str) else ""


def _text_to_path(svg_path: Path) -> bytes:
    inkscape = shutil.which("inkscape")
    if inkscape is None:
        raise UnsupportedSvgError("text elements need Inkscape (text→path), which is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "text.svg"
        subprocess.run(
            [inkscape, "--export-text-to-path", "--export-plain-svg", f"--export-filename={out}", str(svg_path)],
            check=True, capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
        )
        return out.read_bytes()


def _covers(geom: BaseGeometry, viewport: Polygon) -> bool:
    return geom.area >= viewport.area * 0.98 and geom.buffer(viewport.area ** 0.5 * 0.01).contains(viewport)


class _Walker:
    def __init__(self, root: etree._Element, warnings: list[Warning]):
        self.root = root
        self.warnings = warnings
        self.ids = {el.get("id"): el for el in root.iter(etree.Element) if el.get("id")}
        self.vars = _style_declarations(root.get("style", ""))
        self.raw: list[_Raw] = []
        self.counter = 0
        self.warned: set[str] = set()
        vb = [float(v) for v in re.split(r"[\s,]+", root.get("viewBox", "").strip()) if v] if root.get("viewBox") else []
        self.viewbox = vb if len(vb) == 4 else None
        # Sampling step in user units: ~1/600 of the canvas, i.e. ≈0.1 mm at a 60 mm output.
        extent = max(self.viewbox[2:]) if self.viewbox else _length(root.get("width"), 100.0)
        self.step = extent / 600.0

    def viewport_polygon(self) -> Polygon | None:
        if self.viewbox:
            x, y, w, h = self.viewbox
            return box(x, y, x + w, y + h)
        w, h = _length(self.root.get("width"), 0), _length(self.root.get("height"), 0)
        return box(0, 0, w, h) if w and h else None

    def collect(self) -> list[_Raw]:
        self._walk(self.root, np.identity(3), {"fill": "#000000", "stroke": "none", "stroke-width": "1", "fill-rule": "nonzero"}, [])
        return self.raw

    def _warn_once(self, code: str, message: str) -> None:
        if code not in self.warned:
            self.warned.add(code)
            self.warnings.append(Warning(code, message))

    def _walk(self, node: etree._Element, ctm: np.ndarray, inherited: dict[str, str], clips: list[BaseGeometry]) -> None:
        for child in node:
            if not isinstance(child.tag, str):
                continue
            tag = _local(child.tag)
            if tag in NON_RENDERED:
                continue
            if tag == "text":
                raise UnsupportedSvgError("text could not be converted to paths")
            if tag in UNSUPPORTED:
                raise UnsupportedSvgError(f"<{tag}> is not supported in phase 1 ({_describe(child)})")
            props = _properties(child)
            for prop in ("mask", "filter"):
                if props.get(prop, "none") != "none":
                    raise UnsupportedSvgError(f"{prop} is not supported in phase 1 ({_describe(child)})")
            if props.get("display") == "none":
                continue
            style = {**inherited, **{k: v for k, v in props.items() if k in INHERITED}}
            local = ctm @ _parse_transform(child.get("transform", ""))
            child_clips = clips
            if props.get("clip-path", "none") != "none":
                child_clips = [*clips, self._clip(props["clip-path"], local, child)]
            if tag in ("g", "svg", "a"):
                if tag == "svg" and child is not self.root:
                    raise UnsupportedSvgError("nested <svg> is not supported in phase 1")
                self._walk(child, local, style, child_clips)
            elif tag in SHAPES:
                self._shape(child, tag, local, style, props, child_clips)

    def _clip(self, ref: str, ctm: np.ndarray, node: etree._Element) -> BaseGeometry:
        clip = self.ids.get(_url_id(ref) or "")
        if clip is None or _local(clip.tag) != "clipPath":
            raise UnsupportedSvgError(f"clip-path {ref} could not be resolved ({_describe(node)})")
        if clip.get("clipPathUnits", "userSpaceOnUse") != "userSpaceOnUse" or clip.get("clip-path"):
            raise UnsupportedSvgError(f"only simple user-space clip paths are supported ({_describe(node)})")
        clip_ctm = ctm @ _parse_transform(clip.get("transform", ""))
        parts = []
        for shape in clip:
            tag = _local(shape.tag)
            if tag not in SHAPES - {"line", "polyline"}:
                raise UnsupportedSvgError(f"clip path {ref} contains <{tag}>; only basic shapes are supported")
            subpaths = self._subpaths(shape, tag, clip_ctm @ _parse_transform(shape.get("transform", "")))
            parts.append(_fill_polygon(subpaths, _properties(shape).get("clip-rule", "nonzero")))
        return union(parts)

    def _shape(self, node, tag, ctm, style, props, clips) -> None:
        self.counter += 1
        source_id = node.get("id") or f"{tag}#{self.counter}"
        if style.get("visibility") in ("hidden", "collapse"):
            return
        opacity = _float(props.get("opacity"), 1.0)
        subpaths = self._subpaths(node, tag, ctm)
        if not subpaths:
            return

        fill = self._color(style.get("fill", "#000000"), style, node, "fill")
        fill_opacity = opacity * _float(props.get("fill-opacity"), 1.0)
        if fill and fill_opacity > 0 and tag not in ("line", "polyline"):
            self._opacity_check(fill_opacity)
            self._add(source_id, fill, _fill_polygon(subpaths, style.get("fill-rule", "nonzero")), clips, _role(style.get("fill", "")))

        stroke = self._color(style.get("stroke", "none"), style, node, "stroke")
        stroke_opacity = opacity * _float(props.get("stroke-opacity"), 1.0)
        width = _float(style.get("stroke-width"), 1.0) * math.sqrt(abs(np.linalg.det(ctm[:2, :2])))
        if stroke and stroke_opacity > 0 and width > 0:
            self._opacity_check(stroke_opacity)
            self._add(f"{source_id}:stroke", stroke, _stroke_polygon(subpaths, width, style), clips, _role(style.get("stroke", "")))

    def _add(self, source_id: str, color: str, geometry: BaseGeometry, clips: list[BaseGeometry], role: str | None) -> None:
        for clip in clips:
            geometry = geometry.intersection(clip)
        geometry = clean(geometry, min_area=0)
        if not geometry.is_empty:
            self.raw.append(_Raw(source_id, color, geometry, role))

    def _opacity_check(self, opacity: float) -> None:
        if opacity < 1:
            self._warn_once("opacity_ignored", "partial opacity was treated as fully opaque")

    def _color(self, value: str, style: dict[str, str], node, prop: str) -> str | None:
        value = _resolve_vars(value.strip(), self.vars)
        if value in ("", "none", "transparent"):
            return None
        if value == "currentColor":
            value = _resolve_vars(style.get("color", "#000000"), self.vars)
        ref = _url_id(value)
        if ref is not None:
            return self._paint_server(ref, node, prop)
        color = _parse_color(value)
        if color is None:
            raise UnsupportedSvgError(f"unrecognised {prop} colour {value!r} ({_describe(node)})")
        return color

    def _paint_server(self, ref: str, node, prop: str) -> str:
        server = self.ids.get(ref)
        tag = _local(server.tag) if server is not None else ""
        if tag in ("linearGradient", "radialGradient"):
            stops = _gradient_stops(server, self.ids)
            if stops:
                color = _parse_color(_resolve_vars(stops[0], self.vars))
                if color:
                    self.warnings.append(Warning("gradient_flattened", f"{prop} gradient #{ref} replaced by its first stop {color} ({_describe(node)})"))
                    return color
        if tag == "pattern":
            self.warnings.append(Warning("pattern_flattened", f"{prop} pattern #{ref} replaced by #808080 ({_describe(node)})"))
            return "#808080"
        raise UnsupportedSvgError(f"paint server {ref!r} could not be resolved ({_describe(node)})")

    def _subpaths(self, node, tag: str, ctm: np.ndarray) -> list[tuple[np.ndarray, bool]]:
        """List of (points in user units, closed) after applying ctm."""
        scale = math.sqrt(abs(np.linalg.det(ctm[:2, :2]))) or 1.0
        step = self.step / scale  # local units per sample
        local: list[tuple[list[tuple[float, float]], bool]] = []
        if tag == "path":
            local = _sample_path(node.get("d", ""), step)
        elif tag == "rect":
            local = _rect(node, step)
        elif tag in ("circle", "ellipse"):
            cx, cy = _float(node.get("cx"), 0), _float(node.get("cy"), 0)
            rx = _float(node.get("r" if tag == "circle" else "rx"), 0)
            ry = _float(node.get("r" if tag == "circle" else "ry"), 0)
            if rx > 0 and ry > 0:
                local = [(_ellipse(cx, cy, rx, ry, step), True)]
        elif tag in ("polygon", "polyline"):
            nums = [float(v) for v in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?", node.get("points", ""))]
            pts = list(zip(nums[0::2], nums[1::2]))
            if len(pts) >= 2:
                local = [(pts, tag == "polygon")]
        elif tag == "line":
            local = [([(_float(node.get("x1"), 0), _float(node.get("y1"), 0)), (_float(node.get("x2"), 0), _float(node.get("y2"), 0))], False)]
        out = []
        for pts, closed in local:
            arr = np.asarray(pts, dtype=float)
            arr = arr @ ctm[:2, :2].T + ctm[:2, 2]
            out.append((arr, closed))
        return out


def _describe(node) -> str:
    tag = _local(node.tag)
    return f"<{tag} id={node.get('id')}>" if node.get("id") else f"<{tag}> at line {node.sourceline}"


def _role(paint: str) -> str | None:
    match = re.search(r"var\(\s*--hm-([\w-]+)", paint)
    return match.group(1) if match else None


def _style_declarations(style: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for decl in style.split(";"):
        if ":" in decl:
            key, value = decl.split(":", 1)
            out[key.strip()] = value.strip()
    return out


def _properties(node) -> dict[str, str]:
    props = {k: v for k, v in node.attrib.items() if isinstance(k, str) and "}" not in k}
    props.update(_style_declarations(node.get("style", "")))
    return props


def _resolve_vars(value: str, variables: dict[str, str]) -> str:
    for _ in range(8):
        match = re.search(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\))?[^()]*))?\)", value)
        if not match:
            return value
        replacement = variables.get(match.group(1), (match.group(2) or "").strip())
        value = value[: match.start()] + replacement + value[match.end():]
    return value


def _url_id(value: str) -> str | None:
    match = re.match(r"url\(\s*['\"]?#([^'\")]+)['\"]?\s*\)", value.strip())
    return match.group(1) if match else None


def _parse_color(value: str) -> str | None:
    value = value.strip().lower()
    if value in ImageColor.colormap:  # the full CSS colour keyword table
        r, g, b = ImageColor.getrgb(value)[:3]
        return f"#{r:02x}{g:02x}{b:02x}"
    if re.fullmatch(r"#[0-9a-f]{3}", value):
        return "#" + "".join(c * 2 for c in value[1:])
    if re.fullmatch(r"#[0-9a-f]{6}", value):
        return value
    if re.fullmatch(r"#[0-9a-f]{8}", value):
        return value[:7]
    match = re.fullmatch(r"rgba?\(([^)]*)\)", value)
    if match:
        parts = [p.strip() for p in re.split(r"[,\s/]+", match.group(1)) if p.strip()][:3]
        if len(parts) == 3:
            channels = [round(float(p[:-1]) * 2.55) if p.endswith("%") else round(float(p)) for p in parts]
            return "#" + "".join(f"{max(0, min(255, c)):02x}" for c in channels)
    return None


def _gradient_stops(gradient, ids: dict) -> list[str]:
    for _ in range(8):
        stops = [s for s in gradient if _local(s.tag) == "stop"]
        if stops:
            return [_properties(s).get("stop-color", "#000000") for s in stops]
        href = gradient.get(XLINK_HREF) or gradient.get("href")
        if not href or href[1:] not in ids:
            return []
        gradient = ids[href[1:]]
    return []


def _float(value: str | None, default: float) -> float:
    if value is None:
        return default
    match = re.match(r"\s*([-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?)", value)
    return float(match.group(1)) if match else default


def _length(value: str | None, default: float) -> float:
    return _float(value, default)


def _parse_transform(text: str) -> np.ndarray:
    matrix = np.identity(3)
    for name, args in re.findall(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)", text):
        v = [float(a) for a in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?", args)]
        if name == "matrix" and len(v) == 6:
            m = np.array([[v[0], v[2], v[4]], [v[1], v[3], v[5]], [0, 0, 1]])
        elif name == "translate":
            m = np.array([[1, 0, v[0]], [0, 1, v[1] if len(v) > 1 else 0], [0, 0, 1]])
        elif name == "scale":
            sx = v[0]
            sy = v[1] if len(v) > 1 else sx
            m = np.array([[sx, 0, 0], [0, sy, 0], [0, 0, 1]])
        elif name == "rotate":
            a = math.radians(v[0])
            m = np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])
            if len(v) == 3:
                cx, cy = v[1], v[2]
                m = np.array([[1, 0, cx], [0, 1, cy], [0, 0, 1]]) @ m @ np.array([[1, 0, -cx], [0, 1, -cy], [0, 0, 1]])
        elif name == "skewX":
            m = np.array([[1, math.tan(math.radians(v[0])), 0], [0, 1, 0], [0, 0, 1]])
        elif name == "skewY":
            m = np.array([[1, 0, 0], [math.tan(math.radians(v[0])), 1, 0], [0, 0, 1]])
        else:
            continue
        matrix = matrix @ m
    return matrix


def _sample_path(d: str, step: float) -> list[tuple[list[tuple[float, float]], bool]]:
    if not d.strip():
        return []
    path = parse_path(d)
    out = []
    for sub in path.continuous_subpaths():
        pts = [(sub[0].start.real, sub[0].start.imag)]
        for seg in sub:
            if isinstance(seg, Line):
                pts.append((seg.end.real, seg.end.imag))
                continue
            if isinstance(seg, (CubicBezier, QuadraticBezier)):
                ctrl = list(seg.bpoints())
                length = sum(abs(b - a) for a, b in zip(ctrl, ctrl[1:]))
            else:
                length = seg.length()
            n = max(2, math.ceil(length / step))
            for i in range(1, n + 1):
                p = seg.point(i / n)
                pts.append((p.real, p.imag))
        closed = abs(sub.start - sub.end) < 1e-9
        out.append((pts, closed))
    return out


def _ellipse(cx: float, cy: float, rx: float, ry: float, step: float) -> list[tuple[float, float]]:
    n = max(16, math.ceil(2 * math.pi * max(rx, ry) / step))
    return [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _rect(node, step: float) -> list[tuple[list[tuple[float, float]], bool]]:
    x, y = _float(node.get("x"), 0), _float(node.get("y"), 0)
    w, h = _float(node.get("width"), 0), _float(node.get("height"), 0)
    if w <= 0 or h <= 0:
        return []
    rx = _float(node.get("rx"), _float(node.get("ry"), 0))
    rx = min(rx, w / 2, h / 2)
    if rx <= 0:
        return [([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], True)]
    rounded = box(x + rx, y + rx, x + w - rx, y + h - rx).buffer(rx, quad_segs=max(4, math.ceil(math.pi * rx / 2 / step)))
    return [(list(rounded.exterior.coords)[:-1], True)]


def _ring(points: np.ndarray) -> Polygon | None:
    if len(points) < 3:
        return None
    poly = clean(Polygon(points), min_area=0)
    return poly if not poly.is_empty else None


def _signed_area(points: np.ndarray) -> float:
    x, y = points[:, 0], points[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _fill_polygon(subpaths: list[tuple[np.ndarray, bool]], rule: str) -> BaseGeometry:
    rings = [(poly, _signed_area(pts)) for pts, _ in subpaths if (poly := _ring(pts)) is not None]
    if not rings:
        return Polygon()
    if rule == "evenodd":
        result: BaseGeometry = Polygon()
        for poly, _ in rings:
            result = result.symmetric_difference(poly)
        return clean(result, min_area=0)
    # nonzero: walk rings from largest to smallest and toggle by winding number.
    rings.sort(key=lambda r: -r[0].area)
    result = Polygon()
    for i, (poly, area) in enumerate(rings):
        sign = 1 if area > 0 else -1
        probe = poly.representative_point()
        outside = sum((1 if a > 0 else -1) for p, a in rings[:i] if p.contains(probe))
        if outside == 0 and outside + sign != 0:
            result = result.union(poly)
        elif outside != 0 and outside + sign == 0:
            result = result.difference(poly)
    return clean(result, min_area=0)


def _stroke_polygon(subpaths: list[tuple[np.ndarray, bool]], width: float, style: dict[str, str]) -> BaseGeometry:
    cap = {"round": "round", "square": "square"}.get(style.get("stroke-linecap", "butt"), "flat")
    join = {"round": "round", "bevel": "bevel"}.get(style.get("stroke-linejoin", "miter"), "mitre")
    parts = []
    for pts, closed in subpaths:
        if len(pts) < 2:
            continue
        coords = [tuple(p) for p in pts]
        if closed and coords[0] != coords[-1]:
            coords.append(coords[0])
        parts.append(LineString(coords).buffer(width / 2, cap_style=cap, join_style=join, mitre_limit=4))
    return union(parts)
