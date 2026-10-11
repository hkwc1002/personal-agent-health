"""Validate an explicit publication allowlist without printing sensitive values."""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
IGNORED_DIRS = {'.git', '__pycache__', 'dist'}
SIGNATURES = (
    ('private_key', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
    ('provider_key', re.compile(r'\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})')),
    ('aws_key', re.compile(r'\bAKIA[A-Z0-9]{16}\b')),
    ('feishu_identity', re.compile(r'\b(?:ou|oc|on)_[0-9a-f]{16,}\b')),
    ('credential_url', re.compile(r'https?://[^/\s:]+:[^/\s@]+@')),
)


def scan_content(raw):
    text = raw.decode('utf-8')
    return [name for name, pattern in SIGNATURES if pattern.search(text)]


def inspect(root=ROOT):
    root = Path(root).resolve()
    names = json.loads((root / 'release-files.json').read_text(encoding='utf-8'))
    if not isinstance(names, list) or len(names) != len(set(names)):
        raise ValueError('Invalid release file list')
    errors = []
    for name in names:
        path = root / name
        if Path(name).is_absolute() or '..' in Path(name).parts or not path.resolve().is_relative_to(root):
            errors.append('unsafe_path:' + name)
            continue
        if path.is_symlink():
            errors.append('symlink:' + name)
            continue
        parent = path.parent
        while parent != root:
            if parent.is_symlink():
                errors.append('symlink_parent:' + name)
                break
            parent = parent.parent
        if parent != root:
            continue
        if not path.is_file() or path.stat().st_size > 200000:
            errors.append('missing_or_oversized:' + name)
            continue
        if any(part in {'data', 'secrets', 'backups', '_test-data', '.private'} for part in path.relative_to(root).parts):
            errors.append('runtime_data:' + name)
            continue
        if path.suffix.lower() in ('.db', '.sqlite', '.sqlite3', '.zip', '.log', '.jsonl', '.jpg', '.png'):
            errors.append('non_source_file:' + name)
            continue
        try:
            errors.extend(kind + ':' + name for kind in scan_content(path.read_bytes()))
        except UnicodeDecodeError:
            errors.append('non_utf8:' + name)
    allowed = set(names)
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRS for part in relative.parts):
            continue
        if path.is_symlink():
            errors.append('unexpected_symlink:' + relative.as_posix())
        elif path.is_file() and relative.as_posix() not in allowed:
            errors.append('not_allowlisted:' + relative.as_posix())
    return names, sorted(set(errors))


if __name__ == '__main__':
    names, errors = inspect()
    print(json.dumps({'ok': not errors, 'allowlisted_files': len(names), 'errors': errors},
                     ensure_ascii=False, indent=2))
    sys.exit(bool(errors))
