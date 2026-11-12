"""Plot modeled building blocks as interactive 3D extrusions on a street map."""
import argparse
import json
import math
import webbrowser
from pathlib import Path

from pyproj import Transformer

if __package__:
    from .model_common import ROOT
else:
    from model_common import ROOT


HTML = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CityModeller · 3D building map</title>
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<script src="https://unpkg.com/deck.gl@9.1.9/dist.min.js"></script>
<style>
  html,body,#map{margin:0;width:100%;height:100%;font-family:system-ui,-apple-system,sans-serif}
  #map{position:fixed;inset:0;background:#e7edf2}
  .panel{position:absolute;top:16px;left:16px;z-index:2;width:min(330px,calc(100vw - 32px));
    box-sizing:border-box;padding:17px 18px;background:rgba(255,255,255,.96);border-radius:14px;
    box-shadow:0 6px 28px #233a5260;color:#193042}
  h1{font-size:17px;margin:0 0 4px} .sub{font-size:12px;color:#5d7080;margin-bottom:12px}
  .row{display:flex;gap:7px} input{flex:1;min-width:0;padding:9px 10px;border:1px solid #c4d0d8;
    border-radius:8px;font-size:13px} button{padding:8px 11px;border:0;border-radius:8px;
    background:#1767a2;color:white;cursor:pointer}button:hover{background:#0e4d7e}
  #status{font-size:12px;color:#476274;margin-top:10px;line-height:1.4}
  .legend{font-size:12px;color:#476274;margin-top:13px}
  .scale{height:11px;border-radius:6px;margin:6px 0 3px;background:linear-gradient(90deg,#3569b3,#32a884,#e2bf47,#d9573d)}
  .ticks{display:flex;justify-content:space-between;font-variant-numeric:tabular-nums}
  .missing{display:flex;align-items:center;gap:5px;margin-top:5px}
  .swatch{display:inline-block;width:11px;height:11px;background:#9099a1;border-radius:2px}
  .hint{position:absolute;right:12px;bottom:30px;z-index:2;background:rgba(255,255,255,.93);
    border-radius:8px;padding:8px 10px;font-size:12px;color:#365164}
  .maplibregl-ctrl-top-right{top:12px;right:12px}
</style>
</head>
<body>
<div id="map"></div>
<div class="panel">
  <h1>CityModeller · 3D buildings</h1>
  <div class="sub">__BUILDING_COUNT__ buildings · __BLOCK_COUNT__ blocks</div>
  <div class="row"><input id="search" type="search" placeholder="Search building UUID" aria-label="Search building UUID">
  <button id="find">Find</button><button id="all">All</button></div>
  <div id="status">Click a building for details. Drag to pan; right-drag or Ctrl-drag to rotate; scroll to zoom.</div>
  <div class="legend"><b>Construction year</b><div class="scale"></div>
    <div class="ticks"><span>__YEAR_MIN__</span><span>__YEAR_MID__</span><span>__YEAR_MAX__</span></div>
    <div class="missing"><span class="swatch"></span> Year unavailable</div>
  </div>
</div>
<div class="hint">Map tiles require internet · Heights shown relative to local ground</div>
<script>
const blocks = __BLOCKS__;
const initial = __VIEW__;
const byId = new Map();
for (const block of blocks) {
  if (!byId.has(block.uuid)) byId.set(block.uuid, []);
  byId.get(block.uuid).push(block);
}
let selected = null;
const status = document.getElementById('status');
const map = new maplibregl.Map({container:'map',style:'https://tiles.openfreemap.org/styles/positron',
  center:[initial.longitude,initial.latitude],zoom:initial.zoom,pitch:initial.pitch,bearing:initial.bearing,
  antialias:true,dragRotate:true});
map.addControl(new maplibregl.NavigationControl({showCompass:true}),'top-right');
const overlay = new deck.MapboxOverlay({interleaved:false,layers:[]});
map.addControl(overlay);
const yearMin = __YEAR_MIN_VALUE__, yearMax = __YEAR_MAX_VALUE__;
const stops = [[53,105,179],[50,168,132],[226,191,71],[217,87,61]];
function colorForYear(year) {
  if(year===null)return [144,153,161,235];
  const t=Math.max(0,Math.min(1,(year-yearMin)/Math.max(1,yearMax-yearMin)))*3;
  const i=Math.min(2,Math.floor(t)),f=t-i;
  return stops[i].map((v,j)=>Math.round(v+(stops[i+1][j]-v)*f)).concat(235);
}
function redraw() {
  overlay.setProps({layers:[new deck.PolygonLayer({id:'citymodeller-buildings',data:blocks,
    getPolygon:d=>d.polygon,getElevation:d=>d.height,extruded:true,wireframe:true,
    getFillColor:d=>selected&&selected!==d.uuid?[100,119,131,80]:colorForYear(d.year),
    getLineColor:d=>selected===d.uuid?[255,228,86,255]:[28,47,65,210],lineWidthMinPixels:1,pickable:true,autoHighlight:true,
    highlightColor:[255,233,94,180],material:{ambient:.45,diffuse:.65,shininess:22,specularColor:[100,100,100]},
    updateTriggers:{getFillColor:selected,getLineColor:selected}})],
    getTooltip:({object})=>object&&{html:'<b>'+object.uuid+'</b><br>Block '+object.block+
      '<br>Built: '+(object.year??'unknown')+'<br>Height: '+object.height.toFixed(1)+' m<br>Ground: '+object.ground.toFixed(1)+' m<br>Roof: '+object.roof.toFixed(1)+' m',
      style:{backgroundColor:'#193042',color:'white',fontSize:'13px',padding:'9px',borderRadius:'7px'}}});
}
overlay.setProps({onClick:({object})=>{
  if (!object){selected=null;document.getElementById('search').value='';redraw();status.textContent='Showing all buildings.';return;}
  selected=object.uuid;redraw();focus(selected);
}});
function focus(uuid) {
  const group=byId.get(uuid);
  if(!group){status.textContent='No building matches that UUID.';return;}
  const points=group.flatMap(b=>b.polygon[0]);
  const lng=points.map(p=>p[0]),lat=points.map(p=>p[1]);
  map.fitBounds([[Math.min(...lng),Math.min(...lat)],[Math.max(...lng),Math.max(...lat)]],
    {padding:{top:Math.min(180,Math.floor(window.innerHeight/4)),bottom:50,left:40,right:40},
     maxZoom:19.5,duration:800,pitch:65,bearing:-25});
  status.textContent=uuid+' · '+group.length+' block'+(group.length===1?'':'s');
}
document.getElementById('find').onclick=()=>{
  const q=document.getElementById('search').value.trim().toLowerCase();
  const matches=[...byId.keys()].filter(id=>id.toLowerCase().includes(q));
  if(!q||matches.length!==1){status.textContent=matches.length===0?'No matching UUID.':'Enter more of the UUID to narrow the search.';return;}
  selected=matches[0];redraw();focus(selected);
};
document.getElementById('search').onkeydown=e=>{if(e.key==='Enter')document.getElementById('find').click()};
document.getElementById('all').onclick=()=>{selected=null;document.getElementById('search').value='';redraw();map.flyTo({center:[initial.longitude,initial.latitude],zoom:initial.zoom,pitch:initial.pitch,bearing:initial.bearing});status.textContent='Showing all buildings.'};
map.on('load',redraw);
map.on('error',e=>{status.textContent='Map could not load. Check your internet connection.';console.error(e.error)});
</script>
</body>
</html>'''


def model_blocks(data, building_uuid=None):
    """Extract one footprint and roof height per section from a 3D GeoJSON model."""
    features = data['features'] if data.get('type') == 'FeatureCollection' else [data]
    crs = data.get('crs', {}).get('properties', {}).get('name', 'EPSG:3006')
    transformer = Transformer.from_crs(crs, 'EPSG:4326', always_xy=True)
    blocks = []
    found = False
    for feature in features:
        identifier = str(feature.get('properties', {}).get('50A_UUID', ''))
        if building_uuid and identifier != building_uuid:
            continue
        found = True
        properties = feature.get('properties', {})
        year = None
        for key in ('43S_BYGGAR', 'EgenNybyggAr', '43T_BYGGAR'):
            try:
                candidate = int(float(properties[key]))
                if 1600 <= candidate <= 2100:
                    year = candidate
                    break
            except (KeyError, TypeError, ValueError):
                pass
        geometry = feature['geometry']
        if geometry.get('type') != 'GeometryCollection':
            raise ValueError(f'{identifier}: expected a 3D GeometryCollection')
        for index, section in enumerate(geometry['geometries'], 1):
            # The modeled MultiPolygon contains horizontal floor/roof faces and
            # vertical wall faces. The lowest horizontal face is the footprint.
            horizontal = []
            for face in section['coordinates']:
                if not face or not face[0]:
                    continue
                zs = [point[2] for ring in face for point in ring]
                if zs and max(zs) - min(zs) < 0.01:
                    horizontal.append((sum(zs) / len(zs), face))
            if len(horizontal) < 2:
                raise ValueError(f'{identifier} block {index}: floor or roof face missing')
            ground, floor_face = min(horizontal, key=lambda item: item[0])
            roof = max(item[0] for item in horizontal)
            if roof <= ground:
                raise ValueError(f'{identifier} block {index}: roof is not above floor')
            polygon = [
                [[*transformer.transform(point[0], point[1]), 0] for point in ring]
                for ring in floor_face
            ]
            blocks.append({'uuid': identifier, 'block': index, 'polygon': polygon,
                           'ground': ground, 'roof': roof, 'height': roof-ground,
                           'year': year})
    if building_uuid and not found:
        raise ValueError(f'UUID not found in model: {building_uuid}')
    if not blocks:
        raise ValueError('No 3D building blocks found in the input model')
    return blocks


def initial_view(blocks):
    points = [point for block in blocks for point in block['polygon'][0]]
    west, east = min(p[0] for p in points), max(p[0] for p in points)
    south, north = min(p[1] for p in points), max(p[1] for p in points)
    # Approximate zoom for a desktop viewport with space for the control panel.
    longitude_span = max(east-west, 0.00015)
    latitude_span = max(north-south, 0.0001)
    zoom = min(math.log2(360*900/(longitude_span*512)),
               math.log2(360*650/(latitude_span*512))) - 0.5
    return {'longitude': (west+east)/2, 'latitude': (south+north)/2,
            'zoom': max(9, min(18.5, zoom)), 'pitch': 55, 'bearing': -25}


def run(input_path, output_path, open_browser=False, building_uuid=None):
    data = json.loads(Path(input_path).read_text())
    blocks = model_blocks(data, building_uuid)
    years = [block['year'] for block in blocks if block['year'] is not None]
    year_min = min(years) if years else 0
    year_max = max(years) if years else 0
    html = (HTML.replace('__BLOCKS__', json.dumps(blocks, separators=(',', ':')).replace('</', '<\\/'))
                .replace('__VIEW__', json.dumps(initial_view(blocks)))
                .replace('__BUILDING_COUNT__', str(len({block['uuid'] for block in blocks})))
                .replace('__BLOCK_COUNT__', str(len(blocks)))
                .replace('__YEAR_MIN_VALUE__', str(year_min))
                .replace('__YEAR_MAX_VALUE__', str(year_max))
                .replace('__YEAR_MIN__', str(year_min) if years else '—')
                .replace('__YEAR_MID__', str(round((year_min + year_max)/2)) if years else '—')
                .replace('__YEAR_MAX__', str(year_max) if years else '—'))
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding='utf-8')
    if open_browser:
        webbrowser.open(output_path.as_uri())
    print(f'Map created: {output_path} ({len(blocks)} blocks)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', default=ROOT/'output/simplified.geojson')
    parser.add_argument('--output', default=ROOT/'output/simplified_plot.html')
    parser.add_argument('--uuid', help='Show only this building UUID')
    parser.add_argument('--open', action='store_true', help='Open the finished map in a browser')
    args = parser.parse_args()
    run(args.input, args.output, args.open, args.uuid)
