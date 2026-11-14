# 13 — Progress Tracker

Single place to see what is done, what is in progress and what is next. Update it at the end of each piece of work.
Plan and rationale: [11-roadmap-suggestions.md](./11-roadmap-suggestions.md). Phase 0 details: [12-phase0-implementation.md](./12-phase0-implementation.md). Phase 1 details: [14-phase1-implementation.md](./14-phase1-implementation.md). Phase 2 details and the Phase 1 wrap-up: [15-phase2-implementation.md](./15-phase2-implementation.md). Phase 3 and 4 work done so far: [16-phase3-implementation.md](./16-phase3-implementation.md) (judge and prompt v6) and [18-phase3-4-build-notes.md](./18-phase3-4-build-notes.md) (everything else). Phase 5 operations (`17-phase5-operations.md`) was **never written**: the agent was stopped first (see 1b).

Legend: ✅ done · 🟡 partly done · ⬜ not started

_Last updated: 2026-11-14 (end of the build session; real-model evals deliberately still pending)_

## 1. Overview

| Phase | Goal | Status | Progress |
|---|---|---|---|
| 0 | Production hardening | ✅ Done | 9 / 9 items |
| 1 | Supervisor + first specialist agents | ✅ Done (real-model answer quality still to be judged, see 3) | 6 / 6 |
| 2 | Guardrails and platform in Python | ✅ Done | 5 / 5 |
| 3 | Carry-over gate (judge, v2 switch, ingest safety, retrieval, multilingual) + planning agents (forecast, optimizer, bill & tariff) | 🟡 Built except 3.7 and 3.10; four items are partly done and waiting on you or on wiring (3.4, 3.5, 3.6, 3.9) | 8 done, 4 partly, 2 not started (of 14) |
| 4 | Automation and ambient agents | 🟡 Rule engine, storage, API, worker, outbox and the Monitor are done. The Automation Rule agent, Recommender and Reporter are not | 3 done (4.1, 4.3, 4.5), 2 partly (4.4, 4.6), 1 not started (4.2) |
| 5 | Scale and operate | 🟡 An infrastructure agent was stopped part-way: 5.1, 5.2, 5.3, 5.5 are partly written and unverified (see 1b). 5.4 and 5.6 not started | 0 verified; 5.1, 5.2, 5.3, 5.5 partly written and unverified; 5.4, 5.6 not started; 2 conditional |

Stack decision (2026-11-09): everything in **Python / FastAPI / LangGraph**. Go is a future option only, triggered by measurements
([11 §3](./11-roadmap-suggestions.md)).

