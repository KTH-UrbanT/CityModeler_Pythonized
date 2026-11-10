# Source-to-code mapping and fidelity audit

This maps the connected geometry paths, including custom-transformer interiors.
`source_audit/factory_inventory.json` inventories every compiled factory and its
source line, including routing, rejection and logging plumbing. Full extracted
and decoded factory statements are beside it. The runtime handoff records SHA256
hashes of the three source workspaces.

**Status:** the baseline, final-footprint, small-wall and wall-export stages below
are implemented as Python geometric techniques. Numerical equivalence to FME's
proprietary kernels has not been established. The implementation choices at the
end of this document are part of the delivered fidelity description.

**2026-11-10 diagnostic update:** A disposable instrumented FME run identified
the first measured divergence for UUID `1c931069-d3d1-4559-8968-e7f0564579fb`:
eight of 2,160 selected point coordinates differ at tied percentile elevations.
CMP now retains all points tied at the cutoff elevations, which restores two
blocks for this UUID. This is an order-independent robustness choice, not FME's
exact unspecified tie ordering. Areas and heights still differ. See
`output/fme_check/diagnosis_1c931069.md` for measured stage outputs and the
remaining exactness work. The earlier comparison paragraph below describes the
pre-fix state.

## Licensed FME comparison — 2026-11-10

Ran the licensed FME 2026.1 footprint worker for DeSO `0180C4440` and
`0180C4680`, using the configured database, point cloud and terrain inputs.
`fme_check/compare_outputs.py` compares those final Buildings writer outputs with
the baseline and simplified handoffs for the four configured UUIDs. The results
are saved in `output/fme_check/comparison.md` and `.json`.

Three requested buildings have matching section counts. UUID
`1c931069-d3d1-4559-8968-e7f0564579fb` does not: FME outputs two sections and
CMP outputs one in both baseline and simplified files. FME's section footprints
are about 617.565 m² and 85.711 m², with ground/roof ranges of 6.13/22.325 m and
7.14/19.155 m. CMP retains one 702.755 m² footprint at 6.29/22.110 m. The union
of the footprints overlaps at IoU 0.999; CMP misses the internal cut and the
separate section heights. CMP's report for this UUID has four steep triangles
but zero cutting lines. Their dissolved candidates do not pass the configured
0.75 m² area filter. The exact FME intermediate output where the paths first
diverge has not been captured; the final Buildings writer does not expose those
intermediate ports.

The same comparison confirms that the 71ceb UUID has three sections in FME and
both CMP outputs. The 916c and 14765 UUIDs have one section in all three outputs.
Matching counts do not establish coordinate or height identity; the JSON report
contains their area and height comparisons.

One configuration mismatch was corrected during this comparison:
`region_buffer_m` is now 0.5 m, matching FME's second region buffer. Re-running
both CMP scripts with this value did not recover the missing 1c cut. The known
TIN implementation difference remains material: FME SurfaceModeller and the
Python Qhull implementation can construct different triangles at 0.01 m
tolerance. Internal FME caches or temporary stage writers are needed to compare
each intermediate feature set and isolate the first exact divergence.

## Baseline — generate_baseline.py

All line references here are to `Citymodeller_Footprints_worker.fmw`.

