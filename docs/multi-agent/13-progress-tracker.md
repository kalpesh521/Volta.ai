# 13 — Progress Tracker

Single place to see what is done, what is in progress and what is next. Update it at the end of each piece of work.
Plan and rationale: [11-roadmap-suggestions.md](./11-roadmap-suggestions.md). Phase 0 details: [12-phase0-implementation.md](./12-phase0-implementation.md). Phase 1 details: [14-phase1-implementation.md](./14-phase1-implementation.md). Phase 2 details and the Phase 1 wrap-up: [15-phase2-implementation.md](./15-phase2-implementation.md).

Legend: ✅ done · 🟡 partly done · ⬜ not started

_Last updated: 2026-11-12_

## 1. Overview

| Phase | Goal | Status | Progress |
|---|---|---|---|
| 0 | Production hardening | ✅ Done | 9 / 9 items |
| 1 | Supervisor + first specialist agents | ✅ Done (real-model answer quality still to be judged, see 3) | 6 / 6 |
| 2 | Guardrails and platform in Python | ✅ Done | 5 / 5 |
| 3 | Carry-over gate (judge, v2 switch, ingest safety, retrieval, multilingual) + planning agents (forecast, optimizer, bill & tariff) | ⬜ Not started | 0 / 14 |
| 4 | Automation and ambient agents | ⬜ Not started | 0 / 6 |
| 5 | Scale and operate | ⬜ Not started | 0 / 8 |

Stack decision (2026-11-09): everything in **Python / FastAPI / LangGraph**. Go is a future option only, triggered by measurements
([11 §3](./11-roadmap-suggestions.md)).

Test status: **763 tests pass** with Postgres and Redis available. **740 pass and 23 skip** without them. Static check (unused or undefined names): clean for everything written in Phases 1 and 2 (a few unused imports remain in five older files). Trajectory evals: **62 / 62 cases** (76 turns). Migrations: single head, applied to a clean database, the newest one downgrades and re-applies.

### Your actions (not agent work)

Things only you can do. Everything else in this file is planned for an agent in a numbered item.

