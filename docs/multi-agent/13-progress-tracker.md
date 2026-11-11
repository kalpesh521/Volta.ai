# 13 — Progress Tracker

Single place to see what is done, what is in progress and what is next. Update it at the end of each piece of work.
Plan and rationale: [11-roadmap-suggestions.md](./11-roadmap-suggestions.md). Phase 0 details: [12-phase0-implementation.md](./12-phase0-implementation.md). Phase 1 details: [14-phase1-implementation.md](./14-phase1-implementation.md).

Legend: ✅ done · 🟡 partly done · ⬜ not started

_Last updated: 2026-11-11_

## 1. Overview

| Phase | Goal | Status | Progress |
|---|---|---|---|
| 0 | Production hardening | ✅ Done | 9 / 9 items |
| 1 | Supervisor + first specialist agents | 🟡 Done except the `actions/` relocation | 5 / 6 |
| 2 | Guardrails and platform in Python | ⬜ Not started | 0 / 5 |
| 3 | Planning agents (forecast, optimizer, bill & tariff) | ⬜ Not started | 0 / 4 |
| 4 | Automation and ambient agents | ⬜ Not started | 0 / 5 |
| 5 | Scale and operate | ⬜ Not started | 0 / 5 |

Stack decision (2026-11-09): everything in **Python / FastAPI / LangGraph**. Go is a future option only, triggered by measurements
([11 §3](./11-roadmap-suggestions.md)).

Test status: **474 tests pass** with Postgres and Redis available. **462 pass and 12 skip** without them. Static check (unused or undefined names): clean. Trajectory evals: **42 / 42 cases** (56 turns).

---

## 2. Phase 0 — Production hardening ✅

Completed 2026-11-09. Committed in `8b72e13`.

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
| ✅ Grounding failure falls back without a retry (done for v2 in Phase 1; v1 still has no retry) | Agent | Phase 1 |
| ⬜ Device command is published before the DB commit (outbox needed) | Agent | Phase 4 |
| ⬜ Simulator does not send `traceparent` | Agent | Phase 5 |
| ⬜ Vector column is 256 wide; `hnsw.iterative_scan` not set; true BM25/RRF fusion | Agent | Phase 3 |

---

## 3. Phase 1 — Supervisor + first specialists 🟡

Done 2026-11-11, except the relocation noted in 1.4. Design: [02](./02-agent-catalog.md), [03](./03-langgraph-design.md). Details: [14](./14-phase1-implementation.md).

| # | Functionality delivered | Key files | Verified by |
|---|---|---|---|
| 1.1 | ✅ **Supervisor graph**: classify → plan → parallel dispatch with `Send` → `join(defer=True)` → one synthesizer → output gate → finalize. Per-agent deadlines and failure isolation; a whole-request deadline; per-turn state reset | `agents_platform/orchestration/` | 28 graph tests incl. a timed parallelism check; mutation checks on the reset and the output gate |
| 1.2 | ✅ **Energy Insight agent**: the v1 tools, analytics, freshness and projection, returning facts. Never calls the model | `agents/energy_insight.py`, `agents/insight_engine.py` | Agent tests; v1/v2 parity over HTTP on 5 question types |
| 1.3 | ✅ **Knowledge agent**: Phase 0 retrieval, passages as citations, deterministic "not in your documents" on a gap | `agents/knowledge.py` | Gap parity with v1; a compound question with a real uploaded manual |
| 1.4 | 🟡 **Device Action agent** as an exclusive route over the unchanged `ActionService`: proposes, never sends. **`assistant/actions/` itself has not been moved** | `agents/device_action.py` | Propose → confirm → queued over HTTP; nothing published on ask; refused when control is off; mutation check on exclusivity |
| 1.5 | ✅ **Trajectory evals** (42 cases, 56 turns: route, agents, node path, model calls) wired into CI. ✅ **SSE streaming** with `status` and `final` events. Tokens are not streamed (by design, see 14 §3.8) | `agents_platform/evals/`, `api/router.py`, `backend-ci.yml` | 55 eval tests; mutation checks drop the gate or break exclusivity and fail 29 and 8 cases |
| 1.6 | ✅ **Thread memory** (follow-ups inherit the topic; server-issued, owner-checked conversation ids; retention sweep) and ✅ **one regeneration** when grounding fails | `memory.py`, `conversations.py`, `maintenance.py`, migration `202611110001` | Real Postgres: a follow-up works after a restart; retention deletes checkpoints; mutation check on the regeneration cap |

### Extra work found while building

| Item | Why |
|---|---|
| ✅ Shared v1/v2 logic extracted (`domain/finalize.py`, `graph/routing.py`, `fallback_note`) | One implementation of data-time stamping, stale-data caution, citations, confidence and routing. All 179 v1 assistant tests passed unchanged |
| ✅ Streaming opens its own database scope | With this FastAPI version `get_db` commits and closes before a streamed body runs, so a proposal written during a stream would be lost |
| ✅ A name-a-new-subject rule for follow-ups | "and the grid?" would otherwise silently continue the battery topic |
| ✅ Document search uses the follow-up-aware question | Retrieval on a bare "why?" finds nothing |

### Open follow-ups from Phase 1

| Item | Owner | Planned in |
|---|---|---|
| ⬜ Move `assistant/actions/` under `agents_platform/agents/device_action/` | Agent | Phase 2, when v1 retires |
| ⬜ Prove v2 at parity with a **real model** (L3 judge, same questions on both endpoints), then switch the frontend | Agent + You | Phase 2 |
| ⬜ Grow the multi-agent dataset toward 80 compound cases | Agent | Phase 2 |
| ⬜ Mixed "explain and do": explain first, then offer the action | Agent | Phase 2 |
| ⬜ Serialise concurrent questions in one conversation | Agent | Phase 2 or 5 |
| ⬜ Conversation history endpoint, partial results on the whole-request deadline | Agent | When the frontend needs them |
| ⬜ Measure checkpoint write volume per question | Agent | Before Phase 5 |
| 🟡 Run the CI workflow on GitHub once more (it now also runs the trajectory evals) | You | Now |

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
| 2026-11-11 | Agents return facts and one synthesizer writes the answer, instead of each agent answering | A compound question costs 1 model call as in v1 (not 2–3), and the output gate, regeneration and fallback live in one place |
| 2026-11-11 | New `agents_platform/` package and `/v2/assistant` routes; v1 stays live until parity is proven with a real model | Replacing v1 needs evidence, not a fake-model match. Doc 03 §12 already names this layout |
| 2026-11-11 | Device Action is an adapter over the unchanged `ActionService`; the package move waits | Moving it while v1 imports it adds risk and no behaviour. The Phase 0 confirmation lifecycle stays exactly as tested |
| 2026-11-11 | Memory keeps earlier questions, not answers; the server issues conversation ids | A follow-up is answered from live data, so an old number is never repeated as current. Ids cannot be invented or probed |
| 2026-11-11 | No token streaming in Phase 1 | The answer is a structured object the output gate can replace; showing words then pulling them back is worse than progress events plus one checked answer |

## 9. How to update this file

1. When an item starts, change ⬜ to 🟡. When it is finished and tested, change it to ✅ and fill in "Key files" and "Verified by".
2. Update the overview table counts and the test status line.
3. Add any new bug found along the way to the phase's "Extra fixes" list, and any shortcut taken to "Open follow-ups".
4. Add a row to the decision log for any decision that changes the plan.
