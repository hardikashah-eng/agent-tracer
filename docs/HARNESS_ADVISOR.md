# Harness Advisor (v1.0) — design

The Harness Advisor reads many recorded agent sessions and tells the harness
owner **where time and tokens go, what is wasted, what keeps failing, and what to
change**. Example recommendations include "move `list_pull_requests` to code
mode", "defer the 38 unused Jira tool schemas", and "add a retry guard for
`update_issue`".

It complements the existing per-session views (flame chart, context efficiency,
diagnosis). Those views explain **one run**. The advisor aggregates **many
runs** and turns repeated patterns into ranked, owner-aware recommendations.

```
captured sessions ──► 1. decompose ──► 2. detect ──► 3. recommend ──► report
(tracer events or      latency +       efficiency +   ranked actions,   JSON / Markdown /
 OpenTelemetry spans)  token budget    efficacy       per lever owner   HTML / API
```

## Goals and non-goals

**Goals**

- Break down latency into model time, upstream API time, protocol/gateway
  overhead, and unattributed harness time.
- Break down the prompt token budget per model turn into system prompt, tool
  definitions (per MCP server), conversation history, tool results (per tool),
  and cache reads.
- Measure **efficiency**: the same data fetched again, large results the model
  barely uses, schemas that are paid for but never called, unstable cache
  prefixes.
- Measure **efficacy**: the same failing call repeated, tools with high error
  rates, and work done serially that could have run in parallel.
- Produce **actionable recommendations** with estimated per-session savings,
  evidence references, and the party who can act on each one.

**Non-goals (v1)**

- No LLM judge. Every detector is deterministic and reproducible. An LLM judge
  for semantic questions, such as "was this the wrong tool?", is a later,
  optional layer.
- No automatic remediation. The advisor recommends; humans change harnesses.
- No billing accuracy. Reported provider usage is preferred. Estimates use
  `chars / 4` and are always labelled `estimated`.

## Inputs

1. **Agent Tracer sessions.** These are the existing event schema (`tool.before`,
   `tool.after`/`tool.error`, `llm.before`/`llm.after` with `tokens`/`usage`,
   and `context.snapshot`), read from the live tracer, the SQLite store, or
   exported JSON.
2. **OpenTelemetry spans.** OTLP/JSON exports that follow the GenAI and MCP
   semantic conventions (`gen_ai.usage.*`, `gen_ai.tool.name`,
   `mcp.method.name`, `tools/call {name}` span names). These are converted into
   the same event model, so Langfuse, Phoenix, agentgateway, Kong, and FastMCP
   traces can be analysed without re-instrumenting.
3. **Tool catalog (optional, recommended).** This is a JSON inventory of every
   tool the agent can see:

```json
{"tools": {
  "list_pull_requests": {"server": "bitbucket", "ownership": "third_party",
                         "transport": "mcp", "kind": "read", "definition_tokens": 410},
  "pr_summary":         {"server": "internal-dev", "ownership": "first_party",
                         "transport": "mcp", "kind": "read", "definition_tokens": 220}
}}
```

The catalog supplies three things traces often lack:

- **definition cost**: tokens each schema adds to every turn
- **ownership**: whether you can change the server
- **read/write kind**: whether it is safe to cache or batch

### Optional instrumentation fields that sharpen results

| Event | Field | Used for |
|---|---|---|
| `tool.before` | `transport` (`mcp`/`cli`/`code`/`rest`/`function`), `server` | Per-transport breakdown and recommendations |
| `tool.after` | `upstream_ms` | Separates protocol/gateway overhead from upstream API time |
| `tool.after` | `result_tokens`, `used_ratio` | Exact result size and measured usage instead of estimates |
| `llm.after` | `usage.cache_read_input_tokens` (or `cached`) | Cache efficiency |
| `context.snapshot` | `tool_definitions: [{name, server, estimated_tokens}]`, `tool_definitions_hash` | Per-server schema budget and cache-prefix stability |

