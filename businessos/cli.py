#!/usr/bin/env python3
"""JSON command interface for the local BusinessOS preview."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid
from service import Service, ROOT, encode, initialize, lifecycle, Conflict


def load(path):
    return json.loads(Path(path.removeprefix('@')).read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('BOS_HOME', '.businessos')).resolve())
    parser.add_argument('--identity', help='Local synthetic identity; owner, employee, agent, delivery, service')
    commands = parser.add_subparsers(dest='command', required=True)
    for group, verbs in {
        'instance': ('init', 'start', 'stop'), 'context': ('show',),
        'catalog': ('export',), 'module': ('list', 'install'),
        'template': ('list', 'export'), 'action': ('list', 'describe', 'run'),
        'query': ('list', 'describe', 'run'), 'process': ('inspect', 'tick'),
        'change': ('create', 'update', 'inspect', 'diff', 'test', 'publish'),
        'release': ('list',), 'audit': ('list',),
    }.items():
        sub = commands.add_parser(group).add_subparsers(dest='verb', required=True)
        for verb in verbs:
            p = sub.add_parser(verb)
            if group in ('action', 'query') and verb in ('describe', 'run'):
                p.add_argument('name')
            if group in ('action', 'query') and verb == 'run':
                p.add_argument('--input', required=True, help='JSON file, optionally prefixed with @')
                if group == 'action':
                    p.add_argument('--key', required=True, help='Stable idempotency key; reuse on retry')
            if group == 'process' and verb == 'inspect':
                p.add_argument('id')
            if group == 'template' and verb == 'export':
                p.add_argument('name'); p.add_argument('--output', required=True)
            if group == 'catalog':
                p.add_argument('--output', required=True)
            if group == 'module' and verb == 'install':
                p.add_argument('package'); p.add_argument('--cases', required=True); p.add_argument('--request', required=True)
            if group == 'change':
                if verb != 'create':
                    p.add_argument('id')
                if verb in ('create', 'update'):
                    p.add_argument('--candidate', required=True); p.add_argument('--cases', required=True)
                    if verb == 'create':
                        p.add_argument('--request', required=True)
    args = parser.parse_args()
    try:
        result = execute(args)
        print(encode({'ok': True, 'result': result}))
        return 0
    except Exception as exc:
        # No DSNs, credentials or SQL errors in the machine-readable error channel.
        safe = isinstance(exc, (ValueError, PermissionError, KeyError)) or type(exc).__name__ in ('Denied', 'Conflict')
        print(encode({'ok': False, 'error': {'code': type(exc).__name__, 'message': str(exc) if safe else 'Operation failed; check local instance status and logs.'}}))
        return 1


def execute(a):
    if a.command == 'instance':
        return initialize(a.home) if a.verb == 'init' else lifecycle(a.home, a.verb)
    if a.command == 'template':
        if a.verb == 'list':
            return [{'name': p.stem} for p in sorted((ROOT / 'templates').glob('*.json'))]
        if a.name not in [p.stem for p in (ROOT / 'templates').glob('*.json')]:
            raise ValueError('unknown template')
        Path(a.output).write_text(encode(load(str(ROOT / 'templates' / (a.name + '.json')))))
        return {'output': a.output}
    s = Service(a.home, a.identity)
    if a.command == 'context':
        return {'instance': str(a.home), **s.actor, 'release_id': s.catalog()['release_id'], 'fixture': True}
    if a.command == 'catalog':
        s.owner(); Path(a.output).write_text(encode(s.catalog())); return {'output': a.output}
    if a.command == 'module':
        if a.verb == 'list':
            return s.catalog()['modules']
        s.owner()
        candidate = s.catalog()
        package = load(a.package)
        for section in ('objects', 'queries', 'actions', 'modules'):
            if set(package.get(section, {})) & set(candidate.get(section, {})):
                raise Conflict('package overwrites existing ' + section + '; use change create for edits')
            candidate.setdefault(section, {}).update(package.get(section, {}))
        return s.draft(candidate, load(a.cases), a.request)
    if a.command in ('action', 'query'):
        kind = 'actions' if a.command == 'action' else 'queries'
        if a.verb == 'list':
            return s.capabilities(kind)
        if a.verb == 'describe':
            return s.capabilities(kind)[a.name]
        return s.run(kind, a.name, load(a.input), getattr(a, 'key', None))
    if a.command == 'process':
        if a.verb == 'tick':
            return s.rt.tick(s.token)
        rows = s.run('queries', 'records', {'object': 'process'})
        return next(r for r in rows if r['id'] == a.id)
    if a.command == 'change':
        if a.verb == 'create':
            return s.draft(load(a.candidate), load(a.cases), a.request)
        if a.verb == 'update':
            c = s.change(a.id)
            if c['status'] == 'published':
                raise Conflict('published changes are immutable')
            c.update(candidate=load(a.candidate), cases=load(a.cases), status='draft')
            c['candidate']['release_id'] = 'r-' + uuid.uuid4().hex
            c.pop('validation', None)
            return s.save(c)
        return getattr(s, {'inspect': 'change', 'diff': 'diff', 'test': 'test', 'publish': 'publish'}[a.verb])(a.id)
    if a.command in ('release', 'audit'):
        s.owner()
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(s.rt.admin_dsn, row_factory=dict_row) as c:
            if a.command == 'release':
                return c.execute("SELECT tenant,release_id,current FROM releases WHERE tenant IN (%s,'*') ORDER BY release_id", (s.tenant,)).fetchall()
            return c.execute('SELECT id,release_id,action,opkey,actor_id,result FROM execution WHERE tenant_id=%s ORDER BY id', (s.tenant,)).fetchall()
    raise ValueError('unsupported command')


if __name__ == '__main__':
    sys.exit(main())
