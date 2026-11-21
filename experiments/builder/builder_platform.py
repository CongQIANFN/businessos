"""Experimental catalog/draft/validate/publish path over procurement.Runtime.

This module prepares and checks release drafts for the local experiment.
"""
import ast
import copy
import hashlib
import json
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class GateRejected(Exception):
    pass


class BuilderPlatform:
    def __init__(self, runtime, output, tenant):
        self.runtime = runtime
        self.tenant = tenant
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)

    def catalog(self):
        """Read the actual published business catalog; no hardcoded UI snapshot."""
        current = self.runtime.current_definition(self.tenant)
        self.write('catalog.json', current)
        return current

    def write(self, name, value):
        (self.output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))

    def draft(self, request, candidate, name):
        base = self.catalog()
        draft = {'name': name, 'request': request, 'base_hash': digest(base),
                 'candidate': copy.deepcopy(candidate), 'status': 'draft',
                 'checks': []}
        self.write(name + '.draft.json', draft)
        return draft

    def validate(self, draft):
        from runtime import validate_expression
        candidate = draft['candidate']
        current = self.runtime.current_definition(self.tenant)
        checks = []
        def check(name, condition, detail=''):
            checks.append({'name': name, 'passed': bool(condition), 'detail': detail})
        check('base_release_is_current', draft['base_hash'] == digest(current))
        for name, obj in current['objects'].items():
            new = candidate.get('objects', {}).get(name, {}).get('columns', {})
            check('preserve_schema:' + name, all(new.get(k) == v for k, v in obj['columns'].items()))
        check('reuse_project_identity', candidate.get('modules', {}).get('procurement', {}).get('project_object', 'project') == 'project')
        if 'procurement' in candidate.get('modules', {}):
            check('reuse_project_budget_action', candidate['actions'].get('budget.reserve') == current['actions'].get('budget.reserve'))
            check('no_duplicate_project_object', not any(k in candidate['objects'] for k in ['procurement_project', 'project_copy']))
        for name, code in candidate.get('extensions', {}).items():
            try:
                validate_expression(code)
                syntax_ok = True
            except Exception:
                syntax_ok = False
            check('restricted_extension_syntax:' + name, syntax_ok)
            if name == 'contract.sign' and syntax_ok:
                # Independent requirement oracle: reject inverted or >= boundaries.
                for amount, expected in [(0, False), (29999, False), (30000, False), (30001, True), (80000, True)]:
                    try:
                        actual = eval(compile(ast.parse(code, mode='eval'), '<generated>', 'eval'), {'__builtins__': {}}, {'amount': amount, 'threshold': candidate['policy']['cost_threshold']})
                        ok = type(actual) is dict and actual.get('requires_cost') is expected
                    except Exception:
                        ok = False
                    check(f'cost_gate_semantics:{amount}', ok)
        old_roles = {k: set(v['roles']) for k, v in current['actions'].items()}
        check('no_implicit_permission_expansion', all(set(v['roles']) <= old_roles.get(k, set(v['roles'])) for k, v in candidate['actions'].items()))
        draft['checks'] = checks
        draft['validated_hash'] = digest(candidate)
        draft['status'] = 'validated' if all(c['passed'] for c in checks) else 'rejected'
        self.write(draft['name'] + '.validation.json', draft)
        return draft['status'] == 'validated'

    def publish(self, draft, token):
        if self.runtime.actor(token)['tenant'] != self.tenant:
            raise GateRejected('publisher belongs to a different enterprise')
        if draft['status'] != 'validated' or draft.get('validated_hash') != digest(draft['candidate']):
            raise GateRejected('candidate was not validated or changed after validation')
        if draft['base_hash'] != digest(self.runtime.current_definition(self.tenant)):
            raise GateRejected('stale base release')
        # Authorization is enforced again by the shared Runtime, not client status.
        release = self.runtime.publish(draft['candidate'], token)
        draft['status'] = 'published'
        draft['published_release'] = release
        self.write(draft['name'] + '.published.json', draft)
        return release
