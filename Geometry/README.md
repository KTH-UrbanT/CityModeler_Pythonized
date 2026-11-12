# CMP building model workflow

CMP turns 2D building footprints into flat-roof 3D sections using terrain elevations and LAS/LAZ point-cloud heights. The second script simplifies sections and creates wall outputs. It follows the connected FME workflow described in `WORKFLOW_MAPPING.md`, but Python geometry engines can give different coordinates, section splits, and heights from FME.

## Files

| File | Purpose |
| --- | --- |
| `inputs/config.yml` | Source choice, UUIDs, paths, and processing thresholds. Edit this first. |
| `inputs/building_template.geojson` | One example input building with a 2D footprint and properties; used only in template mode. |
| `code/generate_baseline.py` | Reads source data and creates baseline 3D geometry and diagnostic/handoff files. Run first. |
| `code/simplify_model.py` | Reads the baseline handoff, simplifies geometry, cleans small walls, and exports final buildings and walls. Run second. |
| `code/plot_model.py` | Reads an existing 3D GeoJSON and creates an interactive HTML plot. Optional. |
| `code/model_common.py` | Shared config, geometry, extrusion, JSON, and validation functions; do not run directly. |
| `requirements.txt` | Python packages needed to install and run CMP. |
| `WORKFLOW_MAPPING.md` | Detailed FME transformer-to-Python map and known fidelity limits. Some historical diagnostic paths refer to archived files. |
| `source_audit/` | Extracted FME workspace text and transformer inventory used for inspection; not runtime inputs. |
| `../venv/` | Shared Python environment at the repository root. |
| `output/` | Generated files; contents reflect the last run, which may use an older config. |

## Inputs and configuration

All paths in `inputs/config.yml` are relative to that file. The workflow expects **EPSG:3006** coordinates and metre units.

- `source: database`: read 2D footprint WKB and attributes from the prepared SQLite `Buildings` table. The `Zones` table supplies the requested DeSO areas and their neighbouring areas. The database is read only.
- `source: template`: read GeoJSON Feature or FeatureCollection from `paths.template`. Every UUID in `building_uuids` must be present as `properties["50A_UUID"]`. The included template has one example. A 3D GeometryCollection input is also accepted: CMP projects its horizontal bottom faces to 2D and **recalculates** heights from the configured terrain and cloud. Only features in the template can provide nearby-building context.
- `paths.point_cloud`: LAS/LAZ surface point tiles. CMP uses XYZ samples within each footprint. The LAS header or an adjacent JSON must identify EPSG:3006. The code does not filter by LAS classification or return number.
- `paths.terrain`: ground-elevation GeoTIFF tiles. Valid raster cells whose centres lie within the sampling area provide ground samples. Metadata/density files are skipped; the last valid value wins where tiles overlap.
- `paths.fme_directory`: location of the three FME workspaces whose hashes are recorded in the baseline handoff. Python does **not** execute FME.
- `paths.output`: where results are written. `building_uuids` selects one or more final buildings; multiple UUIDs go into **one GeoJSON file per output stage**. Database mode also loads surrounding context buildings for spatial operations.

The `baseline`, `simplification`, and `walls` sections of `inputs/config.yml` contain the numeric thresholds below. Change values there, not inside the scripts.

## Run

```sh
cd /Users/alizad/Desktop/FME/CMP
../venv/bin/python code/generate_baseline.py
../venv/bin/python code/simplify_model.py
```

If needed, create the shared environment from the repository root with `python3 -m venv venv` and install both workflows with `venv/bin/python -m pip install -r requirements.txt`. Both generators accept `--config /path/to/config.yml`. For **one** UUID override, pass `--uuid UUID` to **both** commands. For several, list them in `inputs/config.yml` and omit `--uuid`. Re-run baseline after changing the source, selected UUIDs, template, source paths, or baseline settings. Simplification checks this handoff and reads `output/baseline_context.json`, **not** `output/baseline.geojson`.