## 1. Decomposition

For every session the advisor computes:

**Latency** (milliseconds, from event timestamps or explicit `duration_ms`):

- `llm_ms`: total model call time.
- `tool_upstream_ms`: time inside the upstream API, when `upstream_ms` is reported.
- `tool_overhead_ms`: tool time minus upstream time. This is the MCP
  client/server, gateway, and serialization cost.
- `tool_unattributed_ms`: tool time where no upstream split was reported.
- `harness_ms`: wall-clock time not covered by model or tool calls. This
  includes planning code, queueing, and human pauses.

**Token budget**: one entry per model turn. When a `context.snapshot` exists,
its estimated composition is apportioned onto the reported `input` tokens:

| Category | Source |
|---|---|
| `system` | system/developer messages |
| `tool_definitions` | `tool_definitions` in the snapshot, else catalog `definition_tokens` |
| `conversation` | user/assistant history |
| `tool_results` | tool messages, attributed to their `source` tool |
| `cached` | reported cache-read tokens. This is a subset of input, not added on top |

## 2. Detectors

Every finding carries `session_id`, the evidence `seqs`, measured numbers, and
an estimate of what fixing it would save.

| Detector | Signal | Dimension |
|---|---|---|
| `duplicate_fetch` | The same read tool with the same arguments succeeds 2+ times in a session | Efficiency |
| `large_low_use_result` | Result ≥ threshold tokens, with a low share of its distinctive terms reappearing in later model output (or a reported `used_ratio`) | Efficiency |
| `dead_schema` | A tool definition is paid for on every turn but called in few sessions | Efficiency |
| `cache_prefix_unstable` | Low cache-read ratio while the system prompt or tool-definition hash changes between turns | Efficiency |
| `context_dominance` | Conversation history takes most of the late-turn input and keeps growing | Efficiency |
| `chatty_tool` | Many calls to one read tool per session, with mostly distinct arguments, each needing its own model turn (pagination or per-item fan-out) | Efficiency + latency |
| `serial_independent_calls` | Consecutive tool calls in separate turns where the later arguments do not depend on the earlier result | Latency |
| `protocol_overhead` | Tool time is mostly protocol/gateway overhead rather than upstream work | Latency |
| `failure_loop` | The same tool, arguments, and error class repeat 2+ times | Efficacy |
| `high_error_rate` | A tool fails on ≥ 20% of calls across sessions | Efficacy |

Thresholds are configurable (`AdvisorConfig`). Defaults are deliberately
conservative: repeated calls can be intentional, and low term overlap does not
prove a result was useless.

## 3. Recommendations

Findings are grouped by `(detector, tool)` across sessions. Each group becomes
one recommendation with:

- **What to do**, as concrete actions.
- **Why**, with sessions affected, occurrences, and evidence links.
- **Estimated savings per session**: tokens, milliseconds, model turns.
  These are averaged over all analysed sessions, so rare problems rank lower.
- **Priority**: savings weighted by how many sessions are affected.
- **Lever owner**: who can act on it. See the next section.

| Detector | Typical recommendation |
|---|---|
| `chatty_tool` | Use **code mode** or a **CLI** for this tool: one script paginates and fans out, and returns only the aggregate |
| `dead_schema` | **Defer** the schema (tool search) or expose it **list-only**, or move the workflow into a skill |
| `large_low_use_result` | **Field projection** or a summary mode. Return a handle and let the model read slices |
| `duplicate_fetch` | **Memoize** read results per session (harness or gateway cache) |
| `failure_loop` | **Retry guard** (stop after one identical failure) and an actionable error message |
| `high_error_rate` | Fix the tool **description and schema**, and add argument validation before the call |
| `serial_independent_calls` | Enable **parallel tool calls** or add a batch tool |
| `protocol_overhead` | Co-locate server and API, reuse sessions, remove a gateway hop, or use direct CLI/REST |
| `cache_prefix_unstable` | Make tool order and system prompt deterministic, and put volatile content after the cache breakpoint |
| `context_dominance` | **Compaction / prompt compression**, or offload sub-tasks to a subagent |

