# CityModeller

CityModeller prepares building GeoJSON data for MUBES simulations. It provides two workflows: defining a hypothetical building manually, or extracting existing buildings from City Planner datasets. These scripts generate the input data; the MUBES simulation is run separately.

## Installation

Run these commands from the project directory. If you do not already have a virtual environment, create one first:

```bash
python3 -m venv venv
```

Install the required libraries into the virtual environment:

```bash
venv/bin/python -m pip install -r requirements.txt
```

The commands below use macOS/Linux paths. On Windows, use `venv\Scripts\python.exe` instead of `venv/bin/python`.

## 1. Hypothetical buildings: Data4ExternalStudy

`Scripts/Data4ExternalStudy.py` is intended for external studies and hypothetical buildings. You can define the building's shape and assign relevant features, then generate a CityModeller GeoJSON file for use in MUBES.

Before running, edit `Configs/UserManualInput.yml`. Building settings are stored under `0_Bld_Opt`:

| Setting | Purpose |
| --- | --- |
| `Coordinates` | Footprint coordinates, nested by building, block, and coordinate pair. When provided, these take precedence over the synthetic shape settings. |
| `CoordSys` | Coordinate reference system of the supplied coordinates, such as `EPSG:4326`. |
| `ProjectedCoordSys` | Target projected EPSG code appropriate for the building's location. |
| `height` | Block heights in metres; for explicit coordinates, provide one height per block. |
| `BldShape` | Synthetic shape: `S` for square, `R` for rectangle, or `L` for an L-shaped building. |
| `EgenAtemp` | Area used to construct a synthetic footprint when coordinates are absent. |
| `EgenAntalPlan` | Number of above-ground floors. |
| `EgenAntalKallarplan` | Number of basement floors. |
| `EgenNybyggAr`, `43S_ARVARDE`, `43S_BYGGAR` | Year-related building properties to set for the study. |

The script uses `Configs/CM_Template.geojson` as its base. Configuration properties that match properties in the template replace the template values. Review the remaining template values to ensure they are suitable for your study. For explicit footprints, close each polygon by repeating its first coordinate at the end.

Run the root-level entry point:

```bash
venv/bin/python Features/main_Data4ExternalStudy.py
```

Default output:

```text
examples/Data_for_ExternalStudy/CityModeler.geojson
```

**Current implementation limitations:** the synthetic shape branch needs correction before use: it returns a single object where the writer expects a list, and its height handling differs from the coordinate branch. Use explicit coordinates for now. The writer also retains only the last building when multiple buildings are supplied; use one building per run, with multiple blocks if needed.

## 2. Existing buildings: Generate_CityModeller

`Scripts/Generate_CityModeller.py` reads City Planner datasets from the file paths specified in `Configs/CityModellerConfig.yml`. It selects buildings, links their records using building UUIDs, `FormularId`, and `FNR`, and extracts the geometry and building properties needed to populate the CityModeller template for MUBES.

Before running, set the paths under `0_Setup`:

| Setting | Source |
| --- | --- |
| `cpBuildings` | Building CSV, including building UUIDs and heights. |
| `cpFootprints` | Footprint GeoJSON, including identifiers used to link the datasets. |
| `cpNow` | Energy performance certificate (EPC) CSV. |
| `cpProperties` | Property CSV. |
| `CityModeller_Tempalte` | GeoJSON template; defaults to `Configs/CM_Template.geojson`. Keep this configuration key's spelling as shown. |

Use absolute file paths or paths relative to the project directory. The CSV files must use semicolons as delimiters. The source datasets are not included in this project, so replace the example paths with paths on your computer.

Under `1_Sim`, choose the buildings to extract:

- Set `InputType: 'UUID'` and provide a list in `UUID`. An empty list (`UUID: []`) selects all buildings in the footprint dataset.
- Alternatively, set `InputType: 'Address'`, enter `Address`, and set `address2UUID_DataPath` to the Excel file mapping addresses to UUIDs. This file must contain the `Building` and `55A_UUIDREGBYG` columns.
- Keep `ODEN_API: False` for the local dataset workflow. The optional API integration references an external module that is not included in this repository.

The generator copies matching source properties into the template and builds floor and roof geometry. It assumes footprint coordinates are in `EPSG:4326` and transforms them to `EPSG:3006`. If a building height is missing, it uses the number of floors multiplied by 2.6 metres; buildings missing both values are skipped. Source records must contain matching identifiers and the fields expected by the script.

Run the root-level entry point:

```bash
venv/bin/python Features/main_Generate_CityModeller.py
```

Default output:

```text
examples/Data_for_CityModeller/Generated_Buildings.geojson
```

## Case names and outputs