| Source / connected stage | Conditions, parameters and grouping | Python implementation |
|---|---|---|
| SQLExecutor; geometry restore; CRS setter, 51195–51203 | `Buildings`, selected DeSO; WKB geometry, EPSG:3006 | `load_context` |
| Terrain metadata reader, TIFF reader, RasterMosaicker, clip, 51204–51269 | Spatial tile selection; last valid overlap; nearest-neighbour raster values | `Terrain`, `Terrain.values` |
| Groundheight, 51270–51331 | Per byggnadsid: inset 0.5; raster cell centers; median; round to 2 decimals | `heights` |
| Point-cloud metadata/reader/combiner/splitter/clip, 51332–51383 | All connected LAST_RETURN split groups, bbox/footprint selection | `read_clouds` |
| RoofHeightSimple, 51384–51454 | Per building ID: inset 0.5; median raw cloud Z; roof-ground | `heights` |
| TestFilter_3, 51455–51460 | Buildingheight >3, failed branch dropped | `process_zone` |
| DonutHoleExtractor/area filter/interior point, 51461–51485 | Hole area >=1 m²; preserve hole markers for cut output exclusion | `process_zone` courtyard points |
| PointCloudCleaner input mappings, 51486–51534 | Outer Groundheight → internal Groundheight.mean; byggnadsid → LocalBuildingid; inset 0.5 | `clean_cloud` |
| PointCloudFilter, 51535–51554 | Z > mapped Groundheight +2; vectorize samples | `clean_cloud` |
| PercentileCalculator, 51555–51664 | Sort Z by LocalBuildingid; ranks start 1; ceil(5% N), ceil(95% N); lower ranks excluded, upper bound included | `clean_cloud` |
| SurfaceModeller, 51702 | Per byggnadsid; tolerance .01 m; connected output TRIANGLES | `surface_tin` |
| Footprint3DCuttingLines buffer/clip/normal selection, 51674–51727 | Inset 1 m, clipped triangles, unit normal Z in [0,.25] | `steep_regions` |
| NeighborFinder_2, 51728 | Polygon treated as LINE, keep unmatched triangles >2 m from inset boundary | `steep_regions` |
| Buffer/validate/dissolve/filter/buffer/dissolve, 51731–51767 | +.1 m, dissolve by byggnadsid, area >.75, +.5 m, dissolve | `steep_regions` |
| MINBB/area/counter/CenterLineReplacer, 51768–51787 | Minimum rotated rectangle; independent centerline IDs | `centerline`, `cutting_lines` |
| Chopper/endpoint extraction/NeighborFinder/VertexCreator/extension, 51700, 51788–51819 | Footprint 2-vertex edges; nearest end connections <=20 m; +.5 m extension | `cutting_lines` |
| Generalizer_2/LineCombiner/Generalizer, 51821–51827 | Douglas .5 m; vertex-noded combination by byggnadsid and centerline | `cutting_lines`, `merge_lines` |
| Length/vertex count/TestFilter/SpikeRemover, 51829–51852 | Length / vertex count >=3; 90° spike check; only UNCHANGED emitted | `cutting_lines` |
| PolygonCutter, 51856–52021 | Intersector by building ID; vertex snap .1; AreaBuilder/DonutFactory; within/contains checks; cut/untouched branches | `cut_polygon`, `process_zone` |
| Clipper_4, 52023–52040 | Cut geometries minus same-ID untouched geometry; OUTSIDE branch | `process_zone` |
| Snapper_3/AreaOnAreaOverlayer_3, 52041–52043 | Ungrouped within DeSO; segment snapping .5; planar overlay | `snap_areas`, `process_zone` |
| Geometry save/interior point/owner recovery/hole exclusion, 52045–52075 | Nearest containing accepted original building, distance 0; exclude courtyard marker matches | `process_zone` |
| AreaCalculator_4/TestFilter_4, 52077–52086 | `_newarea <=50` OR `areadel <.1`; denominator `_orginalarea` | `regroup` |
| Dissolver/Counter/NeighborFinder_5/Dissolver_3, 52089–52101 | Dissolve small parts by building; assign nearby retained roof ID within .1 m; dissolve by roof ID+building | `regroup` |
| Attribute cleanup/Groundheight_2/RoofHeightSimple_2, 52108–52254 | Resample cut sections: inset .5, ground median rounded 2, original cloud roof median; untouched bypass retains original heights | `heights`, `process_zone` |
| Baseline handoff | Complete context, section IDs/geometry/heights/attributes; no finalization yet | `run`, `baseline_context.json` |

The baseline GeoJSON is an inspection extrusion of the handoff. Script 2 reads
the handoff, not the preview shells, so no upstream operation is rerun or inferred
from rounded mesh coordinates.

## Final footprints and small walls — simplify_model.py

