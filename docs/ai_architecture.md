# Suryaa AI Assistant — System Architecture (Phase 1: read-only energy analysis)

> Goal of this phase: a user asks a question, LangGraph selects the correct
> existing energy tools, the LLM receives compact grounded facts, and Suryaa
> returns an explainable answer **without controlling any device**.

---

## 1. Design principles

| Principle | What it means here |
|---|---|
| **LLM explains, code calculates** | Every number the user sees (surplus, self-consumption, backup hours, savings, appliance cost) is produced by deterministic Python. The LLM only narrates. |
| **No direct DB access for the LLM** | The LLM never sees SQL, Redis, or Timescale. It sees the output of read-only tools that wrap the existing `EnergyService`. |
| **Tenant isolation by construction** | `household_id` is **never** a tool argument. Tools are bound per request to the household the JWT already owns (`require_owned_household` / `require_my_completed_system`). |
| **Deterministic routing first, LLM second** | A rule-based router handles the common questions with zero tokens. The LLM router is only called for ambiguous questions. |
| **Provider-agnostic** | One factory (`init_chat_model`) supports Anthropic Claude, OpenAI, Google Gemini, Groq, Ollama… Switching model = editing `.env`. |
| **Graceful degradation** | No API key, provider outage, or bad structured output → the graph still returns a deterministic, grounded answer (`meta.llm_used=false`). |
| **Read-only phase** | Device-control intents are detected and refused. No write tools exist. |
| **Testable without network** | The LLM sits behind a `StructuredLLM` port. Tests inject a fake; evals assert tool selection and guardrails. |

---

## 2. High-level architecture

```mermaid
flowchart LR
  Client[Dashboard / Mobile] -->|POST /assistant/me/ask  JWT| API[FastAPI assistant router]
  API --> Auth[require_my_completed_system<br/>owner + onboarding check]
  Auth --> SVC[AssistantService]
  SVC --> G[[LangGraph workflow<br/>compiled once per process]]

  subgraph graph [LangGraph]
    C[classify] --> S[select_tools] --> T[call_tools] --> A[analyze]
    A -->|has data + LLM| GEN[generate]
    A -->|no data / no LLM / blocked| FB[fallback]
    GEN --> F[finalize]
    FB --> F
  end

  G --- graph
  T --> Tools[LangChain read-only tools]
  Tools --> GW[EnergyDataGateway<br/>request-scoped, single-flight cache]
  GW --> ES[EnergyService]
  ES --> Redis[(Redis live)]
  ES --> TS[(TimescaleDB)]
  ES --> MEM[(in-memory store)]
  C -. ambiguous only .-> LLMr[Router LLM<br/>cheap model]
  GEN --> LLMa[Answer LLM<br/>quality model + fallback]
```

### Layering

```
HTTP (router.py)            — auth, validation, rate limit, error envelope
Application (service.py)    — builds runtime context, invokes graph, maps result → DTO
Workflow (graph/*)          — LangGraph state machine, conditional routing
Domain (domain/*)           — intents, analytics, freshness, compact context (pure Python, no I/O)
Tools (tools/*)             — LangChain StructuredTools over a memoized data gateway
AI platform (app/ai/*)      — settings, provider registry, model factory, structured-output gateway
Existing energy module      — EnergyService, Redis/Timescale stores (unchanged)
```

Dependencies only point **downwards**. `app/ai` knows nothing about energy; the
assistant module knows nothing about which provider is configured.

---

## 3. Where LangChain is used and why

| Use | Component | Why LangChain |
|---|---|---|
| **Model connection** | `app/ai/llm/factory.py` → `init_chat_model` | One API for Claude / GPT / Gemini / Groq / Ollama; uniform `timeout`, `max_retries`, `max_tokens`. |
| **Structured outputs** | `app/ai/llm/gateway.py` → `with_structured_output(Schema, include_raw=True)` | Provider-native JSON/tool-calling mode, parsed straight into Pydantic. `include_raw` gives token usage for cost tracking. |
| **Fallback models** | `.with_fallbacks([...])` | If the primary provider errors/times out, the fallback provider answers with the same schema. |
| **Prompt templates** | `prompts.py` → `ChatPromptTemplate` | Versioned, testable prompts; variables are injected, not string-concatenated. |
| **Tool definitions** | `tools/registry.py` → `StructuredTool` | Typed args schema, name + description ready for LLM tool-calling in a later phase (agentic mode / MCP), automatic LangSmith tracing of each tool run. |
| **Response parsing** | Pydantic schemas `IntentClassification`, `LLMAnswer` | Parsing errors are caught and degrade to the deterministic answer. |

