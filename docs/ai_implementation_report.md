# Suryaa AI Assistant — Phase 1 Implementation Report

Status: **complete** · Date: 2026-10-03 · Prompt version: `2026-10-03.v3` (§13 full-context update, §14 token optimisation)

Companion doc: [ai_architecture.md](./ai_architecture.md) (design, diagrams, principles).
This report records **what was built, how to run it, and how it was verified**.

---

## 1. Outcome

Milestone reached: a user asks a question, LangGraph selects the correct read-only
tools, deterministic analytics compute every number, the LLM explains those facts,
and the API returns a grounded answer in the format
**Observation / Explanation / Recommendation / Estimated impact / Data time** —
without controlling any device.

| Check | Result |
|---|---|
| Backend test suite | **154 passed** (61 existing + 93 assistant), 0 failures |
| Linter | 0 errors in `app/ai`, `app/modules/assistant`, `app/main.py` |
| Works with no API key | Yes — deterministic mode (`/assistant/status` → `"mode": "deterministic"`) |
| Provider-agnostic | Google Gemini, Anthropic, OpenAI, Groq, Ollama via one env switch |
| LLM has DB access | **No** — only 9 read-only backend tools, household bound server-side |
| Device control | Refused by rules before any tool or LLM call |

---

## 2. What was built

### 2.1 Platform layer — `backend/app/ai/` (reusable, feature-agnostic)

| File | Responsibility |
|---|---|
| `config.py` | `AISettings` (pydantic-settings, `SecretStr` keys). Roles `router` / `answer`, optional fallback model. |
| `llm/providers.py` | Provider registry: package, env key, provider quirks (`num_predict` for Ollama, `base_url` support). |
| `llm/factory.py` | `ChatModelFactory` → LangChain `init_chat_model`; caches models; validates package + key. |
| `llm/gateway.py` | `StructuredLLM` port + `LangChainStructuredLLM` adapter: structured output, fallbacks, token usage, latency, error wrapping. |
| `observability.py` | Exports LangSmith settings to the process env at startup (tracing is opt-in). |
| `errors.py` | `LLMNotConfiguredError`, `LLMInvocationError`. |

### 2.2 Feature module — `backend/app/modules/assistant/`

| Area | Files | Responsibility |
|---|---|---|
| Domain (pure Python, no LangChain) | `domain/intents.py` | 13 intents, regex rule classifier, appliance synonyms (geyser → water heater), device-control guard |
| | `domain/context.py` | Compact AI context: household, live, battery, grid, devices, weather, today, hourly, preferences |
| | `domain/freshness.py` | Fresh / stale / missing against wall clock (default 600 s) |
| | `domain/analytics.py` | All deterministic calculations + anomaly detection + appliance-run verdict |
| | `domain/answers.py` | Answer schema and deterministic fallback answers |
| Prompts | `prompts.py` | Versioned `ChatPromptTemplate`s (router + answer), grounding rules, XML-tagged compact JSON |
| Tools | `tools/gateway.py` | Request-scoped, single-flight memoised reads over `EnergyService` |
| | `tools/registry.py` | 8 LangChain `StructuredTool`s |
| Graph | `graph/state.py`, `planner.py`, `nodes.py`, `builder.py` | LangGraph `StateGraph`, intent → tool/analytics plan, nodes, wiring |
| API | `schemas.py`, `service.py`, `deps.py`, `router.py` | HTTP contract, orchestration, DI, endpoints |

### 2.3 Read-only tools (step 1 of the plan)

| Tool | Source | Notes |
|---|---|---|
| `get_live_energy_state` | `EnergyService.get_live` | |
| `get_battery_status` | projection of live | no extra fetch |
| `get_grid_status` | projection of live | no extra fetch |
| `get_device_readings` | projection of live | no extra fetch |
| `get_weather_data` | projection of live | no extra fetch |
| `get_daily_energy_summary` | `EnergyService.get_daily` | optional `date` |
| `get_hourly_energy_summary` | `EnergyService.get_hourly` | optional `date` |
| `get_household_profile` | onboarding `build_simulator_profile` | tariff, goal, battery, appliances |

