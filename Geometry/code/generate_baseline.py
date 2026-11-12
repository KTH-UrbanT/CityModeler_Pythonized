"""Generate baseline 3D sections and the complete contextual handoff for simplification.

Run: ../venv/bin/python code/generate_baseline.py [--config inputs/config.yml] [--uuid UUID]
"""
import argparse
import json
import math
import sqlite3
from pathlib import Path

import laspy
import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds, transform as window_transform
from scipy.spatial import ConvexHull, Delaunay, QhullError
from shapely import STRtree, force_2d, from_wkb, intersects_xy, points as make_points
from shapely.geometry import LineString, Point, Polygon, box, mapping, shape
from shapely.ops import nearest_points, polygonize, unary_union

if __package__:
    from .model_common import (ROOT, buffer, collection, edges, encode_records, groups, merge_lines,
                              model_features, parts, read_config, snap_areas, sources_manifest,
                              valid_area, write_json)
else:
    from model_common import (ROOT, buffer, collection, edges, encode_records, groups, merge_lines,
                              model_features, parts, read_config, snap_areas, sources_manifest,
                              valid_area, write_json)



class RejectedBuilding(ValueError):
    """A source feature follows a connected FME rejection/filter path."""


def load_context(config):
    """SQLExecutor plus Walls_Worker's Zones join: retain complete area batches."""
    if config['source'] == 'template':
        return load_template_context(config)
    database = Path(config['paths']['database'])
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        selected = config['building_uuids']
        placeholders = ','.join('?' for _ in selected)
        rows = conn.execute(f'SELECT "50A_UUID", Deso_id FROM Buildings WHERE "50A_UUID" IN ({placeholders})', selected).fetchall()
        missing = set(selected) - {r['50A_UUID'] for r in rows}
        if missing:
            raise ValueError(f'UUIDs missing from prepared database: {sorted(missing)}')
        target_zones = sorted({r['Deso_id'] for r in rows})
        neighbors = {}
        for zone in target_zones:
            row = conn.execute('SELECT zones FROM Zones WHERE Deso_id=?', (zone,)).fetchone()
            if row is None:
                raise ValueError(f'Missing Zones context for {zone}')
            neighbors[zone] = [z.strip() for z in (row['zones'] or '').split(',') if z.strip()]
        all_zones = sorted(set(target_zones).union(*map(set, neighbors.values())))
        placeholders = ','.join('?' for _ in all_zones)
        rows = conn.execute(f'SELECT * FROM Buildings WHERE Deso_id IN ({placeholders}) ORDER BY Deso_id, byggnadsid', all_zones).fetchall()
    buildings = {}
    for row in rows:
        attrs = dict(row)
        identifier = attrs['50A_UUID']
        if identifier in buildings:
            raise ValueError(f'Duplicate prepared UUID {identifier}')
        geometry = valid_area(from_wkb(attrs.pop('_geometry')))
        buildings[identifier] = {'polygon': geometry, 'attributes': attrs}
    return buildings, {'target_zones': target_zones, 'neighbors': neighbors, 'all_zones': all_zones}


def template_footprint(geometry):
    """Use a 2D footprint directly or project the bottom faces of 3D shells."""
    if geometry['type'] in {'Polygon', 'MultiPolygon'}:
        return valid_area(force_2d(shape(geometry)))
    if geometry['type'] != 'GeometryCollection':
        raise ValueError('Template geometry must be Polygon, MultiPolygon, or GeometryCollection')
    bases = []
    for shell in geometry.get('geometries', []):
        if shell.get('type') not in {'Polygon', 'MultiPolygon'}:
            continue
        coordinates = ([shell['coordinates']] if shell['type'] == 'Polygon'
                       else shell['coordinates'])
        heights = [point[2] for face in coordinates for ring in face for point in ring
                   if len(point) >= 3]
        if not heights:
            bases.extend(parts(force_2d(shape(shell))))
            continue
        bottom = min(heights)
        for face in coordinates:
            if not face or not face[0]:
                continue
            if all(len(point) >= 3 and abs(point[2] - bottom) < 1e-4
                   for point in face[0]):
                bases.append(Polygon([(point[0], point[1]) for point in face[0]],
                                     [[(point[0], point[1]) for point in ring]
                                      for ring in face[1:]]))
    if not bases:
        raise ValueError('Template 3D geometry has no horizontal bottom face')
    return valid_area(unary_union(bases))


