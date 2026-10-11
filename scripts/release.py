"""Build a source-only ZIP for review; never commit, push or publish."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

from check_release import ROOT, inspect, scan_content


def build(repository, output, root=ROOT):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/personal-agent-health', repository):
        raise ValueError('Use OWNER/personal-agent-health as the repository name')
    root = Path(root)
    names, errors = inspect(root)
    if errors:
        raise ValueError('Release check failed: ' + ', '.join(errors))
    prepared = {}
    for name in names:
        raw = (root / name).read_bytes()
        if name == 'README.md':
            raw = raw.replace(b'__REPOSITORY_URL__', ('https://github.com/' + repository).encode())
            lines = raw.decode('utf-8').splitlines()
            raw = ('\n'.join(line for line in lines if not line.startswith('> 发布准备说明：')) + '\n').encode('utf-8')
        if scan_content(raw):
            raise ValueError('Content check failed: ' + name)
        prepared[name] = raw
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'personal-agent-health-source.zip'
    sums = output / 'SHA256SUMS'
    if archive.exists() or sums.exists():
        raise ValueError('Release output already exists; choose a new empty directory')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, raw in sorted(prepared.items()):
            bundle.writestr('personal-agent-health/' + name, raw)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with sums.open('x', encoding='ascii') as stream:
        stream.write(digest + '  ' + archive.name + '\n')
    return {'archive': str(archive), 'sha256': digest, 'files': len(prepared),
            'published': False, 'repository': repository}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--output', default=str(ROOT / 'dist'))
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.repository, args.output), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