| FME source | Conditions, parameters and grouping | Python implementation |
|---|---|---|
| TestFilter_5/statistics/TestFilter_6, 52255–52265 | Height >3; roof range <3 per UUID+byggnadsid | `final_footprints` |
| AttributeCreator_5, 52268 | Selected branch: max roof, min ground, max height independently | `final_footprints` |
| Dissolver_2/_5, 52270–52272 | Roof+UUID+building ID; first attributes; second receives selected and bypass | `dissolve`, `final_footprints` |
| CoordinateRounder/Generalizer/Snapper, 52274–52278 | XYZ 3; shared boundaries Douglas .5; segment snap .5; batch DeSO | `final_footprints`, `shared_generalization`, `snap_areas` |
| 3DForcer/Extruder, 52280–52284 | Groundheight + Buildingheight, even if differing from Roofheight attribute | `model_features`, `extrude`; bounds retained in records |
| Bounds/BREP/counters/surface extraction/Multiparts, 52287–52334 | Group surfaces into sections, then buildings; round XYZ 6; >1 part enters cleanup | `remove_small_walls` and section representation |
| RemoveSmallWalls roof selection/2D/chop, 52335–52370 | Unit normal Z=+1; roof rings to edge segments | `remove_small_walls`, `noded_walls` |
| CreatemidLines, 52373–52432 | Building minimum edge <2 m; noded overlaps=2 are shared midlines; exterior short edges <2; nearby midlines within .1 m | `remove_small_walls`, `noded_walls` |
| ExtractNearbyWalls, 52443–52522 | Nearest midline <=.1; length range <1 per part+midline; four contributors per midline; angle rounded to integer degrees; combine by angle/building; two contributors per combined line; HighAngleratio sum=0 | `nearby_wall_pairs`, `angle` |
| IdentifyWallstoRemove, 52540–52600 | Node recreated walls; nearest midline <=2; unmatched distance 3; distance sort; first two nodes per midline; build replacement line; recover original midline ID | `replacement_centerlines` |
| IdentifyWallstoRemove Walls output | Source output is unconnected and explicitly nuked; does not enter AreaBuilder | No downstream geometry effect; recorded as unconnected in inventory |
| ListExploder/FeatureMerger/_2, 52526–52609 | Remove listed original wall IDs and replaced midline IDs; rebuild only affected building IDs | `remove_small_walls` |
| AreaBuilder/DonutFactory/Generalizer, 52611–52620 | Per building ID; no snapping; polygonize; retain holes; Douglas 1 m | `remove_small_walls` |
| Geometry save/point/NeighborFinder_8/restore, 52622–52652 | Representative point, original section footprint distance 0; recover per-section z bounds | `remove_small_walls` |
| 3DForcer/Extruder/BREP/FeatureMerger_3, 52654–52669 | Restore zmin, extrude zmax-zmin; original bypass if no reconstructed output | `remove_small_walls`, `extrude` |
| Post-cleanup geometry routing, 52672–52726 | Reaggregate modified sections; merge untouched multipart and single-part bypasses | `remove_small_walls`, `model_features` |
| CreateWalls, 52727–52775 | Upward roof, chop boundary, force 2D, length >.0001, per-building cumulative `V{byggnadsid}-{count}`, zmin/zmax | `create_walls` |
| Buildings/Walls FFS writer routes, 52778 onward | Contextual geometry and attributes for wall worker | JSON contextual handoff and in-memory records replace FFS transport |

## Wall worker — simplify_model.py

References below are to `CityModeller_Walls_Worker.fmw`.

