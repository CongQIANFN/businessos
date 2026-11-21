import concurrent.futures,json,os,subprocess,sys,time,uuid,socket,urllib.request,atexit
from pathlib import Path
import psycopg
from psycopg.types.json import Jsonb
from runtime import Runtime,Denied,Conflict,insert
from postgres import IsolatedPostgres
HERE=Path(__file__).parent

def fixture():
    pg=IsolatedPostgres().start();rt=Runtime(pg.dsn);rt.bootstrap()
    tokens={name:rt.add_actor(tenant,role,id=name) for name,tenant,role in [('owner','a','owner'),('employee','a','employee'),('service','a','service'),('delivery','a','delivery'),('other','b','owner')]}
    with psycopg.connect(pg.dsn) as c:c.execute("INSERT INTO project VALUES('a','p1','v1','Sample project',20000,0,0)");c.execute("INSERT INTO project VALUES('b','p1','v1','Other enterprise',50000,0,0)")
    return pg,rt,tokens

def run(pg,rt,t):
    checks=[]
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));supplier_port=sock.getsockname()[1]
    supplier_url=f'http://127.0.0.1:{supplier_port}'
    supplier=subprocess.Popen([sys.executable,str(HERE/'supplier.py'),'--db',str(pg.root/'supplier.sqlite'),'--port',str(supplier_port)])
    atexit.register(supplier.terminate)
    for _ in range(40):
        try:
            urllib.request.urlopen(supplier_url,timeout=.1).close();break
        except Exception:time.sleep(.05)
    def ok(name,cond):
        if not cond:raise AssertionError(name)
        checks.append(name)
    def act(name,p,who='owner',key=None):return rt.action(name,t[who],p,key or uuid.uuid4().hex)
    def rows(table,who='owner'):return rt.query('records',t[who],{'object':table})
    def expect_denied(fn):
        try:fn()
        except Denied:return True
        return False
    baseline_contract=act('contract.create',{'amount':40000},'owner','baseline-contract')
    old_a=rt.current_definition('a');old_b=rt.current_definition('b');d=json.loads(json.dumps(old_a));d['release_id']='a-only';d['policy']['cost_gate']=True;rt.publish(d,t['owner'])
    ok('release publication isolated by tenant',rt.current_definition('a')['release_id']=='a-only' and rt.current_definition('b')['release_id']==old_b['release_id'])
    shadow=json.loads(json.dumps(d));shadow['release_id']='v1'
    try:rt.publish(shadow,t['owner']);shadow_denied=False
    except Conflict:shadow_denied=True
    ok('tenant cannot shadow global release ID',shadow_denied)
    ok('old baseline contract retains original policy',act('contract.sign',{'id':baseline_contract['id']})['release_id']=='v1')
    shaped=rt.current_definition('a');shaped['release_id']='a-shape';shaped['objects']['project']['columns']['a_internal_note']='text';rt.publish(shaped,t['owner'])
    ok('query projects only tenant declared schema', 'a_internal_note' not in rows('project','other')[0])
    wrongshape=rt.current_definition('b');wrongshape['release_id']='b-conflict';wrongshape['objects']['project']['columns']['a_internal_note']='numeric'
    try:rt.publish(wrongshape,t['other']);conflict=False
    except Conflict:conflict=True
    ok('cross-tenant incompatible physical type is rejected',conflict)
    restored=json.loads(json.dumps(rt.current_definition('a')));restored['release_id']='a-base';restored['policy']=old_a['policy'];rt.publish(restored,t['owner'])
    candidates=[]
    for name in ['publish-concurrent-a','publish-concurrent-b']:
        candidate=rt.current_definition('a');candidate['release_id']=name;candidates.append(candidate)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:list(pool.map(lambda candidate:rt.publish(candidate,t['owner']),candidates))
    with psycopg.connect(pg.dsn) as c:current_count=c.execute("SELECT count(*) FROM releases WHERE tenant='a' AND current").fetchone()[0]
    ok('concurrent same-tenant publication retains one current release',current_count==1)
    ok('direct internal budget reservation rejected',expect_denied(lambda:act('budget.reserve',{'project_id':'p1','source':'no-approval','amount':1})))
    secret=act('contract.create',{'amount':1},'owner','shared-key')
    ok('receipt cannot be replayed by another same-tenant actor',expect_denied(lambda:act('contract.create',{'amount':1},'employee','shared-key')))
    ok('bootstrap cannot reset published database',expect_denied(lambda:rt.bootstrap()))
    blocked=rt.current_definition();blocked['release_id']='reserved-table';blocked['objects']['actors']={'columns':{'secret':'text'}}
    ok('module cannot redefine reserved platform tables',expect_denied(lambda:rt.publish(blocked,t['owner'])))
    r=act('purchase.submit',{'project_id':'p1','amount':15000},'employee','submit1')
    ok('idempotent submit',act('purchase.submit',{'project_id':'p1','amount':15000},'employee','submit1')==r)
    ok('employee cannot approve',expect_denied(lambda:act('approval.approve',{'id':r['approval_id']},'employee')))
    act('approval.approve',{'id':r['approval_id']})
    worker=subprocess.Popen([sys.executable,str(HERE/'worker.py'),pg.dsn,t['service']],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    deadline=time.time()+5
    while time.time()<deadline and rows('purchase')[0]['status'] not in ('approved','ordering'):time.sleep(.1)
    worker.terminate();worker.wait(timeout=5)
    ok('real worker consumes approval that arrived before wait check',rows('purchase')[0]['status'] in ('approved','ordering'))
    subprocess.run([sys.executable,str(HERE/'worker.py'),pg.dsn,t['service'],'--once'],check=True,capture_output=True)
    ok('new worker recovers persisted waiting receipt',rows('process')[0]['step']=='wait_receipt')
    act('purchase.dispatch',{'id':r['purchase_id']},key='dispatch1');act('purchase.dispatch',{'id':r['purchase_id']},key='dispatch2')
    ok('owner cannot forge supplier acknowledgement',expect_denied(lambda:act('external.confirm',{'id':rows('external_order')[0]['id'],'receipt':'forged'})))
    rt.deliver_pending(t['service'],supplier_url)
    with urllib.request.urlopen(supplier_url) as response:supplier_rows=json.load(response)['orders']
    ok('independent supplier has one durable receipt',len(rows('external_order'))==1 and len(supplier_rows)==1)
    act('purchase.receive',{'id':r['purchase_id']},'delivery');act('purchase.receive',{'id':r['purchase_id']},'delivery')
    rt.tick(t['service']);ok('receipt dedup and completed process',len(rows('cost_entry'))==1 and rows('process')[0]['status']=='completed')
    ok('project converts reserved to actual cost',rows('project')[0]['reserved']==0 and rows('project')[0]['cost']==15000)
    ok('real PG RLS isolates enterprise',rows('project','other')[0]['name']=='Other enterprise' and len(rows('purchase','other'))==0)
    # Independent project for real concurrent connections.
    with psycopg.connect(pg.dsn) as c:c.execute("INSERT INTO project VALUES('a','p2','v1','Concurrent',20000,0,0)")
    requests=[act('purchase.submit',{'project_id':'p2','amount':15000},'employee') for _ in range(2)]
    for a in requests:act('approval.approve',{'id':a['approval_id']})
    with concurrent.futures.ThreadPoolExecutor(2) as pool:out=list(pool.map(lambda a:act('purchase.authorize',{'id':a['purchase_id']}),requests))
    ok('two actual PG connections contend for budget',sorted(x['status'] for x in out)==['approved','awaiting_budget'])
    with psycopg.connect(pg.dsn) as c:c.execute("INSERT INTO project VALUES('a','p3','v1','Rollback',20000,0,0)")
    failure=act('purchase.submit',{'project_id':'p3','amount':10000},'employee');act('approval.approve',{'id':failure['approval_id']})
    try:act('purchase.authorize',{'id':failure['purchase_id'],'fault_after_reserve':True})
    except RuntimeError:pass
    ok('cross-module write rolls back after injected failure',next(x for x in rows('project') if x['id']=='p3')['reserved']==0 and not any(x['source']==failure['purchase_id'] for x in rows('reservation')))
    # Kill an actual process after local action writes and before transaction commit.
    env={**os.environ,'EXPERIMENT_CRASH_AFTER_ACTION':'purchase.authorize'}
    code="from runtime import Runtime; import sys; Runtime(sys.argv[1]).action('purchase.authorize',sys.argv[2],{'id':sys.argv[3]},'crash-key')"
    crash=subprocess.run([sys.executable,'-c',code,pg.dsn,t['owner'],failure['purchase_id']],env=env,cwd=HERE)
    ok('killed writer transaction rolls back',crash.returncode==91 and next(x for x in rows('project') if x['id']=='p3')['reserved']==0)
    act('purchase.authorize',{'id':failure['purchase_id']},key='crash-key');ok('same operation resumes after actual crash',next(x for x in rows('project') if x['id']=='p3')['reserved']==10000)
    lost=act('purchase.submit',{'project_id':'p1','amount':1000},'employee');act('approval.approve',{'id':lost['approval_id']});act('purchase.authorize',{'id':lost['purchase_id']});act('purchase.dispatch',{'id':lost['purchase_id']})
    httpcode="from runtime import Runtime; import sys; Runtime(sys.argv[1]).deliver_pending(sys.argv[2],sys.argv[3])"
    httpcrash=subprocess.run([sys.executable,'-c',httpcode,pg.dsn,t['service'],supplier_url],env={**os.environ,'EXPERIMENT_CRASH_AFTER_SUPPLIER':'1'},cwd=HERE)
    ok('writer killed after supplier accepted leaves local request unresolved',httpcrash.returncode==92 and any(x['source']==lost['purchase_id'] and x['status']=='pending' for x in rows('external_order')))
    Runtime(pg.dsn).deliver_pending(t['service'],supplier_url)
    with urllib.request.urlopen(supplier_url) as response:all_orders=json.load(response)['orders']
    ok('fresh runtime reconciles same supplier key without another order',len(all_orders)==2 and all(x['status']=='confirmed' for x in rows('external_order')))
    bad=rt.current_definition();bad['release_id']='extension-bad';bad['extensions']['purchase.authorize']="{'op':'write_other_tenant'}";rt.publish(bad,t['owner'])
    with psycopg.connect(pg.dsn) as c:c.execute("INSERT INTO project VALUES('a','p4','v1','Extension boundary',20000,0,0)")
    attempt=act('purchase.submit',{'project_id':'p4','amount':10000},'employee');act('approval.approve',{'id':attempt['approval_id']})
    ok('restricted child plan cannot request undeclared write',expect_denied(lambda:act('purchase.authorize',{'id':attempt['purchase_id']})))
    ok('extension rejection rolls back prior budget mutation',next(x for x in rows('project') if x['id']=='p4')['reserved']==0)
    unsafe=rt.current_definition();unsafe['release_id']='import-attempt';unsafe['extensions']['contract.sign']="__import__('os').environ"
    ok('extension imports rejected at publish',expect_denied(lambda:rt.publish(unsafe,t['owner'])))
    rt.revoke(t['employee']);ok('revocation affects next backend call',expect_denied(lambda:act('purchase.submit',{'project_id':'p1','amount':1},'employee')))
    ok('client cannot select old release',expect_denied(lambda:act('contract.create',{'amount':1,'release_id':'v1'})))
    supplier.terminate();supplier.wait(timeout=5)
    return checks

if __name__=='__main__':
    if '--stop' in sys.argv:
        state=json.loads((HERE/'fixture.json').read_text());IsolatedPostgres(state['pg_root']).stop();print('isolated postgres stopped');sys.exit()
    pg,rt,t=fixture()
    try:
        if '--fixture' in sys.argv:
            info={'dsn':pg.dsn,'tokens':t,'pg_root':str(pg.root)};(HERE/'fixture.json').write_text(json.dumps(info,indent=2));print(json.dumps(info));sys.exit()
        checks=run(pg,rt,t);report={'status':'passed','checks':checks,'scope':'real isolated PostgreSQL, no production sandbox','postgres_root':str(pg.root)};(HERE/'results.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
    finally:
        if '--fixture' not in sys.argv:pg.stop()
