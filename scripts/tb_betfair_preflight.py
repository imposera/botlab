#!/usr/bin/env python3
"""Offline deployment checks; never imports gateway code or reads credentials."""
import argparse
import ast
import importlib.machinery
import os
from pathlib import Path
import sys

CONTRACTS = {
    'betfair_gateway': {'HORSE_RACING_EVENT_TYPE_ID', 'betfair_login', 'betting_api',
                        'iso_z', 'load_secrets', 'market_summary', 'utc_now'},
    'manual_arm_request': {'consume_request', 'pending_request', 'reject_request'},
}
OBSERVER_CONTRACT = {'betfair_login', 'betting_api', 'load_secrets'}


def exports(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split('.')[0] for alias in node.names)
    return names


def check(role, secrets_file, search_path=None):
    paths = list(sys.path if search_path is None else search_path)
    reports, errors = [], []
    if importlib.machinery.PathFinder.find_spec('requests', paths) is None:
        errors.append('requests is not installed for this Python interpreter')
    else:
        reports.append('requests dependency found')
    if not any(importlib.machinery.PathFinder.find_spec(name, paths) is not None
               for name in ('tb_names', 'tb_liquidity')):
        errors.append('Runner-name helper missing: provide tb_names.py or bundled tb_liquidity.py')
    required = {'betfair_gateway': OBSERVER_CONTRACT} if role == 'observer' else CONTRACTS
    for module, names in required.items():
        spec = importlib.machinery.PathFinder.find_spec(module, paths)
        if spec is None or not spec.origin:
            errors.append(f'{module}.py not found on the Python search path')
            continue
        path = Path(spec.origin)
        try:
            source = path.read_text(encoding='utf-8')
            tree = ast.parse(source, filename=str(path))
            compile(tree, str(path), 'exec')
        except (OSError, ValueError, SyntaxError):
            errors.append(f'{module}: source cannot be read or compiled')
            continue
        missing = sorted(names - exports(tree))
        if missing:
            errors.append(f'{module}: expected exports not declared: {", ".join(missing)}')
        else:
            reports.append(f'{module}: source contract found at {path}')
    if not secrets_file.is_file() or not os.access(secrets_file, os.R_OK):
        errors.append(f'Credentials file is missing or unreadable: {secrets_file}')
    else:
        reports.append('Credentials file is readable (contents not inspected)')
    return reports, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=['observer', 'emitter'], default='emitter')
    parser.add_argument('--secrets-file', type=Path,
                        default=Path(os.environ.get('BETFAIR_SECRETS_FILE', '/opt/betfair/secrets.env')))
    args = parser.parse_args()
    reports, errors = check(args.role, args.secrets_file.expanduser())
    for message in reports:
        print('OK:', message)
    for message in errors:
        print('BLOCKED:', message)
    print('Offline check only: module execution, certificate contents and API authentication are not verified.')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
