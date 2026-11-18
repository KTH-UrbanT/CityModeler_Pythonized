"""Shared configuration, geometry, serialization and validation utilities."""
from collections import Counter, defaultdict
import csv
from pathlib import Path
import hashlib
import json
import re
import uuid
from openpyxl import load_workbook

import numpy as np
import yaml
from shapely import STRtree, make_valid, set_precision
from shapely.geometry import LineString, Polygon, mapping, shape
from shapely.geometry.polygon import orient
from shapely.ops import linemerge, polygonize, snap, unary_union

ROOT = Path(__file__).resolve().parent.parent


def read_uuid_file(path):
    """Read ordered UUIDs from a text list or a named CSV/Excel column."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f'UUID file not found: {path}')
    columns = {'uuid', '50a_uuid', 'building_uuid'}
    if path.suffix.lower() == '.txt':
        values = [line.strip() for line in path.read_text(encoding='utf-8-sig').splitlines()
                  if line.strip() and not line.lstrip().startswith('#')]
    elif path.suffix.lower() == '.csv':
        with path.open(newline='', encoding='utf-8-sig') as source:
            reader = csv.DictReader(source)
            column = next((name for name in reader.fieldnames or []
                           if name.strip().casefold() in columns), None)
            if column is None:
                raise ValueError(f'UUID column missing from {path}')
            values = [row[column] for row in reader if row[column] and row[column].strip()]
    elif path.suffix.lower() == '.xlsx':
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            values = None
            for sheet in workbook:
                rows = sheet.iter_rows(values_only=True)
                header = next(rows, ())
                column = next((i for i, name in enumerate(header)
                               if str(name).strip().casefold() in columns), None)
                if column is not None:
                    values = [row[column] for row in rows
                              if len(row) > column and row[column] not in (None, '')]
                    break
            if values is None:
                raise ValueError(f'UUID column missing from {path}')
        finally:
            workbook.close()
    else:
        raise ValueError(f'Unsupported UUID file type: {path.suffix}')
    try:
        identifiers = list(dict.fromkeys(str(uuid.UUID(str(value).strip())) for value in values))
    except (ValueError, AttributeError) as error:
        raise ValueError(f'Invalid UUID in {path}: {error}') from error
    if not identifiers:
        raise ValueError(f'No UUIDs found in {path}')
    return identifiers


def read_config(path, uuid_file=None):
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text())
    if '0_Data' in raw and '1_CASE' in raw:
        data, case = raw['0_Data'], raw['1_CASE']
        config = {**case, 'paths': data['0_paths'], 'crs': data['crs']}
    else:
        config = raw
    config['paths'] = dict(config['paths'])
    configured_file = config.get('uuid_file')
    chosen_file = uuid_file if uuid_file is not None else configured_file
    if chosen_file:
        chosen_file = Path(chosen_file).expanduser()
        if not chosen_file.is_absolute():
            chosen_file = path.parent / chosen_file
        config['uuid_file'] = str(chosen_file.resolve())
        config['building_uuids'] = read_uuid_file(chosen_file)
    else:
        config['uuid_file'] = ''
        config['building_uuids'] = list(dict.fromkeys(str(uuid.UUID(v)) for v in config['building_uuids']))
    if not config['building_uuids']:
        raise ValueError('Specify at least one building UUID')
    config['skip_geometry_errors'] = config.get('skip_geometry_errors', False)
    if not isinstance(config['skip_geometry_errors'], bool):
        raise ValueError('skip_geometry_errors must be true or false')
    config['source'] = config.get('source', 'database')
    if config['source'] not in {'database', 'template'}:
        raise ValueError('source must be database or template')
    if config['source'] == 'template' and not (config['paths'].get('cityplanner_footprints') or config['paths'].get('template')):
        raise ValueError('paths.cityplanner_footprints or paths.template is required when source is template')
    for key, value in config['paths'].items():
        config['paths'][key] = str((path.parent / value).resolve()) if value else ''
    case_name = str(config.get('CaseName') or '').strip()
    if case_name:
        if case_name in {'.', '..'} or '/' in case_name or '\\' in case_name:
            raise ValueError('CaseName must be a folder name without path separators')
        config['paths']['output'] = str(Path(config['paths']['output']) / case_name)
    if config['context'] != 'deso_and_neighbor_zones':
        raise ValueError('Use full DeSO and neighbour context for this workflow')
    if config['crs'] != 'EPSG:3006':
        raise ValueError('Prepared database and this workflow use EPSG:3006')
    return config


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)
    number = r'-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?'
    text = re.sub(rf'\[\s*{number}(?:\s*,\s*{number})*\s*\]',
                  lambda m: '[ ' + ', '.join(re.findall(number, m.group())) + ' ]', text)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(text + '\n')
    temp.replace(path)


def collection(features, crs='EPSG:3006'):
    return {'type': 'FeatureCollection', 'crs': {'type': 'name', 'properties': {'name': crs}}, 'features': features}


def parts(geometry, kind='Polygon'):
    if geometry.is_empty:
        return []
    if geometry.geom_type == kind:
        return [geometry]
    return [p for g in getattr(geometry, 'geoms', []) for p in parts(g, kind)]


def valid_area(geometry):
    result = unary_union(parts(make_valid(geometry)))
    if result.is_empty:
        raise ValueError('Geometry operation collapsed all polygon area')
    return result


def buffer(geometry, distance):
    return geometry.buffer(distance, quad_segs=4)


def groups(records, key):
    result = defaultdict(list)
    for record in records:
        result[key(record)].append(record)
    return result


def edges(polygon):
    return [LineString([a, b]) for ring in [polygon.exterior, *polygon.interiors]
            for a, b in zip(ring.coords, list(ring.coords)[1:]) if a != b]


def merge_lines(lines):
    geometry = unary_union(lines)
    if geometry.is_empty:
        return []
    if geometry.geom_type != 'LineString':
        geometry = linemerge(geometry)
    return parts(geometry, 'LineString')


def snap_areas(polygons, tolerance):
    """Batch polygon snapping; None represents FME's unconnected COLLAPSED port.

    FME's ALL mode snaps vertices and segments within the tolerance. GEOS snap
    is the closest available operation, but its vertex choices may differ.
    """
    if not polygons:
        return []
    tree = STRtree(polygons)
    output = []
    for index, polygon in enumerate(polygons):
        nearby = [polygons[int(i)].boundary for i in tree.query(polygon.buffer(tolerance))
                  if int(i) != index]
        if not nearby:
            output.append(polygon)
            continue
        adjusted = snap(polygon, unary_union(nearby), tolerance)
        area = unary_union(parts(make_valid(adjusted)))
        output.append(area if not area.is_empty and area.area > 0 else None)
    return output


def shared_generalization(polygons, tolerance):
    """Simplify noded boundary chains once so shared edges remain shared."""
    if not polygons:
        return []
    chains = merge_lines([p.boundary for p in polygons])
    faces = list(polygonize(unary_union([c.simplify(tolerance) for c in chains])))
    assigned = [[] for _ in polygons]
    for face in faces:
        overlaps = [face.intersection(p).area for p in polygons]
        if max(overlaps, default=0) > 0:
            assigned[int(np.argmax(overlaps))].append(face)
    return [valid_area(unary_union(items)) if items else None for items in assigned]


def extrude(polygon, ground, roof):
    """Outward shell, with courtyards preserved, in the reference GeoJSON layout."""
    polygon = orient(polygon, sign=1)
    rings = [list(polygon.exterior.coords), *[list(r.coords) for r in polygon.interiors]]
    base = [[[float(x), float(y), ground] for x, y in reversed(r)] for r in rings]
    top = [[[float(x), float(y), roof] for x, y in r] for r in rings]
    walls = []
    for ring in rings:
        for a, b in zip(ring, ring[1:]):
            walls.append([[list(a) + [ground], list(b) + [ground], list(b) + [roof],
                           list(a) + [roof], list(a) + [ground]]])
    return {'type': 'MultiPolygon', 'coordinates': [base, *walls, top]}


def audit_shell(shell):
    counter = Counter()
    for face in shell['coordinates']:
        for ring in face:
            if len(ring) < 4 or ring[0] != ring[-1] or not np.isfinite(ring).all():
                raise ValueError('Invalid surface ring')
            for a, b in zip(ring, ring[1:]):
                if a == b:
                    raise ValueError('Zero-length surface edge')
                counter[(tuple(a), tuple(b))] += 1
    if any(n != 1 or counter[(b, a)] != 1 for (a, b), n in counter.items()):
        raise ValueError('Shell is not closed and consistently oriented')


def model_features(records, buildings, selected, skip_geometry_errors=False, issues=None):
    if skip_geometry_errors and issues is None:
        raise ValueError('issues list is required when skip_geometry_errors is true')
    result = []
    for identifier, group in groups(records, lambda r: r['uuid']).items():
        if identifier not in selected:
            continue
        shells, summaries = [], []
        try:
            for i, r in enumerate(group, 1):
                p = r['polygon']
                if p.geom_type != 'Polygon' or not p.is_valid or r['height'] <= 0:
                    raise ValueError('Invalid model section')
                shell = extrude(p, r['ground'], r['ground'] + r['height'])
                audit_shell(shell)
                shells.append(shell)
                summaries.append({'section': i, 'section_id': r['section_id'], 'area_m2': p.area,
                                  'ground_z_m': r['ground'], 'roof_z_m': r['ground'] + r['height'],
                                  'height_m': r['height'], 'Roofheight': r['roof']})
        except ValueError as error:
            if not skip_geometry_errors:
                raise
            issues.append({'uuid': identifier, 'section_id': r['section_id'], 'reason': str(error)})
            continue
        props = {k: v for k, v in buildings[identifier]['attributes'].items() if k != '_geometry'}
        props['50A_UUID'] = identifier
        props['sections'] = summaries
        result.append({'type': 'Feature', 'properties': props,
                       'geometry': {'type': 'GeometryCollection', 'geometries': shells}})
    return result


def encode_records(records):
    return [{**{k: v for k, v in r.items() if k != 'polygon'}, 'polygon': mapping(r['polygon'])} for r in records]


def decode_records(records):
    return [{**r, 'polygon': shape(r['polygon'])} for r in records]


def sources_manifest(config):
    result = {}
    for path in Path(config['paths']['fme_directory']).glob('*.fmw'):
        if path.name.lower() in {'citymodeller_footprints_worker.fmw', 'citymodeller_main.fmw', 'citymodeller_walls_worker.fmw'}:
            result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if config['source'] == 'template':
        input_keys = (('cityplanner_footprints', 'cityplanner_buildings', 'cityplanner_now',
                       'cityplanner_properties', 'template') if config['paths'].get('cityplanner_footprints')
                      else ('template',))
        for key in input_keys:
            source = Path(config['paths'][key])
            result[source.name] = hashlib.sha256(source.read_bytes()).hexdigest()
    return result
