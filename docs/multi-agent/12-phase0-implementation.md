# 12 — Phase 0 Implementation

Status: **done** (2026-11-09). All Python and FastAPI. 307 tests (297 run by default, 10 need Postgres or Redis and are opt-in); the
integration tests were run against real Postgres 16 + pgvector and Redis.

## 1. What changed

| # | Item | Where | One-line behaviour |
|---|---|---|---|
| 1 | Durable checkpoints | [checkpointer.py](../../backend/app/ai/checkpointer.py), [actions/](../../backend/app/agents_platform/agents/device_action/) | Pending device confirmations live in Postgres (own schema + pool) and survive restarts and replicas |
| 2 | Confirmation lifecycle | [service.py](../../backend/app/agents_platform/agents/device_action/service.py), [maintenance.py](../../backend/app/agents_platform/agents/device_action/maintenance.py) | 2-minute deadline, atomic single-use claim, replay refused even if the DB row rolled back, sweep expires and prunes |
| 3 | Rate limiting | [rate_limit.py](../../backend/app/core/rate_limit.py) | Redis-shared counters, per user (token) else per IP, falls back to in-process if Redis is down |
| 4 | Observability | [request_context.py](../../backend/app/core/request_context.py), [logging_config.py](../../backend/app/core/logging_config.py), [telemetry.py](../../backend/app/core/telemetry.py), [metrics.py](../../backend/app/core/metrics.py) | `X-Request-ID` on every request and log line; OTLP traces; `traceparent` across RabbitMQ; Prometheus metrics |
| 5 | Numeric grounding | [grounding.py](../../backend/app/modules/assistant/domain/grounding.py), `ground` node | Every number in an LLM answer must trace to the facts, else the deterministic answer replaces it |
| 6 | Evals + CI | [evals/](../../backend/app/modules/assistant/evals/), [backend-ci.yml](../../.github/workflows/backend-ci.yml) | Cases are validated JSONL; runner reports per case; CI runs migrations, evals, tests |
| 7 | Retrieval | [retrieval.py](../../backend/app/modules/knowledge/retrieval.py), [repository.py](../../backend/app/modules/knowledge/repository.py) | Indexed vector + full-text candidates (Postgres), OpenRouter/Google/OpenAI embedders, `reembed` job |
| 8 | Hygiene | `.env.example`, compose | Committed credentials removed; compose wires Postgres checkpoints, Redis limits, metrics, maintenance loop |
| 9 | JWT leeway | [security.py](../../backend/app/core/security.py) | 10 s clock-skew tolerance (see §5) |

Migrations: `202611090001` (proposal `expires_at`), `202611090002` (full-text column + GIN index).

## 2. Two findings that changed the plan

1. **The HNSW index was never used.** `PGVECTOR_SQL` was dead code. Search loaded the newest 400 chunks and scored them in
   Python, so with more than 400 chunks relevant older ones were silently skipped. A regression test now shows the old
   path missing a chunk that the indexed path finds.
2. **Changing the embedding model hides every existing document** (search filters on `embedding_model`). Run
   `python -m app.modules.knowledge.reembed` after any change.

## 3. Configuration

All keys are in [backend/.env.example](../../backend/.env.example).

| Setting | Default | Notes |
|---|---|---|
| `AI_CHECKPOINT_BACKEND` | `memory` | **Must be `postgres` in production** (the API refuses to start otherwise). Compose sets it |
| `AI_CHECKPOINT_SCHEMA` / `_DATABASE_URL` | `langgraph` / `DATABASE_URL` | Own schema and pool, away from OLTP tables |
| `ACTION_CONFIRMATION_TTL_SECONDS` | 120 | Time to confirm a proposal |
| `ACTION_CHECKPOINT_RETENTION_HOURS` | 24 | Maintenance must run at least this often |
| `RATE_LIMIT_STORAGE_URI` | empty (in-process) | Set a Redis URL, **another DB index** than live data. Compose: `redis://redis:6379/1` |
| `AI_GROUNDING_MODE` | `enforce` | `observe` logs and counts without changing answers: use it first on a new model or prompt |
| `LOG_JSON`, `LOG_LEVEL` | `false`, `INFO` | JSON lines for log shippers |
| `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_TRACES_SAMPLER_RATIO` | off | Needs a collector, e.g. `http://otel-collector:4318` |
| `METRICS_ENABLED`, `WORKER_METRICS_PORT` | off, 0 | `/metrics` is **unauthenticated**: internal network only |
| `AI_EMBEDDING_PROVIDER` / `_MODEL` | empty / `hashing-256` | e.g. `openrouter` + `openai/text-embedding-3-small`; keep dimensions at 256 (the vector column is 256 wide) |
| `RAG_VECTOR_CANDIDATES`, `RAG_LEXICAL_CANDIDATES` | 40, 40 | Candidates per indexed search before ranking |
| `JWT_LEEWAY_SECONDS` | 10 | Clock-skew tolerance |

## 4. Operations

| Task | Command |
|---|---|
| Create checkpoint tables (once per deployment, run by the compose `migrate` job) | `python -m app.ai.checkpointer setup` |
| Expire unanswered proposals, prune old checkpoints (compose runs it hourly) | `python -m app.agents_platform.agents.device_action.maintenance` |
| Re-embed after changing the embedder | `python -m app.modules.knowledge.reembed` |
| Run routing evals | `python -m app.modules.assistant.evals --report out.json` |
| Run the opt-in integration tests | `TEST_POSTGRES_URL=postgresql+asyncpg://… TEST_REDIS_URL=redis://…/15 pytest` |

Metrics to watch: `volta_ingest_message_age_seconds` (effective lag), `volta_ingest_messages_total{outcome}`,
`volta_grounding_checks_total{result}`, `volta_assistant_requests_total{path,fallback_reason}`, `volta_llm_tokens_total`,
`volta_action_proposals_total{status}`, `volta_ws_connections{channel}`, plus process CPU and memory.

## 5. The clock-skew bug

Intermittent `401`s appeared in roughly half of full test runs. Cause: on this WSL2 machine the wall clock steps
**backwards by ~0.8 s about every 30 s**. A token issued just before a step is rejected as "not yet valid (iat)".
Any multi-server deployment has the same class of problem, so token validation now tolerates 10 s of skew.

## 6. Known limits (not done in Phase 0)

| Limit | Next step |
|---|---|
| An answer that fails grounding falls back to the deterministic answer; there is **no retry** with feedback | Phase 1 (supervisor): one regeneration with the failing numbers listed |
| Grounding does not check non-numeric claims, clock times, years, or whole numbers ≤ 10 | Faithfulness judge (doc 05) |
| Device command is published before the DB commit. Replay is blocked, but a failed commit leaves the row "awaiting" and the command queued | Outbox table (Phase 4) |
| Ingest metrics come from the worker only; the simulator does not propagate `traceparent` | Add header injection in the simulator's publisher |
| Hybrid score is still cosine + token overlap on a candidate union, not true BM25/RRF | Phase 3 retrieval work |
| Vector column is 256-wide: real embedders are asked for 256 dims (lossy for non-Matryoshka models) | Migrate to a wider column when a model is chosen |
| `hnsw.iterative_scan` not set: heavy filters can return fewer than 40 ANN rows | Enable on pgvector ≥ 0.8 |
| Rate limiting uses a synchronous Redis client (about one round-trip per limited request) | Acceptable now; revisit if latency shows it |
| The leaked password in `.env.example` history | Rotate it; rewriting history is your decision |
