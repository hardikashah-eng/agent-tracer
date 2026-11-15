"""Optional LLM judge for semantic questions the deterministic detectors cannot answer.

Two judgements:
- tool_selection: given the goal, recent reasoning and the available tools, was this tool a
  reasonable choice, and is there a clearly better one?
- result_usage: what fraction of a large tool result did later model output actually use,
  and which fields were unused?

The judge is OFF unless configured. It sends trace excerpts to the configured endpoint, so
prefer an on-prem OpenAI-compatible server (e.g. vLLM) when traces are sensitive. Excerpts are
truncated and passed through a basic secret redactor first. Results are cached by content hash.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .model import SessionTrace, content_hash

SYSTEM_PROMPT = ('You are an evaluation judge for AI agent traces. Answer ONLY with one JSON object that '
                 'matches the requested schema. Be conservative: if evidence is insufficient, say so with low confidence.')

TOOL_SELECTION_SCHEMA = ('{"appropriate": true|false, "better_tool": string|null, '
                         '"reason": string (max 40 words), "confidence": number 0..1}')
RESULT_USAGE_SCHEMA = ('{"used_ratio": number 0..1, "used_fields": [string], "unused_fields": [string], '
                       '"reason": string (max 40 words), "confidence": number 0..1}')

_SECRET_PATTERNS = [
    (re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}'), r'\1[REDACTED]'),
    (re.compile(r'(?i)("?(?:api[_-]?key|token|secret|password|authorization)"?\s*[:=]\s*"?)[^"\s,}]{6,}'), r'\1[REDACTED]'),
    (re.compile(r'\b(?:sk|pk|ghp|gho|xox[abp])[-_][A-Za-z0-9_\-]{12,}\b'), '[REDACTED]'),
    (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'), '[EMAIL]'),
]


def redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def excerpt(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = redact(text)
    return text if len(text) <= limit else text[:limit] + f' …[truncated {len(text) - limit} chars]'


def parse_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?|```$', '', text, flags=re.M).strip()
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('Judge returned no JSON object')
    return json.loads(text[start:end + 1])


@dataclass
class OpenAICompatibleJudge:
    """Works with vLLM, LiteLLM, OpenAI and other /v1/chat/completions servers."""
    base_url: str
    model: str
    api_key: str | None = None
    timeout: float = 60.0
    name: str = 'openai-compatible'

    def complete(self, prompt: str) -> str:
        import httpx
        headers = {'Authorization': f'Bearer {self.api_key}'} if self.api_key else {}
        r = httpx.post(self.base_url.rstrip('/') + '/chat/completions', headers=headers, timeout=self.timeout,
                       json={'model': self.model, 'temperature': 0, 'max_tokens': 400,
                             'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': prompt}]})
        r.raise_for_status()
        return r.json()['choices'][0]['message']['content']


@dataclass
class AnthropicJudge:
    model: str
    api_key: str
    base_url: str = 'https://api.anthropic.com'
    timeout: float = 60.0
    name: str = 'anthropic'

    def complete(self, prompt: str) -> str:
        import httpx
        r = httpx.post(self.base_url.rstrip('/') + '/v1/messages', timeout=self.timeout,
                       headers={'x-api-key': self.api_key, 'anthropic-version': '2023-06-01'},
                       json={'model': self.model, 'max_tokens': 400, 'temperature': 0, 'system': SYSTEM_PROMPT,
                             'messages': [{'role': 'user', 'content': prompt}]})
        r.raise_for_status()
        return ''.join(b.get('text', '') for b in r.json().get('content', []) if b.get('type') == 'text')


@dataclass
class CallableJudge:
    """Wrap any function(prompt) -> str; used for tests and custom in-house judges."""
    fn: Callable[[str], str]
    name: str = 'callable'
    model: str = 'custom'

    def complete(self, prompt: str) -> str:
        return self.fn(prompt)


def judge_from_env(env=os.environ):
    """AGENT_TRACER_JUDGE=openai|anthropic; AGENT_TRACER_JUDGE_MODEL; AGENT_TRACER_JUDGE_URL; AGENT_TRACER_JUDGE_API_KEY."""
    kind = (env.get('AGENT_TRACER_JUDGE') or '').lower()
    model = env.get('AGENT_TRACER_JUDGE_MODEL')
    if kind in ('openai', 'vllm', 'openai-compatible'):
        url = env.get('AGENT_TRACER_JUDGE_URL')
        if not url or not model:
            raise ValueError('AGENT_TRACER_JUDGE_URL and AGENT_TRACER_JUDGE_MODEL are required for an OpenAI-compatible judge')
        return OpenAICompatibleJudge(url, model, env.get('AGENT_TRACER_JUDGE_API_KEY'))
    if kind == 'anthropic':
        key = env.get('AGENT_TRACER_JUDGE_API_KEY') or env.get('ANTHROPIC_API_KEY')
        if not key or not model:
            raise ValueError('AGENT_TRACER_JUDGE_MODEL and an API key are required for the Anthropic judge')
        return AnthropicJudge(model, key, env.get('AGENT_TRACER_JUDGE_URL') or 'https://api.anthropic.com')
    return None


def judge_status(env=os.environ) -> dict:
    try:
        j = judge_from_env(env)
    except ValueError as exc:
        return {'configured': False, 'error': str(exc)}
    return {'configured': bool(j), 'backend': getattr(j, 'name', None), 'model': getattr(j, 'model', None)}


@dataclass
class JudgeOptions:
    max_tool_selection: int = 25
    max_result_usage: int = 15
    result_usage_min_tokens: int = 800
    excerpt_chars: int = 3000
    min_confidence: float = 0.5


@dataclass
class JudgeRun:
    backend: str
    model: str
    calls: int = 0
    cached: int = 0
    errors: list = field(default_factory=list)
    tool_selection: list = field(default_factory=list)
    result_usage: list = field(default_factory=list)


def _tool_menu(catalog: dict, traces: list[SessionTrace]) -> list[dict]:
    names = {c.name for t in traces for c in t.tool_calls} | set((catalog.get('tools') or {}).keys())
    menu = []
    for name in sorted(names):
        entry = (catalog.get('tools') or {}).get(name, {})
        menu.append({'name': name, 'server': entry.get('server'), 'description': entry.get('description', '')[:200]})
    return menu


def _preceding_reasoning(trace: SessionTrace, call) -> str:
    prior = [c.output for c in trace.llm_calls if c.seq < call.seq and c.output is not None]
    return excerpt(prior[-1], 600) if prior else ''


def _later_output(trace: SessionTrace, call, limit: int) -> str:
    later = [c.output for c in trace.llm_calls if c.seq > (call.end_seq or call.seq) and c.output is not None]
    return excerpt(later, limit) if later else ''


def _select_tool_calls(traces: list[SessionTrace], limit: int) -> list:
    """Prioritise failures, then the first call of each tool per session, round-robin across sessions."""
    picked, seen = [], set()
    for want_fail in (True, False):
        for t in traces:
            for c in t.tool_calls:
                key = (t.id, c.name)
                if (not c.ok) == want_fail and key not in seen:
                    seen.add(key)
                    picked.append((t, c))
    return picked[:limit]


def run_judge(judge, traces: list[SessionTrace], catalog: dict | None = None, options: JudgeOptions | None = None,
              cache: dict | None = None) -> JudgeRun:
    catalog = catalog or {}
    options = options or JudgeOptions()
    cache = cache if cache is not None else {}
    run = JudgeRun(backend=getattr(judge, 'name', 'judge'), model=getattr(judge, 'model', 'unknown'))
    menu = json.dumps(_tool_menu(catalog, traces), ensure_ascii=False)

    def ask(prompt):
        key = content_hash([run.backend, run.model, prompt])
        if key in cache:
            run.cached += 1
            return cache[key]
        run.calls += 1
        result = parse_json_object(judge.complete(prompt))
        cache[key] = result
        return result

    for trace, call in _select_tool_calls(traces, options.max_tool_selection):
        prompt = (f'Judge TOOL SELECTION.\nGoal: {excerpt(trace.goal or "unknown", 400)}\n'
                  f'Agent reasoning just before the call: {_preceding_reasoning(trace, call) or "not captured"}\n'
                  f'Available tools: {menu}\nChosen tool: {call.name}\nArguments: {excerpt(call.arguments, 600)}\n'
                  f'Outcome: {"ok" if call.ok else "error: " + str(call.error_class)}; result excerpt: {excerpt(call.result, 400)}\n'
                  f'Respond with JSON: {TOOL_SELECTION_SCHEMA}')
        try:
            verdict = ask(prompt)
            verdict = {'appropriate': bool(verdict.get('appropriate', True)), 'better_tool': verdict.get('better_tool') or None,
                       'reason': str(verdict.get('reason', ''))[:400], 'confidence': float(verdict.get('confidence', 0.5))}
            call.judgement = {**(call.judgement or {}), 'tool_selection': verdict}
            run.tool_selection.append({'session_id': trace.id, 'seq': call.seq, 'tool': call.name, **verdict})
        except Exception as exc:  # judge failures never break the deterministic report
            run.errors.append({'session_id': trace.id, 'seq': call.seq, 'kind': 'tool_selection', 'error': str(exc)[:200]})

    candidates = sorted([(t, c) for t in traces for c in t.tool_calls
                         if c.ok and c.used_ratio is None and c.result_tokens >= options.result_usage_min_tokens],
                        key=lambda x: -x[1].result_tokens)[:options.max_result_usage]
    for trace, call in candidates:
        later = _later_output(trace, call, options.excerpt_chars)
        if not later:
            continue
        prompt = (f'Judge RESULT USAGE. Estimate what fraction of the tool result the agent actually relied on '
                  f'in its later output (semantic use counts, not only verbatim copying).\n'
                  f'Goal: {excerpt(trace.goal or "unknown", 400)}\nTool: {call.name}\n'
                  f'Tool result (~{call.result_tokens} tokens): {excerpt(call.result, options.excerpt_chars)}\n'
                  f'Later agent output: {later}\nRespond with JSON: {RESULT_USAGE_SCHEMA}')
        try:
            verdict = ask(prompt)
            ratio = min(1.0, max(0.0, float(verdict.get('used_ratio', 1.0))))
            conf = float(verdict.get('confidence', 0.5))
            item = {'session_id': trace.id, 'seq': call.seq, 'tool': call.name, 'used_ratio': ratio, 'confidence': conf,
                    'unused_fields': [str(x) for x in verdict.get('unused_fields', [])][:12],
                    'used_fields': [str(x) for x in verdict.get('used_fields', [])][:12],
                    'reason': str(verdict.get('reason', ''))[:400]}
            run.result_usage.append(item)
            call.judgement = {**(call.judgement or {}), 'result_usage': item}
            if conf >= options.min_confidence:
                call.used_ratio = ratio
                call.used_ratio_source = 'llm_judge'
        except Exception as exc:
            run.errors.append({'session_id': trace.id, 'seq': call.seq, 'kind': 'result_usage', 'error': str(exc)[:200]})
    return run


def tool_selection_findings(run: JudgeRun, min_confidence: float = 0.5) -> list[dict]:
    """Group confident 'inappropriate' verdicts per tool into cross-session findings."""
    groups: dict[str, list] = {}
    for v in run.tool_selection:
        if not v['appropriate'] and v['confidence'] >= min_confidence:
            groups.setdefault(v['tool'], []).append(v)
    out = []
    judged = {}
    for v in run.tool_selection:
        judged[v['tool']] = judged.get(v['tool'], 0) + 1
    for tool, items in groups.items():
        better = {}
        for v in items:
            if v['better_tool']:
                better[v['better_tool']] = better.get(v['better_tool'], 0) + 1
        top_better = max(better, key=better.get) if better else None
        out.append({'detector': 'tool_selection', 'dimension': 'efficacy', 'tool': tool, 'session_id': None,
                    'seqs': [], 'severity': 'high' if len(items) / judged[tool] >= 0.5 else 'medium',
                    'detail': f'LLM judge rated {len(items)} of {judged[tool]} sampled {tool} calls as a poor tool choice'
                              + (f'; most often suggested {top_better}.' if top_better else '.'),
                    'metrics': {'flagged': len(items), 'judged': judged[tool], 'better_tool': top_better,
                                'reasons': [v['reason'] for v in items[:3]]},
                    'savings': {'tokens': 0, 'ms': 0.0, 'turns': 0.0}, 'confidence': 'llm_judge',
                    'sessions': sorted({v['session_id'] for v in items}),
                    'evidence_items': [{'session_id': v['session_id'], 'seq': v['seq'], 'reason': v['reason']} for v in items[:5]]})
    return out


def summarize_run(run: JudgeRun) -> dict:
    flagged = [v for v in run.tool_selection if not v['appropriate']]
    return {'backend': run.backend, 'model': run.model, 'judge_calls': run.calls, 'cache_hits': run.cached,
            'errors': run.errors[:20], 'tool_selection_judged': len(run.tool_selection),
            'tool_selection_flagged': len(flagged), 'result_usage_judged': len(run.result_usage),
            'tool_selection': run.tool_selection, 'result_usage': run.result_usage,
            'disclaimer': 'LLM-judge verdicts are model opinions on truncated, redacted excerpts; they are sampled, '
                          'uncalibrated and can be wrong. Treat them as leads, not ground truth.'}