| Source | Conditions/branch | Python implementation |
|---|---|---|
| Zones join/split/readers, 6921–7012 | Target DeSO plus its `Zones.zones` neighbours; local walls and all neighbour solids | `load_context` (script 1), `shading_relationships` |
| NeighborFinder, 7016 | Neighbour-area walls within 250 m of target-area building boundaries | `shading_relationships` |
| AttributeExposer/geometry-save/center points, 7018–7043 | Export local+matched neighbour lines; use wall midpoint for long-range search | `shading_relationships`, `export_walls` |
| NeighborFinder_3/list expansion/TestFilter, 7046–7061 | <=250 m, cap 999 before excluding same building; source includes local candidates twice | `shading_relationships` |
| VertexCreator/LineExtender, 7064–7068 | Candidate-to-base connector; shorten 2 m at each end; rejected short connectors also continue | `shortened_connection` |
| NeighborFinder_2/list expansion/TestFilter_2, 7071–7086 | Line distance <=1 m, cap 999, different building; bypass occlusion test | `shading_relationships` |
| SurfaceFootprintReplacer/SpatialFilter, 7089–7092 | Reject connector crossing/within/containing any contextual building footprint | `shading_relationships` |
| DuplicateFilter/attribute/sort/aggregate, 7094–7100 | Deduplicate by candidate wall ID+base building; sort IDs; semicolon concatenate; count | `shading_relationships`, `run` |
| FeatureMerger, 7103 | Only MERGED goes to native building export | `run`: `buildings_export.geojson`; separate `simplified.geojson` retains geometry-only buildings |
| AttributeCreator_7/AreaCalculator/attribute cleanup/writers, 7105–7132 | `height=EgenAntalPlan*3`; planar surface-area sum; lines and combined building GeoJSON | `run`, `export_walls`, `model_features` |

`CityModeller_Main.fmw` materializes the Buildings and Zones tables, orchestrates
workers per DeSO and assigns destinations. `load_context` consumes those existing
prepared inputs; the two `run` functions replace worker orchestration and file
transport. Raw upstream database reloads and unrelated energy-data ingestion are
outside this geometry rebuild; original attributes remain available in exports.

## Python kernel choices and remaining differences

These are implementation differences, not undisclosed missing wall stages:

1. **TIN:** tolerance-controlled Delaunay insertion uses Qhull, greatest vertical
   residual per triangle, and first Z for duplicate XY. The licensed comparison
   confirms this can change cuts and section heights: for UUID 1c931069, CMP
   creates zero cutting lines while FME outputs two sections. Only FME's connected
   TRIANGLES output is needed; unconnected contour/grid outputs are not generated.
2. **Centerlines/neighbours:** minimum-rectangle longitudinal centerline uses
   three vertices. Equidistant endpoints, first-contributor attributes and noded
   segment ownership use deterministic Python ordering rather than FME's
   unspecified processing order. The second-endpoint search excludes edges
   already used by any first endpoint in the same building, matching the
   connected UNMATCHED_CANDIDATE branch.
3. **GEOS operations:** buffering uses 22.5° arc discretization; polygon validity
   repair, boundary noding, precision rounding, Douglas–Peucker, and shared-edge
   assignment use GEOS. FME AUTO cleaning, repair routing and feature ordering
   are not proprietary-kernel replicas. Vertex-to-segment snapping freezes
   preceding boundaries and projects later vertices to them. PolygonCutter's
   .1 m vertex snap uses deterministic nearest vertex clusters.
4. **Raster mosaics:** valid overlapping cells use last-tile values. Delivered
   aligned tiles are read by cell centers. Arbitrarily misaligned raster grids
   are not resampled onto a common FME mosaic grid by this implementation.
5. **Small-wall topology:** the same detection/filter/reconstruction stages are
   used. GEOS may simplify a corner differently at 1 m. Duplicate coincident
   segment ownership, midpoint node ordering and angle ties can differ. Missing
   height donors are logged; a building with no rebuilt output follows the
   original-building bypass.
6. **Transport/attributes:** JSON replaces FFS; contextual geometry and all
   prepared source attributes are retained, while FME-internal temporary feature
   types and counters are represented by explicit Python records. UUIDs remain
   strings (the native Walls schema incorrectly declares UUID as float).
7. **Output validation:** shell checks and configuration/handoff checks are
   additional safeguards. Data coverage failures are reported. The vertical
   datum of the input cloud versus terrain has not been independently verified.

No claim of byte-for-byte or numerical identity is made. The test suite checks
techniques, connected paths, thresholds and geometry validity, including a
synthetic case that actually reaches small-wall reconstruction.
