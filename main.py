"""Run baseline geometry, simplification, and the HTML plot for one case."""

import argparse
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
    args = parser.parse_args()
    config_path = args.config.resolve()
    config = read_config(config_path)
    selected = str(uuid.UUID(args.uuid)) if args.uuid else None
    for script in ('generate_baseline.py', 'simplify_model.py'):
        command = [sys.executable, str(ROOT / 'code' / script), '--config', str(config_path)]
        if selected:
            command += ['--uuid', selected]
        subprocess.run(command, check=True)
    output = Path(config['paths']['output'])
    citymodeller = output / 'CityModeller.geojson'
    shutil.copyfile(output / 'simplified.geojson', citymodeller)
    plot_command = [sys.executable, str(ROOT / 'code/plot_model.py'),
                    '--input', str(citymodeller),
                    '--output', str(output / 'CityModeller_plot.html')]
    if selected:
        plot_command += ['--uuid', selected]
    subprocess.run(plot_command, check=True)
    print(f'Case complete: {output}', flush=True)


if __name__ == '__main__':
    main()
