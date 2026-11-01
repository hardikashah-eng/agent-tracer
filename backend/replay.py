"""Fail-closed recorded tool replay. Never invokes a real tool or a model."""
import copy
import json
import argparse
from pathlib import Path


def records(events):
    before = {}
    seen = set()
    output = []
    for e in events:
        cid = e.get('call_id') or e.get('payload', {}).get('call_id')
        if e['kind'] == 'tool.before':
            if not cid or cid in seen:
                raise ValueError('Replay requires unique call IDs on tool.before')
            if 'arguments' not in e['payload']:
                raise ValueError('Replay requires captured arguments')
            seen.add(cid)
            before[cid] = e
        elif e['kind'] in ('tool.after', 'tool.error'):
            start = before.pop(cid, None)
            if not start or start['name'] != e['name']:
                raise ValueError('Unmatched tool response; supply explicit call IDs for concurrent tools')
            output.append(dict(call_id=cid, name=e['name'], arguments=start['payload']['arguments'],
                               response=e['payload'], error=e['kind']=='tool.error', source_seqs=[start['seq'], e['seq']]))
    if before:
        raise ValueError('Incomplete tool calls cannot be replayed')
    if not output:
        raise ValueError('No complete recorded tool calls')
    return output


class RecordedTools:
    def __init__(self, calls, replacements=None):
        self.calls = copy.deepcopy(calls)
        self.replacements = replacements or {}
        known = {c['call_id'] for c in calls}
        if not set(self.replacements) <= known:
            raise ValueError('Unknown intervention call ID')
        self.position = 0

    def call(self, name, arguments):
        if self.position >= len(self.calls):
            raise ValueError('Unrecorded tool request blocked')
        c = self.calls[self.position]
        if name != c['name'] or arguments != c['arguments']:
            raise ValueError('Tool name/arguments differ from recording; live fallback prohibited')
        self.position += 1
        return copy.deepcopy(self.replacements.get(c['call_id'], c['response']))


def evaluate(fixture, replacements=None):
    if fixture.get('schema_version') != 1:
        raise ValueError('Unsupported fixture schema')
    tools = RecordedTools(fixture['calls'], replacements)
    assertions = fixture.get('assertions', [])
    if not assertions:
        raise ValueError('At least one explicit outcome assertion is required')
    by_id = {}
    for c in fixture['calls']:
        by_id[c['call_id']] = tools.call(c['name'], c['arguments'])
    results = []
    for a in assertions:
        if a['call_id'] not in by_id:
            raise ValueError('Assertion references unknown call ID')
        actual = by_id[a['call_id']]
        # Exact object-key path; no eval, expressions, shell, URLs, or plugin execution.
        for key in a.get('path', []):
            if not isinstance(actual, dict) or key not in actual:
                actual = {'missing_path': a['path']}; break
            actual = actual[key]
        results.append(dict(assertion=a, actual=actual, passed=actual == a['equals']))
    return dict(mode='recorded-contract-replay', passed=all(r['passed'] for r in results),
                assertions=results, calls_replayed=tools.position, external_calls=0,
                warning='Checks recorded response contracts only; does not rerun agent decisions or prove causality.')


def export_fixture(session, assertions):
    fixture = dict(schema_version=1, source_session_id=session.id, metadata=session.metadata,
                   calls=records(session.events), assertions=assertions)
    evaluate(fixture)
    return fixture


def intervention(fixture, call_id, replacement):
    baseline = evaluate(fixture)
    variant = evaluate(fixture, {call_id: replacement})
    return dict(baseline=baseline, variant=variant, intervention_call_id=call_id,
                outcome_changed=baseline['passed'] != variant['passed'],
                conclusion='Contract sensitivity only; no causal claim about the original agent.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('fixture', type=Path)
    args = parser.parse_args()
    report = evaluate(json.loads(args.fixture.read_text()))
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report['passed'] else 1)

if __name__ == '__main__':
    main()


def replay_workflow(fixture, workflow, replacements=None):
    """Run trusted application logic with recorded tools injected.

    The application must use the supplied tool interface for ALL effects.
    This is dependency isolation, not an OS sandbox for arbitrary Python code.
    Not exposed as an HTTP code-execution endpoint.
    """
    tools = RecordedTools(fixture['calls'], replacements)
    result = workflow(tools)
    if tools.position != len(tools.calls):
        raise ValueError('Workflow did not consume the complete recording')
    return result


def controlled_trials(baseline, variant, count=10):
    """Execute caller-owned independent paired trials; callbacks must return bool.

    Callbacks own environment reset, tool safety, randomization and real outcomes.
    Never accepts code through HTTP. Use recorded adapters for side-effect isolation.
    """
    from .trials import summarize
    if not 2 <= count <= 1000:
        raise ValueError('Trial count must be 2–1000')
    pairs = []
    for i in range(count):
        # Alternate order to reduce a simple ordering bias; callers must reset state.
        if i % 2:
            b = variant(i); a = baseline(i)
        else:
            a = baseline(i); b = variant(i)
        pairs.append({'trial_id': str(i), 'baseline': a, 'variant': b})
    return {'pairs': pairs, 'report': summarize(pairs)}
