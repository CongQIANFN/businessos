"""Experimental PostgreSQL business runtime. Trusted plugins are NOT sandboxed."""
import ast, hashlib, json, os, subprocess, sys, uuid, urllib.request
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg.conninfo import make_conninfo

BASE = {'tenant_id':'text NOT NULL','id':'text NOT NULL','release_id':"text NOT NULL DEFAULT ''"}
TYPES={'text','integer','bigint','numeric','jsonb','boolean','timestamptz','date'}
def ident(s):
    if not isinstance(s,str) or not s or not all(c.islower() or c.isdigit() or c=='_' for c in s) or not (s[0].islower() or s[0]=='_'): raise ValueError('unsafe SQL identifier')
    return '"'+s+'"'
class Denied(Exception): pass
class Conflict(Exception): pass
class Context:
    def __init__(self,rt,cur,actor,definition): self.runtime,self.cur,self.actor,self.definition=rt,cur,actor,definition; self.release_id=definition['release_id']
    def call(self,name,payload): return self.runtime._invoke(self,name,payload)
    def extension(self,name,values,definition=None):
        source=(definition or self.definition).get('extensions',{}).get(name)
        if source is None: return None
        p=subprocess.run([sys.executable,'-I',str(Path(__file__).with_name('restricted_expression.py'))],input=json.dumps({'code':source,'values':values}),text=True,capture_output=True,timeout=2,env={'PATH':os.defpath})
        if p.returncode: raise Denied('extension rejected: '+p.stderr.strip())
        return json.loads(p.stdout)
