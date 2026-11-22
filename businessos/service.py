"""Local BusinessOS operation layer. Host access is trusted; not a remote security boundary."""
import copy
import difflib
import hashlib
import json
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'experiments/procurement'))
import psycopg
from psycopg.types.json import Jsonb
from psycopg.conninfo import conninfo_to_dict
from postgres import IsolatedPostgres
from runtime import Runtime, Denied, Conflict, insert, get, ident


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def check_input(value, schema):
    if not isinstance(value, dict):
        raise ValueError('input must be an object')
    props = schema.get('properties', {})
    unknown = set(value) - set(props)
    if unknown:
        raise ValueError('unknown input fields: ' + ', '.join(sorted(unknown)))
    for key in schema.get('required', []):
        if key not in value:
            raise ValueError('missing input field: ' + key)
    kinds = {'string': str, 'integer': int, 'boolean': bool, 'object': dict}
    for key, val in value.items():
        typ = props[key]['type']
        if typ not in kinds or type(val) is not kinds[typ]:
            raise ValueError('invalid input type: ' + key)
        if typ == 'integer' and 'minimum' in props[key] and val < props[key]['minimum']:
            raise ValueError('input below minimum: ' + key)


# Descriptions are part of the installed definition, available to every client.
def annotated(definition):
    d = copy.deepcopy(definition)
    inputs = {
        'contract.create': ({'amount': 'integer'}, ['amount'], 'Create a draft contract; amount is whole CNY.'),
        'contract.assess': ({'id': 'string'}, ['id'], 'Complete cost assessment for a contract.'),
        'contract.sign': ({'id': 'string'}, ['id'], 'Sign a contract using its recorded business version; assessment may be required.'),
        'purchase.submit': ({'project_id': 'string', 'amount': 'integer'}, ['project_id', 'amount'], 'Submit a purchase and create approval and process records.'),
        'approval.approve': ({'id': 'string'}, ['id'], 'Approve another applicant’s pending approval.'),
        'purchase.authorize': ({'id': 'string'}, ['id'], 'Authorize a purchase and reserve project budget.'),
        'purchase.dispatch': ({'id': 'string'}, ['id'], 'Queue an authorized order for the connector.'),
        'purchase.receive': ({'id': 'string'}, ['id'], 'Receive a confirmed order and book its cost.'),
        'process.advance': ({'id': 'string'}, ['id'], 'Advance durable process steps until the next wait.'),
    }
    for name, (fields, required, description) in inputs.items():
        if name in d['actions']:
            d['actions'][name].update(description=description, input={'type': 'object', 'properties': {k: {'type': v} for k, v in fields.items()}, 'required': required}, output={'type': 'object'}, errors=['Denied', 'Conflict', 'ValueError'])
    d['queries']['records'].update(description='List rows of one declared Object within the current enterprise.', input={'type': 'object', 'properties': {'object': {'type': 'string'}}, 'required': ['object']}, output={'type': 'array'})
    d.setdefault('modules', {'contracts': {'description': 'Contracts and cost assessment'}, 'procurement': {'description': 'Purchase approval, budgets and durable waits'}})
    return d


def create_record(ctx, payload):
    cfg = ctx.definition['actions'][ctx.action_name]
    check_input(payload, cfg['input'])
    return insert(ctx, cfg['object'], {**cfg.get('defaults', {}), **payload})


def make_runtime(dsn):
    rt = Runtime(dsn)
    rt.register_handler('data.create', create_record)
    return rt


def initialize(home):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (home / 'instance.json').exists() or (home / 'pg/data').exists():
        raise Conflict('instance already exists; use instance start')
    pg = IsolatedPostgres(home / 'pg').start()
    try:
        rt = make_runtime(pg.dsn)
        from runtime import default_definition
        rt.bootstrap(annotated(default_definition()))
        tokens = {role: rt.add_actor('demo', role, 'local-' + role) for role in ('owner', 'employee', 'agent', 'delivery', 'service')}
        config = {'dsn': pg.dsn, 'identities': tokens, 'identity': 'owner', 'tenant': 'demo'}
        with psycopg.connect(pg.dsn) as c:
            c.execute('CREATE TABLE bos_changes(tenant text,id text,body jsonb,PRIMARY KEY(tenant,id))')
            # Synthetic project fixture, only the local initializer uses direct SQL.
            c.execute("INSERT INTO project VALUES('demo','sample-project','v1','Demo project',1000000,0,0)")
        path = home / 'instance.json'
        path.write_text(encode(config)); path.chmod(0o600)
    except Exception:
        pg.stop()
        raise
    return {'instance': str(home), 'tenant': 'demo', 'identity': 'owner', 'fixture': True}


