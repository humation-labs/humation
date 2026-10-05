# stitchgen

Turns a Humation avatar SVG into embroidery data for a patch: Brother PES, generic DST, a stitch
simulation and a `meta.json` that an ordering agent can read. Phase 1 of the Humation embroidery
pipeline. Machine transfer and Web UI integration are out of scope.

## Quick start

```bash
docker build -t humation-stitchgen tools/stitchgen
cd tools/stitchgen && docker run --rm -v "$PWD:/work" humation-stitchgen samples/standard.svg -o out/standard
open out/standard/compare.png
```

```
stitchgen input.svg -o out/ [--size 60] [--border satin|none] [--palette palette.json] [--config config.toml] [--debug]
```

Paths are relative to the mounted directory (`/work`). To convert several SVGs, loop over them in the shell.

| Output        | Contents                                                                                           |
| ------------- | -------------------------------------------------------------------------------------------------- |
| `design.pes`  | Brother embroidery data (primary output)                                                           |
| `design.dst`  | Tajima DST for outside or multi-needle machines                                                    |
| `preview.png` | Stitch simulation. The short side is 2000 px, so individual threads are visible                    |
| `compare.png` | Colour-reduced artwork next to the simulation, for checking that the avatar is still recognisable  |
| `meta.json`   | Stitch count, thread order, colour changes, estimated time, size, warnings                         |
| `debug/`      | Intermediate SVG of every step (`--debug` only). `07_inkstitch.svg` is the file sent to Ink/Stitch |

Exit codes: `0` success, `1` success with warnings (also listed in `meta.json`), `2` conversion failed.

### meta.json

```json
{
  "input": "standard.svg",
  "stitch_count": 8177,
  "color_changes": 5,
  "color_order": [
    { "brother_number": "900", "name": "Black", "hex": "#000000" },
    "..."
  ],
  "estimated_minutes": 13.6,
  "size_mm": { "width": 38.2, "height": 65.0 },
  "avatar_size_mm": { "width": 33.1389, "height": 60.0 },
  "border": "satin",
  "warnings": [
    {
      "code": "detail_dropped",
      "message": "1 part(s) smaller than 1.0 mm were dropped"
    }
  ]
}
```

`stitch_count` counts needle penetrations in the PES as pyembroidery reads it back. `estimated_minutes`
assumes a home machine at 600 stitches/min. `size_mm` is the finished patch including the satin border.
`avatar_size_mm` is the artwork alone.

## How it works

Ink/Stitch is used only as a converter from an attributed SVG to stitches. All decisions are made in Python
and written as `inkstitch:*` attributes, so the Ink/Stitch GUI is never involved.

1. **Normalize** (`normalize.py`). Resolves `var(--hm-*)` colours. Bakes transforms. Turns shapes, strokes and
   `fill-rule` into polygons. Crops to the SVG viewBox, because Humation draws whole bodies and the crop hides the
   rest. Drops a full-canvas background and scales the avatar to `--size` mm. Each element is then cut down to its
   visible part, so the layering is resolved once and fills never stack.
2. **Reduce colours** (`colors.py`). Maps every colour to the nearest `palette.json` thread by CIEDE2000. Merges
   the least-used threads until at most `colors.max` (6) remain. Same-thread neighbours become one region.
3. **Outline** (`outline.py`). The silhouette with notches narrower than 2.5 mm closed and holes dropped, offset
   2.5 mm outward. This is the patch edge.
4. **Stitch attributes** (`attributes.py`). Splits each region into thick and thin parts with a morphological
   opening of `thin.threshold_mm`.
   - Thick parts become tatami fills. Fills that touch get alternating angles (45°/135°), with underlay.
   - Thin parts of the line-art thread (black) are skeletonised into centrelines. They become satin lines
     1.5–2.5 mm wide with centre-walk underlay.
   - Thin parts of other threads stay with their own fill.
   - Small compact marks (eyes) become short satins.
5. **Border** (`border.py`). A 3 mm satin with zigzag and contour underlay, whose outer edge is the patch edge.
   A running stitch along the same path is sewn first, to position the fabric.
6. **Order** (`order.py`). Sewing order:
   1. placement run
   2. fills, by thread total area (largest first), with each thread's own thin satins
   3. line-art satins, chained nearest-first
   4. satin border

   Trims are added between same-thread elements that are more than 1 mm apart.

7. **Export** (`export.py`). Runs Ink/Stitch headless under `xvfb-run` for PES and DST, then reads both back with
   pyembroidery. Unreadable or empty output is an error.

Guardrails add a warning but still finish the conversion:

- more than 15,000 stitches
- more than 8 colour changes
- more than 10% of input elements dropped as smaller than 1 mm

Every number above lives in `config.toml`.

Conversion is deterministic. The same SVG and config give byte-identical `design.pes`, `design.dst` and `meta.json`.

## Input requirements

| Requirement                            | When not met                                                                                                                                    |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Filled paths                           | Strokes are expanded to polygons. Text is converted with Inkscape                                                                               |
| Flat colours                           | Gradients and patterns are replaced by one colour, with a warning                                                                               |
| No mask, filter or image               | The conversion stops with exit code 2. Simple user-space `clip-path`s (basic shapes, as in Figma exports such as the `jacket` part) are applied |
| Detail of at least 1 mm at output size | Thin lines are widened to satin of 1.5 mm or more, and smaller specks are dropped. Both are recorded in `warnings`                              |

Only the `humation-1` asset style is supported in phase 1.

## Palette

`palette.json` holds 10 fixed Brother embroidery threads.

- `hex` is the exact Brother PEC colour, so the PES keeps the thread unchanged.
- `brother_number` is the catalogue number used for ordering thread.
- **The numbers need checking against a physical Brother thread chart before production.**
- The set is chosen for Humation's defaults: black line art, white, skin, brown hair and a few clothing colours.
  Clothing colours outside it are mapped to the nearest thread, e.g. dark green becomes Gray.

## Development

```bash
cd tools/stitchgen
uv run --extra dev pytest                 # pipeline tests without Ink/Stitch
docker run --rm -v "$PWD:/src:ro" -w /src --entrypoint bash humation-stitchgen \
  -c "pip install -q pytest && python -m pytest -p no:cacheprovider"   # including end-to-end PES tests
bun tools/stitchgen/samples/generate.ts   # (from the repo root) regenerate the sample avatars
```

Ink/Stitch is pinned to v3.3.0 with the Linux build matching the host architecture (`INKSTITCH_VERSION` build
arg). It needs `<inkstitch:inkstitch_svg_version>4</…>` in the SVG, otherwise it opens an upgrade dialog and
hangs. `INKSTITCH_BIN` overrides the binary path.

## Known limitations

- The preview shows whether the data is valid. It does not predict how the thread will look, because it ignores
  pull and push, thread loft and fabric. Sewing tests decide final quality.
- The area between the avatar and the satin border is not stitched, so the base fabric shows in closed notches.
  A background fill is a candidate for phase 1.5.
- Black hair merges with the black outline and border. An "embroidery style" with 4–6 colours designed on the
  Humation side is the real fix.

Phase 1.5 (after the machine arrives) is sewing tests:

- 2.5 mm vs 3 mm satin border
- tatami density and pull compensation in `config.toml`
- heat-cut edge finish
