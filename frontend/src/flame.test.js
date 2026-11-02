import test from 'node:test';
import assert from 'node:assert/strict';
import {buildSpans} from './flame.js';
const e=(seq,ts,kind,name,call_id,payload={},parent_call_id)=>({seq,ts,kind,name,call_id,payload,parent_call_id});
test('nested calls retain actual time and overlapping siblings get separate lanes',()=>{
 const {spans}=buildSpans([e(1,0,'agent.start','a','a'),e(2,1,'tool.before','search','b',{},'a'),e(3,2,'tool.before','search','c',{},'a'),e(4,3,'tool.after','search','c'),e(5,4,'tool.after','search','b'),e(6,5,'agent.completed','a','a')]);
 assert.equal(spans[1].depth,1);assert.notEqual(spans[1].row,spans[2].row);assert.equal(spans[1].duration,3);assert.equal(spans[2].duration,1);
});
test('ambiguous legacy completions remain unpaired',()=>{const {spans,markers}=buildSpans([e(1,0,'tool.before','x'),e(2,1,'tool.before','x'),e(3,2,'tool.after','x')]);assert.ok(spans.every(s=>s.open));assert.equal(markers.length,1)});
test('reported metrics, error and incomplete data are distinct',()=>{const {spans}=buildSpans([e(1,0,'llm.before','x','x'),e(2,1,'llm.error','x','x',{tokens:{input:3,output:2},cost_usd:.1}),e(3,2,'tool.before','y','y'),e(4,3,'context.snapshot','z')]);assert.equal(spans[0].tokens,5);assert.equal(spans[0].cost,.1);assert.equal(spans[0].error,true);assert.equal(spans[1].duration,1);assert.equal(spans[1].open,true)});
test('empty, invalid timestamps and cyclic parents do not break layout',()=>{assert.equal(buildSpans([]).rows,0);const d=buildSpans([e(1,NaN,'tool.before','bad'),e(2,1,'tool.before','a','a',{},'b'),e(3,2,'tool.before','b','b',{},'a')]);assert.equal(d.spans.length,2);assert.ok(d.spans.every(s=>s.invalidParent))});
