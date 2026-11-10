"""Shared configuration, geometry, serialization and validation utilities."""
from collections import Counter, defaultdict
from pathlib import Path
import hashlib
import json
import re
import uuid

import numpy as np
import yaml
from shapely import make_valid, set_precision
from shapely.geometry import LineString, Polygon, mapping, shape
from shapely.geometry.polygon import orient
from shapely.ops import linemerge, nearest_points, polygonize, unary_union

ROOT = Path(__file__).resolve().parent.parent


def read_config(path):
    path = Path(path).resolve()
    config = yaml.safe_load(path.read_text())
    config['building_uuids'] = list(dict.fromkeys(str(uuid.UUID(v)) for v in config['building_uuids']))
    if not config['building_uuids']:
        raise ValueError('Specify at least one building UUID')
    config['source'] = config.get('source', 'database')
    if config['source'] not in {'database', 'template'}:
        raise ValueError('source must be database or template')
    if config['source'] == 'template' and not config['paths'].get('template'):
        raise ValueError('paths.template is required when source is template')
    for key, value in config['paths'].items():
        config['paths'][key] = str((path.parent / value).resolve())
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
    """Vertex-to-segment snapping across the entire input batch.

    Freeze each preceding boundary, project later vertices to it, then node
    common linework when the caller overlays areas. Deterministic input order
    replaces FME's undocumented automatic cleaning/order choices.
    """
    output = []
    reference = None
    for polygon in polygons:
        if reference is None:
            adjusted = polygon
        else:
            rings = []
            for ring in [polygon.exterior, *polygon.interiors]:
                coordinates = []
                from shapely.geometry import Point
                for xy in list(ring.coords)[:-1]:
                    point = Point(xy)
                    nearest = nearest_points(point, reference)[1]
                    coordinates.append(nearest.coords[0] if point.distance(nearest) <= tolerance else xy)
                rings.append(coordinates)
            adjusted = valid_area(Polygon(rings[0], rings[1:]))
        output.append(adjusted)
        reference = unary_union([p.boundary for p in output])
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
    return [valid_area(unary_union(items)) for items in assigned]


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


def model_features(records, buildings, selected):
    result = []
    for identifier, group in groups(records, lambda r: r['uuid']).items():
        if identifier not in selected:
            continue
        shells, summaries = [], []
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
        template = Path(config['paths']['template'])
        result['template.geojson'] = hashlib.sha256(template.read_bytes()).hexdigest()
    return result
