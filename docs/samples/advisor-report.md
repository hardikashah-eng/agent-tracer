# Harness Advisor report

Sessions **20** · model turns **236** · tool calls **276** · input tokens **3,908,667** · cache-read ratio **0.1**

## Latency (all sessions)

| Component | ms | Share |
|---|---:|---:|
| Model | 740,823 | 70% |
| Tool: upstream API | 241,820 | 23% |
| Tool: protocol/gateway | 58,820 | 6% |
| Tool: unsplit | 0 | 0% |
| Harness / idle | 19,320 | 2% |

## Token budget (input, all turns)

| Category | Tokens | Share |
|---|---:|---:|
| System prompt | 5,428 | 0% |
| Tool definitions | 1,926,893 | 49% |
| Conversation | 18,194 | 0% |
| Tool results | 1,958,154 | 50% |
| Other / unclassified | 0 | 0% |

Tool definitions by server: jira 865,592, confluence 577,834, bitbucket 377,373, internal-dev 70,800, local 35,400

Tool results by tool: get_pull_request_diff 1,305,482, list_pull_requests 618,476, get_pull_request_activity 26,718, execute_code 3,360, get_issue 2,103, search_code 1,898, run_tests 140

## Efficiency and efficacy

- Duplicate fetch calls: 11
- Estimated recoverable input tokens (combined, overlapping fixes discounted): 2,786,879 (71%)
- Tool error rate: 0.13
- Repeated identical failing calls: 16
- Model turns per tool call: 0.86

## Recommendations

### 1. Move bitbucket calls to code mode or a CLI  `high` · efficiency

Each page or item costs a full model turn; a script can paginate and fan out in one step and return only the aggregate. Affects 15/20 sessions (15 occurrences). Ownership: **third_party**.

Estimated saving per session: ~97,219 tokens · ~15,396 ms · ~4.85 model turns.

> Third-party server: only levers you control are listed (harness, gateway, facade) plus a vendor request.

- **Harness:** Expose bitbucket through code mode: generate typed stubs from tools/list (MCPorter emit-ts, code2mcp) and let the model write one script that loops in a sandbox.
- **Harness:** For bulk reads, call the official CLI/REST API through the egress gateway with delegated credentials.
- **Gateway:** Publish a virtual tool on the gateway that paginates server-side and returns the aggregate.
- **Vendor request:** Ask the vendor for a batch/aggregate endpoint or larger page sizes, citing call counts.

<details><summary>Evidence</summary>

- session `pr-review-01` seqs [4, 9, 14, 19, 24, 26, 31, 33, 38, 40, 45, 47]: 12 calls to 3 bitbucket tools (get_pull_request_activity, get_pull_request_diff, list_pull_requests) across 8 model turns with mostly distinct arguments (pagination or per-item fan-out).
- session `pr-review-02` seqs [4, 9, 14, 19, 21, 26, 28, 33, 35, 40, 42]: 11 calls to 3 bitbucket tools (get_pull_request_activity, get_pull_request_diff, list_pull_requests) across 7 model turns with mostly distinct arguments (pagination or per-item fan-out).
- session `pr-review-03` seqs [4, 9, 14, 19, 24, 26, 31, 33, 38, 40, 45, 47]: 12 calls to 3 bitbucket tools (get_pull_request_activity, get_pull_request_diff, list_pull_requests) across 8 model turns with mostly distinct arguments (pagination or per-item fan-out).
- session `pr-review-04` seqs [4, 9, 14, 19, 21, 26, 28, 33, 35, 40, 42]: 11 calls to 3 bitbucket tools (get_pull_request_activity, get_pull_request_diff, list_pull_requests) across 7 model turns with mostly distinct arguments (pagination or per-item fan-out).
- session `pr-review-05` seqs [4, 9, 14, 19, 24, 26, 31, 33, 38, 40, 45, 47]: 12 calls to 3 bitbucket tools (get_pull_request_activity, get_pull_request_diff, list_pull_requests) across 8 model turns with mostly distinct arguments (pagination or per-item fan-out).

</details>

