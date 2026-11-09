# 13 — Progress Tracker

Single place to see what is done, what is in progress and what is next. Update it at the end of each piece of work.
Plan and rationale: [11-roadmap-suggestions.md](./11-roadmap-suggestions.md). Phase 0 details: [12-phase0-implementation.md](./12-phase0-implementation.md).

Legend: ✅ done · 🟡 partly done · ⬜ not started

_Last updated: 2026-11-09_

## 1. Overview

| Phase | Goal | Status | Progress |
|---|---|---|---|
| 0 | Production hardening | ✅ Done | 9 / 9 items |
| 1 | Supervisor + first specialist agents | ⬜ Not started | 0 / 6 |
| 2 | Guardrails and platform in Python | ⬜ Not started | 0 / 5 |
| 3 | Planning agents (forecast, optimizer, bill & tariff) | ⬜ Not started | 0 / 4 |
| 4 | Automation and ambient agents | ⬜ Not started | 0 / 5 |
| 5 | Scale and operate | ⬜ Not started | 0 / 5 |

Stack decision (2026-11-09): everything in **Python / FastAPI / LangGraph**. Go is a future option only, triggered by measurements
([11 §3](./11-roadmap-suggestions.md)).

Test status: **307 tests pass** with Postgres and Redis available. **297 pass and 10 skip** without them. Static check (unused or undefined names): clean.

---

## 2. Phase 0 — Production hardening ✅

Completed 2026-11-09. Nothing is committed to git yet.

| # | Functionality delivered | Key files | Verified by |
|---|---|---|---|
| 0.1 | ✅ **Durable LangGraph checkpoints** in Postgres (own schema, own pool). Memory saver kept for dev and tests. Production refuses to start without Postgres. `/ready` reports checkpointer health | `app/ai/checkpointer.py`, `app/main.py` | Pause, restart, resume on real Postgres |
| 0.2 | ✅ **Confirmation lifecycle** for device actions: 2-minute deadline, atomic single-use claim, replay blocked even if the DB row rolled back, `expired` status, hourly sweep that expires proposals and prunes checkpoints | `actions/service.py`, `actions/maintenance.py`, migration `202611090001` | 10 tests, including a mutation check of the replay guard |
| 0.3 | ✅ **Redis-backed rate limiting**, per user (token) else per IP, falls back to in-process counters if Redis is down | `app/core/rate_limit.py` | Shared counters across two limiters on real Redis; real process returned `401 ×5` then `429` |
| 0.4 | ✅ **Request and trace correlation**: `X-Request-ID` on every request and log line, JSON log option, OTLP tracing (off by default), `traceparent` and request id carried through RabbitMQ to the ingest and device workers | `request_context.py`, `logging_config.py`, `telemetry.py`, workers | 25 tests, including parent/child trace linkage |
| 0.5 | ✅ **Prometheus metrics** at `/metrics` and per worker: ingest messages, effective lag, assistant paths, LLM tokens, grounding results, proposals, WebSocket sessions, process CPU and memory | `app/core/metrics.py` | Real process served `200 text/plain`; gauge tests |
| 0.6 | ✅ **Numeric grounding guard** (`ground` node): every number in an LLM answer must trace to the facts. Modes `enforce`, `observe`, `off`. `meta.grounding` in the API response | `domain/grounding.py`, `graph/nodes.py` | 27 tests across the checker and all three modes |
| 0.7 | ✅ **Evals as validated JSONL**, per-case runner with JSON report, and a **CI workflow** (single migration head, clean migrate, checkpoint setup, evals, tests, compose validation) | `evals/`, `.github/workflows/backend-ci.yml` | 17 tests; 26/26 routing evals pass |
| 0.8 | ✅ **Retrieval upgrade**: vector index and full-text index actually used, results united then ranked, OpenRouter, Google and OpenAI embedders, `reembed` job, safe fallbacks | `knowledge/retrieval.py`, `repository.py`, `reembed.py`, migration `202611090002` | 7 real-Postgres tests, including a regression for the 400-chunk loss |
| 0.9 | ✅ **Hygiene**: committed credentials removed from `.env.example`; compose now wires Postgres checkpoints, Redis limits, metrics and a maintenance loop; requirements pinned | `.env.example`, `docker-compose.yml`, `requirements.txt` | `docker compose config` valid; dry-run install has no conflicts |

### Extra fixes found while building

| Fix | Why |
|---|---|
| ✅ JWT clock-skew leeway (10 s, `JWT_LEEWAY_SECONDS`) | This WSL2 clock steps back about 0.9 s every ~30 s. Tokens were rejected as "issued in the future" in roughly half of full test runs. Four clean runs after the fix |
| ✅ `/metrics` as a plain route | A mounted app redirected `/metrics` to `/metrics/` |
| ✅ Pgvector write path logs failures and uses a savepoint | It silently swallowed every error before |

### Open follow-ups from Phase 0

| Item | Owner | Planned in |
|---|---|---|
| 🟡 Rotate the password that is still in git history | You | Now |
| 🟡 Choose and enable a real embedding model, add its key, run `reembed` | You | Now |
| 🟡 Run the new CI workflow once on GitHub and fix anything that differs from local | You | Now |
| ⬜ Grounding failure falls back without a retry | Agent | Phase 1 |
| ⬜ Device command is published before the DB commit (outbox needed) | Agent | Phase 4 |
| ⬜ Simulator does not send `traceparent` | Agent | Phase 5 |
| ⬜ Vector column is 256 wide; `hnsw.iterative_scan` not set; true BM25/RRF fusion | Agent | Phase 3 |

