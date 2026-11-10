"""Continue the FME graph: final footprints, small-wall reconstruction and wall exports.

Run: ../venv/bin/python code/simplify_model.py [--config inputs/config.yml]
The baseline_context.json handoff includes the complete DeSO neighbourhood.
"""
import argparse
import copy
import json
import math
from pathlib import Path

import numpy as np
from shapely import STRtree, set_precision
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


def final_footprints(records, settings):
    """Height branches -> two dissolves -> round -> shared generalize -> snap."""
    selected, bypass = [], []
    records = copy.deepcopy([r for r in records if r['height'] > settings['minimum_height_m']])
    for group in groups(records, lambda r: (r['uuid'], r['building_id'])).values():
        if max(r['roof'] for r in group)-min(r['roof'] for r in group) < settings['roof_range_m']:
            roof = max(r['roof'] for r in group)
            ground = min(r['ground'] for r in group)
            height = max(r['height'] for r in group)
            for r in group:
                r.update(roof=roof, ground=ground, height=height)
            selected.extend(group)
        else:
            bypass.extend(group)
    records = dissolve(dissolve(selected) + bypass)
    if not records:
        return []
    rounded = [valid_area(set_precision(r['polygon'], 10**-settings['coordinate_decimals'])) for r in records]
    simplified = shared_generalization(rounded, settings['shared_tolerance_m'])
    snapped = snap_areas(simplified, settings['snap_m'])
    output = []
    for r, geometry in zip(records, snapped):
        for p in parts(geometry):
            # 3DForcer+Extruder: top is Groundheight+Buildingheight, not Roofheight.
            output.append({**r, 'polygon': p, 'section_id': f'{r["building_id"]}:final:{len(output)}'})
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
    write_json(folder/'simplification_report.json', {'zones': reports, 'small_wall_events': trace,
               'context_wall_count': len(walls), 'exported_wall_count': len(wall_export),
               'selected_buildings': len(features), 'native_merged_export_buildings': len(fme_exports)})
    missing = set(config['building_uuids']) - {f['properties']['50A_UUID'] for f in features}
    if missing:
        raise ValueError(f'Requested buildings removed by simplification filters: {sorted(missing)}')
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
