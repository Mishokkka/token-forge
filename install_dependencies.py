"""Use the same verified Windows package constraints in setup and CI."""
from pathlib import Path
import subprocess
import sys


def main():
    base = Path(__file__).resolve().parent
    constraint = base / 'constraints' / f'windows-{sys.version_info.major}.{sys.version_info.minor}.txt'
    if not constraint.is_file():
        raise SystemExit('No tested dependency constraints for this Python version.')
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(base / 'requirements.txt'),
                    '-c', str(constraint)], check=True)
    subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True)


if __name__ == '__main__':
    main()