## Third-party MCP servers you cannot change

Much of the tooling an agent uses is preconfigured and owned by someone else.
Examples are a vendor's Bitbucket or Jira MCP server or a SaaS connector. You
cannot add a composite tool, change a default page size, or fix an error
message. The advisor therefore tags every action with a **lever owner** and,
for `ownership: third_party` tools, recommends only levers you control:

| Lever owner | What you can do without touching the server |
|---|---|
| `harness_owner` (your agent loop/client) | Allowlist or defer the server's tools (tool search, list-only). Post-process results before they enter context (truncate, project fields, summarise). Memoize reads per session. Add a retry guard or circuit breaker. Run independent calls in parallel. Add a skill that teaches efficient use of the existing tool, e.g. "pass `responseFields`" or "use `view: compact`". |
| `harness_owner` (code mode over the unchanged server) | Generate typed stubs from the server's `tools/list` (e.g. MCPorter `emit-ts`, code2mcp), then let the model write one script that calls the **unchanged** server many times inside a sandbox. Only the aggregate reaches the model. |
| `gateway_operator` (Kong, agentgateway, ContextForge) | Publish a **virtual server** that exposes a curated subset. Transform or project responses. Cache idempotent reads. Enforce per-tool ACLs and rate limits. All of this happens in front of the vendor server. |
| `harness_owner` (facade server) | Write a thin first-party MCP server that wraps the vendor's tools into composite, task-shaped tools, e.g. `pr_review_summary(repo)`. You own the facade, so server-side fixes become possible again. |
| `harness_owner` (alternate path) | Where licence and policy allow, call the vendor's official CLI or REST API through the egress gateway with the same delegated credentials (see the credential-broker pattern) for bulk reads. |
| `vendor_request` | File a feature request backed by the advisor's evidence, e.g. "`list_pull_requests` returns ~3.1k tokens per page; 82% of fields unused across 40 sessions; please add field selection". |

Recommendations for `first_party` tools also include `server_owner` levers:
composite tools, default field projection, smaller default pages, and better
error messages.

Tools missing from the catalog are treated as `unknown`. They get both
`harness_owner` and `server_owner` levers, with a note to classify them.

## Interfaces

- **Python:** `from backend.advisor import analyze_sessions, AdvisorConfig`
- **API:** `GET /api/sessions/{id}/advisor` (one session) and
  `GET /api/advisor?session_id=a&session_id=b` (several; all captured sessions
  when omitted).
- **CLI** (run `python -m examples.advisor_demo` first to generate the synthetic sessions):

```bash
python -m backend.advisor examples/advisor/sessions/*.json \
    --catalog examples/advisor/catalog.json --format html -o advisor-report.html
python -m backend.advisor --db data/agent-tracer.sqlite3 --format md
python -m backend.advisor otel-export.json --otel --format json
```

## Limitations

- All findings are heuristic. They are not confirmed root causes, and
  confidence values are not calibrated probabilities.
- Savings are estimates from captured evidence. Validate a recommendation with
  paired trials (`/api/trials`) or an A/B run before adopting it broadly.
- Usage detection (`large_low_use_result`) uses lexical overlap. Paraphrased or
  numeric use can be missed. Prefer a harness-reported `used_ratio`.
- Missing instrumentation hides problems. No findings does not mean the harness
  is efficient.
- Traces may contain sensitive prompts and tool output. Apply the existing
  local-only security guidance.

## Roadmap

1. An optional LLM judge for tool-selection quality and semantic result usage.
2. A dashboard panel in the React UI, with a stacked budget chart, latency
   waterfall, and tool scorecard.
3. Recommendation verification: auto-generate paired-trial plans for the top
   recommendations.
4. A savings ledger that tracks applied recommendations against later sessions.
