"""Continue the FME graph: final footprints, small-wall reconstruction and wall exports.

Run: ../venv/bin/python code/simplify_model.py [--config inputs/config.yml]
The baseline_context.json handoff includes the complete DeSO neighbourhood.
"""
import argparse
from collections import Counter
import copy
import json
import math
from pathlib import Path

import numpy as np
from shapely import STRtree, set_precision
from shapely.errors import GEOSException
from shapely.geometry import LineString, Point, Polygon, mapping, shape
from shapely.ops import nearest_points, polygonize, unary_union

if __package__:
    from .model_common import (ROOT, collection, decode_records, edges, encode_records, groups,
                              merge_lines, model_features, parts, read_config, shared_generalization,
                              snap_areas, sources_manifest, valid_area, write_json)
else:
    from model_common import (ROOT, collection, decode_records, edges, encode_records, groups,
                              merge_lines, model_features, parts, read_config, shared_generalization,
                              snap_areas, sources_manifest, valid_area, write_json)



def dissolve(records):
    """Dissolver_2/_5: roof elevation + UUID + building ID; first attributes win."""
    result = []
    for group in groups(records, lambda r: (r['roof'], r['uuid'], r['building_id'])).values():
        for polygon in parts(unary_union([r['polygon'] for r in group])):
            contributors = [r for r in group if r['polygon'].intersection(polygon).area > 1e-8]
            result.append({**contributors[0], 'polygon': polygon,
                           'source_sections': [r['section_id'] for r in contributors]})
    return result


def remove_repeated_bumps(polygon, settings):
    """Simplify only outlines with repeated, similarly directed short steps."""
    cleanup = settings.get('bump_cleanup', {})
    if not cleanup.get('enabled', False) or polygon.geom_type != 'Polygon' or polygon.interiors:
        return polygon, False
    tolerance = cleanup.get('tolerance_m', 1.2)
    if not has_repeated_bumps(polygon, tolerance):
        return polygon, False
    candidate = polygon.simplify(tolerance, preserve_topology=True)
    area_change = polygon.symmetric_difference(candidate).area / polygon.area
    if (candidate.geom_type != 'Polygon' or not candidate.is_valid or
            len(polygon.exterior.coords) - len(candidate.exterior.coords) < 3 or
            area_change > cleanup.get('max_area_change_fraction', 0.02)):
        return polygon, False
    return candidate, True