def load_template_context(config):
    """Read building footprints and properties from a user-supplied GeoJSON."""
    path = Path(config['paths']['template'])
    data = json.loads(path.read_text())
    features = data['features'] if data.get('type') == 'FeatureCollection' else [data]
    if data.get('type') == 'FeatureCollection':
        name = data.get('crs', {}).get('properties', {}).get('name')
        if name and name != config['crs']:
            raise ValueError(f'Template CRS is {name}; expected {config["crs"]}')
    selected = set(config['building_uuids'])
    buildings = {}
    used_ids = set()
    for feature in features:
        if feature.get('type') != 'Feature':
            raise ValueError('Template must contain GeoJSON Features')
        attrs = dict(feature.get('properties') or {})
        identifier = attrs.get('50A_UUID')
        if identifier not in selected:
            continue
        if identifier in buildings:
            raise ValueError(f'Duplicate UUID in template: {identifier}')
        footprint = template_footprint(feature['geometry'])
        attrs.pop('sections', None)
        attrs.pop('_geometry', None)
        attrs['50A_UUID'] = identifier
        zone = str(attrs.get('Deso_id') or attrs.get('Deso') or 'TEMPLATE')
        attrs['Deso_id'] = zone
        attrs.setdefault('Deso', zone)
        building_id = attrs.get('byggnadsid')
        if building_id is None:
            building_id = len(buildings) + 1
        building_id = int(building_id)
        if building_id in used_ids:
            raise ValueError(f'Duplicate byggnadsid in template: {building_id}')
        used_ids.add(building_id)
        attrs['byggnadsid'] = building_id
        attrs['_orginalarea'] = float(footprint.area)
        buildings[identifier] = {'polygon': footprint, 'attributes': attrs}
    missing = selected - set(buildings)
    if missing:
        raise ValueError(f'UUIDs missing from template: {sorted(missing)}')
    zones = sorted({b['attributes']['Deso_id'] for b in buildings.values()})
    return buildings, {'target_zones': zones, 'neighbors': {z: [] for z in zones},
                       'all_zones': zones}


def source_files(directory, suffixes):
    path = Path(directory)
    return sorted([path] if path.is_file() else [p for p in path.rglob('*') if p.suffix.lower() in suffixes])


def read_clouds(config, buildings):
    """Stream each intersecting tile once and distribute samples to every footprint."""
    identifiers = list(buildings)
    polygons = [buildings[u]['polygon'] for u in identifiers]
    tree = STRtree(polygons)
    chunks = [[] for _ in polygons]
    sources = []
    for path in source_files(config['paths']['point_cloud'], {'.las', '.laz'}):
        with laspy.open(path) as reader:
            h = reader.header
            tile = box(h.mins[0], h.mins[1], h.maxs[0], h.maxs[1])
            if not len(tree.query(tile, predicate='intersects')):
                continue
            crs = h.parse_crs()
            if crs is None and path.with_suffix('.json').exists():
                crs = json.loads(path.with_suffix('.json').read_text()).get('crs', {}).get('properties', {}).get('name')
            if crs is None or rasterio.crs.CRS.from_user_input(crs) != rasterio.crs.CRS.from_string(config['crs']):
                raise ValueError(f'Point-cloud CRS missing or different: {path}')
            sources.append(str(path))
            print(f'Reading point-cloud tile: {path.name}', flush=True)
            for chunk in reader.chunk_iterator(500_000):
                xyz = np.column_stack([np.asarray(chunk.x), np.asarray(chunk.y), np.asarray(chunk.z)])
                query = tree.query(make_points(xyz[:, :2]), predicate='intersects')
                for index in np.unique(query[1]):
                    chosen = xyz[query[0, query[1] == index]]
                    chunks[index].append(chosen[np.isfinite(chosen).all(axis=1)])
    cloud = {u: np.concatenate(chunks[i]) if chunks[i] else np.empty((0, 3)) for i, u in enumerate(identifiers)}
    return cloud, sources