### 2. Defer or list-only unused jira tools  `medium` · efficiency

These schemas are paid for on every model turn but the tools are rarely called. Affects 20/20 sessions (1 occurrence). Ownership: **third_party**.

Estimated saving per session: ~37,170 tokens.

> Third-party server: only levers you control are listed (harness, gateway, facade) plus a vendor request.

- **Harness:** Mark the unused jira tools deferred (tool search / defer_loading) so schemas load only when searched.
- **Harness:** Or remove them from this agent's allowlist and move the workflow into a skill that loads on demand.
- **Gateway:** Expose a curated virtual jira server that omits these tools for this agent.

<details><summary>Evidence</summary>

- cross-session: 6 jira tool schemas add ~3150 tokens to every turn but were called in at most 0% of sessions: jira_create_issue, jira_search_jql, jira_transition_issue, jira_add_worklog, jira_link_issues, jira_get_sprint.

</details>

### 3. Defer or list-only unused confluence tools  `medium` · efficiency

These schemas are paid for on every model turn but the tools are rarely called. Affects 20/20 sessions (1 occurrence). Ownership: **third_party**.

Estimated saving per session: ~28,910 tokens.

> Third-party server: only levers you control are listed (harness, gateway, facade) plus a vendor request.

- **Harness:** Mark the unused confluence tools deferred (tool search / defer_loading) so schemas load only when searched.
- **Harness:** Or remove them from this agent's allowlist and move the workflow into a skill that loads on demand.
- **Gateway:** Expose a curated virtual confluence server that omits these tools for this agent.

<details><summary>Evidence</summary>

- cross-session: 4 confluence tool schemas add ~2450 tokens to every turn but were called in at most 0% of sessions: confluence_create_page, confluence_update_page, confluence_search, confluence_get_page.

</details>

### 4. Stop retry loops on get_issue  `high` · efficacy

Identical failing calls were repeated without changing arguments. Affects 8/20 sessions (8 occurrences). Ownership: **third_party**.

Estimated saving per session: ~10,706 tokens · ~2,906 ms · ~0.8 model turns.

> Third-party server: only levers you control are listed (harness, gateway, facade) plus a vendor request.

- **Harness:** Add a retry guard: block an identical (tool, arguments) call after a failure and surface the error to the model.
- **Harness:** Add a circuit breaker for persistent upstream errors.
- **Gateway:** Validate required arguments at the gateway and reject early with a clear message.
- **Vendor request:** Report the unhelpful error message with examples.

<details><summary>Evidence</summary>

- session `pr-review-02` seqs [47, 52, 57]: get_issue failed 3 times with identical arguments and the same error (validation_error).
- session `pr-review-03` seqs [52, 57, 62]: get_issue failed 3 times with identical arguments and the same error (validation_error).
- session `pr-review-05` seqs [52, 57, 62]: get_issue failed 3 times with identical arguments and the same error (validation_error).
- session `pr-review-06` seqs [47, 52, 57]: get_issue failed 3 times with identical arguments and the same error (validation_error).
- session `trial-1-variant` seqs [9, 14, 19]: get_issue failed 3 times with identical arguments and the same error (validation_error).

</details>

### 5. Memoize search_code reads within a session  `medium` · efficiency

The same read with the same arguments was repeated, re-adding identical data to context. Affects 11/20 sessions (11 occurrences). Ownership: **first_party**.

Estimated saving per session: ~8,529 tokens · ~1,918 ms · ~0.55 model turns.

- **Harness:** Cache read-only results per session keyed by tool + arguments; return the cached copy or a reference.
- **Harness:** Keep earlier results reachable (avoid compacting them away) so the model does not re-fetch.
- **Server owner:** Mark the tool as read-only/idempotent so clients can cache safely.
- **Gateway:** Enable a short-TTL response cache for search_code on the gateway.

<details><summary>Evidence</summary>

