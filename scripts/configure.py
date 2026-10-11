"""Hidden local credential entry. Run by the human, never by a chat tool."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('provider', choices=('intervals', 'usda'))
    parser.add_argument('--destination', default='/opt/personal-agent-health')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0 or not sys.stdin.isatty():
        raise SystemExit('Use a private interactive Linux terminal with sudo; do not paste keys into AI chat.')
    root = Path(args.destination).resolve()
    state = json.loads((root / 'install-state.json').read_text(encoding='utf-8'))
    directory = root / 'secrets'
    if directory.is_symlink() or not directory.is_dir():
        raise SystemExit('Invalid secrets directory')
    if args.provider == 'intervals':
        athlete = input('Intervals athlete ID: ').strip()
        key = getpass.getpass('Intervals API key (hidden): ').strip()
        if not athlete.startswith('i') or not athlete[1:].isdigit() or not 8 <= len(key) <= 256:
            raise SystemExit('Invalid athlete ID or key format')
        value = json.dumps({'athlete_id': athlete, 'api_key': key}, ensure_ascii=False)
        name = 'intervals.json'
    else:
        value = getpass.getpass('USDA FDC API key (hidden): ').strip()
        if not value or any(char.isspace() for char in value):
            raise SystemExit('Invalid key format')
        name = 'fdc-key'
    fd, temporary = tempfile.mkstemp(dir=directory, prefix='.credential-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            os.fchown(stream.fileno(), state['service_uid'], state['hermes_gid'])
            stream.write(value)
        os.replace(temporary, directory / name)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    print('Credential saved locally. Restart only the health service to enable Intervals sync; key was not tested.')


if __name__ == '__main__':
    main()
