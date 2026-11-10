"""Plot either generated GeoJSON model as an interactive, offline 3D figure."""
import argparse
import json
import webbrowser
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
from shapely import constrained_delaunay_triangles
from shapely.geometry import Polygon

if __package__:
    from .model_common import ROOT, parts
else:
    from model_common import ROOT, parts



def surface_triangles(face):
    """Triangulate each surface in its own plane; preserve concavities and holes."""
    vertices = np.asarray(face[0], dtype=float)
    origin = vertices[0]
    _, _, basis = np.linalg.svd(vertices-origin, full_matrices=False)
    axes = basis[:2]
    projected = [(np.asarray(ring)-origin) @ axes.T for ring in face]
    polygon = Polygon(projected[0], projected[1:])
    triangles = constrained_delaunay_triangles(polygon)
    # Make every triangle wind the same way as its face's outer ring, so the
    # renderer's lighting (which depends on vertex order) is consistent.
    ring = np.asarray(face[0], dtype=float)
    normal = sum(np.cross(a, b) for a, b in zip(ring[:-1], np.roll(ring[:-1], -1, axis=0)))
    for triangle in parts(triangles):
        corners = np.asarray(triangle.exterior.coords)[:3] @ axes + origin
        if np.dot(np.cross(corners[1]-corners[0], corners[2]-corners[0]), normal) < 0:
            corners = corners[[0, 2, 1]]
        yield corners


def run(input_path, output_path, open_browser=False, building_uuid=None):
    data = json.loads(Path(input_path).read_text())
    features = data['features'] if data['type'] == 'FeatureCollection' else [data]
    if building_uuid:
        features = [f for f in features if f.get('properties', {}).get('50A_UUID') == building_uuid]
        if not features:
            raise ValueError(f'UUID not found in {input_path}: {building_uuid}')
    figure = go.Figure()
    offset = None
    colors = ['#3569c8', '#ed8b32', '#32a884', '#a35ac7', '#c84e61']
    for feature in features:
        identifier = feature['properties']['50A_UUID']
        summaries = feature['properties'].get('sections', [])
        print(f'{identifier}: plotting {len(feature["geometry"].get("geometries", []))} block(s)')
        for section_index, section in enumerate(feature['geometry']['geometries']):
            vertices, triangles = [], []
            for face in section['coordinates']:
                for triangle in surface_triangles(face):
                    triangles.append([len(vertices), len(vertices)+1, len(vertices)+2])
                    vertices.extend(triangle)
            if not vertices:
                continue
            xyz = np.asarray(vertices)
            if offset is None:
                offset = np.asarray([xyz[:,0].min(), xyz[:,1].min(), 0])
            xyz -= offset
            ijk = np.asarray(triangles)
            summary = summaries[section_index] if section_index < len(summaries) else {}
            label = f'{identifier} — block {section_index + 1}'
            figure.add_trace(go.Mesh3d(x=xyz[:,0], y=xyz[:,1], z=xyz[:,2],
                                      i=ijk[:,0], j=ijk[:,1], k=ijk[:,2],
                                      name=label, color=colors[section_index % len(colors)],
                                      opacity=.9, flatshading=True,
                                      hovertemplate=(f'{label}<br>Ground {summary.get("ground_z_m", "?")} m'
                                                     f'<br>Roof {summary.get("roof_z_m", "?")} m'
                                                     '<br>E %{x:.2f}<br>N %{y:.2f}<br>Z %{z:.2f}<extra></extra>')))
    title = Path(input_path).name
    if building_uuid:
        title = f'{building_uuid} — {title}'
    figure.update_layout(title=title,
                         scene={'aspectmode':'data', 'xaxis_title':'Local easting (m)',
                                'yaxis_title':'Local northing (m)', 'zaxis_title':'Elevation (m)'},
                         margin=dict(l=0,r=0,b=0,t=45))
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.write_html(str(output_path), include_plotlyjs=True)
    if open_browser:
        webbrowser.open(output_path.as_uri())
    print(output_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', default=ROOT/'output/simplified.geojson')
    parser.add_argument('--output', default=ROOT/'output/simplified_plot.html')
    parser.add_argument('--uuid', help='Show only this building UUID, zoomed as its own plot')
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    run(args.input, args.output, args.open, args.uuid)
