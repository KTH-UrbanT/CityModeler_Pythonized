"""Generate building properties, model geometry, and combine them by UUID."""
from copy import deepcopy
import json
import subprocess
import sys
from pathlib import Path
import uuid

from pyproj import Transformer
import yaml

from Features.Scripts.Generate_CityModeller import ShapeCityPlanner
from Features.Scripts.GeneralFunctions import clean_nans
from Geometry.code.model_common import read_config, write_json

ROOT = Path(__file__).resolve().parent


def merge_geometry(features, geometry):
    """Preserve feature properties and replace geometry using unique building UUIDs."""
    by_uuid = {}
    for feature in geometry['features']:
        identifier = feature['properties']['50A_UUID']
        if identifier in by_uuid:
            raise ValueError(f'Duplicate geometry UUID: {identifier}')
        by_uuid[identifier] = feature['geometry']
    result = deepcopy(features)
    for feature in result['features']:
        identifier = feature['properties']['50A_UUID']
        if identifier not in by_uuid:
            raise ValueError(f'Missing simplified geometry for {identifier}')
        feature['geometry'] = deepcopy(by_uuid[identifier])
    result['crs'] = deepcopy(geometry['crs'])
    return result


def main():
    config = yaml.safe_load((ROOT / 'GenConfig.yml').read_text())
    identifiers = list(dict.fromkeys(str(uuid.UUID(value)) for value in config['building_uuids']))
    if not identifiers:
        raise ValueError('building_uuids must contain at least one UUID.')
    template = json.loads((ROOT / 'Features/Configs/CM_Template.geojson').read_text())
    geometry_config = read_config(ROOT / 'Geometry/inputs/config.yml')
    geometry_config['building_uuids'] = identifiers
    geometry_config['source'] = 'template'
    output = ROOT / 'output/full_workflow'
    geometry_config['paths']['output'] = str(output / 'geometry')
    for key in ('point_cloud', 'terrain'):
        if not Path(geometry_config['paths'][key]).exists():
            raise FileNotFoundError(f'Geometry {key} path does not exist: {geometry_config["paths"][key]}')

    print('Generating building features...', flush=True)
    planner = ShapeCityPlanner(str(ROOT / 'Features'), template, identifiers)
    features, _ = planner.GenCore()
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
    combined = merge_geometry(features, geometry)
    destination = output / 'CityModeller.geojson'
    write_json(destination, clean_nans(combined))
    print(f'Generated: {destination}', flush=True)
    plot = output / 'CityModeller_plot.html'
    subprocess.run([sys.executable, str(ROOT / 'Geometry' / 'code' / 'plot_model.py'),
                    '--input', str(destination), '--output', str(plot)], check=True, cwd=ROOT)
    print(f'Plot: {plot}', flush=True)
    return destination


if __name__ == '__main__':
    main()