- session `pr-review-02` seqs [62, 67]: 2 identical successful calls to search_code returned the same result.
- session `pr-review-04` seqs [57, 62]: 2 identical successful calls to search_code returned the same result.
- session `pr-review-06` seqs [62, 67]: 2 identical successful calls to search_code returned the same result.
- session `pr-review-08` seqs [57, 62]: 2 identical successful calls to search_code returned the same result.
- session `pr-review-10` seqs [57, 62]: 2 identical successful calls to search_code returned the same result.

</details>

### 6. Shrink get_pull_request_diff results before they reach the model  `medium` · efficiency

Large results are paid for on this and every later turn, yet most of the content is never used. Affects 15/20 sessions (60 occurrences). Ownership: **third_party**.

Estimated saving per session: ~7,158 tokens.

> Third-party server: only levers you control are listed (harness, gateway, facade) plus a vendor request.

- **Harness:** Post-process get_pull_request_diff results: project only needed fields, truncate, or summarise before adding to context.
- **Harness:** Store the full result as a file/resource and give the model a handle plus a short preview.
- **Harness:** If the tool already supports field selection or compact views (e.g. responseFields, view=compact), add that to the agent's instructions or a skill.
- **Gateway:** Apply a response transform (field projection / size cap) on the gateway route for get_pull_request_diff.
- **Vendor request:** Request field selection or a compact response mode, citing measured unused share.

<details><summary>Evidence</summary>

- session `pr-review-01` seqs [24, 25]: get_pull_request_diff returned ~2983 tokens; only ~1% of its distinctive content reappears later.
- session `pr-review-01` seqs [31, 32]: get_pull_request_diff returned ~2983 tokens; only ~1% of its distinctive content reappears later.
- session `pr-review-01` seqs [38, 39]: get_pull_request_diff returned ~2983 tokens; only ~1% of its distinctive content reappears later.
- session `pr-review-01` seqs [45, 46]: get_pull_request_diff returned ~2983 tokens; only ~1% of its distinctive content reappears later.
- session `pr-review-02` seqs [19, 20]: get_pull_request_diff returned ~2983 tokens; only ~1% of its distinctive content reappears later.

</details>

### 7. Defer or list-only unused bitbucket tools  `medium` · efficiency

These schemas are paid for on every model turn but the tools are rarely called. Affects 20/20 sessions (1 occurrence). Ownership: **third_party**.

Estimated saving per session: ~5,310 tokens.

> Third-party server: only levers you control are listed (harness, gateway, facade) plus a vendor request.

- **Harness:** Mark the unused bitbucket tools deferred (tool search / defer_loading) so schemas load only when searched.
- **Harness:** Or remove them from this agent's allowlist and move the workflow into a skill that loads on demand.
- **Gateway:** Expose a curated virtual bitbucket server that omits these tools for this agent.

<details><summary>Evidence</summary>

- cross-session: 1 bitbucket tool schemas add ~450 tokens to every turn but were called in at most 0% of sessions: create_pull_request_comment.

</details>

### 8. Fix get_issue schema and descriptions  `high` · efficacy

A high share of calls fail, usually from mis-specified arguments rather than upstream outages. Affects 20/20 sessions (1 occurrence). Ownership: **third_party**.

Estimated saving per session: ~1,067 ms · ~1.8 model turns.

> Third-party server: only levers you control are listed (harness, gateway, facade) plus a vendor request.

- **Harness:** Validate arguments against the schema before calling and give the model a corrective message.
- **Harness:** Add usage examples for get_issue in a skill or system prompt.
- **Gateway:** Track error classes per tool and alert on spikes.
- **Vendor request:** Share failing argument patterns and ask for clearer schema/docs.

<details><summary>Evidence</summary>

- cross-session: get_issue failed on 75% of 48 calls across sessions.

</details>

### 9. Shrink list_pull_requests results before they reach the model  `medium` · efficiency

Large results are paid for on this and every later turn, yet most of the content is never used. Affects 15/20 sessions (52 occurrences). Ownership: **third_party**.

Estimated saving per session: ~2,249 tokens.

> Third-party server: only levers you control are listed (harness, gateway, facade) plus a vendor request.

