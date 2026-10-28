"""Generate building GeoJSON using Configs/CityModellerConfig.yml."""

from pathlib import Path


def main():
    from Scripts.Generate_CityModeller import ShapeCityPlanner

    project_dir = Path(__file__).resolve().parent
    case_name = "CityModeller"
    planner = ShapeCityPlanner(str(project_dir))
    buildings, _ = planner.GenCore()
    output_dir = planner.SaveitGeoJson(
        str(project_dir), buildings, "../examples", case_name
    )
    print(f"Generated: {Path(output_dir) / 'Generated_Buildings.geojson'}")


if __name__ == "__main__":
    main()