Test status: **not re-measured at the end of this session.** The full suite was started and then stopped by request, so there is no single pass count for the final tree. The last complete run (826 pass, 11 skip, with Redis, Postgres tests not run) was early in the session, before most of the work below. Per-file counts were measured when each file was finished and are listed in [18 §8](./18-phase3-4-build-notes.md) (1,070 tests across 20 new files by the sum of those counts; each file passed when it was finished, the memory store's 111 as reported by the agent that wrote them). The Postgres-only tests were run once against a scratch database (retrieval 12, outbox 2) and passed. **First thing to do on resume: run `cd backend && TEST_REDIS_URL=redis://localhost:6379/15 ./venv/bin/python -m pytest -q`.** Trajectory evals were 80 / 80 cases earlier in the session. Migrations 0001 to 0004: single head, applied to a clean Postgres, newest one downgrades and re-applies.

## 1b. Session handoff (read this first when you resume)

**State at the end of the 2026-11-14 build session.** Nothing is committed. Everything is in the working tree. Commit before you start anything new: once, in this project, the uncommitted tree vanished between sessions.

**What exists and was verified.** Phase 3 agents (Forecast, Optimizer, Bill & Tariff) with their engines, plan-tier rules, retry, planning-intent routing, "explain and do", multilingual guard, ingest-side document safety, BM25/RRF retrieval, the L3 judge harness, prompt v6 (registered, not default), the transactional device-command outbox, stored automation rules with approval, and the automation worker. Migrations 0001 to 0004 (`202611140001` to `202611140004`) apply, downgrade and re-apply on real Postgres; single head. Details and numbers per item are in sections 5 and 6.

**The Phase 5 infrastructure agent was stopped part-way by request, so that work is incomplete and unverified.** It had started: `app/core/startup_guards.py`, `app/serve.py`, `app/workers/ingest_batch.py`, `ingest_metrics.py`, `queue_lag.py`, `timescale_batch.py`; edits to `app/workers/ingest.py` (about 160 lines changed), `app/main.py` (6 lines), `docker-compose.yml` (a gateway profile), `deploy/gateway/*.yml` (Traefik config), the simulator's `tracing.py`, `clients/*.py`, `config.py`, `main.py`; and tests `test_ingest_batch.py`, `test_ingest_batch_timescale.py`, `test_ingest_rabbitmq.py`, `test_shared_state_across_processes.py`, `test_worker_topology.py`, `simulator/tests/test_tracing.py`. It was killed while making the last one-line edit to `main.py`. **It never wrote `17-phase5-operations.md`, never reported, and its tests were never run as a set.** The app still imports. Decide on resume: review `git diff` of those files and the new files, run their tests, then keep, finish or revert them. Do not assume any of 5.1, 5.2, 5.3 or 5.5 works.

**Next steps, in this order.**

1. Run the full suite (see the test line above), then decide what to do with the stopped Phase 5 agent's partial work (see above).
2. **Wire long-term memory (3.4) into the supervisor.** The store, API and page exist; see section 5, item 3.4, for the exact six wiring steps.
3. **Automation Rule agent (4.2).** Natural language to the rule DSL, using `metric_catalog()`, `TEMPLATES` and `RuleValidationError.to_dicts()` for the repair loop, then `AutomationService.propose` (stored pending, never active). Add its agent name, intent, entitlement and trajectory cases.
4. **Recommender and Reporter (4.4)**, then the checkpoint-volume measurement (4.6).
5. **3.7**: decide whether to store answers for a history endpoint (Y10), add the endpoint and partial results, switch the dashboard page to v2. **Retiring v1 waits for the real-model evals.**
6. **5.4 and 5.6** (judge sampling hooks, nightly red-team generator, plan endpoint, per-tenant monthly budget, reservation-based budgeting).
7. Docs: write `18-phase3-4-build-notes.md` fully (the file is referenced above; at the time of writing only this tracker holds the detail).
8. Only then: **real-model evals** (3.5, 3.6, the grey-zone classifier, prompt v5). Ask the user for permission first, use the capped harness, small model, few calls.

**Things that will surprise you.**

* Real-model evals are **pending on purpose**. Nothing in this session called a real model. Every test uses a fake one.
* `tests/conftest.py` now switches the outbox **off** for every test by default (older tests assert on the publisher directly); `test_device_outbox.py` switches it on for itself. In production the outbox is on (`DEVICE_COMMAND_OUTBOX_ENABLED=true`).
* Planning intents (`forecast`, `optimize`, `bill_estimate`) are a **separate enum** used only by v2, detected by rules beside the legacy classification, so v1 behaviour and its eval set are untouched.
* `AI_PLUS_ONLY_AGENTS` defaults to `optimizer`; the what-if simulator is Plus-only (`AI_PLUS_ONLY_WHAT_IF`). Which users are Plus is still your decision (Y7).
* The shipped tariff tables are **illustrative and marked unverified**; every bill and plan carries that flag and the answer prompt must say so. Replace them with official tariff orders (Y11) before showing rupee figures as official.
* Open-Meteo weather for the Forecast agent is **off by default** (`AI_FORECAST_WEATHER_ENABLED=false`); the forecast then uses persistence of the latest observed cloud cover and says so.
* Several background agents shared one scratchpad directory and one ran another's mutation script against live code. Everything was re-verified afterwards, but give each agent its own scratch directory.
* The Bash tool refused a large script containing `rm -rf`. Use the file tools for edits.

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
| Y8 | Review the 12 calibration labels in `evals/judge/calibration.jsonl` (written by the code's author, not a human reviewer) and label more answers, up to 100 | Item 3.5: no judge score may decide anything before this |
| Y9 | Choose a judge model stronger than the answer model and set `AI_JUDGE_LLM_PROVIDER`, `AI_JUDGE_LLM_MODEL` and its key | Item 3.5 |
| Y10 | Decide whether the assistant may **store answers** (masked, with retention) so a conversation history screen can exist. Today memory keeps questions only | Item 3.7 |
| Y11 | Replace the illustrative tariff tables in `agents/bill_tariff/tariffs/` with official tariff orders for your DISCOMs, mark them `verified: true` | Showing rupee figures as official |
| Y12 | Decide the real-model evaluation budget (calls, model, judge model) when development is finished. You said: ask first, small model, few tokens | Item 3.5, 3.6 |
| Y13 | Apply migrations `202611140001` to `202611140004` to your dev and production databases (compose's `migrate` job does it) | Running the new code |
| Y14 | Start the two new processes where you deploy: `outbox-relay` and `automation-worker` (both are in `docker-compose.yml`; run exactly one `automation-worker`) | Device commands in production; automation rules running |

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

## 5. Phase 3 — Carry-over gate, then planning agents 🟡

Design: [02 §5](./02-agent-catalog.md). Test counts below are the files' own counts, measured when each item was finished.

### Carry-over gate (from Phases 0–2)

| # | Item | Status |
|---|---|---|
| 3.5 | **L3 judge** (doc 05): `agents_platform/evals/judge/`. 15 frozen cases, 6-criterion rubric, deterministic checks, scoring, pairwise A/B with position swap, calibration (Cohen's kappa), baseline gate, hard call cap. 72 tests, 17 mutation checks. **Never run on a real model. Calibration is 12 author-labelled answers of 100** ([16](./16-phase3-implementation.md)). Needs Y6, Y8, Y9, Y12 | 🟡 Built, evals pending |
| 3.6 | **Answer prompt v6** (the first field answers the question). Registered beside v4 and v5, pinned as v5 plus one rule. v4 stays the default until `judge ab` shows v6 wins | 🟡 Built, evals pending |
| 3.7 | **Switch the dashboard to v2, history endpoint, partial results on the deadline, then retire v1.** The frontend here is static HTML, not React. Needs Y10 first (history means storing answers). **Retiring v1 waits for 3.5.** Not started | ⬜ |
| 3.8 | ✅ **Ingest-side safety for documents.** Chunks are masked before embedding and storage (`[hidden phone number]`; consumer and meter numbers kept), scored with the injection patterns, quarantined at or above the block score (stored for review, never retrieved), a document of only instructions is refused, and v5/v6 wrap excerpts in `<untrusted_document>` with angle brackets neutralised. Migration `202611140001`. 20 tests + 1 on real Postgres | ✅ |
| 3.9 | **Retrieval upgrade.** ✅ Real BM25 + Reciprocal Rank Fusion replace the weighted overlap; ✅ `hnsw.iterative_scan` and `ef_search` applied per transaction (skipped quietly on pgvector older than 0.8); ✅ `reembed --resize-vector` resizes the pgvector column. 23 unit tests, 5 on real Postgres. **Still open: choose a real embedding model and run it (Y2); retrieval quality is unmeasured** | 🟡 |
| 3.10 | **Grow the datasets** (6 to 80 compound cases; richer judge snapshots). Trajectory cases grew from 62 to 80 with the new agents, but compound cases are still few. Evals pending by decision | ⬜ |
| 3.11 | ✅ **"Explain and do".** A question plus a command is split: the question is answered, the command is *offered* ("send it on its own and I will ask you to confirm"). Device Action stays exclusive; nothing is proposed or sent. The head must be a question (an assertion before a command stays a device request); a pronoun command ("turn it on") is not split. 21 tests, 6 trajectory cases | ✅ |
| 3.12 | ✅ **Multilingual and obfuscation.** Hinglish, Hindi and Marathi injection patterns, leetspeak folded in a scoring view (units and codes untouched), Hindi follow-up openers, the router model gets the last two earlier questions, and cue-based masking of names and addresses. Rule-based, not a model: see limits in 18. 69 new tests | ✅ (rule-based) |
| 3.14 | ✅ **Retry for slow agents.** Bounded retry for `TransientAgentError` inside the agent's single deadline; Device Action is never retried. LangGraph's own `RetryPolicy` does not fit (the node swallows exceptions) | ✅ |

### Planning agents

| # | Item | Status |
|---|---|---|
| 3.1 | ✅ **Forecast agent.** Engine: clear-sky physics, a clamped performance-ratio calibration from history, hourly load profile, p10/p50/p90 intervals, persistence or Open-Meteo weather with fallback. Agent reads through an authorised data port. 52 + 16 tests, 23 mutation checks | ✅ |
| 3.2 | ✅ **Optimizer agent.** Engine: exact convex DP for the battery, coordinate descent for appliances, an **independent verifier** that must find zero violations or the plan is not returned. Prices come from the tariff engine by finite difference. 62 + 24 tests, 13 mutation checks | ✅ |
| 3.3 | ✅ **Bill & Tariff agent.** Versioned tariff tables (illustrative, `verified: false`), itemised calculator (Decimal), what-if simulator. Usage is estimated from the user's stated bill. 84 + 28 tests, 17 mutation checks | ✅ |
| 3.4 | **Long-term memory store and "What Suryaa remembers" page.** ✅ Built: consent-first (explicit "remember that ..." or the API only), masked, injection-screened, capped, deduped, hard delete, export, per-home scope; `frontend/public/suryaa-memory.html`. 111 tests, 12 mutation checks, migration `202611140002`. **Not wired in:** (1) check `handle_remember_command` before the planner; (2) merge `memory_facts(...)` into the facts and call `mark_used`; (3) add a prompt rule that `<untrusted_memory>` is data; (4) serve the page from `app/webui.py` (`/ui/memory`); (5) call `purge_expired` from `agents_platform/maintenance.py`; (6) call `MemoryService.erase_user` from account deletion | 🟡 |
| 3.13 | ✅ **Plan-tier rules for agents**, as configuration: `AI_PLUS_ONLY_AGENTS` (default `optimizer`) and `AI_PLUS_ONLY_WHAT_IF`. A free user gets fixed wording and the agent never runs; an unknown tier is `free`. Values wait on Y7 | ✅ |

## 6. Phase 4 — Automation and ambient 🟡

| # | Item | Status |
|---|---|---|
| 4.1 | ✅ **Rule engine, storage and execution.** DSL with a closed vocabulary and validator, three-valued logic, edge-triggered evaluation (hysteresis, cooldown, daily cap), dry run, fingerprint. Stored in `automation_rule_defs` (migration `202611140004`), lifecycle `pending_approval` to `active` to `paused`; approval binds to the fingerprint the user saw; the executor re-checks it before every run and pauses a rule that no longer matches. HTTP API under `/v2/assistant/automations`. A rule only ever **notifies or suggests**: it never creates a command or an outbox row. 355 engine + 27 service + 8 API + 8 worker tests | ✅ |
| 4.2 | **Automation Rule agent**: natural language to a rule, validate with repair, dry-run, user approval. The service, validator messages and API are ready; the agent, its intent, entitlement and trajectory cases are not written | ⬜ |
| 4.3 | ✅ **Command executor with an outbox.** `device_command_outbox` (unique `command_id` as the idempotency key), `OutboxRelay` (retries with growing pause, dead after N attempts, kill switch checked at delivery, stale commands dropped, corrupt rows marked dead), a written proposal state machine, an idempotent consumer, `outbox-relay` process. Proven on real Postgres that two relays never take the same row. 23 tests + 2 on Postgres. Migration `202611140003` | ✅ |
| 4.4 | **Monitor, Recommender, Reporter.** ✅ Monitor = the automation worker (`app/workers/automation.py`): its own short-lived queue, bounded, one replica per home set. ⬜ Recommender and ⬜ Reporter (and provider batch APIs) not written | 🟡 |
| 4.5 | ✅ **Publish-before-commit gap closed.** A confirmed command is a row written in the request's own transaction; the broker is called after the commit. Tested: a rolled-back request leaves no row and nothing on the wire; a broker outage no longer fails the user's confirmation | ✅ |
| 4.6 | **Exit gate.** ✅ `HomeNotification` and the legacy `AutomationRule` moved out of the Device Action package (tables unchanged, no migration). ⬜ Measure checkpoint write volume per question with the Phase 0 metrics, then set retention and durability from the numbers | 🟡 |

Known gaps in Phase 4: the device worker accepts only on, off and schedule, so a DSL `set_level` has no executor and `HomeNotification` has no push channel (a `push` notification is delivered in-app only); the automation worker must be a single replica; `domain/alerts.py` thresholds are copied into the DSL with a parity test instead of shared constants.

## 7. Phase 5 — Scale and operate 🟡

| # | Item | Status |
|---|---|---|
| 5.1 | Horizontal ingest workers with batched Timescale writes | 🟡 **Partly written, unverified.** The agent was stopped part-way; batching modules and tests exist, never run as a set |
| 5.2 | Several uvicorn workers per pod; startup guard against the memory store with more than one worker | 🟡 Partly written, unverified (agent stopped) |
| 5.3 | Off-the-shelf gateway (opt-in compose profile) | 🟡 Partly written, unverified (agent stopped) |
| 5.4 | Online judge sampling, canary pipeline, cost dashboards, nightly red-team generator | ⬜ |
| 5.5 | Simulator sends `traceparent` | 🟡 Partly written, unverified (agent stopped) |
| 5.6 | **Plans and cost control:** endpoint to change a user's tier, per-tenant monthly budget, reservation-based budgeting. Needs Y7 | ⬜ |
| 5.7 | Token streaming, only if a plain-text answer mode is added | ⬜ Conditional, not scheduled |
| 5.8 | OPA for policy, only if rules must change without a deploy | ⬜ Conditional, not scheduled |

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
| Judge never run on a real model; calibration is 12 author-labelled answers (target 100 human-labelled); judge runs at the global temperature | 16 | 3.5 (Y6, Y8, Y9) |
| No CI step for the judge (needs a key and costs money) | 16 | 5.4 |
| Conversation history endpoint needs a decision on storing answers first | 14, 16 | 3.7 |
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
| Memory store built but not wired into the supervisor, the sweep or account deletion | 18 | 3.4 |
| DSL `set_level` has no executor; `push` notification channel not delivered; automation worker is single replica | 18 | 4.2, 4.4, 5.2 |
| Automation Rule agent, Recommender, Reporter not built | 18 | 4.2, 4.4 |
| Checkpoint write volume not measured | 14 | 4.6 |
| Name and address masking is cue-based, not a model; Hinglish replies and cases are few | 18 | 3.10, 3.12 follow-up when production questions exist |
| Shipped tariff tables are illustrative | 18 | Y11 |
| Forecast intervals are nominal and not validated against actuals; optimizer treats the forecast as certain | 18 | 3.5 (needs real data) |
| Phase 5 infrastructure items are partly written by an agent that was stopped; nothing verified, no doc written | 1b | 5.1, 5.2, 5.3, 5.5 (review, test, then keep, finish or revert) |
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
| 2026-11-14 | The judge's eval data is frozen snapshots captured from the real pipeline, not hand-written fixtures | Hand-written facts drift from what production sends the model. A snapshot is exactly what the prompt shows |
| 2026-11-14 | A judge criterion is never reported `trusted` below 100 labelled answers, whatever its kappa | A kappa from a dozen answers cannot approve a rubric (doc 05 asks for 100) |
| 2026-11-14 | Prompt changes are decided by position-swapped pairwise comparison; a version wins only if it wins both orders | Judges favour the first position; disagreement between orders is bias, so it counts as a tie |
| 2026-11-14 | Planning intents are a separate v2-only enum, detected by rules beside the legacy classification | Adding members to the shared `Intent` would have changed v1 and its eval set. A miss falls back to the legacy answer, which is correct for everything not clearly a planning question |
| 2026-11-14 | "Explain and do" offers the command instead of running both | Device Action must stay exclusive and every command must go through confirm-before-act on live data. An assertion before a command is never split (the constraint-bypass attack) |
| 2026-11-14 | Plan-tier rules are configuration, validated at startup against the closed agent set | The product rules (Y7) will change; a typo must stop startup, not leave an expensive agent open to everyone |
| 2026-11-14 | A confirmed device command is an outbox row in the request's transaction; the broker is called after the commit | Closes publish-before-commit. A command is on the wire only if the database recorded it, and a broker outage loses nothing. The consumer is idempotent because delivery is at least once |
| 2026-11-14 | The kill switch is checked when the relay delivers, not only when the action is proposed | Flipping it must stop commands that are already queued |
| 2026-11-14 | A rule can only notify or suggest; approval binds to the fingerprint the user saw | A rule must never move a device by itself, and what runs must be exactly what was approved |
| 2026-11-14 | Ingest-side safety quarantines instruction-like chunks in every scope, not only shared | Cheap, and an attacker-supplied PDF in a user's own account is the same indirect-injection route |
| 2026-11-14 | Tariff tables ship illustrative and flagged unverified, and the flag reaches the answer | Invented rates must never be presented as an official tariff |
| 2026-11-14 | Forecast, optimizer and tariff are pure engines with their own tests; agents are thin adapters | The arithmetic is checkable without a model, and the answer model never needs to calculate |
| 2026-11-14 | Real-model evaluation is the last step of development, with the user's permission and a hard call cap | The user's instruction: finish development first, then test with a small model and few tokens |
| 2026-11-14 | Real-model eval calls are charged when sent, retries and fallback are off, and three errors in a row stop the run | A failed request can still be billed, and a wrong key must not walk through the budget |

## 10. How to update this file

1. When an item starts, change ⬜ to 🟡. When it is finished and tested, change it to ✅ and fill in "Key files" and "Verified by".
2. Update the overview table counts and the test status line.
3. Add any new bug found along the way to the phase's "Extra fixes" list, and any shortcut taken to "Open follow-ups".
4. Add a row to the decision log for any decision that changes the plan.
5. **Every limit or shortcut must have an item number.** When a "known limit" is written in an implementation doc, add or point to a
   numbered item in a later phase (or a `Y` action for you) and add a row to section 8. A limit with no item is not allowed to stay.
