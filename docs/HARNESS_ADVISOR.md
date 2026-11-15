# Harness Advisor (v1.1) — design

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

- No mandatory LLM. Every detector is deterministic and reproducible. The LLM
  judge (section 4) is an opt-in layer for semantic questions.
- No automatic remediation. The advisor recommends, plans verification and
  tracks results; humans change harnesses.
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

## 4. Optional LLM judge

The deterministic detectors cannot answer two semantic questions:

- Was this the right tool for the goal?
- Did the model actually use a result that it paraphrased rather than copied?

The judge answers both on a sample of calls:

| Judgement | What is sent | Effect |
|---|---|---|
| `tool_selection` | Goal, the reasoning just before the call, the tool menu (names, servers, catalog descriptions), the chosen tool, its arguments and the outcome | Confident "poor choice" verdicts are grouped per tool into a `tool_selection` recommendation, often naming the better tool |
| `result_usage` | Large tool results (≥ 800 tokens) plus later model output | A confident `used_ratio` replaces the lexical estimate, so `large_low_use_result` findings become semantic (`ratio_source: llm_judge`) |

- **Off by default.** Enable it with environment variables:
  - `AGENT_TRACER_JUDGE=openai` (any OpenAI-compatible server, e.g. **on-prem vLLM**, LiteLLM) or `anthropic`
  - `AGENT_TRACER_JUDGE_MODEL`
  - `AGENT_TRACER_JUDGE_URL` (e.g. `http://gpu-node:8000/v1`)
  - `AGENT_TRACER_JUDGE_API_KEY`
- **Data handling:** excerpts are truncated and passed through a secret/email redactor before they are sent. For sensitive traces, point the judge at an in-network model.
- **Cost control:** at most 25 tool-selection and 15 result-usage judgements per run (configurable via `JudgeOptions`). Verdicts are cached by content hash, so re-analysis is cheap.
- **Failure isolation:** judge errors are recorded in `report.judge.errors` and never break the deterministic report.
- **Status:** verdicts are labelled `llm_judge` and are uncalibrated opinions. Treat them as leads.

## 5. Dashboard panel (React UI)

The **Harness Advisor** panel sits under *Potential root causes* in the main debugger. Its scope selector chooses all captured runs or the current run, and an optional LLM-judge toggle is enabled only when a judge is configured. It shows:

- Summary cards: sessions, turns, tool calls, input tokens, estimated recoverable share, tool error rate, cache-read ratio, and ledger tokens saved.
- **Stacked budget charts** of input tokens by category and of latency by component.
- A **session drill-down**:
  - per-turn stacked input-composition columns
  - a **latency waterfall** of every model and tool call, with tool bars split into upstream API time and protocol/gateway overhead, and failures outlined
- **Recommendations** with levers by owner, evidence, an inline **verification plan** (and its result once tagged sessions exist), and **Mark applied**, which records the change in the ledger.
- A sortable **tool scorecard**, the **savings ledger** table, and the judge summary.

The view logic lives in `frontend/src/advisorView.js` and is unit-tested with `npm test`.

## 6. Recommendation verification (paired-trial plans)

For the top N recommendations (default 3), the advisor generates a plan:

- **Hypothesis**, the **variant arm** (the highest-leverage action you control), and the **control** (unchanged harness).
- **Primary metric**, chosen by detector:
  - `input_tokens` for schema/result/loop waste
  - `uncached_input_tokens` for cache issues
  - `wall_ms` for latency
  - `tool_failures` for efficacy
- The baseline mean and SD from affected sessions, the expected delta, and **pairs required**. Sizing uses a paired normal approximation with α = 0.05 and power 0.8, clamped to 5–200 pairs.
- **Guardrails**: success-rate non-inferiority (−5 pp) and no >10% regression in a secondary metric.
- **Tasks** that showed the problem, and the **metadata to tag** each run with: `experiment_id`, `arm`, `task_id`, `recommendation_id`.

To run a plan:

1. Replay each task once per arm, with the same model and inputs, in randomised order.
2. Tag both sessions.
3. Evaluate. Evaluation pairs sessions by `task_id` and computes the paired mean change with a t-interval, plus success non-inferiority.