## 4. Where LangGraph is used and why

LangGraph owns the **workflow** (`graph/builder.py`):

```
START → classify → select_tools → call_tools → analyze ─┬─> generate ─┐
                                                        └─> fallback ─┴─> finalize → END
```

Why a graph instead of a single LangChain agent loop:

1. **Deterministic, auditable steps.** Each node has one responsibility and writes a typed slice of state. The response includes the trace (`intents`, `tools_used`, `analytics_used`).
2. **Conditional routing.** `analyze → generate | fallback` is an explicit edge, so stale/missing data, device-control requests and LLM-unavailable cases never reach the LLM with an unsafe prompt.
3. **Cost control.** A ReAct agent spends 2–4 LLM round-trips choosing tools. This graph spends **0–2 calls** (router only when ambiguous, plus one answer call).
4. **Typed runtime context.** `context_schema=AssistantRuntime` injects the request-scoped gateway, LLM, clock and settings into nodes without globals — the graph is compiled **once** and reused for every request.
5. **Future-proof.** Checkpointers (conversation memory), human-in-the-loop interrupts (device-control approval), and sub-graphs (multi-agent) plug into the same graph later.

---

## 5. LangGraph workflow details

| Node | Type | Does | LLM? |
|---|---|---|---|
| `classify` | router cascade | Rule-based classifier (regex + appliance synonyms). If not confident → cheap router LLM with structured output. If no LLM → `live_overview`. | 0 or 1 (cheap) |
| `select_tools` | planner | Maps intents → `ToolName` set + `Analytic` set (union for multi-intent) plus two flags: `requires_telemetry` (from the primary intent) and `live_advice` (any intent). Device-control → empty plan. | No |
| `call_tools` | I/O fan-out | Runs selected tools **concurrently** (`asyncio.gather`) with per-tool timeout. Live-derived tools share **one** snapshot read (single-flight memo in the gateway). | No |
| `analyze` | deterministic | Builds compact `AIContext`; runs freshness guard (only blocks when the plan `requires_telemetry`); runs selected analytics; detects anomalies; trims the facts to `AI_MAX_FACTS_CHARS` for the prompt. | No |
| `generate` | LLM | One structured-output call → `Observation / Explanation / Recommendation / Estimated impact`. | 1 |
| `fallback` | deterministic | Grounded template answer for: no telemetry, device-control refusal, out-of-scope, LLM unavailable/failed. | No |
| `finalize` | guardrail | Stamps `data_time` from telemetry (never from the LLM), forces low confidence + warning on stale data, attaches trace & usage. | No |

### Optimisations applied

- **Router cascade**: ~90% of questions classified by rules → zero router tokens and ~0 ms.
- **Two-tier models**: router role can use a cheaper model than the answer role (`AI_ROUTER_LLM_*`).
- **Single-flight memoisation**: battery, grid, devices, weather, live are projections of the same latest tick → 1 Redis/Timescale read per request instead of 5.
- **Parallel tool execution** with timeouts; a slow tool degrades to `unavailable`, never blocks the answer.
- **Compact context**: rounded numbers, `exclude_none`, only non-empty hourly buckets, only devices drawing power → small prompts.
- **Graph compiled once** (`lru_cache`), chains with structured output cached per `(role, schema)`.
- **Bounded output**: `AI_LLM_MAX_OUTPUT_TOKENS`, low temperature, provider timeouts + retries.
- **No LLM on unsafe paths**: stale-data and blocked intents skip or constrain the LLM.
- **Telemetry-aware plans**: setup questions (`home_profile`) answer from onboarding data even when the simulator is off; stale-data warnings and the low-confidence downgrade apply only to plans that give live advice.
- **Per-question field projection** (`domain/projection.py`): tools decide which sections exist; each intent's plan also declares `Detail` groups (hardware, battery limits, grid contract, billing, appliances, lifetime, weather detail, device detail). Fields outside the requested groups, identifiers, per-section timestamps, `preferences`, and values repeated between facts and analytics are not sent. Core numbers are always kept. Measured: −29% to −46% input tokens per question.
- **Modular system prompt**: a short static core (~320 tokens) plus per-intent answer guides and field notes appended only when the intent or field is present. Output-field guidance lives in the `AnswerDraft` schema (Gemini does not count the response schema as input tokens).
- **Facts budget** (`fit_to_budget`): if the facts JSON exceeds `AI_MAX_FACTS_CHARS` (default 6000 ≈ 1.5k tokens), detail is dropped in this order until it fits — hourly buckets, idle devices, weather detail, appliance detail, live warnings. Core numbers are never dropped. The untrimmed context is still used by the deterministic fallback.

