# 11 — Roadmap & Suggestions

## 1. Phased plan from today's code

Each phase ends with the eval gate green. v1 endpoints (`/assistant/me/ask`, actions) stay live until v2 is at
parity on the eval suite.

### Phase 0 — Production hardening (1–2 weeks)

| Item | Why |
|---|---|
| `MemorySaver` → `AsyncPostgresSaver` in `actions/graph.py`, proposal TTL + single use | Confirmations break with >1 replica or a restart |
| Redis-backed rate limiting | In-memory limits do not work across replicas |
| OpenTelemetry + trace ids across API, workers, queue headers | You cannot run multi-agent without traces |
| Real multilingual embedding model + HNSW + BM25 fusion | Hashing embedder will not hold up on real questions |
| Numeric grounding output check (G8 #2) | Cheapest, highest-value guardrail; enforces the core principle |
| Move eval cases to JSONL datasets + CI job | Foundation for the release gate |
| Remove committed sample credentials from `.env.example` | Hygiene |

### Phase 1 — Supervisor + first specialists (3–4 weeks)

- Build `orchestration/` supervisor with `Send` fan-out, `join(defer=True)`, synthesizer, output gate.
- Wrap today's assistant graph as **Energy Insight Agent** and knowledge retrieval as **Knowledge Agent**.
- Move `actions/` under **Device Action Agent** (exclusive route).
- Trajectory evals + multi-agent dataset. SSE streaming with status events.
- Thread memory (checkpointer) for follow-up questions.

### Phase 2 — Go performance plane, part 1 (parallel to Phase 1, 3–5 weeks)

- `volta-ingest` in shadow mode → switch.
- `volta-realtime` for dashboard + notifications WS/SSE.
- Shared `proto/` schemas with `buf`.

### Phase 3 — Planning agents (4–6 weeks)

- **Forecast Agent** (ML: solar + load quantile models; physics fallback from `simulator/engines`).
- **Optimizer Agent** (schedule solver + battery strategy).
- **Bill & Tariff Agent** with versioned tariff tables and what-if simulator (backs the frontend "bill simulator").
- Long-term memory (Store) + "What Suryaa remembers" page.

### Phase 4 — Automation & ambient (4–6 weeks)

- `volta-rules` (Go) for alerts and L3 automations; port `domain/alerts.py` with parity tests.
- **Automation Rule Agent** (NL → DSL, validate, dry-run, interrupt to approve). Backs the frontend rule builder.
- `volta-executor` (Go) with outbox + audit.
- **Monitor**, **Recommender**, **Reporter** ambient/batch agents; provider batch APIs.

### Phase 5 — Edge, guard, scale (ongoing)

- `volta-edge` + `volta-guard`, token budgets per tier, policy engine (OPA/Cedar).
- Online judge sampling, canary pipeline, cost dashboards, red-team generator nightly.

## 2. Suggested additions (beyond the brief)

| Idea | Value for Volta | Effort |
|---|---|---|
| **Multilingual + voice** (Hindi, Marathi, Hinglish; speech-to-text and back) | Most Indian households will prefer voice in their language. Rule classifier is English-only today | M |
| **WhatsApp channel** | Main messaging channel in India; alerts + Q&A + action confirmations with buttons | M |
| **MCP server for Volta tools** | Exposes read tools to external agents/installers safely with the same authZ; also lets you test agents with any MCP client | S–M |
| **Installer / B2B console agent** | Fleet view: "which of my 300 installs are underperforming and why?" Same agents, different tenant scope and policies | M |
| **Digital-twin what-if** | Reuse simulator engines for "add battery", "switch to ToD tariff", "move geyser to noon": sells upgrades with numbers | M |
| **Anomaly → warranty claim helper** | Diagnostics + Knowledge + warranty docs → pre-filled claim for the installer | S |
| **Outage preparedness mode** | Forecast + grid outage history → pre-charge battery before likely outages (L2 suggestion, L3 if rule approved) | M |
| **DISCOM tariff watcher** | Batch agent watches tariff orders; when a new one is published, re-runs savings for affected homes | S |
| **Carbon & savings certificate** | Monthly verified report (shareable). Good for retention | S |
| **Federated "homes like mine" benchmarks** | Compare against similar homes (same city, kWp, battery) with privacy-preserving aggregates | M |
| **Feedback-driven prompt tuning loop** | 👎 traces → dataset → prompt PR → eval gate → canary | S |
| **Self-hosted small models for guard/router** | Lower cost and latency, data stays in-region for the most frequent calls | M |

## 3. Decisions to make early

| Decision | Recommendation |
|---|---|
| LangSmith vs Langfuse | Langfuse if data residency / self-hosting matters (DPDP), LangSmith if you want the tightest LangGraph tooling and are fine with SaaS |
| RabbitMQ vs NATS JetStream | Keep RabbitMQ (already running, quorum queues). Add RabbitMQ Streams for the tick stream to the rule engine before adding a second broker |
| Self-host LangGraph runtime vs LangGraph Platform | Self-host on FastAPI + Postgres checkpointer (you already have the pieces). Revisit the platform if you need its assistants/cron/double-texting features out of the box |
| One Python agent service vs one per agent | **One service**, agents as packages/subgraphs. Split only an agent that needs different scaling (e.g. a GPU-backed forecast model server) |
| Go gateway: build vs adopt | Adopt (Envoy/Kong + OPA, an existing Go LLM gateway). Build only budgets and the guard fast-path |
