"""Run baseline geometry, simplification, and the HTML plot for one case."""

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'code'))
from model_common import read_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'inputs/config.yml')
    parser.add_argument('--uuid', help='Override building_uuids with one UUID')
    parser.add_argument('--uuid-file', type=Path, help='Read UUIDs from .txt, .csv, or .xlsx; overrides config selection')
    args = parser.parse_args()
    if args.uuid and args.uuid_file:
        parser.error('--uuid and --uuid-file cannot be combined')
    config_path = args.config.resolve()
    config = read_config(config_path, uuid_file=args.uuid_file)
    selected = str(uuid.UUID(args.uuid)) if args.uuid else None
    for script in ('generate_baseline.py', 'simplify_model.py'):
        command = [sys.executable, str(ROOT / 'code' / script), '--config', str(config_path)]
        if selected:
            command += ['--uuid', selected]
        elif args.uuid_file:
            command += ['--uuid-file', str(args.uuid_file.resolve())]
        subprocess.run(command, check=True)
    output = Path(config['paths']['output'])
    citymodeller = output / 'CityModeller.geojson'
    shutil.copyfile(output / 'simplified.geojson', citymodeller)
    if not json.loads(citymodeller.read_text())['features']:
        print(f'No 3D buildings passed geometry filters; see {output / "baseline_report.json"}.', flush=True)
        return
    plot_command = [sys.executable, str(ROOT / 'code/plot_model.py'),
                    '--input', str(citymodeller),
                    '--output', str(output / 'CityModeller_plot.html')]
    if selected:
        plot_command += ['--uuid', selected]
    subprocess.run(plot_command, check=True)
    print(f'Case complete: {output}', flush=True)


if __name__ == '__main__':
    main()