### Intent → plan (summary)

| Intent | Tools | Needs telemetry |
|---|---|---|
| `home_profile` ("my home details", "how many panels", "which DISCOM") | household profile + live state (for lifetime totals, location) | No |
| `full_overview` ("tell me everything", "full report") | all 9 tools + surplus, self-consumption, independence, backup, savings, anomalies | No (degrades to setup-only) |
| `solar_production`, `battery`, `usage_pattern` | domain tools + `get_recent_trend` | Yes |
| `appliance_timing`, `grid_import`, `backup`, `savings`, … | as in phase 1 | Yes |
| `device_control`, `out_of_scope` | none (deterministic refusal) | No |

---

## 6. Data contracts

### Compact AI context (what the LLM sees)

```json
{
  "household":     { "system_type": "Hybrid", "location": "Pune, India", "panel_type": "Monocrystalline", "panel_qty": 10,
                     "solar_capacity_kwp": 4.0, "inverter_brand": "Growatt", "inverter_capacity_kw": 5.0,
                     "battery_capacity_kwh": 10, "battery_usable_capacity_kwh": 8, "battery_minimum_soc_percent": 20,
                     "battery_backup_hours_target": 4, "battery_max_charge_kw": 2.5, "battery_max_discharge_kw": 3.0,
                     "meter_type": "Net metering", "discom": "MSEDCL", "sanctioned_load_kw": 5.0,
                     "tariff_type": "Flat rate", "tariff_rate": 8.5, "export_credit_inr_per_kwh": 6.2, "avg_monthly_bill_inr": 3500,
                     "primary_goal": "maximize_self_consumption", "appliances": [ ... ] },
  "live_energy":   { "timestamp": "...", "location": "Pune, India", "timezone": "Asia/Kolkata", "solar_kw": 3.0, "load_kw": 1.5,
                     "flows_kw": {...}, "solar_lifetime_kwh": ..., "consumption_lifetime_kwh": ..., "data_quality": "simulated" },
  "battery":       { "soc_percent": 62, "status": "charging", "charge_kw": 1.5, "soh_percent": 98, "temperature_c": 32, ... },
  "grid":          { "status": "available", "import_kw": 0, "export_kw": 0, "lifetime_import_kwh": ..., "lifetime_export_kwh": ... },
  "devices":       [ { "name": "Refrigerator", "state": "on", "current_power_kw": 0.12, "priority": "critical" } ],
  "weather":       { "condition": "clear sky", "cloud_cover_percent": 20, "temperature_c": 31, "humidity_percent": 40,
                     "shortwave_radiation_wm2": 700, "direct_radiation_wm2": ..., "wind_speed_kmh": ..., "source": "open-meteo" },
  "recent_trend":  { "window_minutes": 60, "samples": 12, "solar_kw_start": 2.1, "solar_kw_now": 3.0, "solar_direction": "rising",
                     "battery_soc_change_percent": 6.0, "grid_import_kwh": 0.0, ... },
  "today_summary": { "solar_generation_kwh": ..., "grid_import_kwh": ..., "estimated_savings": ..., "savings_method": "..." },
  "preferences":   { "primary_goal": "...", "device_control_enabled": false }
}
```

Plus `analytics` (deterministic results) and `data_freshness`. This is the **full** context (used by the
deterministic fallback and the trace). The prompt receives a projection of it: only the sections for the
planned tools, only the `Detail` groups the intent declared, and no per-section timestamps or duplicates.

### Data-minimisation policy

The model sees every **energy-relevant** field the user entered in onboarding and every field the simulator
produces. It deliberately does **not** see identity data: user name, email, user id, `household_id`, and GPS
coordinates (the city/region text is enough for advice). Battery `fault_code` 0 means "healthy" and is
normalised to `null` at the energy-service boundary, so it never reaches the prompt or triggers a false fault warning.

### API response

```json
{
  "answer": {
    "observation": "What the data shows.",
    "explanation": "Why it is happening.",
    "recommendation": "What the user can do.",
    "estimated_impact": "Possible energy or cost effect.",
    "data_time": "2026-10-02T12:30:00+05:30"
  },
  "confidence": "high | medium | low",
  "warnings": ["Live data is 42 minutes old ..."],
  "intents": ["grid_import"],
  "tools_used": ["get_live_energy_state", "get_grid_status", ...],
  "analytics_used": ["calculate_solar_surplus", ...],
  "data_freshness": { "status": "fresh|stale|missing", "age_seconds": 12 },
  "meta": { "llm_used": true, "model": "gemini-3.5-flash-lite", "classification_method": "rules", "latency_ms": 840, "usage": {...}, "request_id": "..." }
}
```