Change `case_name` in the relevant root-level main script to name your study. Each workflow writes to `examples/Data_for_<case_name>/`.

Running a workflow again deletes and recreates its case output folder. Save any results you want to keep elsewhere, or choose a new case name before rerunning.

Review the generated geometry and properties before using the GeoJSON as building input in your MUBES setup.


## Running with Docker

Start Docker Desktop, then build the image from the project directory:

```bash
docker build -t citymodeller .
```

Create a separate configuration folder for Docker. This lets you use container
paths without changing your local Python configuration:

```bash
mkdir -p docker-config examples
cp Features/Configs/* docker-config/
```

Edit `docker-config/UserManualInput.yml` for the hypothetical-building workflow,
then run:

```bash
docker run --rm \
  -v "$(pwd)/docker-config:/app/Configs:ro" \
  -v "$(pwd)/examples:/app/examples" \
  citymodeller
```

For the City Planner workflow, edit `docker-config/CityModellerConfig.yml` so
its `0_Setup` section uses the mounted dataset paths:

```yaml
0_Setup:
  cpBuildings: '/data/cpBuildings.csv'
  cpFootprints: '/data/cpFootprints.geojson'
  cpNow: '/data/cpNow.csv'
  cpProperties: '/data/cpProperties.csv'
  CityModeller_Tempalte: 'Configs/CM_Template.geojson'
```

Keep the `1_Sim` section and set the desired UUIDs or address. Keep `ODEN_API`
set to `False`. For address selection, also mount the mapping workbook and set
`address2UUID_DataPath` to its path inside the container.

Replace `/absolute/path/to/city-planner-data` below with the folder containing
your source files:

```bash
docker run --rm \
  -v "$(pwd)/docker-config:/app/Configs:ro" \
  -v "$(pwd)/examples:/app/examples" \
  -v "/absolute/path/to/city-planner-data:/data:ro" \
  citymodeller python main_Generate_CityModeller.py
```

Outputs are saved in your local `examples/` folder using the same case names
as the Python workflows. Rerunning still replaces the corresponding case
output folder. Source datasets are supplied through mounts, not bundled in
the image. Rebuild the image after changing the Python code or requirements.


## Shared Python environment

Both `Features/` and `Geometry/` use the single `venv/` at the repository root.
Install all dependencies from that root with `venv/bin/python -m pip install -r requirements.txt`.
In PyCharm, select the existing interpreter `venv/bin/python` for both workflows.
Do not create another environment inside either folder. Feature workflow outputs
are now under `Features/examples/` when run locally.

```bash
source venv/bin/activate
python Features/main_Generate_CityModeller.py
python Geometry/code/generate_baseline.py --help
```

## Full workflow: features and geometry

Run from the repository root:

```bash
venv/bin/python Full_Workflow.py
```

Set `CaseName` and either `building_uuids` or `Read_UUID_from_External_file`
in `GenConfig.yml`. An empty `CaseName` uses `output/full_workflow/`; a name such
as `Archetype_1000` uses `output/Archetype_1000/`. When the external path is set,
`building_uuids` takes precedence when it is nonempty. Otherwise, the external path is used. It may point to an `.xlsx` workbook
or a folder containing exactly one `.xlsx`, `.csv`, or `.txt` UUID file. Excel
and CSV files need a `UUID`, `50A_UUID`, or `building_uuid` column; text files
need one UUID per line. Relative paths start at the project root. The feature
data paths are in `Features/Configs/CityModellerConfig.yml`; point-cloud and
terrain paths are in `Geometry/inputs/config.yml`. All selected UUIDs must
exist in the configured City Planner footprints dataset.

The workflow generates properties, projects the selected City Planner footprints
into the geometry CRS, runs baseline generation followed by simplification, and
replaces each feature's geometry by matching `50A_UUID`. Feature properties are
preserved. Buildings rejected by geometry generation are skipped and listed with
their UUID, stage, and reason in `output/<CaseName>/failed_buildings.log` (or
`output/full_workflow/failed_buildings.log` when `CaseName` is empty). The geometry stages run in separate processes using the same virtual
environment to avoid native GDAL conflicts between Fiona and Rasterio.

The default result is `output/full_workflow/Stockholm_Buildings.geojson`. Intermediate
features, footprint input, a resolved geometry configuration, and geometry-stage
outputs are retained alongside it for inspection. Rerunning overwrites these
workflow files. The workflow uses template geometry mode for the selected
buildings; it does not load the database's full surrounding-building context.
The same run also creates `output/full_workflow/CityModeller_plot.html`, an
interactive 3D building map. Open that HTML file in a browser with an internet connection.
Building colors represent construction year, with the range shown in the map legend.
