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
venv/bin/python main_Data4ExternalStudy.py
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
venv/bin/python main_Generate_CityModeller.py
```

Default output:

```text
examples/Data_for_CityModeller/Generated_Buildings.geojson
```

## Case names and outputs

Change `case_name` in the relevant root-level main script to name your study. Each workflow writes to `examples/Data_for_<case_name>/`.

Running a workflow again deletes and recreates its case output folder. Save any results you want to keep elsewhere, or choose a new case name before rerunning.

Review the generated geometry and properties before using the GeoJSON as building input in your MUBES setup.
