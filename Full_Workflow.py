"""Generate building properties, model geometry, and combine them by UUID."""
import csv
from copy import deepcopy
import json
import subprocess
import sys
from pathlib import Path
import uuid

from pyproj import Transformer
import yaml
from openpyxl import load_workbook

from Features.Scripts.Generate_CityModeller import ShapeCityPlanner
from Features.Scripts.GeneralFunctions import clean_nans
from Geometry.code.model_common import read_config, write_json

ROOT = Path(__file__).resolve().parent
UUID_COLUMNS = {'uuid', '50a_uuid', 'building_uuid'}


def external_uuids(path):
    """Read a UUID column from one Excel/CSV file, or UUIDs from a text list."""
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    if path.is_dir():
        candidates = sorted(p for p in path.rglob('*') if p.is_file()
                            and p.suffix.lower() in {'.xlsx', '.csv', '.txt'})
        if len(candidates) != 1:
            raise ValueError(f'Expected one UUID file (.xlsx, .csv, .txt) in {path}; found {len(candidates)}. Set Read_UUID_from_External_file to the exact file.')
        path = candidates[0]
    if not path.is_file():
        raise FileNotFoundError(f'UUID file not found: {path}')

    suffix = path.suffix.lower()
    if suffix == '.xlsx':
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            for sheet in workbook:
                rows = sheet.iter_rows(values_only=True)
                for header in rows:
                    columns = [str(value).strip().casefold() if value is not None else '' for value in header]
                    selected = next((i for i, name in enumerate(columns) if name in UUID_COLUMNS), None)
                    if selected is not None:
                        return [row[selected] for row in rows if len(row) > selected and row[selected] not in (None, '')]
                    break
            raise ValueError(f'No UUID column found in workbook: {path}')
        finally:
            workbook.close()
    if suffix == '.csv':
        with path.open(newline='', encoding='utf-8-sig') as file:
            reader = csv.DictReader(file)
            column = next((name for name in (reader.fieldnames or [])
                           if name.strip().casefold() in UUID_COLUMNS), None)
            if column is None:
                raise ValueError(f'No UUID column found in CSV: {path}')
            return [row[column] for row in reader if row[column] and row[column].strip()]
    if suffix == '.txt':
        return [line.strip() for line in path.read_text(encoding='utf-8-sig').splitlines()
                if line.strip() and not line.lstrip().startswith('#')]
    raise ValueError(f'Unsupported UUID file type: {path.suffix}')


def selected_uuids(config):
    external = config.get('Read_UUID_from_External_file')
    configured = config.get('building_uuids', [])
    if not isinstance(configured, list):
        raise ValueError('building_uuids must be a list of UUIDs.')
    use_inline = bool(configured)
    raw = configured if use_inline else external_uuids(external) if external else []
    if not isinstance(raw, list):
        raise ValueError('building_uuids must be a list of UUIDs.')
    try:
        identifiers = list(dict.fromkeys(str(uuid.UUID(str(value).strip())) for value in raw))
    except (ValueError, AttributeError) as error:
        raise ValueError(f'Invalid building UUID in {"GenConfig.yml" if use_inline else external or "GenConfig.yml"}: {error}') from error
    if not identifiers:
        raise ValueError(f'No building UUIDs found in {"GenConfig.yml" if use_inline else external or "GenConfig.yml"}.')
    return identifiers


def case_output(config):
    name = str(config.get('CaseName') or '').strip() or 'full_workflow'
    if name in {'.', '..'} or '/' in name or '\\' in name:
        raise ValueError('CaseName must be a folder name without path separators.')
    return ROOT / 'output' / name


def merge_geometry(features, geometry):
    """Preserve feature properties and replace geometry using unique building UUIDs."""
    by_uuid = {}
    for feature in geometry['features']:
        identifier = feature['properties']['50A_UUID']
        if identifier in by_uuid:
            raise ValueError(f'Duplicate geometry UUID: {identifier}')
        by_uuid[identifier] = feature['geometry']
    result = deepcopy(features)
    result['features'] = [feature for feature in result['features']
                          if feature['properties']['50A_UUID'] in by_uuid]
    for feature in result['features']:
        identifier = feature['properties']['50A_UUID']
        if identifier not in by_uuid:
            raise ValueError(f'Missing simplified geometry for {identifier}')
        feature['geometry'] = deepcopy(by_uuid[identifier])
    result['crs'] = deepcopy(geometry['crs'])
    return result