| # | Action | Needed before |
|---|---|---|
| Y1 | Rotate the database password that is still in git history | Any shared or production use |
| Y2 | Choose a real embedding model, add its key, then run `python -m app.modules.knowledge.reembed` | Item 3.9 (retrieval quality can only be judged with real embeddings) |
| Y3 | Run the CI workflow once on GitHub and fix anything that differs from local | Phase 3 start |
| Y4 | Apply migration `202611120001` (`users.plan_tier`) to your dev database (compose's `migrate` job does it) | Running v2 on that database |
| Y5 | In every real deployment set `AI_PLATFORM_STORE=redis`, `AI_PII_VAULT_BACKEND=redis` and a vault key (`AI_PII_VAULT_KEY`) | First production deploy and any second API replica |
| Y6 | Approve a real-model spend for the L3 judge (item 3.5) and the prompt measurements (3.6). The current allowance is 2–3 calls and is already used | Items 3.5, 3.6 |
| Y7 | Decide the product rules for plan tiers: which users are `plus`, and what `free` may not do | Item 5.6 |

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
| 🟡 Rotate the password that is still in git history | You | Y1 |
| 🟡 Choose and enable a real embedding model, add its key, run `reembed` | You | Y2 |
| 🟡 Run the new CI workflow once on GitHub and fix anything that differs from local | You | Y3 |
| ✅ Grounding failure falls back without a retry (done for v2 in Phase 1; v1 still has no retry) | Agent | Phase 1 |
| ⬜ Device command is published before the DB commit (outbox needed) | Agent | 4.3, 4.5 |
| ⬜ Simulator does not send `traceparent` | Agent | 5.5 |
| ⬜ Vector column is 256 wide; `hnsw.iterative_scan` not set; true BM25/RRF fusion | Agent | 3.9 |

---

## 3. Phase 1 — Supervisor + first specialists ✅

Done 2026-11-11. The `actions/` relocation and the real-model parity check closed on 2026-11-12. Design: [02](./02-agent-catalog.md), [03](./03-langgraph-design.md). Details: [14](./14-phase1-implementation.md), wrap-up in [15 §9](./15-phase2-implementation.md).

| # | Functionality delivered | Key files | Verified by |
|---|---|---|---|
| 1.1 | ✅ **Supervisor graph**: classify → plan → parallel dispatch with `Send` → `join(defer=True)` → one synthesizer → output gate → finalize. Per-agent deadlines and failure isolation; a whole-request deadline; per-turn state reset | `agents_platform/orchestration/` | 28 graph tests incl. a timed parallelism check; mutation checks on the reset and the output gate |
| 1.2 | ✅ **Energy Insight agent**: the v1 tools, analytics, freshness and projection, returning facts. Never calls the model | `agents/energy_insight.py`, `agents/insight_engine.py` | Agent tests; v1/v2 parity over HTTP on 5 question types |
| 1.3 | ✅ **Knowledge agent**: Phase 0 retrieval, passages as citations, deterministic "not in your documents" on a gap | `agents/knowledge.py` | Gap parity with v1; a compound question with a real uploaded manual |
| 1.4 | ✅ **Device Action agent** as an exclusive route over the unchanged `ActionService`: proposes, never sends. `assistant/actions/` now lives at `agents/device_action/` (moved 2026-11-12) | `agents/device_action/` | Propose → confirm → queued over HTTP; nothing published on ask; refused when control is off; mutation check on exclusivity. The move: the action graph's state is plain JSON so no checkpoint embeds a module path; all 55 action and supervisor tests passed unchanged |
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
| ✅ Move `assistant/actions/` under `agents_platform/agents/device_action/` | Agent | Done 2026-11-12 |
| 🟡 Prove v2 at parity with a **real model**, then switch the frontend. Done as far as 2 real calls allow: structure identical, and a permanent test shows v1 and v2 send the model a byte-identical prompt. **Answer quality across many questions needs the L3 judge, which does not exist yet; the frontend has not been switched** | Agent + You | 3.5, then 3.7 (Y6) |
| 🟡 Grow the multi-agent dataset toward 80 compound cases. 42 → 62 cases (the 20 new ones are guard cases); compound cases are still 6 | Agent | 3.10 |
| ⬜ Mixed "explain and do": explain first, then offer the action | Agent | 3.11 |
| ✅ Serialise concurrent questions in one conversation (a lease lock, `409 conversation_busy`) | Agent | Done 2026-11-12 |
| ⬜ Conversation history endpoint, partial results on the whole-request deadline | Agent | 3.7 (the chat screen needs both) |
| ⬜ Measure checkpoint write volume per question | Agent | 4.6 |
| ⬜ Follow-up detection is English-only rules; the router model is not given conversation history | Agent | 3.12 |
| ⬜ No `RetryPolicy` on agent nodes | Agent | 3.14 |
| ⬜ Answer text is not streamed token by token (by design) | Agent | 5.7, only if a plain-text mode is added |
| ⬜ v1 grounding failure still falls back without a retry | Agent | Closed by 3.7 (v1 is retired) |
| 🟡 Run the CI workflow on GitHub once more (it now also runs the trajectory evals) | You | Y3 |

## 4. Phase 2 — Guardrails and platform in Python ✅

Done 2026-11-12. Design: [04](./04-guardrails-gates-masking.md). Details: [15](./15-phase2-implementation.md).

| # | Functionality delivered | Key files | Verified by |
|---|---|---|---|
| 2.1 | ✅ **Input guard** (G1): Unicode normalisation, injection score with narrow patterns, strong attempts refused, grey inputs treated as untrusted (no router model, no device action) or decided by an optional small model that can only narrow, never widen. Refused injections count toward a progressive cool-down | `guardrails/input_guard.py`, `injection.py`, `normalize.py`, `classifier.py`, `abuse.py`, `orchestration/nodes.py` (`input_guard`) | 12 attacks and 11 legitimate questions in a unit list; 20 new trajectory cases; HTTP: cool-down per user with `Retry-After`; mutation checks on the router gating, a failed classifier and the strike count |
| 2.2 | ✅ **PII masking with a vault** (G1, G8 #6): 12 classes with Luhn, Verhoeff and format validators; masked classes become tokens, card, bank, UPI and secrets refuse the message and are never stored; the output is scanned and only classes the user may be shown are restored. AES-256-GCM Redis vault, in-process vault by default | `guardrails/pii/` | 60 tests incl. a real Redis vault (ciphertext at rest, tamper binding, wrong key, fail-open); HTTP test reads the **stored checkpoint and the captured logs** and finds no raw value |
| 2.3 | ✅ **Token budgets** (G3): daily tokens per user by plan tier in Redis, checked in a dependency, debited with what the answer used; spent budget degrades to a verified answer without the model or returns `429`. New `users.plan_tier` column | `budgets/`, `api/admission.py`, migration `202611120001` | Memory and real-Redis stores under identical assertions; HTTP tests for charge, degrade, reject, tiers, per-user, stream |
| 2.4 | ✅ **Policy checks** (G5, G8 #5): each agent is handed only its tools; a model answer claiming to have switched a device, or giving unsafe electrical advice, is replaced by the verified answer without a retry. Action × device stays in `safety.py` (G6) | `guardrails/policy.py`, `guardrails/output_guard.py` | Planner/policy invariant over every intent; wording that must pass; mutation checks on the filter and the gate |
| 2.5 | ✅ **Registry and kill switches**: prompt versions chosen by config (v5 registered, v4 the default), a model per role, flags `ai.llm.enabled` and `ai.agent.<name>.enabled` flippable at runtime by an admin, closed set of names, everything validated at startup | `registry/`, `api/admin.py`, `ai/llm/gated.py`, `platform.py` | Override beats baseline in both directions; shared through real Redis; typo stops startup; admin-only over HTTP |
| — | ✅ **Conversation lock**: one question at a time per conversation (Phase 1 follow-up) | `locks.py` | Memory and real-Redis parity; lease expiry; holder-only release; released after success and failure |

### Extra work found while building

| Item | Why |
|---|---|
| ✅ Parity harness with a **hard, tested cap on real model calls** | The only thing allowed to spend money needed a guarantee, not a promise. Mutation checks on the cap and the charging |
| ✅ Free, permanent test that v1 and v2 send a byte-identical prompt | A real-model run showed different wording; this proved it was sampling and not v2. Only the data's age in seconds is normalised |
| ✅ Tool map: asking whether a tool exists no longer counts as calling it | Found by a test: `in` raised a denial |
| ✅ Budget switched on by one settings object only (admission) | Found while testing: two objects could disagree |
| ✅ `db_scope` test fixture moved to `conftest.py` | A new test once reached the real dev database through the streaming path because it forgot the fixture |

### Open follow-ups from Phase 2

| Item | Owner | Planned in |
|---|---|---|
| ⬜ **Switch the frontend to v2.** v1 has no guard, masking, budget or kill switch | Agent + You | 3.7 |
| ⬜ Build the **L3 judge** (doc 05) and measure answer quality on a real model across many questions. Also measure prompt v5 and the grey-zone classifier | Agent + You | 3.5 (Y6) |
| ⬜ A new answer-prompt version so the first field always answers the question (seen in the real-model run), measured before it is the default | Agent | 3.6 |
| ⬜ Mask and scan documents at ingest (store a risk score per chunk, quarantine instruction-like text). Today only the output is scanned | Agent | 3.8 |
| ⬜ An endpoint to change a user's plan tier (today: SQL), and a per-tenant monthly budget | Agent | 5.6 (Y7) |
| 🟡 Set `AI_PLATFORM_STORE=redis` and a vault key in the real deployment. Compose already does for the shared store | You | Y5 |
| ⬜ Hinglish and Hindi guard patterns; leetspeak; NER for names and addresses | Agent | 3.12 |
| ⬜ Budget burst can overshoot by a few requests' tokens; budgets are per user only | Agent | 5.6 |
| ⬜ Policy is Python data, not OPA | Agent | 5.8, only if its trigger fires |
| ⬜ With the memory store a flag change, cool-down or lock reaches one process only | You + Agent | Y5, and 5.2 (several workers per pod) |

## 5. Phase 3 — Carry-over gate, then planning agents ⬜

Target: 4–6 weeks for the agents, plus the gate below. Design: [02 §5](./02-agent-catalog.md).

**Rule for this phase: items 3.5 to 3.7 come first.** The planning agents are the first ones whose answers cannot be checked by
reading one number against one tool, so the judge must exist before they do. Each new agent also adds its own trajectory cases and
guard cases to the datasets (3.10) and registers its tools in `guardrails/policy.py` (the planner/policy test fails otherwise).

### Carry-over gate (from Phases 0–2)

| # | Item | Closes | Status |
|---|---|---|---|
| 3.5 | **L3 judge** (doc 05): LLM-as-judge on a labelled question set, with a cost cap like the parity harness. Use it to measure answer quality on a real model, prompt v5 and the grey-zone classifier | Phase 1 "real-model parity"; Phase 2 "classifier and v5 not measured". Needs Y6 | ⬜ |
| 3.6 | **Answer prompt v6**: the first field must answer the question (seen in the real-model run). Registered beside v4 and v5, measured with 3.5, then the default changes | Phase 2 "first field sometimes does not answer" | ⬜ |
| 3.7 | **Switch the frontend to v2 and retire v1**: chat screen on `/v2/assistant`, add the conversation history endpoint and partial results on the whole-request deadline, then remove `/assistant` v1, its graph and its eval set | Phase 2 "v1 has no guard, masking or budget"; Phase 1 history/partial results and "v1 grounding has no retry". After 3.5 | ⬜ |
| 3.8 | **Ingest-side safety for documents**: mask personal data in chunks, store a risk score per chunk, quarantine instruction-like text from shared scope, add the untrusted-document wrapper | Phase 2 "documents not masked or scanned at ingest" | ⬜ |
| 3.9 | **Retrieval upgrade**: vector column width, `hnsw.iterative_scan`, true BM25/RRF fusion, re-embed with the chosen model | Phase 0 retrieval follow-up. Needs Y2 | ⬜ |
| 3.10 | **Grow the datasets**: 6 → 80 compound cases from masked production questions and paraphrases; each new agent adds cases and guard cases | Phase 1 dataset follow-up | ⬜ |
| 3.11 | **Mixed "explain and do"**: explain first, then offer the action as a follow-up proposal (planner change plus eval cases) | Phase 1 limit | ⬜ |
| 3.12 | **Multilingual input**: Hinglish and Hindi guard patterns, follow-up detection and router history, leetspeak, NER for names and addresses | Phase 1 follow-up detection and router history; Phase 2 English-only guard and NER | ⬜ |
| 3.14 | **`RetryPolicy` on agent nodes**, for agents that call slower dependencies (forecast models, solvers) | Phase 1 limit | ⬜ |

### Planning agents

| # | Item | Status |
|---|---|---|
| 3.1 | **Forecast** agent (ML models, physics fallback from the simulator engines) | ⬜ |
| 3.2 | **Optimizer** agent (schedule solver and battery strategy) | ⬜ |
| 3.3 | **Bill & Tariff** agent with versioned tariff tables and the what-if simulator | ⬜ |
| 3.4 | Long-term memory store and a "What Suryaa remembers" page | ⬜ |
| 3.13 | Plan-tier rules for the new agents (for example the Optimizer and what-if only on `plus`), using the existing tier on the user | ⬜ |

## 6. Phase 4 — Automation and ambient ⬜

Target: 4–6 weeks.

| # | Item | Status |
|---|---|---|
| 4.1 | Rule engine in Python (alerts and approved automations, state transitions only), reusing `domain/alerts.py` | ⬜ |
| 4.2 | **Automation Rule** agent: natural language to a rule DSL, validate, dry-run, user approval | ⬜ |
| 4.3 | Command executor with outbox table, idempotency, state machine, kill switch, audit | ⬜ |
| 4.4 | **Monitor**, **Recommender**, **Reporter** ambient and batch agents; provider batch APIs | ⬜ |
| 4.5 | Fix the publish-before-commit gap (follows from 4.3) | ⬜ |
| 4.6 | **Exit gate:** measure checkpoint write volume per question with the Phase 0 metrics, then set retention and checkpoint durability from the numbers. Move `HomeNotification` and `AutomationRule` out of `agents/device_action/` into their own homes | ⬜ |

## 7. Phase 5 — Scale and operate ⬜

Ongoing.

| # | Item | Status |
|---|---|---|
| 5.1 | Horizontal ingest workers with queue-lag autoscaling; batched Timescale writes | ⬜ |
| 5.2 | Several uvicorn workers per pod; scale agent pods on in-flight requests. Confirms the Redis-backed budgets, cool-downs, flags and locks hold across workers | ⬜ |
| 5.3 | Off-the-shelf gateway (Envoy, Kong or Traefik) for TLS, routing and coarse limits | ⬜ |
| 5.4 | Online judge sampling, canary pipeline, cost dashboards, nightly red-team generator | ⬜ |
| 5.5 | Simulator sends `traceparent` so traces start at the source | ⬜ |
| 5.6 | **Plans and cost control:** an endpoint to change a user's plan tier, a per-tenant monthly budget, and reservation-based budgeting so a burst cannot overshoot. Needs Y7 | ⬜ |
| 5.7 | Revisit token streaming, only if a plain-text answer mode is added | ⬜ |
| 5.8 | Adopt OPA for policy, **only if** rules must change without a deploy. Not scheduled otherwise | ⬜ |

---

## 8. Coverage: every known limit and where it is fixed

Nothing recorded as a limit, shortcut or open question in documents 12, 14 and 15 is unscheduled. "Y" items are yours (section 1).

| Known limit | Recorded in | Fixed by |
|---|---|---|
| Password still in git history | 12 | Y1 |
| No real embedding model enabled | 12 | Y2, then 3.9 |
| CI workflow not yet run on GitHub | 12, 14 | Y3 |
| Dev database lacks the newest migration | 15 | Y4 |
| Memory stores and in-process vault in a real deployment | 15 | Y5, 5.2 |
| Device command published before the DB commit | 12 | 4.3, 4.5 |
| Simulator sends no `traceparent` | 12 | 5.5 |
| Vector width 256, no `hnsw.iterative_scan`, no BM25/RRF | 12 | 3.9 |
| v2 answer quality not measured on a real model; classifier and prompt v5 unmeasured | 14, 15 | 3.5 (Y6) |
| First field of a model answer sometimes does not answer | 15 | 3.6 |
| v1 has no guard, masking, budget or kill switch; v1 grounding has no retry; frontend still on v1 | 14, 15 | 3.7 |
| No conversation history endpoint; no partial results on the whole-request deadline | 14 | 3.7 |
| Documents not masked or scanned at ingest | 15 | 3.8 |
| Compound dataset has 6 cases (target 80) | 14, 15 | 3.10 |
| "Explain and do" runs only the action | 14, 15 | 3.11 |
| English-only follow-ups, router has no history, English-only guard, no leetspeak, no NER | 14, 15 | 3.12 |
| No `RetryPolicy` on agent nodes | 14, 15 | 3.14 |
| Checkpoint write volume not measured | 14 | 4.6 |
| `HomeNotification` and `AutomationRule` live in the Device Action package | 15 | 4.6 |
| Plan tier changed only by SQL; budgets per user only; burst can overshoot | 15 | 5.6 (Y7) |
| Policy is Python data, not OPA | 15 | 5.8 (conditional) |
| Answer text not streamed token by token | 14 | 5.7 (conditional) |
| Go components (edge, guard, executor) | 06 | Not scheduled: only if measurements demand it ([11 §3](./11-roadmap-suggestions.md)) |

## 9. Decision log

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
| 2026-11-12 | The guard's fast path runs in the service, before the graph; its verdict then enters the graph as state | Masking must happen before the checkpoint is written (the checkpoint stores the question). A refusal still shows up in the node path, metrics and evals like any route |
| 2026-11-12 | A grey-zone input is restricted (no router model, no device action), not refused; a classifier can only narrow that, and its failure never promotes an input | Refusing grey inputs turns away real customers; a failing classifier must not be a way around the guard |
| 2026-11-12 | Identifiers (consumer, meter, bank account) are recognised only next to their label; card and Aadhaar need their checksum | A bare run of digits may be an energy reading. Precision matters more than recall for a refusal |
| 2026-11-12 | Card, bank, UPI and secrets refuse the message and are never stored; everything else is masked and the question is still answered | Nothing safe can be kept from the former; the latter is the user's ordinary question with a value in it |
| 2026-11-12 | A model claim of having switched a device is not regenerated: the verified answer replaces it | A model that thinks it can act will say so again. Numbers are still regenerated once |
| 2026-11-12 | Every Redis-backed store fails open | A cache outage must not take the assistant down. `store_error` is counted |
| 2026-11-12 | The PII vault defaults to in-process memory; the Redis (AES-GCM) vault is built and tested but optional | Mask and restore happen in one request, so memory is correct for one API process. Redis exists for unmasking in another process later |
| 2026-11-12 | Prompt v5 is registered but not the default | A prompt change is not shipped on a fake-model match (same rule as the v2 switch) |
| 2026-11-12 | Spent budget degrades to a verified answer by default (`reject` is the option) | Still useful, costs no tokens |
| 2026-11-12 | `assistant/actions/` moved under the Device Action agent | The action graph's state is plain JSON, so no checkpoint embeds a module path and in-flight proposals survive. Public URLs unchanged |
| 2026-11-12 | The parity harness enforces its call cap in code and prices each request before sending it | The model is the user's money. A cap you promise is not a cap |
| 2026-11-12 | A different stated number between v1 and v2 is informational in the parity run | Both passed the grounding gate; it is a different verified fact chosen, not an error |
| 2026-11-12 | Phase 3 starts with a carry-over gate (judge, prompt v6, v2 switch) before any new agent | The planning agents' answers cannot be verified by reading one number against one tool. Measure first, then build. Every known limit now has an item number (section 8) |

## 10. How to update this file

1. When an item starts, change ⬜ to 🟡. When it is finished and tested, change it to ✅ and fill in "Key files" and "Verified by".
2. Update the overview table counts and the test status line.
3. Add any new bug found along the way to the phase's "Extra fixes" list, and any shortcut taken to "Open follow-ups".
4. Add a row to the decision log for any decision that changes the plan.
5. **Every limit or shortcut must have an item number.** When a "known limit" is written in an implementation doc, add or point to a
   numbered item in a later phase (or a `Y` action for you) and add a row to section 8. A limit with no item is not allowed to stay.
