from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any
import json


@dataclass(frozen=True)
class SizeConfig:
    default_mm: float


@dataclass(frozen=True)
class ColorsConfig:
    max: int


@dataclass(frozen=True)
class DetailConfig:
    min_mm: float


@dataclass(frozen=True)
class FillConfig:
    angles: tuple[float, ...]
    row_spacing_mm: float
    max_stitch_length_mm: float
    underlay: bool
    overlap_mm: float


@dataclass(frozen=True)
class ThinConfig:
    threshold_mm: float
    satin_min_mm: float
    satin_max_mm: float
    zigzag_spacing_mm: float
    spur_min_mm: float
    simplify_mm: float


@dataclass(frozen=True)
class OutlineConfig:
    concavity_fill_mm: float


@dataclass(frozen=True)
class BorderConfig:
    offset_mm: float
    width_mm: float
    zigzag_spacing_mm: float
    color: str
    placement_stitch_length_mm: float


@dataclass(frozen=True)
class GuardrailsConfig:
    max_stitches: int
    max_color_changes: int
    max_dropped_ratio: float


@dataclass(frozen=True)
class ExportConfig:
    timeout_s: int
    stitches_per_minute: int


@dataclass(frozen=True)
class Config:
    size: SizeConfig
    colors: ColorsConfig
    detail: DetailConfig
    fill: FillConfig
    thin: ThinConfig
    outline: OutlineConfig
    border: BorderConfig
    guardrails: GuardrailsConfig
    export: ExportConfig


@dataclass(frozen=True)
class PaletteColor:
    brother_number: str
    name: str
    hex: str
    rgb: tuple[int, int, int]


def data_dir() -> Path:
    """Directory that holds config.toml / palette.json.

    Source checkout: tools/stitchgen/. Docker image: /app/.
    """
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent,
        Path("/app"),
        Path.cwd(),
    ]
    for candidate in candidates:
        if (candidate / "config.toml").is_file():
            return candidate
    return here.parent


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def load_config(path: str | Path | None = None) -> Config:
    config_path = Path(path) if path is not None else data_dir() / "config.toml"
    with config_path.open("rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)
    return Config(
        size=SizeConfig(default_mm=float(raw["size"]["default_mm"])),
        colors=ColorsConfig(max=int(raw["colors"]["max"])),
        detail=DetailConfig(min_mm=float(raw["detail"]["min_mm"])),
        fill=FillConfig(
            angles=tuple(float(x) for x in raw["fill"]["angles"]),
            row_spacing_mm=float(raw["fill"]["row_spacing_mm"]),
            max_stitch_length_mm=float(raw["fill"]["max_stitch_length_mm"]),
            underlay=bool(raw["fill"]["underlay"]),
            overlap_mm=float(raw["fill"]["overlap_mm"]),
        ),
        thin=ThinConfig(
            threshold_mm=float(raw["thin"]["threshold_mm"]),
            satin_min_mm=float(raw["thin"]["satin_min_mm"]),
            satin_max_mm=float(raw["thin"]["satin_max_mm"]),
            zigzag_spacing_mm=float(raw["thin"]["zigzag_spacing_mm"]),
            spur_min_mm=float(raw["thin"]["spur_min_mm"]),
            simplify_mm=float(raw["thin"]["simplify_mm"]),
        ),
        outline=OutlineConfig(concavity_fill_mm=float(raw["outline"]["concavity_fill_mm"])),
        border=BorderConfig(
            offset_mm=float(raw["border"]["offset_mm"]),
            width_mm=float(raw["border"]["width_mm"]),
            zigzag_spacing_mm=float(raw["border"]["zigzag_spacing_mm"]),
            color=str(raw["border"]["color"]),
            placement_stitch_length_mm=float(raw["border"]["placement_stitch_length_mm"]),
        ),
        guardrails=GuardrailsConfig(
            max_stitches=int(raw["guardrails"]["max_stitches"]),
            max_color_changes=int(raw["guardrails"]["max_color_changes"]),
            max_dropped_ratio=float(raw["guardrails"]["max_dropped_ratio"]),
        ),
        export=ExportConfig(
            timeout_s=int(raw["export"]["timeout_s"]),
            stitches_per_minute=int(raw["export"]["stitches_per_minute"]),
        ),
    )


def load_palette(path: str | Path | None = None) -> list[PaletteColor]:
    palette_path = Path(path) if path is not None else data_dir() / "palette.json"
    with palette_path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    colors: list[PaletteColor] = []
    for entry in raw["colors"]:
        hex_color = str(entry["hex"]).strip().lower()
        if not hex_color.startswith("#"):
            hex_color = "#" + hex_color
        colors.append(
            PaletteColor(
                brother_number=str(entry["brother_number"]),
                name=str(entry["name"]),
                hex=hex_color,
                rgb=_hex_to_rgb(hex_color),
            )
        )
    return colors