## Baseline: from data to 3D

1. **Load footprints and attributes.** Database mode loads the requested buildings plus their DeSO and neighbour-area context. Template mode loads the requested GeoJSON features. Invalid polygon geometry is repaired where possible. Building properties are carried into the output.
2. **Gather point and terrain samples.** Intersecting LAS/LAZ tiles are streamed in chunks and XYZ points are assigned to footprints. Terrain raster windows are sampled at valid cell centres. A building without both sources in its sampling area is rejected and recorded in `baseline_report.json`.
3. **Find initial ground and roof elevations.** Inset the footprint by **0.5 m**. Ground is the **median terrain elevation**, rounded to 0.01 m. Roof is the **median raw cloud Z** inside that inset. Height is roof minus ground; buildings with height at or below **3 m** are rejected. These are flat section elevations: CMP does not preserve a sloping roof surface.
4. **Find roof-height breaks.** Keep cloud points within a second **0.5 m** inset and more than **2 m** above ground. Retain the central **5–95%** Z range. Triangulate the points, select steep triangles by surface normal, exclude candidates near the footprint edge, buffer/dissolve them, and drop regions smaller than **0.75 m²**. CMP uses SciPy Delaunay and handles tied percentile points differently from FME.
5. **Cut and regroup the footprint.** Make a centreline through each retained steep region using its minimum rotated rectangle; connect its ends to nearby footprint edges within **20 m**; simplify/filter the lines and cut the polygon. A **0.5 m** batch snap and area overlay help recover sections. Pieces **≤50 m²** or **<10%** of the original footprint are assigned to nearby retained pieces where possible. Courtyard holes are excluded.
6. **Recalculate cut-section heights.** Sample each cut piece again with the **0.5 m** inset: median terrain for ground and median original cloud Z for roof. Untouched footprints retain their initial heights. Sections without coverage are omitted and reported.
7. **Extrude sections.** For each valid polygon, create a bottom face at ground Z, a top face at ground + height, and vertical walls along every outer or hole edge. Validate that the shell is closed. One selected UUID becomes **one Feature**, with a `GeometryCollection` containing one `MultiPolygon` shell per block. `properties.sections` stores area, ground, roof, and height for each block. Each XYZ triple is formatted on one JSON row.

`baseline_stages.geojson` is **diagnostic 2D geometry**, not the final model. Its `stage` values are `steep_region` (candidate height-break area), `cutting_line` (proposed division), and `cut_polygon` (pieces immediately after cutting). Later overlay, regrouping, height sampling, and extrusion may change or discard these pieces.

## Simplification and wall steps

1. **Filter and align elevations.** Drop sections with height **≤3 m**. If a building's roof-elevation range is **<3 m**, set its sections to the group's maximum roof, minimum ground, and maximum height. Those values are chosen independently, so the `Roofheight` property can differ from the actual extruded top, which is ground + height. Other buildings keep their per-section elevations.
2. **Dissolve and simplify 2D footprints.** Merge connected sections with the same roof elevation, UUID, and building ID. Round coordinates to **0.001 m**, simplify shared boundary chains together with **0.5 m** tolerance, then snap neighbouring areas within **0.5 m**. Extrude the resulting valid polygons at their assigned elevations.
3. **Clean selected short walls.** Multipart buildings whose shortest roof-boundary edge is **<2 m** enter this branch. Node their edges; find shared interior midlines and nearby short exterior edges; require the configured four-wall pairing, **0.1 m** proximity, and **<1 m** length difference. Where matched, remove selected old edges, add replacement centre lines, reconstruct polygon faces, simplify those faces at **1 m**, and recover each face's height from its original section. Others pass through. The report records eligible and modified buildings.
4. **Create wall lines and relationships.** Chop final footprint boundaries into wall segments, with IDs `V{byggnadsid}-{count}` and `zmin`/`zmax`. For each target DeSO, compare local and neighbouring walls within **250 m**; a separate branch includes walls within **1 m**. Long-range links crossing intervening contextual footprints are excluded. Deduplicate relationships by building and wall ID.
5. **Export buildings.** `simplified.geojson` contains all selected buildings that survive. `buildings_export.geojson` reflects FME's merged wall branch and can omit buildings with no wall match. Its `height` property is the separate estimate `EgenAntalPlan × 3`; actual modeled heights are in `properties.sections` and shell Z values.

