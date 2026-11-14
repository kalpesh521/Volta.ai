# 11 — Roadmap & Suggestions

**Decision (2026-11-09): the whole system is built in Python (FastAPI + LangGraph).** Go is not part of this
roadmap. [06-golang-performance-plane.md](./06-golang-performance-plane.md) is kept as a future option. Revisit
it only if the measurements in §3 show a Python service cannot meet its target.

## 1. Phased plan from today's code

Each phase ends with the eval gate green. v1 endpoints (`/assistant/me/ask`, actions) stay live until v2 is at
parity on the eval suite.

### Phase 0 — Production hardening ✅ implemented (2026-11-09)

Details, configuration and operations: [12-phase0-implementation.md](./12-phase0-implementation.md).

| Item | Status | Why |
|---|---|---|
| `MemorySaver` → `AsyncPostgresSaver`, proposal TTL + single use | ✅ | Confirmations broke with >1 replica or a restart |
| Redis-backed rate limiting, per user | ✅ | In-memory, per-IP limits do not work across replicas |
| OpenTelemetry + request/trace ids across API, workers, queue headers | ✅ | You cannot run multi-agent without traces |
| Real embedder options + indexed vector **and** full-text retrieval + re-embed job | ✅ | The HNSW index existed but was never queried; relevant chunks were silently skipped past 400 |
| Numeric grounding output check (G8 #2) | ✅ | Cheapest, highest-value guardrail; enforces the core principle |
| Eval cases as validated JSONL + runner + CI | ✅ | Foundation for the release gate |
| Remove committed sample credentials from `.env.example` | ✅ (rotate the password: it is still in git history) | Hygiene |
| Baseline metrics (ingest lag, msgs/s, CPU, WS count, memory) | ✅ Prometheus | Data to decide on any later optimisation |
| JWT clock-skew leeway | ✅ added (found while testing) | Tokens were rejected as "issued in the future" when a clock stepped back |

### Phase 1 — Supervisor + first specialists ✅ implemented (2026-11-11); `actions/` moved and real-model parity checked 2026-11-12

Details, configuration and known limits: [14-phase1-implementation.md](./14-phase1-implementation.md).

- Build `orchestration/` supervisor with `Send` fan-out, `join(defer=True)`, synthesizer, output gate.
- Wrap today's assistant graph as **Energy Insight Agent** and knowledge retrieval as **Knowledge Agent**.
- Move `actions/` under **Device Action Agent** (exclusive route).
- Trajectory evals + multi-agent dataset. SSE streaming with status events.
- Thread memory (checkpointer) for follow-up questions.

### Phase 2 — Guardrails & platform in Python ✅ implemented (2026-11-12)

Details, configuration and known limits: [15-phase2-implementation.md](./15-phase2-implementation.md). Two deliberate differences from
the plan below: the guard's fast path runs in the service before the graph (masking must precede the checkpoint) and acts through a
graph node, and tokens are debited by the service after the graph from the same usage records the metrics use, not by each node.

- Input guard as a FastAPI middleware plus a graph node: normalisation, regex/jailbreak fast-path, optional
  classifier for grey-zone inputs.
- PII masking and Redis vault as a Python package (`guardrails/pii.py`) with checksum validators.
- Token budgets per user/tier in Redis, enforced in a FastAPI dependency and debited by graph nodes.
- Policy checks (tool × agent, action × device) as plain Python modules first. Adopt OPA only if rules need to
  change without a deploy.
- Prompt/model registry and feature flags (kill switches per agent).

### Phase 3 — Carry-over gate, then planning agents (4–6 weeks) 🟡 started (2026-11-14)

Built so far: the three planning agents, plan-tier rules, ingest-side document safety, the retrieval upgrade, explain-and-do, the multilingual guard, retry, the L3 judge and prompt v6 (both waiting for a real-model run), and the long-term memory store (not yet wired in). Not started: 3.7 (history endpoint and the v2 switch) and 3.10. Status per item: [13-progress-tracker.md](./13-progress-tracker.md). Details: [16](./16-phase3-implementation.md) and [18](./18-phase3-4-build-notes.md).

**Gate first** (items 3.5 to 3.14 in the [tracker](./13-progress-tracker.md)): the L3 judge and real-model quality measurement, answer
prompt v6, switching the frontend to v2 and retiring v1, ingest-side masking and risk scoring of documents, the retrieval upgrade,
the compound eval dataset (6 → 80), mixed "explain and do", multilingual guard and follow-ups, and a retry policy on agent nodes.
Every known limit from Phases 0–2 is traced to an item in the tracker's coverage table (section 8).

**Then the agents:**

- **Forecast Agent** (ML: solar + load quantile models; physics fallback from `simulator/engines`).
- **Optimizer Agent** (schedule solver + battery strategy).
- **Bill & Tariff Agent** with versioned tariff tables and what-if simulator (backs the frontend "bill simulator").
- Long-term memory (Store) + "What Suryaa remembers" page.

### Phase 4 — Automation & ambient (4–6 weeks) 🟡 partly built (2026-11-14)

Built: rule engine and DSL, stored rules with approval, the automation worker, the device-command outbox, the publish-before-commit fix. Not built: the Automation Rule agent, Recommender, Reporter, the checkpoint-volume measurement. See [13](./13-progress-tracker.md).

- **Rule engine in Python** (`modules/automation/`): evaluates alerts and approved automations on each tick inside
  the ingest worker or a dedicated consumer, emits state transitions only. Reuses `domain/alerts.py`.
- **Automation Rule Agent** (NL → DSL, validate, dry-run, interrupt to approve). Backs the frontend rule builder.
- **Command executor in Python** (extends `workers/device_commands.py`): outbox table, idempotency key, state
  machine (`RECEIVED → VALIDATED → POLICY_OK → DISPATCHED → ACKED | FAILED | EXPIRED`), kill switch, per-device
  rate limit, append-only audit. No LLM in this path.
- **Monitor**, **Recommender**, **Reporter** ambient/batch agents; provider batch APIs.
- Exit gate: measure checkpoint write volume per question and set retention and durability from it; move `HomeNotification` and `AutomationRule` out of the Device Action package.

### Phase 5 — Scale and operate (ongoing) 🟡 in flight (2026-11-14)

Infrastructure items (5.1, 5.2, 5.3, 5.5) were partly written by a background agent that was stopped; they are unverified. 5.4 and 5.6 are not started. See [13](./13-progress-tracker.md).

- Scale ingest workers horizontally (`--scale ingest-worker=N`, KEDA on queue lag). Batch Timescale writes
  (`COPY`/`executemany`) and pipeline Redis calls.
- Run several uvicorn workers per pod. Scale agent pods on in-flight requests.
- Plans and cost control: a plan-tier endpoint, per-tenant monthly budget, reservation-based budgeting. Revisit token streaming and OPA only if their triggers fire (tracker items 5.6 to 5.8).
- Use an off-the-shelf gateway (Envoy, Kong or Traefik) for TLS, routing and coarse rate limits.
- Online judge sampling, canary pipeline, cost dashboards, nightly red-team generator.

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

## 3. Keep the door open for Go (no work now)

Build in a way that a later migration is cheap, and measure so the decision is based on data.

| Habit | How |
|---|---|
| Hot paths behind interfaces | Ingest, live fan-out, rule evaluation and command execution each sit behind a `Protocol` port |
| Queues and HTTP, not shared code | Services talk through RabbitMQ/Redis/HTTP so a consumer can be replaced in another language |
| Schemas outside Python classes | Define `Tick`, `DeviceCommand`, `Incident` as JSON Schema (or protobuf) and generate Pydantic models from them |
| Pure, fixture-tested rules | Validator, alerts and safety checks take plain inputs and return plain outputs. Save cases as JSON so a port can be proven identical |
| Gateways, not direct storage access | Agents read through `EnergyDataGateway`-style classes, never tables or Redis keys |
| Baseline metrics | Ingest lag, messages/s, CPU per tick, WebSocket count, memory per socket |

**Triggers to reconsider Go** (any one, confirmed by benchmark): ingest cannot keep up on reasonable hardware or
costs too much per home; one API pod cannot hold the target WebSocket count; L3 automations need a stricter
audited executor than Python can provide. If none occur, Go is never needed.

## 4. Decisions to make early

| Decision | Recommendation |
|---|---|
| LangSmith vs Langfuse | Langfuse if data residency / self-hosting matters (DPDP), LangSmith if you want the tightest LangGraph tooling and are fine with SaaS |
| RabbitMQ vs NATS JetStream | Keep RabbitMQ (already running, quorum queues). Add RabbitMQ Streams for the tick stream to the rule engine before adding a second broker |
| Self-host LangGraph runtime vs LangGraph Platform | Self-host on FastAPI + Postgres checkpointer (you already have the pieces). Revisit the platform if you need its assistants/cron/double-texting features out of the box |
| One Python agent service vs one per agent | **One service**, agents as packages/subgraphs. Split only an agent that needs different scaling (e.g. a GPU-backed forecast model server) |
| Gateway: build vs adopt | Adopt Envoy/Kong/Traefik for edge concerns. Keep budgets, guards and policy in Python |