| Verdict | Meaning |
|---|---|
| `confirmed` | The 95% interval is entirely below zero and success did not drop |
| `regressed` | The metric rose, or success dropped beyond the margin |
| `inconclusive` | The interval includes zero |
| `insufficient_data` | Fewer than 2 complete pairs |

When tagged sessions are part of the analysed set, the plan's `result` is filled in automatically. Plan IDs are stable per recommendation, so the tags keep matching as new sessions arrive.

## 7. Savings ledger

**Mark applied** (or `POST /api/advisor/ledger`, or `--apply` in the CLI) records:

- the recommendation and its expected savings
- when the change took effect (`applied_at`)
- an optional **scope**: session metadata filters such as `agent=pr-review`
- a note
- a **baseline snapshot** built from in-scope sessions that started before `applied_at`: the recommendation's measured waste, how often the finding appeared, input tokens, turns, failures, latency and success rate

Each evaluation compares that baseline with in-scope sessions that started **after** `applied_at` (experiment-tagged sessions are excluded):

| Status | Rule |
|---|---|
| `awaiting_data` | Fewer than 3 sessions after the change |
| `realized` | Finding share at least halved and measured waste at least halved |
| `partial` | Some waste or primary-metric improvement |
| `no_effect` | No measurable change |
| `regressed` | Success fell by more than 10 pp, or the primary metric rose by more than 10% |

The ledger reports realized tokens saved per session, cumulative tokens saved, and the share of the expected saving. Entries live in an `advisor_ledger` table in the same SQLite file as sessions (or a JSON file for the CLI). The ledger is observational before/after evidence: workload mix, model or prompt changes can confound it, so pair important changes with a verification plan.

## Interfaces

- **Python:** `from backend.advisor import analyze_sessions, AdvisorConfig, JudgeOptions`, plus
  `backend.advisor.verify` (`verification_plans`, `evaluate_plan`) and `backend.advisor.ledger`
  (`make_entry`, `evaluate_ledger`, `SQLiteLedger`, `JSONLedger`).
- **API:**
  - `GET /api/advisor?session_id=…&judge=true&plans=3` (all captured sessions when no IDs) and
    `GET /api/sessions/{id}/advisor?judge=true`. Reports include `verification_plans`, `ledger` and `judge`.
  - `GET /api/advisor/judge`: is a judge configured?
  - `GET /api/advisor/verification?top=3` and `POST /api/advisor/verification/evaluate` with `{"plan": …}`
  - `GET /api/advisor/ledger`, `POST /api/advisor/ledger` with
    `{"recommendation_id", "applied_at"?, "scope"?, "note"?, "plan_id"?}`, and `DELETE /api/advisor/ledger/{id}`
- **CLI** (run `python -m examples.advisor_demo` first to generate the synthetic sessions and ledger):

```bash
python -m backend.advisor examples/advisor/sessions/*.json \
    --catalog examples/advisor/catalog.json --ledger examples/advisor/ledger.json --format html -o advisor-report.html
python -m backend.advisor --db data/agent-tracer.sqlite3 --format md --plans 5
python -m backend.advisor otel-export.json --otel --format json
# Judge (on-prem vLLM example)
AGENT_TRACER_JUDGE=openai AGENT_TRACER_JUDGE_URL=http://gpu-node:8000/v1 AGENT_TRACER_JUDGE_MODEL=qwen2.5-32b-instruct \
    python -m backend.advisor sessions/*.json --judge --format md
# Ledger and verification
python -m backend.advisor sessions/*.json --ledger ledger.json --apply chatty_tool:bitbucket --scope agent=pr-review \
    --note "Code mode for Bitbucket reads"
python -m backend.advisor sessions/*.json --format json -o report.json   # plans are in report.verification_plans
python -m backend.advisor sessions/*.json --evaluate-plan plan.json
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

1. Execute verification plans automatically through the replay harness, where tools can be recorded.
2. Calibrate judge verdicts against a small human-labelled set, and report agreement.
3. Push ledger realizations into the A/B harness and alert when a realized saving regresses.