`household_id` is **never** a tool argument — it is bound from the authenticated
request, so a prompt injection cannot read another home.

### 2.4 Deterministic analytics (step 6)

`calculate_solar_surplus`, `calculate_solar_self_consumption`,
`calculate_energy_independence`, `calculate_backup_duration`,
`calculate_estimated_savings`, `detect_energy_anomalies`, plus
`evaluate_appliance_run` for "should I run X?" questions.

Anomaly codes: `data_quality_degraded`, `data_quality_fallback`,
`energy_balance_mismatch`, `unserved_load`, `load_near_inverter_limit`,
`solar_underperforming`, `battery_fault`, `battery_hot`,
`battery_below_reserve`, `grid_outage`, `export_in_zero_export_mode`,
`importing_with_battery_available`, `exporting_while_battery_not_full`.

Appliance verdicts: `run_now_on_solar`, `run_with_battery_support`,
`wait_for_solar`, `grid_import_required`, `not_recommended`,
`already_running`, `insufficient_data`.

### 2.5 LangGraph flow (step 5)

```
START → classify → select_tools ─┬─► call_tools ─► analyze ─┬─► generate ─┬─► finalize → END
                                 └──(no tools)──► analyze   └─► fallback ◄┘      ▲
                                                                   └─────────────┘
```

| Node | Uses LLM? | What it does |
|---|---|---|
| `classify` | Only if rules are not confident (cheap router model) | Intent(s) + appliance |
| `select_tools` | No | Deterministic intent → tools + analytics plan |
| `call_tools` | No | Parallel `asyncio.gather`, per-tool timeout, status per tool |
| `analyze` | No | Builds context, freshness, analytics, warnings; picks generate vs fallback |
| `generate` | Yes (answer model) | Structured `AnswerDraft` from facts only |
| `fallback` | No | Deterministic answer (refusal, no data, no key, LLM error) |
| `finalize` | No | Stale-data prefix, `data_time`, confidence |

