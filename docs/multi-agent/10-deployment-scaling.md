# 10 — Deployment & Scaling

## 1. Kubernetes topology

| Deployment | Lang | Replicas (start) | Scale on | Notes |
|---|---|---|---|---|
| `volta-edge` | Go | 2–3 | RPS, CPU | Behind cloud LB / ingress. HPA |
| `volta-guard` | Go | 2 | CPU | Or a library inside `volta-edge` to save a hop |
| `volta-realtime` | Go | 2+ | Active connections (custom metric) | Sticky not required (Redis pub/sub fan-out) |
| `volta-agents` | Python | 3+ | **In-flight requests per pod** (custom metric), not CPU | Mostly waiting on I/O. Run uvicorn with several workers per pod, or more pods with 1–2 workers |
| `volta-api` | Python | 2+ | RPS | Existing FastAPI (auth, onboarding, energy, knowledge) |
| `volta-agent-workers` | Python | 2+ | **Queue depth** (KEDA) | Ambient + batch agents |
| `volta-ingest` | Go | 2+ | Queue lag (KEDA) | Partition by household for ordering if needed |
| `volta-rules` | Go | N shards | Households per shard | StatefulSet, consistent hashing on `household_id` |
| `volta-executor` | Go | 2 (leader-elected or idempotent) | — | Safety-critical. Small and boring on purpose |
| `volta-llm-gateway` | Go | 2 | RPS | Optional |
| Model servers (embeddings, reranker, guard classifier) | Python | 1–2 (CPU or small GPU) | Request rate | Only if self-hosting these models |

Data: managed Postgres (OLTP + pgvector + LangGraph checkpoints/store, separate databases or at least separate
pools), Timescale (managed or self-run), Redis (HA), RabbitMQ (quorum queues) or NATS JetStream.

## 2. Why scale agents on concurrency, not CPU

A LangGraph request spends most of its life awaiting LLM and I/O. CPU stays low while the pod is saturated on
open connections, DB pool slots and provider rate limits. Scale on `inflight_requests / pod` and cap concurrency
per pod (semaphore) so a slow provider cannot pile up unbounded coroutines.

## 3. Resilience patterns

| Pattern | Where |
|---|---|
| Timeouts everywhere | Per tool, per agent (`deadline_ms`), per LLM call, per request (8 s) |
| Retries with jitter, only on idempotent/transient | LLM 429/5xx, DB transient. Never retry a device publish without its idempotency key |
| Circuit breaker per provider/model | LLM gateway or `StructuredLLM` wrapper. Open → go straight to fallback |
| Bulkheads | Separate pools/semaphores for chat vs ambient vs batch; separate DB pools for checkpoints vs OLTP |
| Graceful degradation | Agent timeout → partial answer with warning. LLM down → deterministic mode (exists ✅) |
| Queue DLQs | Every consumer: max deliveries → DLQ → alert + replay tool |
| Outbox pattern | Action proposals and commands written to Postgres and the queue atomically |
| Provider rate-limit awareness | Token-bucket per provider key in the gateway, priority for chat over batch |

## 4. Environments and rollout

| Env | Purpose | LLM |
|---|---|---|
| `dev` (docker compose, today) | Local | Cheapest model or fake |
| `ci` | Evals | Fake for L1–L2, real for L3–L5 (cached) |
| `staging` | Full stack + simulator load | Production mapping |
| `prod` | Canary 5 % → 25 % → 100 % via flags | Production mapping |

Deploy Go and Python services independently. Shared protobuf schemas are versioned and kept backward-compatible
(add fields, never repurpose).

## 5. Capacity planning inputs

Record these numbers before sizing anything:
- Households × tick rate → ingest msgs/s.
- Daily active users × questions/user/day × peak factor → chat RPS.
- Average and p95 tokens per question per role → provider TPM/RPM needs (request quota increases early).
- Concurrent dashboard sessions → realtime connections.
- Rules per household × tick rate → rule evaluations/s.

## 6. Disaster recovery

| Component | RPO | RTO | Mechanism |
|---|---|---|---|
| OLTP Postgres (users, onboarding, knowledge, audit) | ≤ 5 min | ≤ 1 h | PITR backups, cross-zone replica |
| Checkpoints | ≤ 15 min | ≤ 1 h | Same cluster or separate; loss = users re-ask, pending confirmations expire |
| Timescale | ≤ 15 min | ≤ 2 h | Backups; Redis live rebuilds from next tick |
| Redis | Best effort | Minutes | AOF (already enabled), vault data intentionally ephemeral |
| Queue | Durable | Minutes | Quorum queues / JetStream replicas |
