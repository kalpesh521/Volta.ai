# 18 — Phase 3 and 4 build notes

What was built in the 2026-11-14 session beyond the judge and prompt v6 (those are in [16](./16-phase3-implementation.md)): the
planning agents, document and retrieval safety, the multilingual guard, the device-command outbox, and automation rules with
their worker. Status per item lives in [13](./13-progress-tracker.md); this file explains how each piece works, how to run it,
and where it stops.

**Real-model evaluation was not done and no real model was called.** Every test uses a scripted or fake model.

## 1. How a planning question travels

```
question ─► input guard ─► classify ─► plan ─► agents in parallel ─► join ─► one answer call ─► output gate ─► finalize
                              │                    │
                 rules, legacy Intent         forecast | optimizer | bill_tariff | energy_insight | knowledge | device_action
                 + planning intents           (each failure-isolated, each with its own deadline)
```

* **Planning intents** (`forecast`, `optimize`, `bill_estimate`) are a separate enum, detected by narrow rules *beside* the
  legacy classification ([planning_intents.py](../../backend/app/agents_platform/orchestration/planning_intents.py)). They take
  the primary place when they match. A device command, a full overview and "can I run the geyser now" keep their own paths. A
  legacy intent the same words also matched is dropped unless the question is plainly two-part or asks for a document.
* **Plan tier.** `AI_PLUS_ONLY_AGENTS` (default `optimizer`) and `AI_PLUS_ONLY_WHAT_IF` are checked in the agent node. A free
  user gets fixed wording (`plan_required`), costs no tokens, and the agent never runs. An unknown tier is `free`.
* **Retry.** An agent raises `TransientAgentError` for a dependency that may answer next time. The node retries inside the agent's
  one deadline (`AI_PLANNING_AGENT_MAX_ATTEMPTS`, default 2). Device Action is never retried.
* **Data scopes (gate G5, second half).** Planning agents read only through `AuthorizedData`, whose scopes (`live`, `history`,
  `profile`) are listed per agent in `guardrails/policy.py`. A read outside the scope raises and is counted.

## 2. The agents

All three follow one shape: a pure engine with its own tests, and a thin agent that reads data, calls the engine and returns
`AgentResult` facts. The agent never writes the answer and the answer model never calculates: everything it may quote is a
rounded field in the facts, and the grounding guard checks the answer against exactly those facts.

| Agent | Engine | Honesty it enforces |
|---|---|---|
| **Forecast** | `agents/forecast/`: clear-sky physics, cloud transmission, a calibration factor clamped to 0.6–1.1 from history, an hourly load profile, p10/p50/p90 intervals. Weather: persistence by default, Open-Meteo when `AI_FORECAST_WEATHER_ENABLED=true`, with a typed failure that degrades to persistence plus a warning | Needs the home's coordinates; without them it says so instead of forecasting for the wrong sun. Confidence is reported with reasons |
| **Optimizer** | `agents/optimizer/`: exact convex DP for the battery, coordinate descent for appliances, and an independent verifier that must find zero violations or no plan is returned. Prices from the tariff engine by finite difference (`optimizer/pricing.py`) | Never claims high confidence; states assumptions (sanctioned load, run hours, flat load) as numbers; carries the tariff's `verified` flag; advises only |
| **Bill & Tariff** | `agents/bill_tariff/`: versioned tables, Decimal calculator, what-if simulator. Usage is estimated by inverting the calculator on the user's **stated** monthly bill | The shipped tables are illustrative and `verified: false`; the flag is in the facts, the warnings and the prompt notes. Without a stated bill it asks for one instead of guessing |

Answer-prompt additions: intent guides for `forecast`, `optimize`, `bill_estimate`, and field notes that apply only when
`peak_solar`, `plan_summary`, `tariff_verified`, `usage_basis` or `what_if` are in the facts. Existing questions therefore produce
byte-identical prompts.

## 3. Explain and do, and the multilingual guard

* **Explain and do** ([explain_and_do.py](../../backend/app/agents_platform/orchestration/explain_and_do.py)): "Why is the battery
  draining, and turn off the AC" answers the question and ends with an offer to send the command on its own. Nothing is proposed or
  queued. The head must be a question; an assertion before a command (the constraint-bypass attack) is not split; a pronoun command
  is not split; anything unsure behaves as before.
