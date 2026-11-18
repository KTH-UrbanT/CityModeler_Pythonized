# CityModeller

CityModeller creates 3D building GeoJSON from 2D footprints, terrain GeoTIFFs, and LAS/LAZ point clouds. It runs independently of the former Features workflow. The pipeline calculates building sections, simplifies their geometry, creates wall data, and produces an interactive map.

## Set up

Use Python 3.12 from the project root:

```sh
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
```

Edit `inputs/config.yml` before running. Paths under `0_Data.0_paths` may be absolute or relative to the config file. The processing coordinate system is **EPSG:3006** and distances and elevations are in metres. City Planner footprints are read as EPSG:4326 and projected automatically.

Set these values under `1_CASE`:

| Key | Purpose |
| --- | --- |
| `CaseName` | Output subfolder name. `Stockholm` writes to `output/Stockholm/`. |
| `source` | `database` or `template`. |
| `building_uuids` | One or more building UUIDs to export. |

The `baseline`, `simplification`, and `walls` sections contain processing thresholds.

## Choose an input mode

### Database mode

Set `1_CASE.source: database` and `0_Data.0_paths.database` to the prepared SQLite file. CityModeller reads footprints and properties from its `Buildings` table. It also reads `Zones` to load the selected buildings' DeSO areas and neighbouring areas. This surrounding context is used in geometry and wall relationship operations. The database is opened read only.

### Template mode

Set `1_CASE.source: template` and point these paths to the City Planner files:

| Path key | File | Link to footprint |
| --- | --- | --- |
| `cityplanner_footprints` | `cpFootprints.geojson` | Building selection by `50A_UUID`; coordinates projected from EPSG:4326. |
| `cityplanner_buildings` | `cpBuildings.csv` | `50A_UUID` |
| `cityplanner_now` | `cpNow.csv` | Footprint `FormularId` to CSV `FormularId` |
| `cityplanner_properties` | `cpProperties.csv` | `FNR` |
| `template` | `building_template.geojson` | Defines the output property fields. |

The CSV files use semicolons. Values missing from the City Planner sources become `null`. Geometry is calculated from the footprints, terrain, and point cloud; heights in a template are recalculated. Template mode processes only the selected footprints, so it has less surrounding context than database mode.

To use a GeoJSON feature directly instead of the City Planner files, clear `cityplanner_footprints` and set `template` to that Feature or FeatureCollection. Each selected feature needs `properties["50A_UUID"]` and EPSG:3006 geometry. A 3D `GeometryCollection` with horizontal bottom faces can also supply a footprint.

## Run

```sh
venv/bin/python main.py
```

`main.py` runs baseline generation, simplification, and plotting in sequence. It writes the final building model to `output/<CaseName>/CityModeller.geojson` and the viewer to `CityModeller_plot.html` in the same folder. Run one UUID without changing the config with:

```sh
venv/bin/python main.py --uuid 1c931069-d3d1-4559-8968-e7f0564579fb
```

To select buildings from a file, set `uuid_file` in `inputs/config.yml` under
`1_CASE` and run `venv/bin/python main.py`:

```yaml
uuid_file: /path/to/building_uuids.xlsx
```

When `uuid_file` is set, it overrides `building_uuids`. Leave it empty to use
the inline list. Relative paths are resolved from the config file's folder.
In database mode, UUIDs absent from the prepared database use their City Planner
footprint when available. Missing `FormularId` leaves fields from its `cpNow`
join as `null`; the footprint can still produce geometry. The fallback UUIDs
are listed in `baseline_report.json`. UUIDs absent from both sources still raise
an error.

Set `skip_geometry_errors: true` under `1_CASE` to omit an entire building if
its 3D section fails shell validation. The stage report lists each skipped UUID,
section ID, and reason in `geometry_errors_skipped`. With `false` (the default),
geometry errors stop the workflow as before.
You can also override the config selection for one run:

```sh
venv/bin/python main.py --uuid-file /path/to/building_uuids.xlsx
```

The file may be `.xlsx` or `.csv` with a `UUID`, `50A_UUID`, or
`building_uuid` column, or `.txt` with one UUID per line. `--uuid-file`
overrides the config list for that run and cannot be combined with `--uuid`.
Use the same `--uuid-file` option on both scripts if running the stages separately.