- **Harness:** Post-process list_pull_requests results: project only needed fields, truncate, or summarise before adding to context.
- **Harness:** Store the full result as a file/resource and give the model a handle plus a short preview.
- **Harness:** If the tool already supports field selection or compact views (e.g. responseFields, view=compact), add that to the agent's instructions or a skill.
- **Gateway:** Apply a response transform (field projection / size cap) on the gateway route for list_pull_requests.
- **Vendor request:** Request field selection or a compact response mode, citing measured unused share.

<details><summary>Evidence</summary>

- session `pr-review-01` seqs [4, 5]: list_pull_requests returned ~1082 tokens; only ~4% of its distinctive content reappears later.
- session `pr-review-01` seqs [9, 10]: list_pull_requests returned ~1082 tokens; only ~4% of its distinctive content reappears later.
- session `pr-review-01` seqs [14, 15]: list_pull_requests returned ~1082 tokens; only ~4% of its distinctive content reappears later.
- session `pr-review-01` seqs [19, 20]: list_pull_requests returned ~1082 tokens; only ~4% of its distinctive content reappears later.
- session `pr-review-02` seqs [4, 5]: list_pull_requests returned ~1082 tokens; only ~4% of its distinctive content reappears later.

</details>

### 10. Stabilise the cached prompt prefix  `medium` · efficiency

Low cache-read ratio while the system/tool prefix changes between turns. Affects 1/20 sessions (1 occurrence). Ownership: **harness**.

Estimated saving per session: ~2,579 tokens.

- **Harness:** Make tool order and system prompt deterministic; move volatile content (time, IDs) after the cache breakpoint.
- **Harness:** Use deferred tools so discovered tools are appended rather than changing the prefix.

<details><summary>Evidence</summary>

- session `pr-review-03` seqs [2, 7, 12, 17, 22, 29, 36, 43, 50, 55, 60, 65]: Only 10% of input tokens were cache reads; the system/tool prefix changed 4 times.

</details>

### 11. Cut transport overhead on the bitbucket server  `low` · latency

Most of the tool time is spent outside the upstream API (MCP client/server, gateway, serialization). Affects 15/20 sessions (15 occurrences). Ownership: **third_party**.

Estimated saving per session: ~1,565 ms.

> Third-party server: only levers you control are listed (harness, gateway, facade) plus a vendor request.

- **Harness:** Reuse warm MCP sessions/connections; use stdio for local servers.
- **Harness:** For hot paths, call the API directly (CLI/REST) through the gateway.
- **Gateway:** Remove redundant gateway hops and check per-request auth/introspection latency.

<details><summary>Evidence</summary>

- session `pr-review-01` seqs [4, 9, 14, 19, 24, 26, 31, 33, 38, 40, 45, 47]: bitbucket: median 260 ms per call (40% of tool time) is spent outside the upstream API.
- session `pr-review-02` seqs [4, 9, 14, 19, 21, 26, 28, 33, 35, 40, 42]: bitbucket: median 260 ms per call (39% of tool time) is spent outside the upstream API.
- session `pr-review-03` seqs [4, 9, 14, 19, 24, 26, 31, 33, 38, 40, 45, 47]: bitbucket: median 260 ms per call (40% of tool time) is spent outside the upstream API.
- session `pr-review-04` seqs [4, 9, 14, 19, 21, 26, 28, 33, 35, 40, 42]: bitbucket: median 260 ms per call (43% of tool time) is spent outside the upstream API.
- session `pr-review-05` seqs [4, 9, 14, 19, 24, 26, 31, 33, 38, 40, 45, 47]: bitbucket: median 260 ms per call (36% of tool time) is spent outside the upstream API.

</details>

### 12. Cut transport overhead on the jira server  `low` · latency

Most of the tool time is spent outside the upstream API (MCP client/server, gateway, serialization). Affects 20/20 sessions (20 occurrences). Ownership: **third_party**.

Estimated saving per session: ~437 ms.

> Third-party server: only levers you control are listed (harness, gateway, facade) plus a vendor request.

- **Harness:** Reuse warm MCP sessions/connections; use stdio for local servers.
- **Harness:** For hot paths, call the API directly (CLI/REST) through the gateway.
- **Gateway:** Remove redundant gateway hops and check per-request auth/introspection latency.

