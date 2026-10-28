"""Generate an external-study GeoJSON using Configs/UserManualInput.yml."""

from pathlib import Path


def main():
    from Scripts.Data4ExternalStudy import CityModellerFactory, Read_json, Read_yml

    project_dir = Path(__file__).resolve().parent
    case_name = "ExternalStudy"
    factory = CityModellerFactory(
        Read_yml(project_dir / "Configs" / "UserManualInput.yml"),
        Read_json(project_dir / "Configs" / "CM_Template.geojson"),
        case_name,
        "../examples",
        output_dir=project_dir / "examples" / f"Data_for_{case_name}",
    )
    output_dir = factory.MakeChanges()
    print(f"Generated: {Path(output_dir) / 'CityModeler.geojson'}")


if __name__ == "__main__":
    main()
