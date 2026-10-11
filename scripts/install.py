"""Prepare a Linux installation; --apply starts only the dedicated sidecar."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zoneinfo import ZoneInfo

NAME = 'personal-agent-health'
ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILES = ('__init__.py', 'personal_health.py', 'plugin.yaml',
                'skills/personal-health/SKILL.md')


def absolute(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Expected an absolute path without parent traversal')
    # Compose expands dollar signs even in otherwise valid JSON strings.
    if any(char in str(path) for char in ('$', '\n', '\r', '\x00')):
        raise ValueError('Unsupported characters in path')
    return path


def no_symlinks(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('Refusing symlink path: ' + str(part))


def build_plan(args):
    destination = absolute(args.destination)
    home = absolute(args.hermes_home)
    if destination == Path('/') or destination == home or destination.is_relative_to(ROOT):
        raise ValueError('Installation data must be separate from the checkout and Hermes home')
    if home.is_relative_to(destination) or destination.is_relative_to(home):
        raise ValueError('Health data and Hermes home must not overlap')
    no_symlinks(destination)
    no_symlinks(home)
    ZoneInfo(args.timezone)
    if min(args.hermes_uid, args.hermes_gid, args.service_uid) < 0:
        raise ValueError('UID and GID must be non-negative')
    if args.service_uid in (0, args.hermes_uid):
        raise ValueError('The health service needs a distinct non-root UID')
    if not home.is_dir():
        raise ValueError('Hermes home must already exist')
    if args.mode == 'docker' and (not args.compose_file or not args.compose_project or not args.hermes_service):
        raise ValueError('Docker mode requires original compose files, project and Hermes service name')
    for filename in args.compose_file:
        if not absolute(filename).is_file():
            raise ValueError('Original compose file not found')
    if args.mode == 'docker' and not args.hermes_container_home:
        raise ValueError('Docker mode requires the verified in-container Hermes home')
    runtime = destination / 'runtime'
    socket = '/run/personal-agent-health/health.sock' if args.mode == 'docker' else str(runtime / 'health.sock')
    hermes_home = absolute(args.hermes_container_home) if args.mode == 'docker' else home
    config = {'socket': socket, 'state_db': str(hermes_home / 'state.db'),
              'image_roots': [str(hermes_home / 'cache'), str(hermes_home / 'image_cache')],
              'owner_id': args.owner_id}
    uid, gid = args.service_uid, args.hermes_gid
    mount = lambda source, target, ro=False: {
        'type': 'bind', 'source': str(source), 'target': target, 'read_only': ro}
    compose = {'services': {'health': {
        'build': {'context': str(destination / 'app')},
        'user': f'{uid}:{gid}', 'restart': 'unless-stopped',
        'read_only': True, 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
        'pids_limit': 64, 'mem_limit': '512m', 'cpus': 0.5,
        'tmpfs': ['/tmp:rw,noexec,nosuid,size=64m'],
        'environment': {'HEALTH_DATA': '/data',
                        'HEALTH_SOCKET': '/run/personal-agent-health/health.sock',
                        'HEALTH_ALLOWED_UIDS': f'{args.hermes_uid},{uid}',
                        'HEALTH_TIMEZONE': args.timezone,
                        'HEALTH_INTERVALS_CONFIG': '/secrets/intervals.json',
                        'HEALTH_FDC_KEY_FILE': '/secrets/fdc-key'},
        'volumes': [mount(destination / 'data', '/data'),
                    mount(runtime, '/run/personal-agent-health'),
                    mount(destination / 'secrets', '/secrets', True)],
        'healthcheck': {'test': ['CMD', 'python', '-B', '/app/check.py'],
                        'interval': '30s', 'timeout': '5s', 'retries': 3, 'start_period': '15s'}
    }}}
    commands = []
    override = None
    if args.mode == 'docker':
        override = {'services': {args.hermes_service: {
            'volumes': [mount(runtime, '/run/personal-agent-health', True),
                        mount(home / 'plugins' / NAME, str(hermes_home / 'plugins' / NAME), True),
                        mount(home / (NAME + '.json'), str(hermes_home / (NAME + '.json')), True)],
            'environment': {'PERSONAL_AGENT_HEALTH_CONFIG': str(hermes_home / (NAME + '.json'))}
        }}}
        base = ['docker', 'compose', '-p', args.compose_project]
        for filename in args.compose_file:
            base += ['-f', str(absolute(filename))]
        base += ['-f', str(destination / 'compose.override.json')]
        commands = [base + ['config', '--quiet'],
                    base + ['up', '-d', '--no-deps', args.hermes_service],
                    base + ['exec', '-T', args.hermes_service, 'hermes', 'plugins', 'validate',
                            str(hermes_home / 'plugins' / NAME)],
                    base + ['exec', '-T', args.hermes_service, 'hermes', 'plugins', 'enable', NAME],
                    base + ['restart', args.hermes_service]]
    else:
        commands = [['hermes', 'plugins', 'validate', str(home / 'plugins' / NAME)],
                    ['hermes', 'plugins', 'enable', NAME]]
    return {'destination': str(destination), 'home': str(home), 'config': config,
            'compose': compose, 'override': override, 'activation_commands': commands,
            'mode': args.mode, 'service_uid': uid, 'hermes_gid': gid,
            'hermes_uid': args.hermes_uid}


def write_json(path, value, mode=0o600):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    os.chmod(path, mode)


def apply(plan, skip_foods=False):
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise ValueError('--apply requires Linux and root for isolated data-directory ownership')
    destination, home = Path(plan['destination']), Path(plan['home'])
    plugin = home / 'plugins' / NAME
    config = home / (NAME + '.json')
    for path in (destination, plugin, config):
        no_symlinks(path)
        if path.exists():
            raise ValueError('Initial install refuses to overwrite: ' + str(path))
    no_symlinks(home / 'plugins')
    subprocess.run(['docker', 'compose', 'version'], check=True, capture_output=True)
    info = json.loads(subprocess.check_output(['docker', 'info', '--format', '{{json .SecurityOptions}}']))
    if any('rootless' in item or 'userns' in item for item in info):
        raise ValueError('Rootless Docker/user namespaces need a separately verified UID mapping')
    if not (home / 'state.db').is_file():
        raise ValueError('Expected Hermes state.db; verify the actual profile home first')
    destination.mkdir(mode=0o755)
    os.chmod(destination, 0o755)
    uid, gid = plan['service_uid'], plan['hermes_gid']
    for name, mode in (('data', 0o700), ('secrets', 0o700), ('runtime', 0o750)):
        path = destination / name
        path.mkdir(mode=mode)
        os.chmod(path, mode)
        os.chown(path, uid, gid)
    app = destination / 'app'
    app.mkdir(mode=0o755)
    shutil.copytree(ROOT / 'service', app / 'service', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('Dockerfile', '.dockerignore'):
        shutil.copyfile(ROOT / name, app / name)
    for path in (app, *app.rglob('*')):
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    created_plugins = not (home / 'plugins').exists()
    plugin.mkdir(parents=True, mode=0o755)
    if created_plugins:
        os.chown(home / 'plugins', plan['hermes_uid'], gid)
        os.chmod(home / 'plugins', 0o755)
    for name in PLUGIN_FILES:
        target = plugin / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
        os.chmod(target, 0o644)
    for path in (plugin, *plugin.rglob('*')):
        os.chown(path, plan['hermes_uid'], gid)
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    write_json(config, plan['config'])
    os.chown(config, plan['hermes_uid'], gid)
    compose_path = destination / 'compose.generated.json'
    write_json(compose_path, plan['compose'])
    if plan['override']:
        write_json(destination / 'compose.override.json', plan['override'])
    write_json(destination / 'install-state.json', plan)
    base = ['docker', 'compose', '-p', NAME, '-f', str(compose_path)]
    subprocess.run(base + ['build', 'health'], check=True)
    if not skip_foods:
        subprocess.run(base + ['run', '--rm', '--no-deps', '--entrypoint', 'python',
                               'health', '-B', '/app/import_foods.py'], check=True)
    subprocess.run(base + ['up', '-d', '--wait', 'health'], check=True)
    print(json.dumps({'sidecar_started': True, 'hermes_activated': False,
                      'activation_commands': plan['activation_commands']}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('host', 'docker'), required=True)
    parser.add_argument('--destination', default='/opt/personal-agent-health')
    parser.add_argument('--hermes-home', required=True, help='Actual host-side persisted Hermes profile')
    parser.add_argument('--hermes-uid', type=int, required=True)
    parser.add_argument('--hermes-gid', type=int, required=True)
    parser.add_argument('--service-uid', type=int, default=21000)
    parser.add_argument('--owner-id', default='', help='One Feishu owner ID; empty disables Feishu access')
    parser.add_argument('--timezone', default='Asia/Shanghai')
    parser.add_argument('--compose-file', action='append', default=[])
    parser.add_argument('--compose-project')
    parser.add_argument('--hermes-service')
    parser.add_argument('--hermes-container-home')
    parser.add_argument('--skip-foods', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        plan = build_plan(args)
        if args.apply:
            apply(plan, args.skip_foods)
        else:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except (OSError, subprocess.CalledProcessError):
        # Do not echo CLI output that may include user configuration or credentials.
        raise SystemExit('Install incomplete. Check prerequisites and existing paths; do not delete user data to retry.')


if __name__ == '__main__':
    main()