<details><summary>Evidence</summary>

- session `pr-review-01` seqs [52, 57]: jira: median 260 ms per call (44% of tool time) is spent outside the upstream API.
- session `pr-review-02` seqs [47, 52, 57]: jira: median 260 ms per call (42% of tool time) is spent outside the upstream API.
- session `pr-review-03` seqs [52, 57, 62]: jira: median 260 ms per call (43% of tool time) is spent outside the upstream API.
- session `pr-review-04` seqs [47, 52]: jira: median 260 ms per call (38% of tool time) is spent outside the upstream API.
- session `pr-review-05` seqs [52, 57, 62]: jira: median 260 ms per call (44% of tool time) is spent outside the upstream API.

</details>

## Verification plans (top recommendations)

### Verify: Move bitbucket calls to code mode or a CLI

- **Experiment:** `exp-chatty_tool-654d4302`
- **Hypothesis:** Applying 'Expose bitbucket through code mode: generate typed stubs from tools/list (MCPorter emit-ts, code2mcp) and let the model write one script that loops in a sandbox.' lowers mean input_tokens per task by about 97,219 (baseline 239,961) without reducing task success by more than 5%.
- **Change (variant arm):** Expose bitbucket through code mode: generate typed stubs from tools/list (MCPorter emit-ts, code2mcp) and let the model write one script that loops in a sandbox.
- **Primary metric:** input_tokens (baseline mean 239,961, SD 11,151, expected −97,219 per task)
- **Design:** 5 task pairs, paired by task, same model and inputs, randomised arm order; sizing: paired normal approximation using between-session SD as a conservative proxy for paired-difference SD
- **Guardrails:** success: variant success rate ≥ baseline − 5%; wall_ms: no increase > 10%
- **Tag sessions with:** `{"experiment_id": "exp-chatty_tool-654d4302", "arm": "baseline | variant", "task_id": "<one of design.tasks>", "recommendation_id": "chatty_tool:bitbucket"}`
- **Tasks to start with:** pr-review-01, pr-review-02, pr-review-03, pr-review-04, pr-review-05
- **Result: CONFIRMED** — mean change -171,790 (95% CI -176,569 to -167,010, -74%) over 5 pairs. The 95% interval of the paired change is entirely below zero and success did not drop.

### Verify: Defer or list-only unused jira tools

- **Experiment:** `exp-dead_schema-d9e17250`
- **Hypothesis:** Applying 'Mark the unused jira tools deferred (tool search / defer_loading) so schemas load only when searched.' lowers mean input_tokens per task by about 37,170 (baseline 195,433) without reducing task success by more than 5%.
- **Change (variant arm):** Mark the unused jira tools deferred (tool search / defer_loading) so schemas load only when searched.
- **Primary metric:** input_tokens (baseline mean 195,433, SD 79,733, expected −37,170 per task)
- **Design:** 37 task pairs, paired by task, same model and inputs, randomised arm order; sizing: paired normal approximation using between-session SD as a conservative proxy for paired-difference SD
- **Guardrails:** success: variant success rate ≥ baseline − 5%; wall_ms: no increase > 10%
- **Tag sessions with:** `{"experiment_id": "exp-dead_schema-d9e17250", "arm": "baseline | variant", "task_id": "<one of design.tasks>", "recommendation_id": "dead_schema:jira"}`
- **Tasks to start with:** pr-review-01, pr-review-02, pr-review-03, pr-review-04, pr-review-05, pr-review-06, pr-review-07, pr-review-08

### Verify: Defer or list-only unused confluence tools

