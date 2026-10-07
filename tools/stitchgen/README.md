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
stitchgen input.svg -o out/ [--size 60] [--border outline|satin|none] [--patch felt|stitched|none] [--fill-pattern random|regular] [--palette palette.json] [--config config.toml] [--debug]
```

Paths are relative to the mounted directory (`/work`). To convert several SVGs, loop over them in the shell.

| Output        | Contents                                                                                                                                                                                                        |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `design.pes`  | Brother embroidery data (primary output)                                                                                                                                                                        |
| `design.dst`  | Tajima DST for outside or multi-needle machines                                                                                                                                                                 |
| `preview.png` | Stitch simulation (short side 2000 px). Each stitch is drawn as a round, glossy thread that casts a soft shadow on the fabric; its sheen depends on its direction against a top-left light, as with real thread |
| `compare.png` | Colour-reduced artwork next to the simulation, for checking that the avatar is still recognisable                                                                                                               |
| `cutline.svg` | Cut line for the felt patch (`--patch felt`): a red hairline path in mm, for scissors, a cutting plotter or a laser                                                                                             |
| `meta.json`   | Stitch count, thread order, colour changes, estimated time, size, warnings                                                                                                                                      |
| `debug/`      | Intermediate SVG of every step (`--debug` only). `07_inkstitch.svg` is the file sent to Ink/Stitch                                                                                                              |

Exit codes: `0` success, `1` success with warnings (also listed in `meta.json`), `2` conversion failed.

### meta.json

```json
{
  "input": "standard.svg",
  "stitch_count": 7343,
  "color_changes": 5,
  "color_order": [
    { "brother_number": "900", "name": "Black", "hex": "#000000" },
    "..."
  ],
  "estimated_minutes": 12.2,
  "size_mm": { "width": 33.4, "height": 60.2 },
  "avatar_size_mm": { "width": 33.1389, "height": 60.0 },
  "border": "outline",
  "patch": "felt",
  "cut_size_mm": { "width": 36.1, "height": 63.0 },
  "warnings": [
    {
      "code": "detail_dropped",
      "message": "1 part(s) smaller than 1.0 mm were dropped"
    }
  ]
}
```

`stitch_count` counts needle penetrations in the PES as pyembroidery reads it back. `estimated_minutes`
assumes a home machine at 600 stitches/min. `size_mm` is the finished patch including the edge.
`avatar_size_mm` is the artwork alone.

## How it works

Ink/Stitch is used only as a converter from an attributed SVG to stitches. All decisions are made in Python
and written as `inkstitch:*` attributes, so the Ink/Stitch GUI is never involved.

1. **Normalize** (`normalize.py`). Resolves `var(--hm-*)` colours. Bakes transforms. Turns shapes, strokes and
   `fill-rule` into polygons. Crops to the SVG viewBox, because Humation draws whole bodies and the crop hides the
   rest. Drops a full-canvas background and scales the avatar to `--size` mm. Elements painted with Humation's line
   colour (`--hm-stroke`) are the **line layer**. Everything else is the **fill layer**.
   - Fills are cut down by the fills drawn over them, so they never stack. They are not cut by line art: a fill
     runs on underneath every line drawn over it, and the line, sewn later, covers the seam (fill first, satin
     over the edges, as in hand digitising). Only the solid core of a line-art shape wider than
     `thin.threshold_mm` stops a fill, with a 0.3 mm tuck under its edge.
   - Line art is cut down by everything drawn over it, so it shows exactly what the artwork shows.
2. **Reduce colours** (`colors.py`). Maps every colour to the nearest `palette.json` thread by CIEDE2000.
   Colour slots listed in `[colors.roles]` are pinned instead: skin is always White. Merges the least-used threads
   until at most `colors.max` (6) remain. Same-thread neighbours become one region.
3. **Outline** (`outline.py`). The silhouette, with notches narrower than 2.5 mm closed, holes dropped and
   floating parts (items) bridged.
4. **Outer line and patch** (`border.py`). The avatar already has a drawn outer outline. The border replaces
   it instead of adding a second edge.
   - `--border outline` (default). The drawn outer line is kept as line art and sewn like every other line, at its
     own width. Nothing is added: where the artwork leaves the edge open (the straight crop at the bottom, an
     undrawn gap) the embroidery leaves it open too. `border.bridge_max_mm` can bridge short gaps if wanted.
   - `--patch felt` (default). Embroider on white felt and cut it out along `cutline.svg`, a smooth contour
     1.5 mm outside the outer line. Felt does not fray, and the artwork's own outer line finishes the edge, so
     nothing is stitched around the artwork and no placement run is needed. `preview.png` shows the cut-out felt.
   - `--patch stitched` is for fabric that frays (twill). It builds a sticker-style patch around the artwork.
     `[patch]` sets the margin, edge width and colours.
     - It has a smooth contour 2 mm outside the outer line.
     - The space between is filled with a White background, which also fills unpainted notches inside the
       silhouette.
     - It is finished with a 2.5 mm White satin edge for heat cutting.
     - A running stitch along the edge is sewn first, to position the fabric.
   - `--patch none` is direct embroidery on a garment.
   - `--border satin` is the older patch style: a 2.5 mm black satin that covers the drawn outer line, and is
     itself the cut edge.

   The artwork is cut back to the area inside the outer line before stitch types are chosen, so the drawn outer
   line is not sewn twice.

5. **Stitch attributes** (`attributes.py`, `satin.py`). Splits each region into thick and thin parts with a
   morphological opening of `thin.threshold_mm`.
   - Thick parts are tatami. Rows follow the motif, taken from the Humation colour slot that painted the part
     (`[fill.flow]`):

     | Motif   | Flow    | Rows                                                                                |
     | ------- | ------- | ----------------------------------------------------------------------------------- |
     | hair    | `arch`  | concentric arcs round the crown that fall straight down the sides (2.5 mm stitches) |
     | skin    | `wrap`  | bowed across the face like latitude lines on a ball (3 mm stitches)                 |
     | clothes | `drape` | hanging vertically with a slight bow (3.5 mm stitches)                              |

     These use Ink/Stitch guided fills with a generated guide line. Each motif has its own stitch direction, so
     each catches the light differently, which gives the embroidery its depth. Other parts (items, fixed
     colours) use straight rows, alternating 45°/135° between neighbours. If a guided fill ever fails in
     Ink/Stitch, the export is retried once with straight rows and a `flow_fallback` warning.

   - `--fill-pattern` sets the needle points:
     - `random` (default) randomises stitch lengths, so no needle-point pattern shows.
     - `regular` is classic tatami: needle points step along and repeat every 4 rows.
   - Parts of a fill narrower than `fill.satin_max_width_mm` (6 mm) are satin instead of tatami, the usual
     digitising rule. This covers petals, leaves, stems, strands of hair and collars.
     - A part that branches nowhere is one satin between its own two sides.
     - Tapering edges and rounded-off corners that meet the wide area along a long seam stay tatami.
   - Line-layer parts are skeletonised into centrelines. Each becomes a satin column made of two rails.
     - Each rail follows its own drawn edge, measured on a raster, so a line that bulges on one side keeps the
       bulge. Width is at least 1 mm.
     - A free end runs on to the drawn tip and is rounded like the brush stroke.
     - An end at a junction is cut flat and tucked 0.3 mm under the line it meets, so lines do not pile up at
       joins. Where no line passes through the junction, the end stays rounded so no hole opens.
     - An end cut by the crop is clipped flush to it.
     - The inner rail is pulled in on tight bends so it never folds.
     - Separate strokes drawn closer than `thin.min_gap_mm` (0.35 mm, a double collar) are thinned on the facing
       side, so a gap stays open between the needles.
     - Skeleton branches count by how far their drawn tip sticks out past the stroke (`thin.spur_reach_mm`), so
       hooks, curl tips and toes stay and only noise inside thick strokes goes.
   - Compact marks up to 5 mm long (eyes, short dashes) are sewn as their own recipe:
     - rails are chords cut across the mark, so every stitch is a full row on the drawn outline;
     - the first and last rows lie half a thread inside the tips, and the end rows are shortened a little;
     - a nearly round mark is always horizontal, and every mark is sewn in the same direction, so the two eyes
       of a face come out alike;
     - dense (0.25 mm), no underlay, no pull compensation, no short stitches;
     - a short lead-in run from the middle carries the tie-in under the rows.
   - Line art finer than `thin.running_max_mm` (0.7 mm), whether a line or a mark, is a triple running stitch
     (bean stitch) rather than a satin widened to 1 mm.
   - Every satin keeps the start and end stitchgen chose (Ink/Stitch's nearest-point start and end are off: on a
     small satin they split it and leave travel and tie stitches on top).
   - Pull compensation is 0.05 mm on lines (their rails already sit on the drawn edge) and 0.15 mm on wide
     satins. Short stitches on tight curves are mild (15 %), so they do not notch the visible edge.
   - Fill underlay is inset 0.3 mm so it never shows at the edges.
   - Thin parts of fill-layer regions are one satin between their own sides when that fits cleanly, and tatami
     otherwise. A fill that carries several motifs of one thread (White skin and a white tee) is split by motif,
     so each gets its own flow.
   - Underlap: a fill reaches 0.3 mm under whatever of the fill layer is sewn after it. Against things sewn
     before it, and against felt, it keeps its drawn edge.

6. **Order** (`order.py`). Sewing order:
   1. placement run
   2. fill layer, thread by thread by total area (largest first), each with its own narrow satins, then the fill
      layer's small marks (catchlights, polka dots), so no later fill buries them. The stitched
      patch background is included here. The placement thread goes first, and a fill in the line colour (black
      hair) goes last so it runs straight into the line layer.
   3. line layer (outlines, eyes, details), chained nearest-first. It is sewn over the fills, as it is drawn
      over them.
   4. outer line
   5. stitched-patch edge

   Trims are added between same-thread elements that are more than 1 mm apart.

7. **Export** (`export.py`). Runs Ink/Stitch headless under `xvfb-run` for PES and DST, then reads both back with
   pyembroidery. Unreadable or empty output is an error.

Guardrails add a warning but still finish the conversion:

- more than 15,000 stitches
- more than 8 colour changes
- more than 10% of input elements dropped as smaller than 1 mm

Every number above lives in `config.toml`.

stitchgen's own output (`debug/07_inkstitch.svg`, `meta.json`) is byte-identical for the same SVG and config. Ink/Stitch's
guided fill is not always bit-stable between processes: a few fill stitches can land one PES unit (0.1 mm) apart
between runs. Satins and straight fills are identical.

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
- With `--border satin`, protrusions narrower than about 3 mm (a cat's tail, flower petals) are mostly taken by the border. Fur-like
  dashes along the silhouette merge into it.
- Black hair merges with the black outline and border. An "embroidery style" with 4–6 colours designed on the
  Humation side is the real fix.

Phase 1.5 (after the machine arrives) is sewing tests:

- 2.5 mm vs 3 mm satin border, and the inset/outset split
- tatami density and pull compensation in `config.toml`
- heat-cut edge finish