class Terrain:
    """Keep tile bounds in memory; read small windows for cell-center medians."""
    def __init__(self, config):
        self.tiles = []
        for path in source_files(config['paths']['terrain'], {'.tif', '.tiff'}):
            if 'metadata' in path.parts or 'density' in path.name.lower():
                continue
            with rasterio.open(path) as ds:
                if ds.crs != rasterio.crs.CRS.from_string(config['crs']):
                    raise ValueError(f'Terrain CRS differs: {path}')
                self.tiles.append((path, box(*ds.bounds)))

    def values(self, polygon):
        result = {}
        if polygon.is_empty:
            return np.empty(0)
        for path, bounds in self.tiles:
            if not bounds.intersects(polygon):
                continue
            with rasterio.open(path) as ds:
                window = from_bounds(*polygon.bounds, ds.transform).round_offsets().round_lengths()
                data = ds.read(1, window=window, masked=True, boundless=True)
                transform = window_transform(window, ds.transform)
                if not data.size:
                    continue
                mask = geometry_mask([mapping(polygon)], out_shape=data.shape, transform=transform, invert=True)
                for row, col in zip(*np.where(mask & ~np.ma.getmaskarray(data))):
                    xy = transform * (col + .5, row + .5)
                    value = float(data[row, col])
                    if np.isfinite(value):
                        result[xy] = value  # RasterMosaicker: last non-NoData wins.
        return np.asarray(list(result.values()))


def heights(polygon, cloud, terrain, settings):
    interior = buffer(polygon, -settings['height_inset_m'])
    values = terrain.values(interior)
    samples = cloud[intersects_xy(interior, cloud[:, 0], cloud[:, 1])]
    if not len(values) or not len(samples):
        raise RejectedBuilding('Empty terrain/roof sample after inward buffer')
    return round(float(np.median(values)), 2), float(np.median(samples[:, 2]))


def clean_cloud(cloud, footprint, ground, settings):
    # Custom transformer input maps Groundheight -> Groundheight.mean.
    interior = buffer(footprint, -settings['cleaner_inset_m'])
    cloud = cloud[intersects_xy(interior, cloud[:, 0], cloud[:, 1])]
    cloud = cloud[cloud[:, 2] > ground + settings['cleaner_offset_m']]
    cloud = cloud[np.argsort(cloud[:, 2], kind='stable')]
    low = math.ceil(len(cloud) * settings['percentile_low'] / 100)
    high = math.ceil(len(cloud) * settings['percentile_high'] / 100)
    if low >= high:
        return cloud[:0]
    # FME's Sorter does not specify a tie-break key. Its selection at the
    # percentile boundaries can depend on feature arrival order. Retain all
    # points at either boundary elevation so roof divisions do not depend on
    # the LAS reader's ordering of points with equal Z. This is an intentional
    # robustness choice, not an exact copy of FME's tied-point selection.
    lower_z, upper_z = cloud[low, 2], cloud[high - 1, 2]
    return cloud[(cloud[:, 2] >= lower_z) & (cloud[:, 2] <= upper_z)]