### 2.6 HTTP API

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/assistant/status` | Bearer JWT | mode, configured models, availability (no secrets) |
| POST | `/assistant/me/ask` | Bearer JWT | primary home (`?household_id=` optional); rate limited |
| POST | `/assistant/{household_id}/ask` | Bearer JWT | owned home only (404 otherwise); rate limited |

Request: `{"question": "Should I run the geyser?"}` (2–500 chars).
Optional `X-Request-ID` header is echoed in `meta.request_id` for log correlation.

---

## 3. Where LangChain and LangGraph are used, and why

| Concern | Library | Where | Why |
|---|---|---|---|
| Model connection | LangChain `init_chat_model` | `app/ai/llm/factory.py` | One call signature for every provider; switch with env only |
| Structured output | LangChain `with_structured_output(include_raw=True)` | `app/ai/llm/gateway.py` | Pydantic-validated answer + token usage, no regex parsing |
| Failover | LangChain `.with_fallbacks()` | `app/ai/llm/gateway.py` | Provider outage → secondary model automatically |
| Prompt templates | LangChain `ChatPromptTemplate` | `assistant/prompts.py` | Versioned, testable, static system prefix (provider prompt caching) |
| Tools | LangChain `StructuredTool` | `assistant/tools/registry.py` | Typed args, standard interface; ready for tool-calling agents later |
| Tracing | LangSmith (via LangChain env) | `app/ai/observability.py` | Per-node traces, tags, metadata, cost |
| Orchestration | LangGraph `StateGraph` | `assistant/graph/` | Explicit, auditable branches (refuse / no data / no key / LLM error), typed state with reducers, runtime context injection, compiled once |

Deliberate choice: **tool selection is deterministic** (intent → plan), not an
LLM tool-calling loop. For a fixed set of 8 read-only tools this is faster
(one fewer LLM round-trip), cheaper, and 100 % reproducible for the eval cases.
The tools are still LangChain tools, so a tool-calling agent can be added later
without rewriting them.

---

## 4. Measured efficiency

Prompt size, estimated at ~4 characters per token, from a real run:

| Question | System | Facts + question | Total input |
|---|---|---|---|
| What is my current battery status? | ~314 | ~460 | **~770** |
| What is happening in my home right now? | ~314 | ~556 | ~870 |
| Why am I importing electricity? | ~314 | ~557 | ~870 |
| Should I run the geyser? | ~314 | ~736 | ~1,050 |

- Router prompt: ~370 tokens, and it is **skipped** for rule-confident questions (most).
- Answer output is capped by `AI_LLM_MAX_OUTPUT_TOKENS=800`; typical answer is ~150–300.
- Approximate cost on `gemini-3.5-flash-lite` (check current AI Studio pricing):
  **≈ $0.0002 per question (~5,000 questions per US$1)**.
- Tool latency in-process: 0–2 ms each; one live read serves 5 tools (single-flight).
- Device-control refusal and no-data replies cost **0 tokens**.

---

## 5. Model and API-key recommendation

Start with the cheapest key; switching later is an `.env` change only.

| Use | `AI_LLM_PROVIDER` | `AI_LLM_MODEL` | Key | Approx. price / 1M tokens (in / out) |
|---|---|---|---|---|
| **Start here (cheapest)** | `google_genai` | `gemini-3.5-flash-lite` | `GOOGLE_API_KEY` | check AI Studio |
| Newer cheap Gemini | `google_genai` | `gemini-3.1-flash-lite` (verify exact ID in AI Studio) | `GOOGLE_API_KEY` | ~$0.25 / $1.50 |
| Production answer quality | `anthropic` | `claude-haiku-4-5` | `ANTHROPIC_API_KEY` | $1 / $5 |
| Premium answers | `anthropic` | current Sonnet model ID | `ANTHROPIC_API_KEY` | higher |
| OpenAI option | `openai` | `gpt-5-mini` | `OPENAI_API_KEY` | low |

Recommended production split: cheap router (`AI_ROUTER_LLM_*` = Gemini Flash-Lite),
better answer model (`AI_LLM_*` = Claude Haiku), and a cross-provider fallback
(`AI_FALLBACK_LLM_*`). Prices and model IDs change often — verify on the
provider pricing page before going live.

---

## 6. Setup

```bash
cd backend
source venv/bin/activate
pip install -r requirements.txt        # AI packages are already installed in venv
cp .env.example .env                   # only if .env does not exist yet
```

Add to `backend/.env` (cheapest path):

```env
AI_ENABLED=true
AI_LLM_PROVIDER=google_genai
AI_LLM_MODEL=gemini-3.5-flash-lite
GOOGLE_API_KEY=<key from https://aistudio.google.com/apikey>
```

Optional tracing:

```env
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<key>
LANGSMITH_PROJECT=suryaa-assistant
```

Run and try it (API + ingest worker + simulator running so live data exists):

```bash
uvicorn main:app --reload

curl -s localhost:8000/assistant/status -H "Authorization: Bearer $TOKEN"

