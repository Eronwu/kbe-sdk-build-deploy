#!/usr/bin/env python3
"""Create a teammate-local platform config from the sanitized profile."""

import argparse
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent / 'kbe-deploy' / 'config'


def absolute_path(value, label):
    path = Path(value).expanduser()
    if not path.is_absolute() or '..' in path.parts or not re.fullmatch(r'/[A-Za-z0-9_./+-]+', str(path)):
        raise argparse.ArgumentTypeError(f'{label} must be a simple absolute path')
    return str(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', required=True, help='SSH target, for example user@host')
    for platform in ('rk3568', 'rk3576'):
        parser.add_argument(f'--{platform}-local', required=True)
        parser.add_argument(f'--{platform}-remote', required=True)
    parser.add_argument('--remote-jobs', required=True)
    parser.add_argument('--force', action='store_true', help='replace existing local config')
    args = parser.parse_args()

    if not re.fullmatch(r'[A-Za-z0-9_.@-]+', args.server) or args.server.startswith('-'):
        parser.error('--server must be a simple SSH user@host target')
    output = ROOT / 'platforms.json'
    if output.exists() and not args.force:
        parser.error(f'{output} already exists; use --force to replace it')
    data = json.loads((ROOT / 'platforms.example.json').read_text())
    for name, profile in data['platforms'].items():
        try:
            local = absolute_path(getattr(args, name + '_local'), f'--{name}-local')
            remote = absolute_path(getattr(args, name + '_remote'), f'--{name}-remote')
            jobs = absolute_path(args.remote_jobs, '--remote-jobs')
        except argparse.ArgumentTypeError as error:
            parser.error(str(error))
        if not Path(local).is_dir():
            parser.error(f'local SDK directory does not exist: {local}')
        profile['server'] = args.server
        profile['local_root'] = local
        profile['remote_root'] = remote
        profile['remote_jobs'] = jobs
        profile['product_out'] = remote.rstrip('/') + '/out/target/product/' + profile['product']
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    output.chmod(0o600)
    print(f'Wrote {output}; run deploy.sh list, then review paths before plan/run.')


if __name__ == '__main__':
    main()