def lifecycle(home, operation):
    cfg = json.loads((home / 'instance.json').read_text())
    args = ['pg_ctl', '-D', str(home / 'pg/data'), '-w']
    if operation == 'stop':
        args += ['-m', 'fast', 'stop']
    else:
        info = conninfo_to_dict(cfg['dsn'])
        options = '-F -p ' + info['port'] + ' -k ' + shlex.quote(info['host']) + ' -h ""'
        args += ['-l', str(home / 'pg/postgres.log'), '-o', options, 'start']
    subprocess.run(args, check=True, capture_output=True)
    return {'instance': str(home), 'status': operation + 'ped' if operation == 'stop' else 'started'}


class Service:
    def __init__(self, home, identity=None):
        self.home = home
        self.config = json.loads((home / 'instance.json').read_text())
        selected = identity or self.config['identity']
        self.token = self.config['identities'][selected]
        self.rt = make_runtime(self.config['dsn'])
        self.actor = self.rt.actor(self.token)
        self.tenant = self.actor['tenant']

    def owner(self):
        if self.rt.actor(self.token)['role'] != 'owner':
            raise Denied('owner permission required')

    def catalog(self):
        return self.rt.current_definition(self.tenant)

    def capabilities(self, kind):
        return {k: v for k, v in self.catalog()[kind].items() if not v.get('internal') and self.actor['role'] in v['roles']}

    def run(self, kind, name, payload, key=None):
        caps = self.capabilities(kind)
        if name not in caps:
            raise Denied('capability not permitted: ' + name)
        check_input(payload, caps[name]['input'])
        if kind == 'queries':
            return self.rt.query(name, self.token, payload)
        return self.rt.action(name, self.token, payload, key or uuid.uuid4().hex)

    def change(self, change_id):
        self.owner()
        with psycopg.connect(self.rt.admin_dsn) as c:
            row = c.execute('SELECT body FROM bos_changes WHERE tenant=%s AND id=%s', (self.tenant, change_id)).fetchone()
        if not row:
            raise ValueError('unknown change')
        return row[0]

    def save(self, change):
        with psycopg.connect(self.rt.admin_dsn) as c:
            c.execute('INSERT INTO bos_changes VALUES(%s,%s,%s) ON CONFLICT(tenant,id) DO UPDATE SET body=excluded.body', (self.tenant, change['id'], Jsonb(change)))
        return change

    def draft(self, candidate, cases, request):
        self.owner()
        base = self.catalog()
        candidate = copy.deepcopy(candidate)
        candidate['release_id'] = 'r-' + uuid.uuid4().hex
        return self.save({'id': uuid.uuid4().hex, 'request': request, 'base': base, 'base_hash': digest(base), 'candidate': candidate, 'cases': cases, 'status': 'draft'})

    def diff(self, change_id):
        c = self.change(change_id)
        return {'id': change_id, 'request': c['request'], 'diff': ''.join(difflib.unified_diff((encode(c['base'])+'\n').splitlines(True), (encode(c['candidate'])+'\n').splitlines(True), fromfile='base', tofile='candidate'))}

    def validate(self, change):
        if change['base_hash'] != digest(self.catalog()):
            raise Conflict('stale base release')
        candidate = change['candidate']
        # Keep platform schemas and capability contracts compatible with existing records.
        for name, old in change['base']['objects'].items():
            new = candidate['objects'].get(name, {}).get('columns', {})
            if any(new.get(k) != v for k, v in old['columns'].items()):
                raise Conflict('incompatible object: ' + name)
        for kind in ('actions', 'queries'):
            for name, cfg in candidate[kind].items():
                if cfg.get('internal'):
                    continue
                for field in ('description', 'input', 'output', 'roles'):
                    if field not in cfg:
                        raise ValueError(name + ': missing ' + field)
                if cfg['input'].get('type') != 'object':
                    raise ValueError('input must declare object schema')
                props = cfg['input'].get('properties', {})
                if set(cfg['input'].get('required', [])) - set(props):
                    raise ValueError('required fields must exist in input schema')
                if any(v.get('type') not in ('string', 'integer', 'boolean', 'object') for v in props.values()):
                    raise ValueError('unsupported input type')
                if cfg.get('handler') == 'data.create':
                    obj = candidate['objects'][cfg['object']]['columns']
                    if (set(props) | set(cfg.get('defaults', {}))) - set(obj):
                        raise ValueError('create action references undeclared fields')
        cases = change['cases']
        if len(cases.get('before', [])) + len(cases.get('after', [])) > 200:
            raise ValueError('too many test steps; maximum 200')
        if not cases.get('after') or not all('expect' in s or 'error' in s for s in cases['after']):
            raise ValueError('after cases require explicit expected outcomes')
        if not any(s.get('expect') or s.get('error') for s in cases['after']):
            raise ValueError('at least one behavioral assertion is required')
        with tempfile.TemporaryDirectory(prefix='bos-validation-') as temp:
            with IsolatedPostgres(Path(temp)) as pg:
                rt = make_runtime(pg.dsn)
                rt.bootstrap(change['base'])
                tokens = {r: rt.add_actor('test', r) for r in self.config['identities']}
                values, evidence = {}, []
                def resolve(value):
                    if isinstance(value, str) and value.startswith('$'):
                        parts = value[1:].split('.')
                        result = values[parts[0]]
                        for key in parts[1:]:
                            result = result[key]
                        return result
                    if isinstance(value, dict):
                        return {k: resolve(v) for k, v in value.items()}
                    return value
                def steps(items):
                    for step in items:
                        payload = resolve(step.get('input', {}))
                        token = tokens[step.get('identity', 'owner')]
                        kind = step.get('kind', 'action')
                        cfg = rt.current_definition('test')[kind + 's'][step['name']]
                        try:
                            check_input(payload, cfg['input'])
                            result = rt.query(step['name'], token, payload) if kind == 'query' else rt.action(step['name'], token, payload, uuid.uuid4().hex)
                        except (Denied, Conflict, ValueError) as exc:
                            if not step.get('error') or step['error'] not in str(exc):
                                raise
                            result = {'error': str(exc)}
                        else:
                            if 'error' in step:
                                raise ValueError('expected operation to fail: ' + step['name'])
                            for key, expected in step.get('expect', {}).items():
                                if result.get(key) != expected:
                                    raise ValueError('unexpected outcome: ' + step['name'] + '.' + key)
                        if 'save' in step:
                            values[step['save']] = result
                        evidence.append({'name': step['name'], 'result': result})
                steps(cases.get('before', []))
                rt.publish(candidate, tokens['owner'])
                steps(cases['after'])
        return {'passed': True, 'artifact_hash': digest({'candidate': candidate, 'cases': cases}), 'evidence': evidence}

    def test(self, change_id):
        c = self.change(change_id)
        if c['status'] == 'published':
            raise Conflict('change already published')
        try:
            c['validation'] = self.validate(c)
            c['status'] = 'validated'
        except Exception as exc:
            c['status'] = 'rejected'; c['validation'] = {'passed': False, 'error': str(exc)}
            self.save(c)
            raise
        return self.save(c)

    def publish(self, change_id):
        c = self.change(change_id)
        if c['status'] == 'published':
            return {'release_id': c['candidate']['release_id'], 'deduplicated': True}
        artifact = digest({'candidate': c['candidate'], 'cases': c['cases']})
        if c['status'] != 'validated' or c.get('validation', {}).get('artifact_hash') != artifact:
            raise Denied('change requires passing validation')
        # Re-execute tests in the operation layer, not trust a client-supplied success flag.
        self.validate(c)
        rid = self.rt.publish(c['candidate'], self.token, expected_base=c['base_hash'])
        c['status'] = 'published'; self.save(c)
        return {'release_id': rid, 'capabilities_updated': True}