curl -s -X POST localhost:8000/assistant/me/ask \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"Should I run the geyser?"}'
```

Without a key the endpoint still works and returns deterministic answers
(`meta.llm_used=false`, `meta.fallback_reason="llm_unavailable"`).

---

## 7. Sample response (real run, deterministic mode)

Home: 10 panels, 5 kW inverter, 10 kWh battery (20 % reserve), net metering, ₹8.50/kWh.

```json
{
  "question": "Should I run the geyser?",
  "answer": {
    "observation": "Water heater is rated about 2.00 kW; current solar surplus is 1.80 kW. Solar is producing 3.20 kW and the home is using 1.40 kW, battery at 62% (charging).",
    "explanation": "Solar exceeds home demand by 1.80 kW, so the extra charges the battery or flows to the grid. Solar covers 90%; the battery has 4.20 kWh above reserve to cover the remaining 0.20 kW for about an hour.",
    "recommendation": "You can run it now; solar plus battery can cover it without grid import.",
    "estimated_impact": "No grid import expected while it runs.",
    "data_time": "2026-10-02T18:16:43+05:30"
  },
  "confidence": "high",
  "intents": ["appliance_timing"],
  "appliance": "water_heater",
  "tools_used": ["get_live_energy_state", "get_battery_status", "get_device_readings", "get_weather_data", "get_household_profile"],
  "analytics_used": ["calculate_solar_surplus", "calculate_backup_duration", "detect_energy_anomalies", "evaluate_appliance_run"],
  "data_freshness": {"status": "fresh", "age_seconds": 0, "threshold_seconds": 600},
  "meta": {"llm_used": false, "classification_method": "rules", "fallback_reason": "llm_unavailable", "latency_ms": 11}
}
```

With a key configured, the same facts and analytics are sent to the LLM, which
rewrites them in natural language. It is instructed not to introduce new numbers,
and `meta` then reports `model`, `usage` tokens and `llm_used=true`.

---

## 8. Eval results (step 8)

All evals run over HTTP with a fake LLM injected through
`app.dependency_overrides[get_structured_llm]` (no network, deterministic).

| Eval | Assertion | Result |
|---|---|---|
| "What is my current battery status?" | uses `get_battery_status` | pass |
| "Why am I exporting power?" | uses `get_live_energy_state` + `get_daily_energy_summary` | pass |
| "Should I run the geyser?" | uses live (solar), battery, devices, profile (tariff); appliance = water heater | pass |
| Stale data (tick older than 10 min) | `data_freshness.status=stale`, warning, confidence `low`, recommendation prefixed with a caution | pass |
| "Turn off the AC" | refused, 0 tools, 0 LLM calls | pass |
| No telemetry yet | "no data" answer, LLM not called | pass |
| No API key | deterministic answer, `llm_used=false` | pass |
| LLM raises | deterministic answer, `fallback_reason=llm_error` | pass |
| Ambiguous question | router LLM used, `classification_method=llm` | pass |
| Prompt size | compact facts only, no raw ticks, no `household_id` | pass |
| Another user's home | 404 | pass |

Test files: `tests/test_assistant_analytics.py`, `test_assistant_intents.py`,
`test_assistant_llm.py`, `test_assistant_tools.py`, `test_assistant_api.py`,
helpers in `tests/assistant_helpers.py`.

```bash
cd backend && venv/bin/python -m pytest -q          # 127 passed
venv/bin/python -m pytest -q tests/test_assistant_*  # 66 passed
```

---

## 9. Data readiness (checklist from the plan)

Every field the plan asked for already exists in the energy API, so no backend
data changes were required:

| Group | Fields used |
|---|---|
| Live snapshot | `timestamp`, `data_quality`, solar / load / battery charge & discharge / grid import & export kW, battery SOC & status, grid availability, devices, weather, `peak_load_kw` |
| Daily summary | solar, load, import, export, charge, discharge kWh, `peak_load_kw`, `estimated_savings` |
| Household profile | panels, inverter kW, battery kWh and reserve, `tariff_rate`, export credit, meter type, `primary_goal`, critical / flexible appliances (`device_priority`) |

---

## 10. Dependency changes (venv only)

| Package | Version |
|---|---|
| langchain | 1.4.3 |
| langchain-core | 1.6.6 |
| langgraph | 1.2.12 |
| langchain-google-genai | 4.4.0 |
| langchain-anthropic | 1.7.5 |
| langchain-openai | 1.6.7 |
| pydantic | 2.10.4 → **2.13.5** (required by langchain-core 1.6) |
| websockets | 17 → **16.1.1** (pulled by google-genai; existing WS tests pass) |

All installed into `backend/venv`; nothing installed globally.

---

## 11. Security and safety

- LLM never sees credentials, DB handles, `household_id`, or raw ticks.
- Tenant isolation: ownership is checked by `require_owned_household` /
  `require_my_completed_system` **before** the graph runs; tools are bound to that home.
- Read-only: no tool writes; device-control requests are refused by rules.
- Prompt injection: the question is wrapped as untrusted input; the model can only
  rephrase supplied facts, and the output is schema-validated.
- Secrets are `SecretStr`; `/assistant/status` never returns keys; logs record
  question **length**, not text.
- Abuse / cost: `RATE_LIMIT_ASSISTANT=20/minute`, question ≤ 500 chars,
  output token cap, per-tool and per-LLM timeouts, bounded retries,
  `recursion_limit=20`.

---

## 12. Known limitations and next steps

| Limitation | Suggested next step |
|---|---|
| Date words like "yesterday" are not parsed; daily/hourly default to today | Add a date-resolution step in `classify` (rules first, LLM fallback) |
| No conversation memory (each question is independent) | LangGraph checkpointer (Postgres / Redis) keyed by user + thread |
| Rule classifier patterns are English-only | Add Hindi / Marathi synonyms; or route low-confidence to the router LLM (already supported) |
| Model IDs and prices change | Verify the IDs in the provider console; fallback model covers a bad ID |
| Rate limiting is in-memory | Back slowapi with Redis for multi-instance |
| No streaming | Expose `graph.astream` via SSE once the frontend needs typing effect |
| No offline eval set against real models | Add a LangSmith dataset with the 4 evals and run on each prompt version |

Phase 2 candidates (deliberately not built now): RAG over manuals / tariffs,
MCP server, device control with explicit confirmation, multi-agent, ML
forecasting, real hardware integration.

---

## 13. Update 2026-10-03 — full household context (prompt `2026-10-03.v2`)

Goal: the model can use **all** onboarding data and **all** simulator data, while
staying cheap, grounded and privacy-safe.

### 13.1 What changed

| Area | Change |
|---|---|
| Onboarding facts | Added panel type and count, inverter brand, average monthly bill, battery backup-hours target, battery max charge/discharge kW, sanctioned load, DISCOM (`gateway.onboarding_details()` reads them; no identity fields). |
| Simulator facts | Added location, timezone, lifetime solar/consumption/import/export, energy-balance error (only when invalid), weather condition text (WMO code), humidity, precipitation, wind, direct/diffuse radiation, weather source, savings method. |
| New tool | `get_recent_trend` — last `AI_TREND_WINDOW_MINUTES` (60) of history: solar start/now/max and direction, average/max load, battery SOC change, interval grid import/export. |
| New intents | `home_profile` (setup questions, works with the simulator off) and `full_overview` ("tell me everything" — all tools and analytics). |
| Plan flags | `requires_telemetry` (primary intent) and `live_advice` (any intent) gate the no-data refusal, stale warning and confidence downgrade. |
| Token budget | `fit_to_budget` trims facts above `AI_MAX_FACTS_CHARS` (6000) in a fixed order; core numbers are kept. |
| Privacy | Name, email, user/household ids and GPS coordinates are never in the prompt (asserted in tests). |
| Bug fixes | Battery `fault_code` 0 is "healthy", no longer a critical warning. The impact line now quotes analytics ("No grid import expected") instead of "Not enough data". |
| Prompt v2 | Per-intent rules for home details and full overview, impact rule, field glossary, and a ban on raw field names in answers. |
| Deterministic mode | Fallback answers for home details and full overview, so these work without an API key. |

### 13.2 Live check (Gemini `gemini-3.5-flash-lite`, 2026-10-03)

| Question | Intent | Input / output tokens | Latency | Result |
|---|---|---|---|---|
| What are my home details? (no telemetry) | `home_profile` | 1038 / 179 | ~2.0 s | Full setup: location, panels, kWp, inverter, battery and reserve, backup target, DISCOM, sanctioned load, tariff, export credit, bill, goal. |
| Tell me everything about my home | `full_overview` | 2462 / 220 | ~1.9 s | Setup plus live state, today's totals, recent trend, surplus recommendation, savings impact. |
| Should I run the washing machine now? | `appliance_timing` | 1837 / 110 | ~1.8 s | Confidence high, no false fault warning, impact "No grid import expected". |

### 13.3 Tests

`tests/test_assistant_home_context.py` (20 tests): routing for 11 setup questions and
3 overview questions, plan flags, fault-code normalisation, trend computation,
budget trimming without mutating input, a no-key home-details answer, and a
full-overview prompt check that confirms the enriched fields are present and that
PII is absent.

---

## 14. Update 2026-10-03 — input-token optimisation (prompt `2026-10-03.v3`)

### 14.1 Where the tokens went (measured on Gemini, washing-machine question)

| Part | Tokens | Share |
|---|---|---|
| System prompt (every rule for every intent) | 626 | 33% |
| Facts + analytics + freshness JSON | ~1,263 | 67% |
| Structured-output schema | 0 (not counted by Gemini) | 0% |
| Thinking tokens | 0 | — |

Prompt caching does not help at this size: Gemini and Claude only cache prefixes of 1,024+ tokens. The right
lever is sending less, not caching more.

### 14.2 Techniques applied

| Technique | What it removes |
|---|---|
| Per-question field projection (`Detail` groups declared per intent in the planner) | Setup fields an answer does not use, e.g. panel brand, DISCOM and bill for "run the washing machine?" |
| Structural de-duplication | 5 per-section timestamps, `preferences` (repeats `primary_goal`), device `id`/`type`/`critical` (priority already says it), weather `source`, repeated `data_quality`, energy-balance warnings that repeat `energy_balance_*` |
| Analytics de-duplication | Analytics inputs already in facts (solar, load, SOC, reserve, tariff, today's kWh), anomaly codes; every derived value is kept |
| Compact freshness | Only `status`, `age_seconds` and the stale message (data time is stamped by `finalize`) |
| Modular system prompt | Home-profile and full-overview guides and field notes are sent only when relevant; output-field guidance moved into the schema |
| Format | Sunrise/sunset as `HH:MM` instead of full ISO timestamps |

Unchanged on purpose: compact JSON (models read it reliably), English field names (renaming to short codes
would hurt grounding), the facts budget as a last-resort safety net, and the full untrimmed context for the
deterministic fallback.

### 14.3 Result (same data, input tokens estimated from the measured chars-per-token ratios)

| Question | Before (chars → tokens) | After (chars → tokens) | Saving |
|---|---|---|---|
| What are my home details? | 4,372 → ~1,240 | 3,092 → ~880 | −29% |
| Should I run the washing machine now? | 6,223 → ~1,890 | 3,333 → ~1,020 | −46% |
| Why am I importing from the grid? | 5,485 → ~1,630 | 2,949 → ~890 | −46% |
| How is my battery doing? | 5,296 → ~1,560 | 2,930 → ~870 | −44% |
| Tell me everything about my home | 7,598 → ~2,380 | 5,362 → ~1,690 | −29% |

Live confirmation (Gemini `gemini-3.5-flash-lite`, washing-machine question): **1,889 → 977 input tokens (−48%)**,
121 output tokens. The answer quoted the analytics values, flagged the balance anomaly, and gave the impact as
"No grid import expected".

### 14.4 Tests

`tests/test_assistant_projection.py` (7 tests): core fields kept and duplicates dropped without mutating
input, detail groups opt fields back in, analytics de-duplication keeps derived values, compact freshness,
planner detail declarations, conditional guidance, and an API test proving the appliance prompt omits setup
fields while the home-details prompt includes them.