* **Guard.** Hinglish, Hindi and Marathi injection patterns (a verb of ignoring and a noun of instructions, close together, so
  "mere battery ke niyam kya hain?" passes); leetspeak is folded into a *scoring view only* (`normalize.deleet`), so units and codes
  such as `5kWp`, `E04`, `25%` are never rewritten and the text that is answered is untouched; Hindi/Hinglish follow-up openers; the router
  model sees the last two earlier questions; names and addresses are masked by cue ("my name is", "mera naam", a title, a house
  number followed by a locality word, a labelled PIN). **Limit:** this is rules, not a model. A name or address typed without a cue is
  not masked, and a city after an address is taken only after a comma.

## 4. Documents and retrieval

* **Ingest-side safety** ([safety.py](../../backend/app/modules/knowledge/safety.py)): chunks are masked before embedding and storage,
  scored with the injection patterns (on the original text, so masking cannot hide an attack), and quarantined at or above the guard's
  block score. Quarantined chunks are stored for the reviewer, counted on the document, and excluded from both indexed searches and the
  scan. A document made only of such chunks is refused. v5 and v6 wrap excerpts in `<untrusted_document>` tags.
* **Retrieval** ([ranking.py](../../backend/app/modules/knowledge/ranking.py)): BM25 and Reciprocal Rank Fusion (`RAG_RRF_K`, default 60)
  replace the weighted word overlap. `hnsw.ef_search` and `hnsw.iterative_scan` are set per transaction (`RAG_HNSW_*`), each in its own
  savepoint so an older pgvector skips them quietly. `python -m app.modules.knowledge.reembed --resize-vector` resizes the pgvector
  column to `AI_EMBEDDING_DIMENSIONS` (empties it; the re-embed refills it; JSON embeddings and full-text keep search alive meanwhile).
  **Still needs you:** choose a real embedding model (Y2).

## 5. The device-command outbox (4.3, 4.5)

A confirmed command is now a row in `device_command_outbox`, written in the **same transaction** as the proposal's `queued` status
and its audit event. After the commit the API delivers it at once (`dispatch_now`, a background task), and the `outbox-relay` process
delivers anything left, with retries.

* `command_id` is unique: the same command is never recorded twice, and it is the message id the consumer de-duplicates on.
* The relay claims rows with `FOR UPDATE SKIP LOCKED`, so replicas never take the same row (proven on real Postgres).
* At delivery it refuses: the kill switch (checked then, not only at proposal time), a command older than
  `DEVICE_COMMAND_MAX_AGE_SECONDS`, a corrupt payload (marked dead so it cannot block the queue). After `OUTBOX_MAX_ATTEMPTS` the row is dead,
  the proposal fails with an audit event and the user is notified.
* A written proposal state machine (`device_action/states.py`): a terminal state never changes again, which is what makes at-least-once
  delivery safe. The consumer ignores a command that already ended.
* `DEVICE_COMMAND_OUTBOX_ENABLED=false` restores the old inline publish.

## 6. Automation rules (4.1) and the Monitor (4.4)

* **Engine** (`agents_platform/automation/`): a closed DSL (metrics, comparisons, `all`/`any`/`not`, a schedule, notify and propose actions only),
  a validator that returns coded problems with paths, three-valued logic (unknown never fires and never silently clears), edge-triggered evaluation with
  `for_seconds`, hysteresis, a cooldown and a daily cap, a dry run, and a fingerprint.