def surface_tin(cloud, tolerance):
    """Delaunay TIN, adding greatest residual per triangle until Z error <= tolerance.

    Implements tolerance-controlled surface modelling. Qhull triangulation and
    point-insertion choices replace FME's proprietary SurfaceModeller internals.
    """
    if len(cloud) < 3:
        raise RejectedBuilding('Insufficient cleaned samples for TIN')
    _, unique = np.unique(cloud[:, :2], axis=0, return_index=True)
    cloud = cloud[np.sort(unique)]
    xy = cloud[:, :2] - cloud[0, :2]
    try:
        selected = set(ConvexHull(xy).vertices.tolist())
        while True:
            ids = np.asarray(sorted(selected))
            tin = Delaunay(xy[ids])
            simplex = tin.find_simplex(xy)
            outside = np.where(simplex < 0)[0]
            safe = np.maximum(simplex, 0)
            delta = xy - tin.transform[safe, 2]
            bary = np.einsum('ijk,ik->ij', tin.transform[safe, :2], delta)
            weights = np.column_stack([bary, 1-bary.sum(axis=1)])
            predicted = (cloud[ids[tin.simplices[safe]], 2] * weights).sum(axis=1)
            error = np.abs(predicted - cloud[:, 2])
            error[list(selected)] = 0
            bad = np.where(error > tolerance)[0]
            insert = set(outside) - selected
            for triangle in np.unique(simplex[bad]):
                candidates = bad[simplex[bad] == triangle]
                insert.add(int(candidates[np.argmax(error[candidates])]))
            insert -= selected
            if not insert:
                return cloud[ids[tin.simplices]], float(error.max(initial=0))
            selected.update(insert)
    except QhullError as error:
        raise RejectedBuilding('Degenerate or collinear TIN input') from error


def steep_regions(cloud, footprint, settings):
    triangles, residual = surface_tin(cloud, settings['tin_tolerance_m'])
    normals = np.cross(triangles[:, 1]-triangles[:, 0], triangles[:, 2]-triangles[:, 0])
    nz = np.abs(normals[:, 2]) / np.linalg.norm(normals, axis=1)
    inset = buffer(footprint, -settings['triangle_inset_m'])
    steep = []
    for triangle in triangles[nz <= settings['normal_z_max']]:
        for polygon in parts(Polygon(triangle[:, :2]).intersection(inset)):
            if polygon.distance(inset.boundary) > settings['edge_exclusion_m']:
                steep.append(buffer(polygon, settings['triangle_buffer_m']))
    dissolved = [p for p in parts(unary_union(steep)) if p.area > settings['region_min_area_m2']]
    regions = parts(unary_union([buffer(p, settings['region_buffer_m']) for p in dissolved]))
    return regions, {'tin_triangles': len(triangles), 'tin_max_residual_m': residual, 'steep_triangles': len(steep)}


def centerline(region):
    corners = np.asarray(region.minimum_rotated_rectangle.exterior.coords)[:4]
    lengths = np.linalg.norm(np.roll(corners, -1, axis=0)-corners, axis=1)
    i = int(np.argmin(lengths))
    a, b = (corners[i]+corners[(i+1)%4])/2, (corners[(i+2)%4]+corners[(i+3)%4])/2
    return LineString([a, (a+b)/2, b])


def cutting_lines(regions, footprint, settings):
    boundary = [edge for p in parts(footprint) for edge in edges(p)]
    output = []
    plans, used_first_edges = [], set()
    for region in regions:
        line = centerline(region)
        a, b = Point(line.coords[0]), Point(line.coords[-1])
        first = min(range(len(boundary)), key=lambda i: a.distance(boundary[i]))
        if a.distance(boundary[first]) <= settings['endpoint_distance_m']:
            used_first_edges.add(first)
        plans.append((line, a, b, first))
    # NeighborFinder_3's UNMATCHED_CANDIDATE port is shared across centerlines.
    unused = [i for i in range(len(boundary)) if i not in used_first_edges]
    for line, a, b, first in plans:
        connections = [line]
        endpoints = [(a, first)]
        if unused:
            second = min(unused, key=lambda i: b.distance(boundary[i]))
            endpoints.append((b, second))
        for endpoint, i in endpoints:
            target = nearest_points(endpoint, boundary[i])[1]
            x, y = np.asarray(endpoint.coords[0]), np.asarray(target.coords[0])
            distance = np.linalg.norm(y-x)
            if 0 < distance <= settings['endpoint_distance_m']:
                end = y + (y-x)/distance * settings['extension_m']
                connections.append(LineString([x, y, end]))
        combined = merge_lines([p.simplify(settings['line_simplification_m']) for p in connections])
        combined = [p.simplify(settings['line_simplification_m']) for p in combined]
        avg = sum(p.length for p in combined) / sum(len(p.coords) for p in combined)
        if avg < settings['average_segment_m']:
            continue
        for p in combined:
            c = np.asarray(p.coords)
            # Only the SpikeRemover UNCHANGED port is connected.
            if not any(np.dot(x-y, z-y) > 0 for x, y, z in zip(c, c[1:], c[2:])):
                output.append(p)
    return output