Simplification uses the saved baseline records and context; it does **not** re-read the point cloud or terrain.

## Output files

| File | Created by | What it contains |
| --- | --- | --- |
| `output/baseline.geojson` | Baseline | Selected 3D buildings, one Feature per UUID. |
| `output/baseline_stages.geojson` | Baseline | Intermediate 2D steep regions, cutting lines, and cut polygons labeled by UUID and stage. |
| `output/baseline_context.json` | Baseline | Required simplification handoff: contextual footprints/properties, section records, config/source manifest, selected UUIDs. |
| `output/baseline_report.json` | Baseline | Point counts, TIN/cutting statistics, rejection reasons, missing UUIDs. |
| `output/simplified.geojson` | Simplification | Final selected 3D buildings, including ones without wall matches. |
| `output/buildings_export.geojson` | Simplification | FME-style merged building export subset with wall-related properties. |
| `output/walls.geojson` | Simplification | Wall lines with IDs, UUIDs, section IDs, and Z bounds. |
| `output/wall_relationships.json` | Simplification | Building-to-wall links and their match reason. |
| `output/simplified_context.json` | Simplification | Final section records and wall count for inspection. |
| `output/simplification_report.json` | Simplification | Per-zone counts and short-wall cleanup diagnostics. |

Only the four baseline outputs are present until the simplifier runs. Each run overwrites its outputs. An old HTML plot does not update automatically.

## Plot an existing model

```sh
../venv/bin/python code/plot_model.py --input output/baseline.geojson --output output/baseline_plot.html --open
../venv/bin/python code/plot_model.py --input output/simplified.geojson --output output/simplified_plot.html --open
```

Add `--uuid 1c931069-d3d1-4559-8968-e7f0564579fb` to either command to show just that building. The plot places building blocks at their real locations on an interactive street map. Building colors follow construction year (`43S_BYGGAR`, with `EgenNybyggAr` or `43T_BYGGAR` as fallbacks); gray means no usable year. The map legend shows the year range. Drag to pan, right-drag or Ctrl-drag to rotate, and scroll to zoom. Search a UUID to focus on one building. Map tiles and the viewer libraries require an internet connection; the GeoJSON itself keeps EPSG:3006 coordinates.

## FME fidelity

This implements the FME **workflow and techniques**, not FME's internal geometry kernels. Point ordering at percentile ties, TIN triangulation, polygon snapping, and boundary generalization can change splits or heights. For example, comparison against the Minneberg building GeoJSON found the same UUID set but different block divisions in some buildings. Use the stages and reports above to investigate a discrepancy; `WORKFLOW_MAPPING.md` identifies the matching FME transformers and remaining limitations.

## Optional footprint bump cleanup

Set `simplification.bump_cleanup.enabled` in `inputs/config.yml` to `true` to
remove repeated short steps from eligible building outlines, including buildings
with multiple roof sections. For multi-section buildings, the cleanup adjusts
the shared outer outline while keeping the internal section boundaries aligned.
`tolerance_m` controls the simplification distance, and
`max_area_change_fraction` limits the permitted footprint change. Set
`enabled: false` to preserve the original outline. Changed UUIDs are listed
in `simplification_report.json` under `bump_cleanup_uuids`.

## Roof merging

Set `simplification.roof_merge_mode` in `inputs/config.yml` to `overall` or
`neighboring`. Both use `roof_range_m`. `overall` merges only when the full
building's roof-height range is below the threshold. `neighboring` merges
adjacent sections with a combined roof-height range below the threshold, so
nearby high roofs can merge while lower sections remain separate.