Small roof-division bends are controlled by `cut_line_straightening_m`,
`cut_line_straightening_min_length_m`, and `cut_line_straightening_max_fraction`
under `baseline`. The corresponding `shared_wall_*` settings under
`simplification` straighten eligible internal section walls after snapping.
Both stages retain wall endpoints; final shared-wall edits rebuild adjoining
sections from one line network and are accepted only when the building outline
and all section faces are retained. Set either distance limit to `0` to disable
that stage's straightening.

You can also use another config file with `--config /path/to/config.yml`. A run overwrites files in its case output folder. To preserve a result, give the next run a different `CaseName` or copy the output elsewhere.

### Run stages separately

```sh
venv/bin/python code/generate_baseline.py
venv/bin/python code/simplify_model.py
venv/bin/python code/plot_model.py
```

Pass the same `--config` and `--uuid` to both geometry stages when overriding either. The simplifier reads `baseline_context.json`; it does not recalculate terrain or cloud heights. Re-run baseline after changing the input source, UUIDs, paths, or baseline settings. The plot command defaults to the configured case's `CityModeller.geojson`; that file is created by `main.py`. When running stages separately, pass `--input output/<CaseName>/simplified.geojson` to plot the simplifier's output.

## What the pipeline does

1. **Baseline:** Load footprints and properties, sample terrain and cloud heights, detect roof height breaks, split and regroup sections, and extrude closed 3D shells. `baseline_stages.geojson` records diagnostic 2D cuts and lines.
2. **Simplification:** Merge and simplify section footprints, align nearby boundaries, clean selected short walls, and calculate wall relationships. Collapsed polygons are routed out of the connected geometry path.
3. **Export and plot:** Write one GeoJSON Feature per surviving selected UUID. Each Feature contains a `GeometryCollection` of 3D `MultiPolygon` sections. Create an HTML map of those sections.

The two modes use the same baseline and simplification code. Their input records and available surrounding context differ.

## Output files

All files are written under `output/<CaseName>/`:

| File | Contents |
| --- | --- |
| `CityModeller.geojson` | Final selected 3D buildings; copy of `simplified.geojson`. |
| `CityModeller_plot.html` | Interactive 3D map. Map tiles and viewer libraries need an internet connection. |
| `baseline.geojson` | Selected buildings before simplification. |
| `baseline_stages.geojson` | Diagnostic 2D steep regions, cutting lines, and cut polygons. |
| `baseline_context.json` | Handoff required by the simplifier, including contextual geometry and source hashes. |
| `baseline_report.json` | Sampling, cuts, rejection reasons, and missing UUIDs. |
| `simplified.geojson` | Selected buildings after simplification, including buildings without wall matches. |
| `buildings_export.geojson` | FME style wall matched subset; it can omit buildings with no wall match. |
| `walls.geojson` | Wall line segments with IDs and height bounds. |
| `wall_relationships.json` | Building to wall relationships. |
| `simplified_context.json` | Final section records for inspection. |
| `simplification_report.json` | Zone counts, cleanup diagnostics, and missing UUIDs. |

The plot colors buildings by construction year when one is available. The GeoJSON coordinates remain in EPSG:3006.

## FME relationship and limits

The Python code follows the connected FME workflow mapped in `WORKFLOW_MAPPING.md`. It does **not** run FME. `0_Data.0_paths.fme_directory` is used to record hashes of the reference FME workspaces when present. `source_audit/` contains extracted workspace text for inspection and is not a runtime input.

Python uses GEOS for polygon operations and SciPy for triangulation. FME's internal kernels can choose different triangles, snapped vertices, section splits, or heights. The 0.5 m batch snapping follows the FME stage settings, but it is not guaranteed to produce identical coordinates. Use the stage GeoJSONs and reports to investigate differences. The point cloud and terrain vertical datums should also be checked before relying on the heights.

## Project files

| Path | Purpose |
| --- | --- |
| `main.py` | Single command runner. |
| `inputs/config.yml` | Input paths, source mode, UUIDs, case name, and thresholds. |
| `inputs/building_template.geojson` | Property schema and optional direct GeoJSON input. |
| `code/generate_baseline.py` | Height sampling, cutting, and baseline 3D geometry. |
| `code/simplify_model.py` | Final footprints, walls, and exports. |
| `code/plot_model.py` | Interactive HTML map. |
| `code/model_common.py` | Shared configuration, geometry, and serialization functions. |
| `WORKFLOW_MAPPING.md` | FME transformer mapping and fidelity notes. |
| `source_audit/` | Extracted reference workspace text. |