def cut_polygon(footprint, lines, tolerance=0.1):
    # PolygonCutter: Intersector -> vertex snapping -> AreaBuilder.
    from scipy.spatial import cKDTree
    noded = parts(unary_union([footprint.boundary, *lines]), 'LineString')
    coordinates = sorted({tuple(xy) for line in noded for xy in line.coords})
    if not coordinates:
        return []
    tree = cKDTree(coordinates)
    representative = {}
    for i, xy in enumerate(coordinates):
        if i not in representative:
            for j in tree.query_ball_point(xy, tolerance):
                representative.setdefault(j, xy)
    lookup = {xy: representative[i] for i, xy in enumerate(coordinates)}
    snapped = [LineString([lookup[tuple(xy)] for xy in line.coords]) for line in noded]
    return [p for p in polygonize(unary_union([l for l in snapped if l.length > 0]))
            if footprint.covers(p.representative_point())]


def regroup(pieces, settings, original_area=None):
    # Executable TestFilter_4 is <=50 m² OR <10%, despite its old >100 port label.
    total = original_area if original_area is not None else sum(p.area for p in pieces)
    if total <= 0:
        raise ValueError('Invalid original area for cut-part fraction')
    small, retained = [], []
    for p in pieces:
        (small if p.area <= settings['small_area_m2'] or p.area/total < settings['small_area_fraction'] else retained).append(p)
    grouped = [[p] for p in retained]
    unmatched = []
    for area in parts(unary_union(small)):
        near = sorted((i for i, p in enumerate(retained) if area.distance(p) <= settings['part_join_m']), key=lambda i: (area.distance(retained[i]), i))
        if near:
            grouped[near[0]].append(area)
        else:
            unmatched.append(area)
    return unmatched + [p for group in grouped for p in parts(unary_union(group))]


def process_zone(zone, identifiers, buildings, clouds, terrain, settings):
    """Preserve batch context for ungrouped Snapper and area overlay stages."""
    candidates, untouched, stages, reports = [], [], [], []
    initial_heights = {}
    for identifier in identifiers:
        footprint = buildings[identifier]['polygon']
        cloud = clouds[identifier]
        try:
            ground, roof = heights(footprint, cloud, terrain, settings)
            if roof-ground <= settings['minimum_height_m']:
                raise RejectedBuilding('Initial Buildingheight <= 3 m')
            initial_heights[identifier] = (ground, roof)
            cleaned = clean_cloud(cloud, footprint, ground, settings)
            try:
                regions, stats = steep_regions(cleaned, footprint, settings)
                lines = cutting_lines(regions, footprint, settings)
            except RejectedBuilding as error:
                # The polygon branch still reaches PolygonCutter's UNTOUCHED port.
                regions, lines = [], []
                stats = {'tin_rejected': str(error)}
            cuts = cut_polygon(footprint, lines, settings['cutter_vertex_snap_m'])
            changed = (len(cuts) != len(parts(footprint))
                       or unary_union(cuts).symmetric_difference(footprint).area > 1e-8)
            reports.append({'uuid': identifier, 'status': 'processed', 'raw_points': len(cloud), 'cleaned_points': len(cleaned), **stats, 'cutting_lines': len(lines)})
            for kind, geometries in [('steep_region', regions), ('cutting_line', lines), ('cut_polygon', cuts)]:
                stages.extend({'type': 'Feature', 'properties': {'50A_UUID': identifier, 'stage': kind}, 'geometry': mapping(p)} for p in geometries)
            if changed:
                candidates.extend((identifier, p) for p in cuts)
            else:
                untouched.extend((identifier, p) for p in parts(footprint))
        except RejectedBuilding as error:
            reports.append({'uuid': identifier, 'status': 'filtered', 'reason': str(error)})
    # Clipper_4 removes cut geometries covered by untouched parts of same ID.
    unclipped = []
    for identifier, polygon in candidates:
        mask = unary_union([p for u, p in untouched if u == identifier])
        unclipped.extend((identifier, p) for p in parts(polygon.difference(mask)))
    records = []
    if unclipped:
        snapped = snap_areas([p for _, p in unclipped], settings['snap_m'])
        faces = list(polygonize(unary_union([p.boundary for p in snapped])))
        by_owner = {}
        # FME CenterPointReplacer -> NeighborFinder (distance 0, area mode).
        for face in faces:
            point = face.representative_point()
            owner = next((u for u in initial_heights if buildings[u]['polygon'].covers(point)), None)
            if owner:
                # Original courtyard points >=1 m² exclude overlaid hole faces.
                holes = [Polygon(r).representative_point() for p in parts(buildings[owner]['polygon']) for r in p.interiors if Polygon(r).area >= 1]
                if not any(face.covers(p) for p in holes):
                    by_owner.setdefault(owner, []).append(face)
        for owner, pieces in by_owner.items():
            records.extend((owner, p, 'yes') for p in regroup(pieces, settings, float(buildings[owner]['attributes']['_orginalarea'])))
    records.extend((u, p, 'no') for u, p in untouched)
    output = []
    for identifier, polygon, was_cut in records:
        try:
            ground, roof = (initial_heights[identifier] if was_cut == 'no'
                            else heights(polygon, clouds[identifier], terrain, settings))
            attrs = buildings[identifier]['attributes']
            output.append({'uuid': identifier, 'building_id': attrs['byggnadsid'], 'zone': zone,
                           'polygon': polygon, 'ground': ground, 'roof': roof, 'height': roof-ground,
                           'Footprint_cut': was_cut, 'section_id': f'{attrs["byggnadsid"]}:{len(output)}'})
        except RejectedBuilding as error:
            reports.append({'uuid': identifier, 'status': 'section_filtered', 'area_m2': polygon.area, 'reason': str(error)})
    return output, reports, stages