class Runtime:
    def __init__(self,admin_dsn):
        self.admin_dsn=admin_dsn; self.app_dsn=make_conninfo(admin_dsn,user='experiment_app'); self.handlers={};self.query_handlers={}
        self.register_handler('purchase.submit',submit);self.register_handler('approval.approve',approve)
        self.register_handler('purchase.authorize',authorize);self.register_handler('budget.reserve',reserve)
        self.register_handler('external.confirm',external_confirm);self.register_handler('purchase.receive',receive);self.register_handler('purchase.dispatch',dispatch)
        self.register_handler('process.advance',advance);self.register_handler('contract.create',contract_create)
        self.register_handler('contract.sign',contract_sign);self.register_handler('contract.assess',contract_assess)
        self.register_query('record.list',list_records)
    def register_handler(self,name,fn):self.handlers[name]=fn
    def register_query(self,name,fn):self.query_handlers[name]=fn
    def bootstrap(self,definition=None):
        with psycopg.connect(self.admin_dsn) as c:
            if c.execute("SELECT to_regclass('public.releases')").fetchone()[0]:raise Denied('bootstrap requires empty experimental database')
            c.execute("DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='experiment_app') THEN CREATE ROLE experiment_app LOGIN; END IF; END $$")
            c.execute("CREATE TABLE actors(token text primary key,id text,tenant text,role text,active boolean default true)")
            c.execute("CREATE TABLE releases(tenant text,release_id text,definition jsonb,current boolean,PRIMARY KEY(tenant,release_id))")
            c.execute("CREATE UNIQUE INDEX one_current_release_per_tenant ON releases(tenant) WHERE current")
            c.execute("CREATE TABLE execution(tenant_id text,id text,release_id text,action text,opkey text,input_hash text,result jsonb,actor_id text,primary key(tenant_id,action,opkey))")
            self._secure(c,'execution')
        self._install(definition or default_definition(),'*')
    def _secure(self,c,t):
        q=ident(t);c.execute(f'ALTER TABLE {q} ENABLE ROW LEVEL SECURITY');c.execute(f'ALTER TABLE {q} FORCE ROW LEVEL SECURITY')
        c.execute(f"CREATE POLICY tenant_only ON {q} USING (tenant_id=current_setting('experiment.tenant',true)) WITH CHECK (tenant_id=current_setting('experiment.tenant',true))")
        c.execute(f'GRANT SELECT,INSERT,UPDATE,DELETE ON {q} TO experiment_app')
    def add_actor(self,tenant,role,id=None):
        token=uuid.uuid4().hex
        with psycopg.connect(self.admin_dsn) as c:c.execute('INSERT INTO actors(token,id,tenant,role) VALUES(%s,%s,%s,%s)',(token,id or uuid.uuid4().hex,tenant,role))
        return token
    def revoke(self,token):
        with psycopg.connect(self.admin_dsn) as c:c.execute('UPDATE actors SET active=false WHERE token=%s',(token,))
    def actor(self,token):
        with psycopg.connect(self.admin_dsn,row_factory=dict_row) as c:a=c.execute('SELECT id,tenant,role,active FROM actors WHERE token=%s',(token,)).fetchone()
        if not a or not a['active']:raise Denied('inactive or unknown identity')
        return a
    def current_definition(self,tenant='a'):
        with psycopg.connect(self.admin_dsn) as c:return c.execute("SELECT definition FROM releases WHERE current AND tenant IN (%s,'*') ORDER BY (tenant=%s) DESC LIMIT 1",(tenant,tenant)).fetchone()[0]
    def definition(self,rid,tenant='a'):
        with psycopg.connect(self.admin_dsn) as c:return c.execute("SELECT definition FROM releases WHERE release_id=%s AND tenant IN (%s,'*') ORDER BY (tenant=%s) DESC LIMIT 1",(rid,tenant,tenant)).fetchone()[0]
    def publish(self,definition,token,expected_base=None):
        actor=self.actor(token)
        if actor['role']!='owner':raise Denied('publish permission')
        return self._install(definition,actor['tenant'],expected_base)
    def _install(self,definition,tenant,expected_base=None):
        d=json.loads(json.dumps(definition));rid=d.setdefault('release_id','release-'+uuid.uuid4().hex)
        for cfg in d.get('actions',{}).values():
            if cfg['handler'] not in self.handlers:raise ValueError('unknown handler '+cfg['handler'])
        for code in d.get('extensions',{}).values():validate_expression(code)
        with psycopg.connect(self.admin_dsn) as c:
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('release:'+tenant,))
            if expected_base is not None:
                from hashlib import sha256
                live=c.execute("SELECT definition FROM releases WHERE current AND tenant IN (%s,'*') ORDER BY (tenant=%s) DESC LIMIT 1",(tenant,tenant)).fetchone()[0]
                actual=sha256(json.dumps(live,sort_keys=True,separators=(',', ':')).encode()).hexdigest()
                if actual!=expected_base:raise Conflict('stale base release')
            if tenant!='*' and c.execute("SELECT 1 FROM releases WHERE tenant='*' AND release_id=%s",(rid,)).fetchone():raise Conflict('tenant release cannot shadow global baseline ID')
            if c.execute('SELECT 1 FROM releases WHERE tenant=%s AND release_id=%s',(tenant,rid)).fetchone():raise Conflict('immutable release ID already exists')
            for name,obj in d.get('objects',{}).items():
                columns=obj.get('columns',{}); q=ident(name)
                if name in ('actors','releases','execution') or name.startswith('bos_'):raise Denied('reserved runtime table')
                if set(columns)&set(BASE):raise Denied('cannot redefine runtime columns')
                for field,typ in columns.items():
                    ident(field)
                    if typ not in TYPES:raise ValueError('unsupported field type')
                exists=c.execute('SELECT to_regclass(%s)',(name,)).fetchone()[0]
                if not exists:
                    spec={**BASE,**columns};c.execute(f'CREATE TABLE {q} ('+','.join(ident(k)+' '+v for k,v in spec.items())+',PRIMARY KEY(tenant_id,id))');self._secure(c,name)
                else:
                    current={r[0]:r[1] for r in c.execute("SELECT column_name,data_type FROM information_schema.columns WHERE table_schema='public' AND table_name=%s",(name,))}
                    previous=self.current_definition(tenant).get('objects',{}).get(name,{}).get('columns',{})
                    if set(previous)-set(columns):raise Conflict('cannot remove fields with active compatibility contract')
                    for field,typ in columns.items():
                        if field in previous and previous[field]!=typ:raise Conflict('incompatible field type')
                        expected={'timestamptz':'timestamp with time zone'}.get(typ,typ)
                        if field in current and current[field]!=expected:raise Conflict('physical schema type conflict across tenants')
                        if field not in current:c.execute(f'ALTER TABLE {q} ADD COLUMN {ident(field)} {typ}')
            c.execute('UPDATE releases SET current=false WHERE tenant=%s',(tenant,));c.execute('INSERT INTO releases VALUES(%s,%s,%s,true)',(tenant,rid,Jsonb(d)))
        return rid
    def _invoke(self,ctx,name,payload,internal=True):
        cfg=ctx.definition.get('actions',{}).get(name)
        if not cfg or ctx.actor['role'] not in cfg['roles'] or (cfg.get('internal') and not internal):raise Denied('action not permitted: '+name)
        previous=getattr(ctx,'action_name',None)
        ctx.action_name=name
        try:return self.handlers[cfg['handler']](ctx,payload)
        finally:ctx.action_name=previous
    def action(self,name,token,payload,key,**kwargs):
        if kwargs or any(x in payload for x in ('release_id','actor','tenant_id','role')):raise Denied('client context/version override')
        actor=self.actor(token);d=self.current_definition(actor['tenant']);h=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        with psycopg.connect(self.app_dsn,row_factory=dict_row) as c:
            c.execute("SELECT set_config('experiment.tenant',%s,true)",(actor['tenant'],));cur=c.cursor();ctx=Context(self,cur,actor,d)
            cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(actor['tenant']+name+key,))
            old=cur.execute('SELECT * FROM execution WHERE action=%s AND opkey=%s',(name,key)).fetchone()
            cfg=d.get('actions',{}).get(name)
            if not cfg or cfg.get('internal') or actor['role'] not in cfg['roles']:raise Denied('action not permitted')
            if old:
                if old['actor_id']!=actor['id']:raise Denied('execution receipt belongs to different actor')
                if old['input_hash']!=h:raise Conflict('idempotency input conflict')
                return old['result']
            result=self._invoke(ctx,name,payload,internal=False)
            if os.getenv('EXPERIMENT_CRASH_AFTER_ACTION')==name:os._exit(91)
            cur.execute('INSERT INTO execution VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(actor['tenant'],uuid.uuid4().hex,d['release_id'],name,key,h,Jsonb(result),actor['id']))
            return result
    def query(self,name,token,payload):
        actor=self.actor(token);d=self.current_definition(actor['tenant']);cfg=d['queries'].get(name)
        if not cfg or actor['role'] not in cfg['roles']:raise Denied('query not permitted')
        with psycopg.connect(self.app_dsn,row_factory=dict_row) as c:
            c.execute('SET TRANSACTION READ ONLY');c.execute("SELECT set_config('experiment.tenant',%s,true)",(actor['tenant'],));return self.query_handlers[cfg['handler']](Context(self,c.cursor(),actor,d),payload)
    def deliver_pending(self,token,supplier_url):
        actor=self.actor(token)
        if actor['role'] not in ('service','owner'):raise Denied('dispatch permission')
        results=[]
        for row in self.query('records',token,{'object':'external_order'}):
            if row['status']!='pending':continue
            self.actor(token)
            request=urllib.request.Request(supplier_url,data=json.dumps({'opkey':actor['tenant']+':'+row['source'],'purchase_id':row['source']}).encode(),headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(request,timeout=3) as response:ack=json.load(response)
            if os.getenv('EXPERIMENT_CRASH_AFTER_SUPPLIER')=='1':os._exit(92)
            results.append(self._record_supplier_ack(token,{'id':row['id'],'receipt':ack['receipt']}))
        return results
    def _record_supplier_ack(self,token,payload):
        actor=self.actor(token)
        with psycopg.connect(self.app_dsn,row_factory=dict_row) as c:
            c.execute("SELECT set_config('experiment.tenant',%s,true)",(actor['tenant'],))
            ctx=Context(self,c.cursor(),actor,self.current_definition(actor['tenant']))
            ctx.connector_verified=True
            return ctx.call('external.confirm',payload)
    def tick(self,token):
        rows=self.query('records',token,{'object':'process'})
        return [self.action('process.advance',token,{'id':r['id']},uuid.uuid4().hex) for r in rows if r['status'] not in ('completed','failed')]

def validate_expression(source):
    tree=ast.parse(source,mode='eval');allowed=(ast.Expression,ast.Dict,ast.List,ast.Tuple,ast.Constant,ast.Name,ast.Load,ast.Compare,ast.Gt,ast.GtE,ast.Lt,ast.LtE,ast.Eq,ast.NotEq,ast.BoolOp,ast.And,ast.Or,ast.UnaryOp,ast.Not,ast.BinOp,ast.Add,ast.Sub,ast.Mult)
    if any(not isinstance(n,allowed) for n in ast.walk(tree)):raise Denied('unsafe expression syntax')
    return tree
def get(ctx,t,id,lock=False):
    r=ctx.cur.execute(f'SELECT * FROM {ident(t)} WHERE id=%s'+(' FOR UPDATE' if lock else ''),(id,)).fetchone()
    if not r:raise Denied('record not visible')
    return r
def insert(ctx,t,values):
    v={'tenant_id':ctx.actor['tenant'],'id':uuid.uuid4().hex,'release_id':ctx.release_id,**values};ctx.cur.execute(f'INSERT INTO {ident(t)} ('+','.join(ident(k) for k in v)+') VALUES('+','.join(['%s']*len(v))+')',tuple(Jsonb(x) if isinstance(x,(dict,list)) else x for x in v.values()));return v
def list_records(ctx,p):
    t=p['object']
    if t not in ctx.definition['objects']:raise Denied('undeclared object')
    columns=list(BASE)+list(ctx.definition['objects'][t].get('columns',{}))
    rows=ctx.cur.execute('SELECT '+','.join(ident(k) for k in columns)+f' FROM {ident(t)} ORDER BY id').fetchall()
    return json.loads(json.dumps(rows,default=str))
def submit(ctx,p):
    amount=int(p['amount']);get(ctx,'project',p['project_id'])
    if amount<=0:raise ValueError('amount')
    r=insert(ctx,'purchase',{'project_id':p['project_id'],'amount':amount,'status':'pending','revision':1,'applicant':ctx.actor['id']})
    a=insert(ctx,'approval',{'purchase_id':r['id'],'status':'pending','revision':1,'applicant':ctx.actor['id']})
    pr=insert(ctx,'process',{'purchase_id':r['id'],'approval_id':a['id'],'status':'waiting','step':'wait_approval','generation':1})
    return {'purchase_id':r['id'],'approval_id':a['id'],'process_id':pr['id'],'status':'pending'}
def approve(ctx,p):
    a=get(ctx,'approval',p['id'],True)
    if a['applicant']==ctx.actor['id']:raise Denied('self approval')
    if a['status']!='pending':return {'status':a['status']}
    ctx.cur.execute("UPDATE approval SET status='approved' WHERE id=%s",(a['id'],));return {'status':'approved'}
def reserve(ctx,p):
    source=get(ctx,'purchase',p['source'],True)
    approval=ctx.cur.execute("SELECT id FROM approval WHERE purchase_id=%s AND revision=%s AND status='approved'",(source['id'],source['revision'])).fetchone()
    if not approval or source['amount']!=p['amount'] or source['project_id']!=p['project_id']:raise Denied('reservation requires matching approval')
    project=get(ctx,'project',p['project_id'],True)
    old=ctx.cur.execute('SELECT * FROM reservation WHERE source=%s',(p['source'],)).fetchone()
    if old:return {'status':'reserved','amount':old['amount']}
    if project['budget']-project['reserved']-project['cost']<p['amount']:return {'status':'budget_unavailable'}
    ctx.cur.execute('UPDATE project SET reserved=reserved+%s WHERE id=%s',(p['amount'],project['id']));insert(ctx,'reservation',{'project_id':project['id'],'source':p['source'],'amount':p['amount']});return {'status':'reserved'}
def authorize(ctx,p):
    r=get(ctx,'purchase',p['id'],True)
    if r['status'] in ('approved','ordered','received'):return {'status':r['status']}
    a=ctx.cur.execute("SELECT * FROM approval WHERE purchase_id=%s AND status='approved' AND revision=%s",(r['id'],r['revision'])).fetchone()
    if not a:raise Denied('valid approval required')
    outcome=ctx.call('budget.reserve',{'project_id':r['project_id'],'source':r['id'],'amount':r['amount']})
    extension=ctx.extension('purchase.authorize',{'amount':r['amount'],'threshold':30000})
    if extension and extension.get('op') not in (None,'noop'):raise Denied('extension requested undeclared capability')
    if p.get('fault_after_reserve'):raise RuntimeError('injected failure after reserve')
    status='approved' if outcome['status']=='reserved' else 'awaiting_budget';ctx.cur.execute('UPDATE purchase SET status=%s WHERE id=%s',(status,r['id']));return {'status':status}
def advance(ctx,p):
    pr=get(ctx,'process',p['id'],True)
    d=ctx.runtime.definition(pr['release_id'],ctx.actor['tenant']);ctx.definition=d;ctx.release_id=pr['release_id']
    steps=d['process']['purchase']['steps']
    for _ in range(12):
        step=steps[pr['step']]
        if step['kind']=='end':
            ctx.cur.execute("UPDATE process SET status='completed' WHERE id=%s",(pr['id'],));return {'status':'completed'}
        if step['kind']=='wait':
            target=get(ctx,step['object'],pr[step['ref']])
            if target[step['field']]!=step['equals']:return {'status':'waiting','step':pr['step']}
            next_step=step['next']
        elif step['kind']=='action':
            out=ctx.call(step['action'],{'id':pr[step['ref']]})
            if out['status'] not in step['success']:return out
            next_step=step['next']
        else:raise ValueError('unknown process step')
        pr['step']=next_step
        ctx.cur.execute('UPDATE process SET step=%s WHERE id=%s',(next_step,pr['id']))
    raise ValueError('process step limit')
def dispatch(ctx,p):
    r=get(ctx,'purchase',p['id'],True)
    if r['status'] not in ('approved','ordering','ordered'):raise Denied('authorized reservation required')
    prior=ctx.cur.execute('SELECT * FROM external_order WHERE source=%s',(r['id'],)).fetchone()
    if not prior:prior=insert(ctx,'external_order',{'source':r['id'],'status':'pending','receipt':None})
    if prior['status']!='confirmed':ctx.cur.execute("UPDATE purchase SET status='ordering' WHERE id=%s",(r['id'],))
    return {'external_id':prior['id'],'status':prior['status']}
def external_confirm(ctx,p):
    if not getattr(ctx,'connector_verified',False):raise Denied('verified connector acknowledgement required')
    order=get(ctx,'external_order',p['id'],True)
    if order['status']=='confirmed':return {'status':'confirmed','receipt':order['receipt']}
    ctx.cur.execute("UPDATE external_order SET status='confirmed',receipt=%s WHERE id=%s",(p['receipt'],order['id']))
    ctx.cur.execute("UPDATE purchase SET status='ordered' WHERE id=%s",(order['source'],));return {'status':'confirmed','receipt':p['receipt']}
def receive(ctx,p):
    r=get(ctx,'purchase',p['id'],True)
    if r['status']=='received':return {'status':'received','deduplicated':True}
    if r['status']!='ordered':raise Denied('order confirmation required')
    project=get(ctx,'project',r['project_id'],True);ctx.cur.execute('UPDATE project SET reserved=reserved-%s,cost=cost+%s WHERE id=%s',(r['amount'],r['amount'],project['id']))
    insert(ctx,'cost_entry',{'source':r['id'],'amount':r['amount']});ctx.cur.execute("UPDATE purchase SET status='received' WHERE id=%s",(r['id'],));return {'status':'received'}
def contract_create(ctx,p):
    r=insert(ctx,'contract',{'amount':int(p['amount']),'status':'draft','assessed':False});return {'id':r['id'],'release_id':r['release_id']}
def contract_assess(ctx,p):
    get(ctx,'contract',p['id'],True);ctx.cur.execute('UPDATE contract SET assessed=true WHERE id=%s',(p['id'],));return {'status':'assessed'}
def contract_sign(ctx,p):
    r=get(ctx,'contract',p['id'],True);d=ctx.runtime.definition(r['release_id'],ctx.actor['tenant']);rule=ctx.extension('contract.sign',{'amount':r['amount'],'threshold':d.get('policy',{}).get('cost_threshold',30000)},d)
    required=rule.get('requires_cost',False) if rule is not None else d.get('policy',{}).get('cost_gate',False) and r['amount']>d['policy'].get('cost_threshold',30000)
    if required and not r['assessed']:raise Denied('cost assessment required')
    ctx.cur.execute("UPDATE contract SET status='signed' WHERE id=%s",(r['id'],));return {'status':'signed','release_id':r['release_id']}
def default_definition():
    objects={
      'project':{'columns':{'name':'text','budget':'bigint','reserved':'bigint','cost':'bigint'}},
      'purchase':{'columns':{'project_id':'text','amount':'bigint','status':'text','revision':'integer','applicant':'text'}},
      'approval':{'columns':{'purchase_id':'text','status':'text','revision':'integer','applicant':'text'}},
      'process':{'columns':{'purchase_id':'text','approval_id':'text','status':'text','step':'text','generation':'integer'}},
      'reservation':{'columns':{'project_id':'text','source':'text','amount':'bigint'}},
      'external_order':{'columns':{'source':'text','status':'text','receipt':'text'}},
      'cost_entry':{'columns':{'source':'text','amount':'bigint'}},
      'contract':{'columns':{'amount':'bigint','status':'text','assessed':'boolean'}}}
    roles={'purchase.submit':['employee','owner','agent'],'approval.approve':['owner'],'purchase.authorize':['owner','service'],'budget.reserve':['owner','service'],'purchase.receive':['owner','delivery'],'purchase.dispatch':['owner','service'],'external.confirm':['service','owner'],'process.advance':['service'],'contract.create':['owner','employee','agent'],'contract.sign':['owner','agent'],'contract.assess':['owner','delivery']}
    return {'release_id':'v1','version':1,'objects':objects,'actions':{n:{'handler':n,'roles':r,'internal':n in ('budget.reserve','external.confirm')} for n,r in roles.items()},'queries':{'records':{'handler':'record.list','roles':['owner','employee','agent','service','delivery']}},'policy':{'cost_gate':False,'cost_threshold':30000,'approval_threshold':50000},'process':{'purchase':{'steps':{'wait_approval':{'kind':'wait','object':'approval','ref':'approval_id','field':'status','equals':'approved','next':'authorize'},'authorize':{'kind':'action','action':'purchase.authorize','ref':'purchase_id','success':['approved','ordered','received','ordering'],'next':'dispatch'},'dispatch':{'kind':'action','action':'purchase.dispatch','ref':'purchase_id','success':['pending','confirmed'],'next':'wait_receipt'},'wait_receipt':{'kind':'wait','object':'purchase','ref':'purchase_id','field':'status','equals':'received','next':'end'},'end':{'kind':'end'}}}},'extensions':{}}
