"""Generate building GeoJSON using Configs/CityModellerConfig.yml."""

from pathlib import Path
from Scripts.GeneralFunctions import Read_yml, Read_json, SaveitGeoJson
from Scripts.Generate_CityModeller import ShapeCityPlanner
import os

def main():
    project_dir = Path(__file__).resolve().parent
    config = Read_yml(project_dir / 'Configs' / 'CityModellerConfig.yml')
    case_name = config['1_Sim']['CaseName']
    CM_Template = Read_json(os.path.join(project_dir, config['0_Setup']['CityModeller_Tempalte']))
    if config['1_Sim']['ODEN_API']:
        from Scripts import ODEN_API
        ODENAPI = ODEN_API.API(config['1_Sim']['API_BaseURL'], config['1_Sim']['UUID'])
        BldData_API = ODENAPI.Get_Building_by_ID()
        buildings = ODEN_API.GenCore_API(BldData_API, CM_Template)

    else:
        planner = ShapeCityPlanner(str(project_dir), CM_Template)
        buildings, _ = planner.GenCore()

    output_dir = SaveitGeoJson(
        str(project_dir), buildings, "../examples", case_name
    )
    print(f"Generated: {Path(output_dir) / (case_name + '.geojson')}")


if __name__ == "__main__":
    main()