def write_issue_log(output, identifiers, geometry):
    baseline_report = json.loads((output / 'geometry' / 'baseline_report.json').read_text())
    baseline_reasons = {}
    for entry in baseline_report['features']:
        if entry.get('status') in {'filtered', 'section_filtered'}:
            baseline_reasons.setdefault(entry['uuid'], []).append(entry['reason'])
    baseline_missing = set(baseline_report['missing_requested_uuids'])
    simplified_ids = {feature['properties']['50A_UUID'] for feature in geometry['features']}
    issues = []
    for identifier in identifiers:
        if identifier in simplified_ids:
            continue
        if identifier in baseline_missing:
            reason = '; '.join(dict.fromkeys(baseline_reasons.get(identifier, []))) or 'No baseline geometry produced'
            stage = 'baseline'
        else:
            reason = 'Removed by simplification filters'
            stage = 'simplification'
        issues.append(f'{identifier}\t{stage}\t{reason}')
    log_path = output / 'failed_buildings.log'
    log_path.write_text('UUID\tStage\tIssue\n' + '\n'.join(issues) + ('\n' if issues else ''), encoding='utf-8')
    print(f'Skipped {len(issues)} buildings; issue log: {log_path}', flush=True)
    return log_path


def main():
    config = yaml.safe_load((ROOT / 'GenConfig.yml').read_text())
    identifiers = selected_uuids(config)
    print(f'Selected {len(identifiers)} building UUIDs.', flush=True)
    template = json.loads((ROOT / 'Features/Configs/CM_Template.geojson').read_text())
    geometry_config = read_config(ROOT / 'Geometry/inputs/config.yml')
    geometry_config['building_uuids'] = identifiers
    geometry_config['source'] = 'template'
    output = case_output(config)
    geometry_config['paths']['output'] = str(output / 'geometry')
    for key in ('point_cloud', 'terrain'):
        if not Path(geometry_config['paths'][key]).exists():
            raise FileNotFoundError(f'Geometry {key} path does not exist: {geometry_config["paths"][key]}')

    print('Generating building features...', flush=True)
    planner = ShapeCityPlanner(str(ROOT / 'Features'), template, identifiers)
    available = {feature['50A_UUID'] for feature in planner.cpFootprints}
    missing = sorted(set(identifiers) - available)
    if missing:
        source = planner.config['0_Setup']['cpFootprints']
        raise ValueError(
            f'{len(missing)} of {len(identifiers)} selected UUIDs are missing from the configured '
            f'City Planner footprints ({source}). First missing UUIDs: {missing[:5]}. '
            'Use a UUID list covered by this dataset or configure the matching City Planner source.'
        )
    features, _ = planner.GenCore(identifiers)
    if not isinstance(features, dict):
        raise ValueError('Feature generation returned no buildings.')
    generated = {f['properties']['50A_UUID'] for f in features['features']}
    if generated != set(identifiers):
        raise ValueError(f'Feature UUIDs do not match selection; missing: {set(identifiers) - generated}')

    # Use source footprints as the geometry input, not the empty template geometry.
    seed = deepcopy(features)
    transformer = Transformer.from_crs('EPSG:4326', geometry_config['crs'], always_xy=True)
    footprints = {f['50A_UUID']: f['FootPrints'] for f in planner.cpFootprints}
    for index, feature in enumerate(seed['features'], start=1):
        identifier = feature['properties']['50A_UUID']
        feature['geometry'] = {'type': 'Polygon', 'coordinates': [
            [list(transformer.transform(*point[:2])) for point in ring]
            for ring in footprints[identifier]
        ]}
        # Geometry requires unique numeric IDs; template defaults are not unique.
        feature['properties']['byggnadsid'] = index
    seed['crs'] = {'type': 'name', 'properties': {'name': geometry_config['crs']}}
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / 'features.geojson', clean_nans(features))
    seed_path = output / 'geometry_input.geojson'
    write_json(seed_path, clean_nans(seed))
    geometry_config['paths']['template'] = str(seed_path)
    run_config = output / 'geometry_config.yml'
    run_config.write_text(yaml.safe_dump(geometry_config, sort_keys=False))
    # Fiona and Rasterio can load incompatible GDAL libraries in one process.
    # Separate stages retain the same interpreter without sharing native libraries.
    for script in ('generate_baseline.py', 'simplify_model.py'):
        print(f'Running {script}...', flush=True)
        subprocess.run([sys.executable, str(ROOT / 'Geometry' / 'code' / script),
                        '--config', str(run_config)], check=True, cwd=ROOT)
    geometry = json.loads((output / 'geometry' / 'simplified.geojson').read_text())
    write_issue_log(output, identifiers, geometry)
    combined = merge_geometry(features, geometry)
    destination = output / 'CityModeller.geojson'
    write_json(destination, clean_nans(combined))
    print(f'Generated: {destination}', flush=True)
    plot = output / 'CityModeller_plot.html'
    if combined['features']:
        subprocess.run([sys.executable, str(ROOT / 'Geometry' / 'code' / 'plot_model.py'),
                        '--input', str(destination), '--output', str(plot)], check=True, cwd=ROOT)
        print(f'Plot: {plot}', flush=True)
    return destination


if __name__ == '__main__':
    main()