* **Storage and lifecycle**: `automation_rule_defs`; `pending_approval` to `active` to `paused`. Approval binds to the fingerprint the user was shown; it
  is refused if the stored rule no longer hashes to it, and the executor re-checks before every run and pauses a rule that no longer matches. Owner
  and home scoped (another user's rule is a 404). Limits: `AI_AUTOMATION_MAX_RULES` (10), `AI_AUTOMATION_MAX_PENDING` (5).
* **API**: `/v2/assistant/automations` (list, propose, get, approve, reject, pause, resume, delete). The plain-language summary on every rule is generated from the
  rule by code, never by a model.
* **What a rule can do**: notify (a fixed template with typed numbers) or write a *suggestion* notification. It never creates a command or an outbox row;
  the user still asks for the action and still confirms it on live data.
* **Worker** (`python -m app.workers.automation`, compose service `automation-worker`): its own short-lived queue (`telemetry.automation`, TTL and length
  bounded, oldest dropped) so a stopped worker cannot make the broker grow and ingest is unaffected. State is kept in memory and written only when a rule's
  phase changes or it fires. **Run exactly one replica.**

## 7. Configuration added

| Setting | Default | Meaning |
|---|---|---|
| `AI_FORECAST/OPTIMIZER/BILL_TARIFF_AGENT_DEADLINE_SECONDS` | 10 / 12 / 6 | per-agent deadline |
| `AI_PLANNING_AGENT_MAX_ATTEMPTS`, `AI_AGENT_RETRY_BACKOFF_SECONDS` | 2, 0.2 | bounded retry |
| `AI_PLUS_ONLY_AGENTS`, `AI_PLUS_ONLY_WHAT_IF` | `optimizer`, true | plan-tier rules |
| `AI_FORECAST_WEATHER_ENABLED`, `AI_FORECAST_WEATHER_TIMEOUT_SECONDS` | false, 4 | Open-Meteo |
| `AI_JUDGE_LLM_PROVIDER`, `AI_JUDGE_LLM_MODEL` | empty | the L3 judge model |
| `AI_AUTOMATION_*` | see above | rule limits, cache, draft attempts |
| `RAG_RRF_K`, `RAG_HNSW_ITERATIVE_SCAN`, `RAG_HNSW_EF_SEARCH` | 60, relaxed_order, 100 | retrieval |
| `AI_MEMORY_MAX_ITEMS`, `..._ITEM_MAX_CHARS`, `..._FACTS_LIMIT`, `..._FACTS_MAX_CHARS` | 50, 200, 8, 600 | long-term memory |
| `DEVICE_COMMAND_OUTBOX_ENABLED`, `OUTBOX_*`, `DEVICE_COMMAND_MAX_AGE_SECONDS` | true, see config | outbox |
| `RABBITMQ_AUTOMATION_*` | queue, 60 s, 5000 | automation worker queue |

New processes: `outbox-relay` and `automation-worker` (both in `docker-compose.yml`).

## 8. Tests

| File | Tests |
|---|---|
| `test_planning_plumbing.py` | 55 |
| `test_explain_and_do.py` | 21 |
| `test_multilingual_guard.py` | 69 |
| `test_knowledge_ingest_safety.py` | 20 |
| `test_knowledge_ranking.py` | 23 |
| `test_forecast_engine.py` / `test_forecast_agent.py` | 52 / 16 |
| `test_optimizer_engine.py` / `test_optimizer_agent.py` | 62 / 24 |
| `test_bill_tariff_engine.py` / `test_bill_tariff_agent.py` | 84 / 28 |
| `test_automation_rules.py` | 355 |
| `test_automation_service.py` / `_api.py` / `_worker.py` | 27 / 8 / 8 |
| `test_device_outbox.py` (+ `_postgres.py`, opt-in) | 23 (+2) |
| `test_planning_agents_api.py` (the whole stack over HTTP) | 10 |
| `test_user_memory.py` | 111 |

Mutation checks (break the code, see a test fail, restore): 17 on the judge, 23 on the forecast engine, 13 on the optimizer, 17 on the tariff engine,
18 on the rule engine, 12 on the memory store. **Not mutation-checked:** the planning plumbing, explain-and-do, the multilingual guard, ingest safety,
BM25/RRF, the outbox and the automation service and worker. Do this before calling them finished.

Not measured at the end of the session: the full-suite total (the run was stopped by request). Verified on a real Postgres (scratch database, dropped afterwards): migrations 0001–0004 apply, downgrade and re-apply with a single head; the indexed
retrieval tests (12); `FOR UPDATE SKIP LOCKED` across two relays; the width resize.

## 9. Known limits

| Limit | Item |
|---|---|
| Memory store is not wired into the supervisor, the maintenance sweep, account deletion or `app/webui.py`; the page was only syntax-checked | 3.4 |
| Forecast intervals are nominal, not validated against actuals; the optimizer treats the forecast as certain and its multi-appliance plan is a coordinate-wise optimum | 3.5 once real data exists |
| Tariff tables are illustrative; net-metering banking is not modelled in the optimizer's export credit | Y11 |
| Names and addresses are masked by cue only; the Hinglish and Hindi cases in the evals are few | 3.10, 3.12 follow-up |
| The DSL `set_level` has no executor (the device worker accepts on, off, schedule); `push` notifications are delivered in-app only | 4.2, 4.4 |
| The automation worker is one replica per home set; scaling out needs partitioning by home | 5.2 |
| `domain/alerts.py` thresholds are copied into the DSL with a parity test, not shared constants | 4.4 |
| The Automation Rule agent, Recommender and Reporter are not written | 4.2, 4.4 |
| Concurrent memory saves can overshoot the cap by the number of simultaneous requests (the rate limit bounds it) | 3.4 |