---

## 7. Folder structure

```
backend/app/
├── ai/                                  # Reusable AI platform layer (domain-agnostic)
│   ├── config.py                        # AISettings: providers, models, keys (SecretStr), budgets
│   ├── errors.py                        # LLMNotConfiguredError, LLMInvocationError
│   └── llm/
│       ├── providers.py                 # Provider registry (anthropic, openai, google_genai, groq, ollama)
│       ├── factory.py                   # ChatModelFactory: role → BaseChatModel (+ fallback)
│       └── gateway.py                   # StructuredLLM port + LangChain implementation (usage capture)
└── modules/assistant/                   # Suryaa energy assistant (feature module)
    ├── router.py                        # POST /assistant/me/ask, POST /assistant/{household_id}/ask
    ├── schemas.py                       # AskRequest, AssistantResponse, AssistantAnswer
    ├── service.py                       # AssistantService (use case)
    ├── deps.py                          # DI providers
    ├── prompts.py                       # Versioned ChatPromptTemplates
    ├── domain/
    │   ├── intents.py                   # Intent enum, rule-based classifier, appliance matcher
    │   ├── context.py                   # Compact AI context models + builders + facts budget
    │   ├── projection.py                # Per-question field projection (Detail groups, de-duplication)
    │   ├── freshness.py                 # Stale-data guard
    │   └── analytics.py                 # Deterministic energy calculators
    ├── tools/
    │   ├── gateway.py                   # EnergyDataGateway (request-scoped, single-flight cache)
    │   └── registry.py                  # 9 read-only LangChain tools (incl. get_recent_trend)
    └── graph/
        ├── state.py                     # AssistantState + AssistantRuntime
        ├── planner.py                   # Intent → tools + analytics plan
        ├── nodes.py                     # Node implementations
        └── builder.py                   # StateGraph wiring + compile
```

---

## 8. Model & API-key strategy

| Tier | Recommended (Oct 2026) | Provider id | Env key | Approx. price / 1M tokens (in / out) |
|---|---|---|---|---|
| **Cheapest – start here** | `gemini-3.5-flash-lite` | `google_genai` | `GOOGLE_API_KEY` (AI Studio, free tier available) | check AI Studio |
| Cheap, newer | `gemini-3.1-flash-lite` (check exact id in AI Studio) | `google_genai` | `GOOGLE_API_KEY` | ~$0.25 / $1.50 |
| OpenAI budget | `gpt-5-mini` / latest mini | `openai` | `OPENAI_API_KEY` | ~$0.20–0.75 / $1.25–4.50 |
| **Claude (target)** | `claude-haiku-4-5` | `anthropic` | `ANTHROPIC_API_KEY` | $1 / $5 |
| Claude quality | latest Sonnet (e.g. `claude-sonnet-4-5`) | `anthropic` | `ANTHROPIC_API_KEY` | ~$3 / $15 |

Prices move often — verify on the provider pricing page before production.

Recommended rollout:

1. **Now:** `AI_LLM_PROVIDER=google_genai`, `AI_LLM_MODEL=gemini-3.5-flash-lite`, set `GOOGLE_API_KEY`.
2. **When Claude key arrives:** `AI_LLM_PROVIDER=anthropic`, `AI_LLM_MODEL=claude-haiku-4-5` (answers), keep Gemini as router: `AI_ROUTER_LLM_PROVIDER=google_genai`, `AI_ROUTER_LLM_MODEL=gemini-3.5-flash-lite`, and Gemini as fallback.
3. **Observability (optional):** `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY=...`, `LANGSMITH_PROJECT=suryaa-assistant`.

---

## 9. Security & safety

- JWT + household ownership enforced by existing dependencies before the graph runs.
- `household_id` bound in a closure → prompt injection cannot read another tenant's data.
- Question length capped (`AI_MAX_QUESTION_CHARS`), rate limited (`RATE_LIMIT_ASSISTANT`).
- System prompt: treat the question as untrusted, never invent numbers, never claim to control devices.
- API keys are `SecretStr`, never logged; logs carry `request_id`, intents, tools, latency, tokens only.
- Device control intent → deterministic refusal. No write tools are registered.
- PII never enters prompts (name, email, ids, GPS coordinates) — see the data-minimisation policy above.

## 10. Explicitly out of scope (later phases)

RAG, MCP, automatic device control, multi-agent, ML forecasting, real hardware
integrations, conversation memory (LangGraph checkpointer). The graph and tool
registry are shaped so each of these is additive.
