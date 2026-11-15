import test from 'node:test';
import assert from 'node:assert/strict';
import {segments, turnColumns, waterfall, ticks, sortTools, groupLevers, planFor, ledgerFor, fmt, fmtMs, CATEGORIES, LATENCY} from './advisorView.js';

test('segments drop zero/invalid values and sum to 100%', () => {
  const {total, parts} = segments({system: 10, tool_definitions: 30, conversation: 0, tool_results: NaN, other: 60}, CATEGORIES);
  assert.equal(total, 100);
  assert.deepEqual(parts.map(p => p.key), ['system', 'tool_definitions', 'other']);
  assert.equal(parts.reduce((n, p) => n + p.pct, 0), 100);
  assert.equal(segments(null, LATENCY).parts.length, 0);
});

test('turn columns scale to the largest turn and split by composition', () => {
  const cols = turnColumns([{seq: 1, input_tokens: 50, composition: {system: 50}}, {seq: 2, input_tokens: 100, composition: {system: 25, tool_results: 75}}]);
  assert.equal(cols[0].height, 50);
  assert.equal(cols[1].height, 100);
  assert.equal(cols[1].parts.find(p => p.key === 'tool_results').height, 75);
  assert.deepEqual(turnColumns([]), []);
});

test('waterfall positions calls, splits tool upstream vs overhead, and tolerates missing times', () => {
  const {span, rows} = waterfall([
    {kind: 'llm', seq: 1, name: 'm', start_ms: 0, duration_ms: 1000},
    {kind: 'tool', seq: 2, name: 't', start_ms: 1000, duration_ms: 1000, upstream_ms: 750, ok: false},
    {kind: 'tool', seq: 3, name: 'u', start_ms: null, duration_ms: 500},
  ]);
  assert.equal(span, 2500);
  assert.equal(rows[1].left, 40);
  assert.equal(rows[1].upstreamPct, 75);
  assert.equal(rows[1].overheadMs, 250);
  assert.equal(rows[1].ok, false);
  assert.equal(rows[2].left, 80);
  assert.equal(rows[2].upstreamPct, null);
  assert.equal(waterfall([], 10).rows.length, 0);
  assert.equal(waterfall(Array.from({length: 5}, (_, i) => ({kind: 'llm', seq: i, start_ms: i, duration_ms: 1})), 3).truncated, true);
  assert.equal(ticks(2000, 4).at(-1).label, '2.0 s');
});

test('scorecard sorting handles nulls, strings and flag counts', () => {
  const tools = [{tool: 'a', calls: 3, error_rate: null, flags: ['x']}, {tool: 'b', calls: 9, error_rate: 0.5, flags: []}, {tool: 'c', calls: 3, error_rate: 0.1, flags: ['x', 'y']}];
  assert.deepEqual(sortTools(tools, 'calls').map(t => t.tool), ['b', 'a', 'c']);
  assert.deepEqual(sortTools(tools, 'error_rate').map(t => t.tool), ['b', 'c', 'a']);
  assert.deepEqual(sortTools(tools, 'flags').map(t => t.tool), ['c', 'a', 'b']);
  assert.deepEqual(sortTools(tools, 'tool', 'asc').map(t => t.tool), ['a', 'b', 'c']);
});

test('levers group by owner; plans and ledger entries resolve by recommendation id', () => {
  const g = groupLevers([{owner: 'harness_owner', action: 'a'}, {owner: 'vendor_request', action: 'b'}, {owner: 'harness_owner', action: 'c'}]);
  assert.deepEqual(g.map(x => [x.label, x.actions.length]), [['Harness', 2], ['Vendor request', 1]]);
  const report = {verification_plans: [{recommendation_id: 'r1', id: 'p'}], ledger: {entries: [{recommendation_id: 'r1'}, {recommendation_id: 'r2'}]}};
  assert.equal(planFor(report, 'r1').id, 'p');
  assert.equal(planFor(report, 'zz'), null);
  assert.equal(ledgerFor(report, 'r1').length, 1);
  assert.deepEqual(ledgerFor({}, 'r1'), []);
});

test('formatting', () => {
  assert.equal(fmt(1234567), '1.2M');
  assert.equal(fmt(45678), '46k');
  assert.equal(fmt(undefined), '–');
  assert.equal(fmtMs(250), '250 ms');
  assert.equal(fmtMs(25000), '25 s');
});
