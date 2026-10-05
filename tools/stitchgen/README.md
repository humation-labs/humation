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
stitchgen input.svg -o out/ [--size 60] [--border satin|outline|none] [--palette palette.json] [--config config.toml] [--debug]
```

Paths are relative to the mounted directory (`/work`). To convert several SVGs, loop over them in the shell.

| Output        | Contents                                                                                                                                                                                                        |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `design.pes`  | Brother embroidery data (primary output)                                                                                                                                                                        |
| `design.dst`  | Tajima DST for outside or multi-needle machines                                                                                                                                                                 |
| `preview.png` | Stitch simulation (short side 2000 px). Each stitch is drawn as a round, glossy thread that casts a soft shadow on the fabric; its sheen depends on its direction against a top-left light, as with real thread |
| `compare.png` | Colour-reduced artwork next to the simulation, for checking that the avatar is still recognisable                                                                                                               |
| `meta.json`   | Stitch count, thread order, colour changes, estimated time, size, warnings                                                                                                                                      |
| `debug/`      | Intermediate SVG of every step (`--debug` only). `07_inkstitch.svg` is the file sent to Ink/Stitch                                                                                                              |

Exit codes: `0` success, `1` success with warnings (also listed in `meta.json`), `2` conversion failed.

### meta.json

```json
{
  "input": "standard.svg",
  "stitch_count": 7528,
  "color_changes": 5,
  "color_order": [
    { "brother_number": "900", "name": "Black", "hex": "#000000" },
    "..."
  ],
  "estimated_minutes": 12.5,
  "size_mm": { "width": 35.6, "height": 62.6 },
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
2. **Reduce colours** (`colors.py`). Maps every colour to the nearest `palette.json` thread by CIEDE2000.
   Colour slots listed in `[colors.roles]` are pinned instead: skin is always White. Merges the least-used threads
   until at most `colors.max` (6) remain. Same-thread neighbours become one region.
3. **Outline** (`outline.py`). The silhouette, with notches narrower than 2.5 mm closed, holes dropped and
   floating parts (items) bridged.
4. **Border** (`border.py`). The avatar already has a drawn outer outline, so the border replaces it instead of
   adding a second edge. Two modes:
   - `--border satin` (default, patches). A 2.5 mm satin that reaches `border.inset_mm` (1.2 mm) inside the
     silhouette, just over the drawn line; the remaining 1.3 mm lies outside, and its outer edge is the heat-cut
     patch edge. A running stitch along its centre is sewn first, to position the fabric.
   - `--border outline` (direct embroidery on a garment). The outer line is sewn like every other line, a 1.1 mm
     satin entirely inside the silhouette, with no placement run and nothing added outside the artwork.

   In both modes, gaps in the hand-drawn line and the straight crop at the bottom are closed by the same satin.
   The artwork is cut back to the area inside the border before stitch types are chosen, so the drawn outer
   line is not sewn twice.

5. **Stitch attributes** (`attributes.py`, `satin.py`). Splits each region into thick and thin parts with a
   morphological opening of `thin.threshold_mm`.
   - Thick parts are filled with straight stitches of random length (Ink/Stitch's random stitch length),
     so no tatami brick pattern shows. Rows follow the motif, taken from the Humation colour slot that painted
     the part (`[fill.flow]`):

     | Motif   | Flow    | Rows                                                                                |
     | ------- | ------- | ----------------------------------------------------------------------------------- |
     | hair    | `arch`  | concentric arcs round the crown that fall straight down the sides (2.5 mm stitches) |
     | skin    | `wrap`  | bowed across the face like latitude lines on a ball (3 mm stitches)                 |
     | clothes | `drape` | hanging vertically with a slight bow (3.5 mm stitches)                              |

     These use Ink/Stitch guided fills with a generated guide line. The change of stitch direction between
     motifs catches the light differently, which is what gives embroidery its depth. Other parts (items,
     fixed colours) use straight rows, alternating 45°/135° between neighbours. If a guided fill ever fails
     in Ink/Stitch, the export is retried once with straight rows and a `flow_fallback` warning.

   - Thin parts of the line-art thread (black) are skeletonised into centrelines. Each becomes a satin column
     made of two rails. Each rail is offset by the line's own local half-width, with a minimum width of 1 mm.
     Satin width therefore follows the drawn line, ends round off like the brush stroke, and the inner rail is
     pulled in on tight bends so it never folds.
   - Compact marks up to 5 mm long, such as eyes and short dashes, use their own contour as rails. The contour is
     split at both ends of the long axis, so a round eye stays round.
   - Thin parts of other threads stay with their own fill.

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
| Detail of at least 1 mm at output size | Thin lines are widened to satin of 1 mm, and smaller specks are dropped. Both are recorded in `warnings`                                        |

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
- Protrusions narrower than about 3 mm (a cat's tail, flower petals) are mostly taken by the border. Fur-like
  dashes along the silhouette merge into it.
- The area between the avatar and the border in closed notches is not stitched, so the base fabric shows there.
  A background fill is a candidate for phase 1.5.
- Black hair merges with the black outline and border. An "embroidery style" with 4–6 colours designed on the
  Humation side is the real fix.

Phase 1.5 (after the machine arrives) is sewing tests:

- 2.5 mm vs 3 mm satin border, and the inset/outset split
- tatami density and pull compensation in `config.toml`
- heat-cut edge finish
