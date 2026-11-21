import copy
import hashlib
import json
import sys
import socket
import subprocess
import urllib.request
import time
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

HERE = Path(__file__).resolve().parent
CORE = HERE.parent / 'procurement'
sys.path.insert(0, str(CORE))
import psycopg
from runtime import Runtime, default_definition, Denied, Conflict
from postgres import IsolatedPostgres
from builder_platform import BuilderPlatform, GateRejected


def main():
    started = time.monotonic()
    out = HERE / 'evidence'
    out.mkdir(exist_ok=True)
    checks = []
    def check(name, cond, detail=None):
        record = {'name': name, 'passed': bool(cond)}
        if detail is not None:
            record['detail'] = detail
        checks.append(record)
        if not cond:
            raise AssertionError(name)
    def rejects(fn, classes=(Denied, Conflict, GateRejected)):
        try:
            fn()
        except classes:
            return True
        return False

    pg = IsolatedPostgres().start()
    supplier = None
    report = {'status': 'running', 'checks': checks,
              'scope': 'prepared change package on a local PostgreSQL fixture and shared runtime',
              'input_mode': 'prepared sample change'}
    try:
        rt = Runtime(pg.dsn)
        full = default_definition()
        base = copy.deepcopy(full)
        base['release_id'] = 'builder-base-v1'
        base['objects'] = {k: v for k, v in base['objects'].items() if k in ['project', 'reservation', 'contract']}
        base['actions'] = {k: v for k, v in base['actions'].items() if k.startswith('contract.') or k == 'budget.reserve'}
        base['process'] = {}
        base['modules'] = {'project': {'objects': ['project', 'reservation']}, 'contract': {'objects': ['contract']}}
        rt.bootstrap(base)
        tokens = {name: rt.add_actor(tenant, role, id=name) for name, tenant, role in [
            ('owner','a','owner'), ('employee','a','employee'), ('service','a','service'), ('delivery','a','delivery'), ('other','b','owner')]}
        platform = BuilderPlatform(rt, out, tenant='a')
        def action(name, payload, who='owner', key=None):
            return rt.action(name, tokens[who], payload, key or uuid.uuid4().hex)
        def rows(table):
            return rt.query('records', tokens['owner'], {'object': table})
        with psycopg.connect(pg.dsn) as db:
            db.execute("INSERT INTO project(tenant_id,id,release_id,name,budget,reserved,cost) VALUES('a','p1','builder-base-v1','Existing Project',20000,0,0)")
            project_oid = db.execute("SELECT 'project'::regclass::oid").fetchone()[0]
            check('procurement table absent before installation', db.execute("SELECT to_regclass('purchase')").fetchone()[0] is None)
        old_contract = action('contract.create', {'amount': 80000})
        installed = copy.deepcopy(full)
        installed['release_id'] = 'builder-procurement-v2'
        installed['version'] = 2
        installed['modules'] = {**base['modules'], 'procurement': {'project_object': 'project', 'budget_action': 'budget.reserve'}}
        draft = platform.draft('安装项目采购，复用已有项目和预算公开动作，不复制项目。', installed, 'install-procurement')
        check('installation passes release-gate checks', platform.validate(draft))
        check('nonpublisher blocked by actual Runtime', rejects(lambda: platform.publish(draft, tokens['employee'])))
        check('other-enterprise publisher blocked', rejects(lambda: platform.publish(draft, tokens['other'])))
        platform.publish(draft, tokens['owner'])
        with psycopg.connect(pg.dsn) as db:
            check('actual procurement table created', db.execute("SELECT to_regclass('purchase')").fetchone()[0] is not None)
            check('existing project physical table reused', db.execute("SELECT 'project'::regclass::oid").fetchone()[0] == project_oid)
        check('project data preserved', len(rows('project')) == 1 and rows('project')[0]['id'] == 'p1')
        waiting = action('purchase.submit', {'project_id': 'p1', 'amount': 15000}, 'employee')
        check('actual old process is waiting before new release', rows('process')[0]['status'] == 'waiting')
        before_wait = copy.deepcopy(rows('process')[0])

        candidate = platform.catalog()
        candidate['version'] = 3
        candidate['release_id'] = 'builder-cost-v3'
        candidate['extensions']['contract.sign'] = (HERE / 'generated/cost_gate.expr').read_text().strip()
        candidate['policy']['cost_gate'] = True
        candidate['policy']['cost_threshold'] = 30000
        candidate['objects']['contract']['columns']['cost_assessment_note'] = 'text'
        (HERE / 'generated/change-package.json').write_text(json.dumps(candidate, ensure_ascii=False, indent=2))

        # Negative changes remain unpublished. These tests are not authored into the generated business expression.
        inverted = copy.deepcopy(candidate)
        inverted['extensions']['contract.sign'] = "{'requires_cost': amount < threshold}"
        bad = platform.draft('坏修改：把大于误写成小于', inverted, 'reject-inverted-rule')
        check('independent business oracle rejects inverted rule', not platform.validate(bad))
        wrong_threshold = copy.deepcopy(candidate)
        wrong_threshold['policy']['cost_threshold'] = 999999
        bad = platform.draft('坏修改：门槛与需求不符', wrong_threshold, 'reject-wrong-threshold')
        check('independent oracle uses actual policy threshold', not platform.validate(bad))
        unsafe = copy.deepcopy(candidate)
        unsafe['extensions']['contract.sign'] = "__import__('os').environ"
        bad = platform.draft('坏修改：读取宿主环境', unsafe, 'reject-unsafe-code')
        check('unsafe extension blocked before release', not platform.validate(bad))
        breaking = copy.deepcopy(candidate)
        del breaking['objects']['contract']['columns']['amount']
        bad = platform.draft('坏修改：删除旧合同金额', breaking, 'reject-breaking-schema')
        check('old schema deletion blocked by gate', not platform.validate(bad))
        check('old schema deletion also blocked by Runtime', rejects(lambda: rt.publish(breaking, tokens['owner'])))
        newdraft = platform.draft((HERE / 'request.md').read_text(), candidate, 'cost-evaluation')
        check('generated package passes independent validation', platform.validate(newdraft))
        mutated = copy.deepcopy(newdraft)
        mutated['candidate']['policy']['cost_threshold'] = 999999
        check('candidate mutation after validation rejected', rejects(lambda: platform.publish(mutated, tokens['owner'])))
        release = platform.publish(newdraft, tokens['owner'])
        check('actual new release activated', platform.catalog()['release_id'] == release == 'builder-cost-v3')
        check('other enterprise release unaffected', rt.current_definition('b')['release_id'] == 'builder-base-v1')
        old = next(r for r in rows('contract') if r['id'] == old_contract['id'])
        check('old data and release binding preserved', old['amount'] == 80000 and old['release_id'] == 'builder-base-v1')
        check('additive migration preserves unknown evaluation as null', old['cost_assessment_note'] is None and not old['assessed'])
        check('old draft contract still signs under old business rule', action('contract.sign', {'id': old_contract['id']})['status'] == 'signed')
        boundary = action('contract.create', {'amount': 30000})
        check('30000 boundary can sign without cost assessment', action('contract.sign', {'id': boundary['id']})['status'] == 'signed')
        high = action('contract.create', {'amount': 30001})
        check('30001 new contract blocked without assessment', rejects(lambda: action('contract.sign', {'id': high['id']})))
        check('blocked sign has not changed persisted status', next(r for r in rows('contract') if r['id'] == high['id'])['status'] == 'draft')
        check('client cannot select an old release', rejects(lambda: action('contract.sign', {'id': high['id'], 'release_id': 'builder-base-v1'})))
        action('contract.assess', {'id': high['id']}, 'delivery')
        check('new assessed contract signs via actual generated extension', action('contract.sign', {'id': high['id']})['status'] == 'signed')
        action('approval.approve', {'id': waiting['approval_id']})
        rt.tick(tokens['service'])
        after_wait = next(r for r in rows('process') if r['id'] == before_wait['id'])
        check('old waiting procurement resumes after publishing', after_wait['step'] == 'wait_receipt' and after_wait['release_id'] == before_wait['release_id'])
        check('public project budget action used once', rows('project')[0]['reserved'] == 15000 and len(rows('reservation')) == 1)
        action('purchase.dispatch', {'id': waiting['purchase_id']})
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        supplier_url = f'http://127.0.0.1:{port}'
        supplier = subprocess.Popen([sys.executable, str(CORE/'supplier.py'), '--db', str(pg.root/'supplier.sqlite'), '--port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for attempt in range(50):
            try:
                urllib.request.urlopen(supplier_url, timeout=.1).close()
                break
            except Exception:
                time.sleep(.05)
        else:
            raise RuntimeError('local supplier did not become ready')
        rt.deliver_pending(tokens['service'], supplier_url)
        with urllib.request.urlopen(supplier_url) as response:
            supplier_orders = json.load(response)['orders']
        check('actual local HTTP supplier records one external order', len(supplier_orders) == 1)
        report['supplier_orders'] = supplier_orders
        action('purchase.receive', {'id': waiting['purchase_id']}, 'delivery')
        rt.tick(tokens['service'])
        check('old procurement completes without duplicated facts', rows('process')[0]['status'] == 'completed' and len(rows('cost_entry')) == 1)
        # Actual separate PostgreSQL connections, not a sequential UI exercise.
        with psycopg.connect(pg.dsn) as db:
            db.execute("INSERT INTO project(tenant_id,id,release_id,name,budget,reserved,cost) VALUES('a','p2','builder-cost-v3','Concurrency',20000,0,0)")
        attempts = [action('purchase.submit', {'project_id':'p2','amount':15000}, 'employee') for _ in range(2)]
        for r in attempts:
            action('approval.approve', {'id':r['approval_id']})
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(lambda r: action('purchase.authorize', {'id':r['purchase_id']}), attempts))
        check('installed module shares actual concurrent project budget', sorted(r['status'] for r in outcomes) == ['approved', 'awaiting_budget'])
        report['snapshots'] = {name: rows(name) for name in ['project','contract','purchase','approval','process','reservation','cost_entry']}
        report['generated_expression_sha256'] = hashlib.sha256((HERE/'generated/cost_gate.expr').read_bytes()).hexdigest()
        report['runtime_sha256'] = hashlib.sha256((CORE/'runtime.py').read_bytes()).hexdigest()
        report['status'] = 'passed'
        report['limitations'] = [
            'No integrated autonomous model API; the live child agent authored this specific expression and package.',
            'Release-gate checks are a Builder wrapper and can be bypassed by calling Runtime.publish directly; not a platform-wide enforcement proof.',
            'Baseline check and publication are separate operations; concurrent stale publication TOCTOU is not solved by this experiment.',
            'Old contract is a draft record; old waiting process is a separate procurement instance, not an end-to-end waiting contract process.',
            'Cost assessment is a boolean field in the shared core; no independently evidenced assessment document or business revision validation.',
            'Physical schema shared across tenants; tenant release selection is isolated, but independent tenant schema customization is not proved.',
            'Runtime built-in trusted handlers use database cursors; this experiment does not sandbox arbitrary generated Python handlers.',
            'External side is an actual localhost HTTP simulator with SQLite receipts, not a real vendor account; no production connector/authentication claim.',
            'One natural-language change and one module installation, not the frozen 20-case Builder benchmark.'
        ]
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = {'type':type(exc).__name__, 'message':str(exc)}
        raise
    finally:
        report['duration_seconds'] = round(time.monotonic()-started,3)
        (out/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        if supplier is not None:
            supplier.terminate()
            supplier.wait(timeout=5)
        pg.stop()
        print(json.dumps({'status':report['status'],'checks':len(checks),'passed':sum(c['passed'] for c in checks),'duration_seconds':report['duration_seconds']},ensure_ascii=False))


if __name__ == '__main__':
    main()