- **Experiment:** `exp-dead_schema-240ac84d`
- **Hypothesis:** Applying 'Mark the unused confluence tools deferred (tool search / defer_loading) so schemas load only when searched.' lowers mean input_tokens per task by about 28,910 (baseline 195,433) without reducing task success by more than 5%.
- **Change (variant arm):** Mark the unused confluence tools deferred (tool search / defer_loading) so schemas load only when searched.
- **Primary metric:** input_tokens (baseline mean 195,433, SD 79,733, expected −28,910 per task)
- **Design:** 60 task pairs, paired by task, same model and inputs, randomised arm order; sizing: paired normal approximation using between-session SD as a conservative proxy for paired-difference SD
- **Guardrails:** success: variant success rate ≥ baseline − 5%; wall_ms: no increase > 10%
- **Tag sessions with:** `{"experiment_id": "exp-dead_schema-240ac84d", "arm": "baseline | variant", "task_id": "<one of design.tasks>", "recommendation_id": "dead_schema:confluence"}`
- **Tasks to start with:** pr-review-01, pr-review-02, pr-review-03, pr-review-04, pr-review-05, pr-review-06, pr-review-07, pr-review-08

## Savings ledger

Cumulative tokens saved: **98,179** · per session: **24,545** · Realized 1

| Applied | Recommendation | Status | Expected tok/session | Realized tok/session | Sessions before → after |
|---|---|---|---:|---:|---|
| 2026-09-21 | Stop retry loops on get_issue | Realized | 24,544 | 24,545 | 6 → 4 |

> Before/after comparison over observed sessions; workload mix, model or prompt changes can confound it. Use verification plans for causal confirmation.

## Tool scorecard

| Tool | Server | Transport | Ownership | Calls | Sessions | Errors | Dupes | Avg result tok | Avg ms | Def tok | Flags |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| get_issue | jira | mcp | third_party | 48 | 20 | 75% | 0% | 3 | 589 | 520 | failure_loop, high_error_rate |
| get_pull_request_diff | bitbucket | mcp | third_party | 60 | 15 | 0% | 0% | 2,983 | 735 | 380 | large_low_use_result |
| list_pull_requests | bitbucket | mcp | third_party | 52 | 15 | 0% | 0% | 1,082 | 639 | 410 | large_low_use_result |
| search_code | internal-dev | mcp | first_party | 31 | 20 | 0% | 36% | 26 | 378 | 300 | duplicate_fetch |
| get_pull_request_activity | bitbucket | mcp | third_party | 60 | 15 | 0% | 0% | 61 | 600 | 360 |  |
| run_tests | local | cli | first_party | 20 | 20 | 0% | 0% | 7 | 6,541 | 150 |  |
| execute_code | bitbucket | code | unknown | 5 | 5 | 0% | 0% | 105 | 3,294 | – |  |
| confluence_create_page | confluence | mcp | third_party | 0 | 0 | – | – | 0 | – | 720 |  |
| confluence_get_page | confluence | mcp | third_party | 0 | 0 | – | – | 0 | – | 480 |  |
| confluence_search | confluence | mcp | third_party | 0 | 0 | – | – | 0 | – | 560 |  |
| confluence_update_page | confluence | mcp | third_party | 0 | 0 | – | – | 0 | – | 690 |  |
| create_pull_request_comment | bitbucket | mcp | third_party | 0 | 0 | – | – | 0 | – | 450 |  |
| jira_add_worklog | jira | mcp | third_party | 0 | 0 | – | – | 0 | – | 470 |  |
| jira_create_issue | jira | mcp | third_party | 0 | 0 | – | – | 0 | – | 640 |  |
| jira_get_sprint | jira | mcp | third_party | 0 | 0 | – | – | 0 | – | 420 |  |
| jira_link_issues | jira | mcp | third_party | 0 | 0 | – | – | 0 | – | 430 |  |
| jira_search_jql | jira | mcp | third_party | 0 | 0 | – | – | 0 | – | 610 |  |
| jira_transition_issue | jira | mcp | third_party | 0 | 0 | – | – | 0 | – | 580 |  |

## Limitations

- Findings are heuristics from captured events, not confirmed root causes; confidence is not a calibrated probability.
- Token composition is estimated (chars/4) and apportioned onto reported usage when available.
- Usage of tool results is inferred from lexical overlap unless the harness reports used_ratio.
- Savings are estimates; validate top recommendations with paired trials before adopting them.
- Missing instrumentation hides problems; an empty report does not certify an efficient harness.
