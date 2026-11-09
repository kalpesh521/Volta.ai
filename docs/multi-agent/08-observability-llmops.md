# 08 — Observability & LLMOps

## 1. One trace across Go and Python

```
trace_id (W3C traceparent, created at volta-edge)
├── edge.request                          (Go)
│   ├── guard.inspect                     (Go)
│   └── agents.chat                       (Python)
│       ├── node.input_gate
│       ├── node.classify   [method=rules]
│       ├── node.plan       [agents=energy_insight,optimizer]
│       ├── agent.energy_insight
│       │   ├── tool.get_live_energy_state  [cache=hit]
│       │   └── llm.answer  [model, prompt_version, tokens_in/out, cost]
│       ├── agent.optimizer
│       ├── node.synthesize  llm.answer
│       └── node.output_gate [grounding=pass]
└── edge.stream
```

| Piece | Choice |
|---|---|
| Spans | OpenTelemetry SDK in Go and Python. Propagate `traceparent` over HTTP/gRPC **and in queue message headers** so ambient chains connect |
| LLM-level traces | LangSmith (LangGraph native) or Langfuse (self-host, OTel-compatible). Both receive **masked** content only |
| Metrics | Prometheus (OTel metrics exporter) → Grafana |
| Logs | Structured JSON, `request_id` + `trace_id` on every line, masked by the shared redactor |
| Errors | Sentry or equivalent, with PII scrubbing |

## 2. Metrics that matter

| Group | Metric (labels) |
|---|---|
| Traffic | `assistant_requests_total{intent, route, tier}` |
| Latency | `assistant_ttft_seconds`, `assistant_latency_seconds{path}`, `agent_latency_seconds{agent}`, `tool_latency_seconds{tool}` |
| LLM | `llm_calls_total{role, model, outcome}`, `llm_tokens_total{role, model, direction}`, `llm_cost_usd_total{model, tenant}`, `llm_fallback_total{from, to}` |
| Routing | `classification_total{method}` (rules share is a KPI), `agents_per_plan` histogram |
| Gates | `gate_decisions_total{gate, decision, reason}` |
| Quality | `output_grounding_fail_total{agent}`, `judge_score{rubric, agent}` (sampled), `feedback_total{thumb}` |
| Safety | `action_decisions_total{decision, reason}`, `injection_detected_total{tier}`, `pii_masked_total{class}` |
| Data | `telemetry_age_seconds` at answer time, `freshness_status_total{status}` |
| Ingest (Go) | `ingest_lag_messages`, `ingest_batch_commit_seconds`, `ingest_invalid_total{reason}` |
| Realtime (Go) | `ws_connections`, `ws_fanout_delay_seconds`, `ws_dropped_slow_consumers_total` |

## 3. SLOs and alerts

| SLO | Objective | Alert |
|---|---|---|
| Chat availability | 99.9 % non-5xx | Burn-rate alert (fast 1 h / slow 6 h) |
| Chat latency | p95 full answer ≤ 4 s | p95 > 5 s for 15 min |
| Groundedness | ≥ 99.5 % pass | Fail rate > 1 % for 30 min → page + auto-flag model version |
| Action safety | 0 unsafe published | Any `executor_policy_reject` after `agent_approved` → page (agent and executor disagree) |
| Cost | Daily spend ≤ budget | 80 % of daily budget by midday → alert. 100 % → auto-switch to cheaper role mapping / deterministic mode |
| Ingest | Lag < 30 s | Lag > 2 min |

## 4. Prompt & model registry

| Item | Practice |
|---|---|
| Prompts | Files in `agents/<name>/prompts/` with a semver + hash (today: `PROMPT_VERSION = "2026-10-03.v3"`). Version logged on every LLM span |
| Model mapping | `role → [primary, fallback]` per environment in config/flags, not code |
| Change process | PR → eval gate ([05](./05-evals.md)) → canary → full |
| Rollback | Flag flip to previous prompt version / model mapping, no deploy needed |
| Experiments | Flag-driven A/B per user hash, compared on online judge + feedback + cost |

## 5. Cost management

- Per-request `TokenUsage` already captured (`include_raw=True`). Aggregate per user, household, tenant, agent, model.
- Show **cost per answered question** and **cost per active household per month** as product KPIs.
- Levers, in order of impact: rules routing share → fact projection → skip synthesizer for single-agent →
  smaller model for low-risk intents → batch APIs for ambient/batch agents → prompt caching when prompts grow.
- Automatic degradation ladder when budget is hit: quality model → cheap model → deterministic answers.

## 6. Operations runbook hooks

| Situation | Switch |
|---|---|
| Provider outage | Fallback model kicks in automatically. If both are down → `ai.llm.enabled=false` (deterministic mode, already supported) |
| Bad prompt release | Roll back prompt version flag |
| Suspected unsafe actions | `ai.actions.enabled=false` (global kill switch) + executor kill switch |
| Cost spike | Lower per-tier budgets, force cheap role mapping |
| Retrieval poisoning | Unpublish doc (status) → retrieval filters it instantly |