---

## 3. Phase 1 — Supervisor + first specialists ⬜

Target: 3–4 weeks. Design: [02](./02-agent-catalog.md), [03](./03-langgraph-design.md).

| # | Item | Status | Depends on |
|---|---|---|---|
| 1.1 | Supervisor graph with `Send` fan-out, `join(defer=True)`, synthesizer, output gate | ⬜ | Phase 0 grounding and checkpointer |
| 1.2 | Wrap today's assistant graph as the **Energy Insight** agent | ⬜ | 1.1 |
| 1.3 | Wrap knowledge retrieval as the **Knowledge** agent | ⬜ | 1.1, retrieval from 0.8 |
| 1.4 | Move `actions/` under the **Device Action** agent as an exclusive route | ⬜ | 1.1, lifecycle from 0.2 |
| 1.5 | Trajectory evals and a multi-agent dataset; SSE streaming with status events | ⬜ | 0.7 |
| 1.6 | Thread memory for follow-up questions, plus one regeneration when grounding fails | ⬜ | 0.1, 0.6 |

## 4. Phase 2 — Guardrails and platform in Python ⬜

Target: 2–3 weeks, can overlap Phase 1. Design: [04](./04-guardrails-gates-masking.md).

| # | Item | Status |
|---|---|---|
| 2.1 | Input guard (middleware plus graph node): normalisation, jailbreak fast path, optional classifier for grey-zone inputs | ⬜ |
| 2.2 | PII masking with a Redis vault (`guardrails/pii.py`) and checksum validators | ⬜ |
| 2.3 | Token budgets per user and tier in Redis, enforced in a dependency and debited by graph nodes | ⬜ |
| 2.4 | Policy checks (tool × agent, action × device) as Python modules | ⬜ |
| 2.5 | Prompt and model registry, plus feature flags with a kill switch per agent | ⬜ |

## 5. Phase 3 — Planning agents ⬜

Target: 4–6 weeks. Design: [02 §5](./02-agent-catalog.md).

| # | Item | Status |
|---|---|---|
| 3.1 | **Forecast** agent (ML models, physics fallback from the simulator engines) | ⬜ |
| 3.2 | **Optimizer** agent (schedule solver and battery strategy) | ⬜ |
| 3.3 | **Bill & Tariff** agent with versioned tariff tables and the what-if simulator | ⬜ |
| 3.4 | Long-term memory store and a "What Suryaa remembers" page | ⬜ |

## 6. Phase 4 — Automation and ambient ⬜

Target: 4–6 weeks.

| # | Item | Status |
|---|---|---|
| 4.1 | Rule engine in Python (alerts and approved automations, state transitions only), reusing `domain/alerts.py` | ⬜ |
| 4.2 | **Automation Rule** agent: natural language to a rule DSL, validate, dry-run, user approval | ⬜ |
| 4.3 | Command executor with outbox table, idempotency, state machine, kill switch, audit | ⬜ |
| 4.4 | **Monitor**, **Recommender**, **Reporter** ambient and batch agents; provider batch APIs | ⬜ |
| 4.5 | Fix the publish-before-commit gap (follows from 4.3) | ⬜ |

## 7. Phase 5 — Scale and operate ⬜

Ongoing.

| # | Item | Status |
|---|---|---|
| 5.1 | Horizontal ingest workers with queue-lag autoscaling; batched Timescale writes | ⬜ |
| 5.2 | Several uvicorn workers per pod; scale agent pods on in-flight requests | ⬜ |
| 5.3 | Off-the-shelf gateway (Envoy, Kong or Traefik) for TLS, routing and coarse limits | ⬜ |
| 5.4 | Online judge sampling, canary pipeline, cost dashboards, nightly red-team generator | ⬜ |
| 5.5 | Simulator sends `traceparent` so traces start at the source | ⬜ |

---

## 8. Decision log

| Date | Decision | Why |
|---|---|---|
| 2026-11-08 | Target architecture: supervisor plus specialist agents on LangGraph, with gates, evals and masking | [01](./01-architecture-overview.md)–[10](./10-deployment-scaling.md) |
| 2026-11-09 | Build everything in Python / FastAPI first; Go only if measurements demand it | Fewer moving parts. LLM latency dominates, so Go gains little on the chat path |
| 2026-11-09 | Grounding falls back to the deterministic answer instead of retrying | Keeps Phase 0 small. Retry arrives with the supervisor in Phase 1 |
| 2026-11-09 | JWT leeway of 10 s | Clock skew caused false `401`s, and multi-server skew is normal in production |

## 9. How to update this file

1. When an item starts, change ⬜ to 🟡. When it is finished and tested, change it to ✅ and fill in "Key files" and "Verified by".
2. Update the overview table counts and the test status line.
3. Add any new bug found along the way to the phase's "Extra fixes" list, and any shortcut taken to "Open follow-ups".
4. Add a row to the decision log for any decision that changes the plan.