def run(config):
    buildings, context = load_context(config)
    output = Path(config['paths']['output'])
    print(f'Context: {len(buildings)} buildings in {len(context["all_zones"])} DeSO areas', flush=True)
    clouds, sources = read_clouds(config, buildings)
    terrain = Terrain(config)
    records, reports, stages = [], [], []
    for zone in context['all_zones']:
        identifiers = [u for u, b in buildings.items() if b['attributes']['Deso_id'] == zone]
        result, report, debug = process_zone(zone, identifiers, buildings, clouds, terrain, config['baseline'])
        records.extend(result); reports.extend(report); stages.extend(debug)
        print(f'{zone}: {len(identifiers)} buildings -> {len(result)} baseline sections', flush=True)
    # Nonpositive sections cannot be extruded; preserve them in handoff for script 2's height filter.
    drawable = [r for r in records if r['height'] > 0]
    write_json(output/'baseline.geojson', collection(model_features(drawable, buildings, config['building_uuids'])))
    serial_buildings = {u: {'polygon': mapping(b['polygon']), 'attributes': b['attributes']} for u, b in buildings.items()}
    handoff = {'schema_version': 1, 'crs': config['crs'], 'selected_uuids': config['building_uuids'],
               'context': context, 'buildings': serial_buildings, 'records': encode_records(records),
               'source_workspaces': sources_manifest(config), 'config': config, 'cloud_sources': sources}
    write_json(output/'baseline_context.json', handoff)
    write_json(output/'baseline_stages.geojson', collection(stages))
    missing = sorted(set(config['building_uuids']) - {r['uuid'] for r in drawable})
    write_json(output/'baseline_report.json', {'buildings_in_context': len(buildings), 'sections': len(records), 'missing_requested_uuids': missing, 'features': reports})
    if missing:
        print(f'Skipped {len(missing)} buildings without baseline geometry; see baseline_report.json', flush=True)
    print(f'Baseline and contextual handoff written to {output}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=ROOT/'inputs/config.yml')
    parser.add_argument('--uuid')
    args = parser.parse_args()
    config = read_config(args.config)
    if args.uuid:
        import uuid
        config['building_uuids'] = [str(uuid.UUID(args.uuid))]
    run(config)