def has_repeated_bumps(polygon, tolerance):
    if polygon.geom_type != 'Polygon' or polygon.interiors:
        return False
    short_edges = []
    distance = 0.0
    for a, b in zip(polygon.exterior.coords, list(polygon.exterior.coords)[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if 0.5 * tolerance <= length <= 1.25 * tolerance:
            short_edges.append((distance, math.atan2(dy, dx) % math.pi))
        distance += length
    return any(sum(0 <= other_distance - start <= 40 and
                   min(abs(angle - direction), math.pi - abs(angle - direction)) <= math.radians(20)
                   for other_distance, angle in short_edges) >= 3
               for start, direction in short_edges)


def clean_multi_section_bumps(polygons, settings):
    """Clean the shared outer outline, then assign its changes to roof sections."""
    cleanup = settings.get('bump_cleanup', {})
    original = unary_union(polygons)
    if original.geom_type != 'Polygon' or original.interiors:
        return polygons, False
    tolerance = cleanup.get('tolerance_m', 1.2)
    candidate = None
    while tolerance >= 0.5:
        trial = original.simplify(tolerance, preserve_topology=True)
        change = original.symmetric_difference(trial).area / original.area
        if (trial.geom_type == 'Polygon' and trial.is_valid and
                len(original.exterior.coords) - len(trial.exterior.coords) >= 3 and
                change <= cleanup.get('max_area_change_fraction', 0.02)):
            candidate = trial
            break
        tolerance = round(tolerance - 0.1, 2)
    if candidate is None:
        return polygons, False
    additions = [[] for _ in polygons]
    for piece in parts(candidate.difference(original)):
        owner = max(range(len(polygons)), key=lambda i: piece.boundary.intersection(polygons[i].boundary).length)
        additions[owner].append(piece)
    try:
        revised = [valid_area(unary_union([polygon.intersection(candidate), *additions[i]]))
                   for i, polygon in enumerate(polygons)]
    except (ValueError, GEOSException):
        return polygons, False
    if (unary_union(revised).symmetric_difference(candidate).area > 1e-6 or
            any(a.intersection(b).area > 1e-6 for i, a in enumerate(revised) for b in revised[i+1:])):
        return polygons, False
    return revised, True


def roof_merge_groups(records, mode, threshold):
    """Group adjacent sections without chaining across the roof-height limit."""
    if mode == 'overall':
        return [records] if max(r['roof'] for r in records) - min(r['roof'] for r in records) < threshold else [[r] for r in records]
    if mode != 'neighboring':
        raise ValueError('simplification.roof_merge_mode must be overall or neighboring')
    clusters = [[i] for i in range(len(records))]
    edges = sorted((abs(a['roof'] - b['roof']), i, j)
                   for i, a in enumerate(records) for j, b in enumerate(records)
                   if i < j and a['polygon'].boundary.intersection(b['polygon'].boundary).length > 1e-6)
    for _, i, j in edges:
        first = next(c for c in clusters if i in c)
        second = next(c for c in clusters if j in c)
        if first is second:
            continue
        merged = first + second
        if max(records[k]['roof'] for k in merged) - min(records[k]['roof'] for k in merged) < threshold:
            first.extend(second)
            clusters.remove(second)
    return [[records[i] for i in cluster] for cluster in clusters]


def final_footprints(records, settings):
    """Height branches -> two dissolves -> round -> shared generalize -> snap."""
    selected, bypass = [], []
    records = copy.deepcopy([r for r in records if r['height'] > settings['minimum_height_m']])
    original_groups = groups(records, lambda r: r['uuid'])
    multi_bump_ids = {identifier for identifier, group in original_groups.items()
                      if len(group) > 1 and settings.get('bump_cleanup', {}).get('enabled', False)
                      and has_repeated_bumps(unary_union([r['polygon'] for r in group]),
                                             settings['bump_cleanup'].get('tolerance_m', 1.2))}
    mode = settings.get('roof_merge_mode', 'overall')
    for group in groups(records, lambda r: (r['uuid'], r['building_id'])).values():
        for cluster in roof_merge_groups(group, mode, settings['roof_range_m']):
            if len(cluster) == 1:
                bypass.extend(cluster)
                continue
            roof = max(r['roof'] for r in cluster)
            ground = min(r['ground'] for r in cluster)
            height = roof - ground if mode == 'neighboring' else max(r['height'] for r in cluster)
            for r in cluster:
                r.update(roof=roof, ground=ground, height=height)
            selected.extend(cluster)
    records = dissolve(dissolve(selected) + bypass)
    if not records:
        return []
    rounded = [valid_area(set_precision(r['polygon'], 10**-settings['coordinate_decimals'])) for r in records]
    simplified = shared_generalization(rounded, settings['shared_tolerance_m'])
    snapped = snap_areas(simplified, settings['snap_m'])
    output = []
    section_counts = Counter(r['uuid'] for r in records)
    multi_sections = groups(list(enumerate(records)), lambda item: item[1]['uuid'])
    cleaned_multi = set()
    for identifier in multi_bump_ids:
        sections = multi_sections.get(identifier, [])
        if len(sections) < 2:
            continue
        indices = [index for index, _ in sections]
        revised, changed = clean_multi_section_bumps([snapped[index] for index in indices], settings)
        if changed:
            for index, polygon in zip(indices, revised):
                snapped[index] = polygon
            cleaned_multi.add(identifier)
    for r, geometry in zip(records, snapped):
        cleaned = r['uuid'] in cleaned_multi
        if section_counts[r['uuid']] == 1:
            geometry, cleaned = remove_repeated_bumps(geometry, settings)
        for p in parts(geometry):
            # 3DForcer+Extruder: top is Groundheight+Buildingheight, not Roofheight.
            output.append({**r, 'polygon': p, 'bump_cleanup_applied': cleaned,
                           'section_id': f'{r["building_id"]}:final:{len(output)}'})
    return output


def angle(line):
    """HorizontalAngleCalculator and zero-decimal AttributeRounder."""
    a, b = np.asarray(line.coords[0]), np.asarray(line.coords[-1])
    value = math.degrees(math.atan2(b[1]-a[1], b[0]-a[0])) % 360
    return math.floor(value + .5) % 360


def noded_walls(records):
    """Chopper -> Intersector: attach original owner/part/edge to each segment."""
    originals = []
    for r in records:
        for edge in edges(r['polygon']):
            originals.append({'line': edge, 'record': r, 'edge_id': len(originals)})
    if not originals:
        return []
    source_lines = [r['line'] for r in originals]
    index = STRtree(source_lines)
    result = []
    for line in parts(unary_union(source_lines), 'LineString'):
        # Keep every noded segment. Collinear duplicate inputs contribute to overlaps.
        for a, b in zip(line.coords, list(line.coords)[1:]):
            segment = LineString([a, b])
            owners = [originals[i] for i in index.query(segment) if source_lines[i].intersection(segment).length >= segment.length-1e-7]
            if owners:
                result.append({'line': segment, 'owners': owners, 'overlaps': len(owners),
                               'edge_id': owners[0]['edge_id'], 'record': owners[0]['record']})
    return result


def nearby_wall_pairs(exterior, midlines, settings):
    """ExtractNearbyWalls: per-part delta<1, count=4, angle chains of 2, sum=0."""
    associated = []
    for wall in exterior:
        choices = [(wall['line'].distance(mid['line']), i) for i, mid in enumerate(midlines)]
        if not choices:
            continue
        distance, index = min(choices)
        if distance <= settings['nearby_wall_m']:
            associated.append({**wall, 'mid_id': index})
    length_pass = []
    for group in groups(associated, lambda w: (w['record']['section_id'], w['mid_id'])).values():
        lengths = [w['line'].length for w in group]
        if max(lengths)-min(lengths) < settings['length_difference_m']:
            length_pass.extend(group)
    output = []
    for mid_id, walls in groups(length_pass, lambda w: w['mid_id']).items():
        if len(walls) != 4:
            continue
        combined = []
        for subset in groups(walls, lambda w: (angle(w['line']), w['record']['building_id'])).values():
            for line in merge_lines([w['line'] for w in subset]):
                contributing = [w for w in subset if line.intersection(w['line']).length > 1e-7]
                combined.append({'line': line, 'walls': contributing, 'mid_id': mid_id})
        # HighAngleratio is 0 only for two contributors; sum must be 0 per midline.
        if combined and all(len(c['walls']) == 2 for c in combined):
            output.extend(combined)
    return output


def replacement_centerlines(pairs, midlines, settings):
    """IdentifyWallstoRemove: node -> distance sort -> first two -> LineBuilder."""
    replacements = []
    for mid_id, group in groups(pairs, lambda c: c['mid_id']).items():
        network = unary_union([c['line'] for c in group])
        coordinates = sorted({tuple(xy) for line in parts(network, 'LineString') for xy in line.coords})
        # Intersector NODE includes junctions. Stable coordinate ties replace FME order.
        candidates = []
        for xy in coordinates:
            p = Point(xy)
            choices = [(p.distance(m['line']), i) for i, m in enumerate(midlines)]
            distance, closest = min(choices)
            if distance > settings['short_wall_m']:
                distance, closest = settings['short_wall_m']+1, mid_id
            if closest == mid_id:
                candidates.append((distance, xy))
        chosen = sorted(candidates)[:2]
        if len(chosen) != 2 or chosen[0][1] == chosen[1][1]:
            continue
        line = LineString([chosen[0][1], chosen[1][1]])
        # NeighborFinder_4 assigns the nearest original midline ID/owner within 2 m.
        nearest = min(midlines, key=lambda m: line.distance(m['line']))
        replacements.append({'line': line, 'old': nearest,
                             'building_id': group[0]['walls'][0]['record']['building_id']})
    return replacements


def remove_small_walls(records, settings):
    """Complete connected RemoveSmallWalls path; single parts bypass it."""
    by_building = groups(records, lambda r: r['building_id'])
    eligible, passthrough = [], []
    for group in by_building.values():
        # Multiparts rounds XYZ to 6 decimals before the part count/filter.
        rounded = []
        decimals = settings['multipart_coordinate_decimals']
        for r in group:
            ground = round(r['ground'], decimals)
            top = round(r['ground'] + r['height'], decimals)
            rounded.append({**r, 'polygon': valid_area(set_precision(r['polygon'], 10**-decimals)),
                            'ground': ground, 'height': top-ground})
        shortest = min(e.length for r in rounded for e in edges(r['polygon']))
        if len(rounded) > 1 and shortest < settings['short_wall_m']:
            eligible.extend(rounded)
        else:
            passthrough.extend(rounded)
    if not eligible:
        return passthrough, {'eligible_buildings': 0, 'modified_buildings': 0}, []
    walls = noded_walls(eligible)
    mids = [w for w in walls if w['overlaps'] == 2]
    exterior = [w for w in walls if w['overlaps'] != 2]
    short = [w for w in exterior if w['line'].length < settings['short_wall_m']]
    nearby = [m for m in mids if any(m['line'].distance(w['line']) <= settings['nearby_wall_m'] for w in short)]
    pairs = nearby_wall_pairs(exterior, nearby, settings)
    replacements = replacement_centerlines(pairs, nearby, settings) if pairs else []
    changed_ids = {p['walls'][0]['record']['building_id'] for p in pairs}
    removed_ids = {w['edge_id'] for p in pairs for w in p['walls']}
    removed_mid_ids = {r['old']['edge_id'] for r in replacements}
    output = list(passthrough)
    trace = []
    modifications = 0
    for building_id, original in groups(eligible, lambda r: r['building_id']).items():
        if building_id not in changed_ids:
            output.extend(original)
            continue
        # FeatureMerger removes original walls listed by ListExploder and replaced mids.
        remaining = [w['line'] for w in walls if w['record']['building_id'] == building_id
                     and w['edge_id'] not in removed_ids | removed_mid_ids]
        rebuilt = [p['line'] for p in pairs if p['walls'][0]['record']['building_id'] == building_id]
        new_centers = [r['line'] for r in replacements if r['building_id'] == building_id]
        faces = list(polygonize(unary_union(remaining + rebuilt + new_centers)))
        recovered = []
        original_union = unary_union([r['polygon'] for r in original])
        for face in faces:
            # DonutFactory preserves holes; discard courtyard faces before attribute recovery.
            if not original_union.covers(face.representative_point()):
                continue
            geometry = valid_area(face.simplify(settings['reconstruction_tolerance_m'], preserve_topology=False))
            for polygon in parts(geometry):
                point = polygon.representative_point()
                donor = next((r for r in original if r['polygon'].covers(point)), None)
                if donor is None:
                    # Native unjoined stream has no usable _zmin/_zmax; report it.
                    trace.append({'stage': 'height_donor_missing', 'building_id': building_id, 'area_m2': polygon.area})
                    continue
                recovered.append({**donor, 'polygon': polygon, 'section_id': f'{building_id}:rebuilt:{len(recovered)}'})
        # FeatureMerger_3 retains the original building only if reconstruction yielded none.
        if recovered:
            output.extend(recovered)
            modifications += 1
        else:
            output.extend(original)
        trace.extend({'stage': 'replacement_centerline', 'building_id': building_id,
                      'geometry': mapping(line)} for line in new_centers)
        trace.append({'stage': 'reconstruction', 'building_id': building_id,
                      'original_parts': len(original), 'rebuilt_parts': len(recovered)})
    return output, {'eligible_buildings': len(groups(eligible, lambda r: r['building_id'])),
                    'shared_midlines': len(mids), 'near_short_midlines': len(nearby),
                    'recombined_wall_pairs': len(pairs), 'replacement_centerlines': len(replacements),
                    'modified_buildings': modifications}, trace


def create_walls(records, settings):
    """CreateWalls selects upward roofs, chops boundary segments and assigns V{id}-{n}."""
    result = []
    for building_id, group in groups(records, lambda r: r['building_id']).items():
        counter = 0
        for r in group:
            from shapely.geometry.polygon import orient
            for line in edges(orient(r['polygon'], sign=1)):
                if line.length <= settings['minimum_length_m']:
                    continue
                counter += 1
                result.append({'line': line, 'uuid': r['uuid'], 'building_id': building_id,
                               'zone': r['zone'], 'section_id': r['section_id'],
                               'vaggid': f'V{building_id}-{counter}',
                               'zmin': r['ground'], 'zmax': r['ground']+r['height']})
    return result


def shortened_connection(a, b, amount):
    """LineExtender -2: shorten both ends; rejected short lines keep original geometry."""
    a, b = np.asarray(a), np.asarray(b)
    distance = np.linalg.norm(b-a)
    if distance <= 2*amount:
        return LineString([a, b]) if distance else Point(a)
    unit = (b-a)/distance
    return LineString([a + amount*unit, b - amount*unit])


def shading_relationships(records, walls, context, settings):
    """Walls_Worker: 250 m centroid neighbours + <=1 m walls; exclude occluded links."""
    footprints = {bid: unary_union([r['polygon'] for r in group])
                  for bid, group in groups(records, lambda r: r['building_id']).items()}
    by_id = groups(records, lambda r: r['building_id'])
    selected_walls, relations = {}, []
    for zone in context['target_zones']:
        local = [w for w in walls if w['zone'] == zone]
        local_footprints = [p for bid, p in footprints.items() if by_id[bid][0]['zone'] == zone]
        neighbor_zones = set(context['neighbors'][zone])
        neighbors = [w for w in walls if w['zone'] in neighbor_zones
                     and any(w['line'].distance(p.boundary) <= settings['search_distance_m'] for p in local_footprints)]
        # AttributeExposer exports local + matched surrounding wall lines.
        # Native candidate ports include local walls twice; keep their effect on caps.
        candidates = neighbors + local + local
        for w in candidates:
            selected_walls[w['vaggid']] = w
        if not candidates:
            continue
        line_tree = STRtree([w['line'] for w in candidates])
        centers = [w['line'].interpolate(.5, normalized=True) for w in candidates]
        center_tree = STRtree(centers)
        area_candidates = [p for bid, p in footprints.items() if by_id[bid][0]['zone'] in neighbor_zones | {zone}]
        area_tree = STRtree(area_candidates)
        for base in local:
            center = base['line'].interpolate(.5, normalized=True)
            # NeighborFinder's limit includes self/same-building candidates before TestFilter.
            indices = center_tree.query(center, predicate='dwithin', distance=settings['search_distance_m'])
            indices = sorted(indices, key=lambda i: (center.distance(centers[i]), int(i)))[:settings['search_count']]
            accepted = {}
            for i in indices:
                candidate = candidates[i]
                if candidate['building_id'] == base['building_id']:
                    continue
                connection = shortened_connection(centers[i].coords[0], center.coords[0], settings['connection_shorten_m'])
                blocked = any(connection.crosses(area_candidates[j]) or connection.within(area_candidates[j]) or connection.contains(area_candidates[j])
                              for j in area_tree.query(connection))
                if not blocked:
                    accepted[candidate['vaggid']] = 'visible_250m'
            near = line_tree.query(base['line'], predicate='dwithin', distance=settings['touching_distance_m'])
            near = sorted(near, key=lambda i: (base['line'].distance(candidates[i]['line']), int(i)))[:settings['search_count']]
            for i in near:
                candidate = candidates[i]
                if candidate['building_id'] != base['building_id']:
                    accepted[candidate['vaggid']] = 'within_1m'
            # ListExploder overwrites vaggid with candidate id, keeps base building id.
            for wall_id, reason in accepted.items():
                relations.append({'building_id': base['building_id'], 'wall_id': wall_id, 'reason': reason})
    unique = {(r['building_id'], r['wall_id']): r for r in relations}
    return list(selected_walls.values()), list(unique.values())


def export_walls(walls):
    return [{'type': 'Feature', 'geometry': mapping(w['line']),
             'properties': {'vaggid': w['vaggid'], 'byggnadsid': w['building_id'], '50A_UUID': w['uuid'],
                            'section_id': w['section_id'], 'zmin': w['zmin'], 'zmax': w['zmax']}} for w in walls]


def run(config):
    folder = Path(config['paths']['output'])
    handoff = json.loads((folder/'baseline_context.json').read_text())
    if handoff['schema_version'] != 1 or handoff['source_workspaces'] != sources_manifest(config):
        raise ValueError('Baseline handoff/source version mismatch; regenerate baseline')
    if set(config['building_uuids']) != set(handoff['selected_uuids']):
        raise ValueError('UUID selection changed; regenerate baseline to retain full context')
    if config['source'] != handoff['config']['source']:
        raise ValueError('Input source changed; regenerate baseline')
    if config['baseline'] != handoff['config']['baseline']:
        raise ValueError('Baseline settings changed; regenerate baseline')
    for key in [config['source'], 'point_cloud', 'terrain']:
        if config['paths'][key] != handoff['config']['paths'][key]:
            raise ValueError(f'Baseline input path changed ({key}); regenerate baseline')
    records = decode_records(handoff['records'])
    buildings = {u: {**b, 'polygon': shape(b['polygon'])} for u, b in handoff['buildings'].items()}
    final, reports, trace = [], [], []
    for zone in handoff['context']['all_zones']:
        batch = [r for r in records if r['zone'] == zone]
        footprint_records = final_footprints(batch, config['simplification'])
        rebuilt, report, events = remove_small_walls(footprint_records, config['simplification'])
        final.extend(rebuilt)
        reports.append({'zone': zone, 'baseline_sections': len(batch), 'simplified_sections': len(rebuilt), **report})
        trace.extend(events)
        print(f'{zone}: {len(batch)} -> {len(rebuilt)} sections; {report["modified_buildings"]} buildings rebuilt', flush=True)
    walls = create_walls(final, config['walls'])
    exported_walls, relations = shading_relationships(final, walls, handoff['context'], config['walls'])
    features = model_features(final, buildings, config['building_uuids'])
    relation_groups = groups(relations, lambda r: r['building_id'])
    fme_exports = []
    for feature in features:
        props = feature['properties']
        neighbors = sorted(r['wall_id'] for r in relation_groups.get(props['byggnadsid'], []))
        props.update(vaggid=';'.join(neighbors), NumberOfShadingWalls=len(neighbors))
        try:
            props['height'] = float(props.get('EgenAntalPlan'))*3
        except (ValueError, TypeError):
            props['height'] = None
        props['Footprint_area_Sweref'] = sum(s['area_m2'] for s in props['sections'])*2
        # Native FeatureMerger connects only MERGED; keep geometry-only output too.
        props['fme_export_branch'] = 'MERGED' if neighbors else 'UNMERGED_REQUESTOR'
        if neighbors:
            fme_exports.append(feature)
    selected_ids = {buildings[u]['attributes']['byggnadsid'] for u in config['building_uuids']}
    selected_relations = [r for r in relations if r['building_id'] in selected_ids]
    relevant_ids = {r['wall_id'] for r in selected_relations}
    wall_export = [w for w in exported_walls if w['building_id'] in selected_ids or w['vaggid'] in relevant_ids]
    write_json(folder/'simplified.geojson', collection(features))
    write_json(folder/'buildings_export.geojson', collection(fme_exports))
    write_json(folder/'walls.geojson', collection(export_walls(wall_export)))
    write_json(folder/'wall_relationships.json', selected_relations)
    write_json(folder/'simplified_context.json', {'records': encode_records(final), 'wall_count': len(walls)})
    missing = sorted(set(config['building_uuids']) - {f['properties']['50A_UUID'] for f in features})
    write_json(folder/'simplification_report.json', {'zones': reports, 'small_wall_events': trace,
               'context_wall_count': len(walls), 'exported_wall_count': len(wall_export),
               'selected_buildings': len(features), 'native_merged_export_buildings': len(fme_exports),
               'bump_cleanup_uuids': sorted({r['uuid'] for r in final if r.get('bump_cleanup_applied')}),
               'missing_requested_uuids': missing})
    if missing:
        print(f'Skipped {len(missing)} buildings during simplification; see simplification_report.json', flush=True)
    print(f'Combined models, walls and relationships written to {folder}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=ROOT/'inputs/config.yml')
    parser.add_argument('--uuid', help='Match a baseline --uuid selection')
    args = parser.parse_args()
    config = read_config(args.config)
    if args.uuid:
        import uuid
        config['building_uuids'] = [str(uuid.UUID(args.uuid))]
    run(config)
