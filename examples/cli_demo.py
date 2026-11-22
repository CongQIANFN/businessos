"""Persistent walkthrough: all enterprise operations go through the public CLI."""
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

def command(*args):
    result = subprocess.run([str(ROOT/'bos'), *args], capture_output=True, text=True)
    body = json.loads(result.stdout)
    if not body['ok']:
        raise RuntimeError(body['error'])
    return body['result']

with tempfile.TemporaryDirectory(prefix='bos-demo-artifacts-') as tmp:
    def artifact(name, value):
        path = Path(tmp)/name; path.write_text(json.dumps(value)); return str(path)
    def action(name, data, key):
        return command('action', 'run', name, '--input', artifact('input.json', data), '--key', key)
    instance = command('instance', 'init')
    print('1. Persistent local enterprise initialized:', instance['instance'])
    try:
        old = action('contract.create', {'amount': 80000}, 'demo-old')
        command('catalog', 'export', '--output', str(Path(tmp)/'candidate.json'))
        candidate = json.loads((Path(tmp)/'candidate.json').read_text())
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
        change = command('change', 'create', '--candidate', artifact('candidate.json', candidate), '--cases', artifact('cases.json', cases), '--request', 'New contracts above CNY 40000 require cost assessment')
        command('change', 'test', change['id'])
        release = command('change', 'publish', change['id'])
        print('2. Builder validated and published the CNY 40000 rule:', release['release_id'])
        assert action('contract.sign', {'id': old['id']}, 'demo-old-sign')['release_id'] == 'v1'
        high = action('contract.create', {'amount': 40001}, 'demo-high')
        # The rejection was verified independently by the published change cases.
        action('contract.assess', {'id': high['id']}, 'demo-assess')
        action('contract.sign', {'id': high['id']}, 'demo-sign')
        print('3. User signed the old v1 contract and assessed/signed a new contract.')
        change = command('module', 'install', str(ROOT/'templates/expenses.json'), '--cases', str(ROOT/'examples/expense-cases.json'), '--request', 'Install simple expense submission')
        command('change', 'test', change['id']); command('change', 'publish', change['id'])
        command('--identity', 'employee', 'action', 'describe', 'expense.submit')
        expense = command('--identity', 'employee', 'action', 'run', 'expense.submit', '--input', artifact('expense.json', {'description': 'Train ticket', 'amount': 120}), '--key', 'demo-expense')
        assert expense['status'] == 'submitted'
        print('4. User discovered expense.submit and submitted an expense:', expense['id'])
    finally:
        command('instance', 'stop')
        print('Instance stopped; records and definitions remain. Run ./bos instance start to continue.')
