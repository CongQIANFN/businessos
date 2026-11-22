"""Black-box acceptance: each operation starts a fresh CLI process."""
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
checks = 0
with tempfile.TemporaryDirectory(prefix='bos-cli-') as tmp:
    home = Path(tmp) / 'instance'
    def run(*args, ok=True, identity='owner'):
        global checks
        proc = subprocess.run([str(ROOT/'bos'), '--home', str(home), '--identity', identity, *args], capture_output=True, text=True)
        result = json.loads(proc.stdout)
        assert result['ok'] is ok, (args, proc.stdout, proc.stderr)
        assert (proc.returncode == 0) is ok
        checks += 1
        return result.get('result') if ok else result['error']
    def file(name, value):
        p = Path(tmp)/name; p.write_text(json.dumps(value)); return str(p)
    def action(name, payload, key, **kw):
        return run('action', 'run', name, '--input', file('input.json', payload), '--key', key, **kw)
    run('instance', 'init')
    try:
        base = run('context', 'show')['release_id']
        run('catalog', 'export', '--output', str(Path(tmp)/'candidate.json'))
        candidate = json.loads((Path(tmp)/'candidate.json').read_text())
        old = action('contract.create', {'amount': 80000}, 'old')['id']
        candidate['policy'].update(cost_gate=True, cost_threshold=40000)
        candidate['extensions']['contract.sign'] = "{'requires_cost': amount > threshold}"
        cases = {'before': [{'name': 'contract.create', 'input': {'amount': 80000}, 'save': 'old'}], 'after': [
            {'name': 'contract.sign', 'input': {'id': '$old.id'}, 'expect': {'status': 'signed'}},
            {'name': 'contract.create', 'input': {'amount': 40000}, 'save': 'equal', 'expect': {}},
            {'name': 'contract.sign', 'input': {'id': '$equal.id'}, 'expect': {'status': 'signed'}},
            {'name': 'contract.create', 'input': {'amount': 40001}, 'save': 'above', 'expect': {}},
            {'name': 'contract.sign', 'input': {'id': '$above.id'}, 'error': 'cost assessment required'},
            {'name': 'contract.assess', 'input': {'id': '$above.id'}, 'expect': {'status': 'assessed'}},
            {'name': 'contract.sign', 'input': {'id': '$above.id'}, 'expect': {'status': 'signed'}}]}
        cp, tp = file('candidate.json', candidate), file('cases.json', cases)
        def draft():
            return run('change', 'create', '--candidate', cp, '--cases', tp, '--request', 'Above CNY 40000 requires assessment')['id']
        change, stale = draft(), draft()
        assert run('change', 'publish', change, ok=False)['code'] == 'Denied'
        run('change', 'diff', change)
        run('change', 'test', change)
        run('change', 'test', stale)
        run('change', 'publish', change, identity='employee', ok=False)
        release = run('change', 'publish', change)['release_id']
        assert release != base
        run('change', 'publish', change)  # retry is idempotent
        assert 'stale' in run('change', 'publish', stale, ok=False)['message']
        action('contract.sign', {'id': old}, 'sign-old')
        equal = action('contract.create', {'amount': 40000}, 'equal')['id']
        action('contract.sign', {'id': equal}, 'sign-equal')
        high = action('contract.create', {'amount': 40001}, 'high')['id']
        assert high == action('contract.create', {'amount': 40001}, 'high')['id']
        action('contract.create', {'amount': 40002}, 'high', ok=False)
        action('contract.sign', {'id': high}, 'sign-high', ok=False)
        action('contract.assess', {'id': high}, 'assess-high')
        action('contract.sign', {'id': high}, 'sign-high')
        action('contract.create', {'amount': '40000'}, 'bad-type', ok=False)
        # Update invalidates validation, and an inverted boundary cannot pass these cases.
        tp = file('current-cases.json', {'after': cases['after'][1:]})
        changed = draft()
        run('change', 'test', changed)
        candidate['extensions']['contract.sign'] = "{'requires_cost': amount >= threshold}"
        cp = file('candidate.json', candidate)
        run('change', 'update', changed, '--candidate', cp, '--cases', tp)
        run('change', 'publish', changed, ok=False)
        run('change', 'test', changed, ok=False)
        # A new module becomes discoverable without modifying the CLI or User skill.
        ep = file('expense-cases.json', {'after': [{'name': 'expense.submit', 'identity': 'employee', 'input': {'description': 'Train ticket', 'amount': 120}, 'expect': {'status': 'submitted'}}]})
        installed = run('module', 'install', str(ROOT/'templates/expenses.json'), '--cases', ep, '--request', 'Install simple expense submission')['id']
        run('change', 'test', installed)
        run('change', 'publish', installed)
        assert 'expense.submit' in run('action', 'list', identity='employee')
        assert 'amount' in run('action', 'describe', 'expense.submit', identity='employee')['input']['properties']
        action('expense.submit', {'description': 'Train ticket', 'amount': 120}, 'expense', identity='employee')
        rows = run('query', 'run', 'records', '--input', file('query.json', {'object': 'expense'}), identity='employee')
        assert len(rows) == 1 and rows[0]['amount'] == 120
        # The exported starter's rejection case is also executable.
        bad = action('expense.submit', {'description': 'Bad', 'amount': 0}, 'bad-expense', identity='employee', ok=False)
        assert 'minimum' in bad['message']
        # Competing publications share an expected baseline; exactly one may activate.
        run('catalog', 'export', '--output', cp)
        race1, race2 = draft(), draft()
        run('change', 'test', race1); run('change', 'test', race2)
        processes = [subprocess.Popen([str(ROOT/'bos'), '--home', str(home), 'change', 'publish', cid], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for cid in (race1, race2)]
        outcomes = [json.loads(p.communicate()[0]) for p in processes]
        assert sum(out['ok'] for out in outcomes) == 1, outcomes
        checks += 1
        run('instance', 'stop'); run('instance', 'start')
        assert run('context', 'show')['release_id'] != base
        rows = run('query', 'run', 'records', '--input', file('query.json', {'object': 'expense'}))
        assert len(rows) == 1
        run('release', 'list'); run('audit', 'list')
        # Ordinary role cannot publish or read Builder drafts.
        run('change', 'inspect', installed, identity='employee', ok=False)
        print(json.dumps({'passed': True, 'cli_checks': checks, 'scenarios': ['rule boundary', 'old record version', 'idempotency', 'stale release', 'validation invalidation', 'new module discovery', 'permissions', 'restart persistence']}))
    finally:
        run('instance', 'stop')
