# Telemetry ingest

What happens **after** a tick is a JSON object. Simulator publishes. Worker validates, stores history, holds live state, and pushes to the dashboard.

| | |
|---|---|
| **Project** | Backend ingest of solar-home ticks (Suryaa / Volta.ai) |
| **Write path** | Simulator (aio-pika) → RabbitMQ 3.13 → ingest worker |
| **Stores** | TimescaleDB 2.x (history) · Redis 7 (live + pub/sub) |
| **Read path** | FastAPI REST + WebSocket · JWT from the auth service |

> The generator never sees Redis or Timescale. Do not mix this file with [simulator.md](./simulator.md) (how one tick is calculated) or [auth.md](./auth.md) (how the user JWT is issued).

**Sister docs:** [Auth](./auth.md) · [Onboarding](./onboarding.md) · [Simulator](./simulator.md) · [Docs hub](./README.md)

---

## On this page

1. [Memory map](#1-memory-map)
2. [System diagram](#2-system-diagram)
3. [Data contract](#3-data-contract)
4. [Endpoints + broker](#4-endpoints--broker)
5. [Why each piece exists](#5-why-each-piece-exists)
6. [Code workflows](#6-code-workflows)
7. [Security checklist](#7-security-checklist)
8. [Config](#8-config)
9. [Run, check, stop](#9-run-check-stop)
10. [Testing](#10-testing)
11. [Trade-offs](#11-trade-offs)
12. [Elevator pitch](#12-elevator-pitch)
13. [Cheat sheet](#13-cheat-sheet)
14. [End-to-end flow](#14-end-to-end-flow)
15. [Timescale vs Redis](#15-timescale-vs-redis)
16. [How RabbitMQ works](#16-how-rabbitmq-works)

---

## 1. Memory map

Auth is an HTTP request:

```
Router → Service → Repository → Model / DB
```

Ingest is a **pipeline**, not an HTTP request:

```
Publisher (simulator)
     → Broker (RabbitMQ exchange + queue)
          → Worker (validate + persist)
               → History store (Timescale hypertable)
               → Live store   (Redis key + pub/sub)
                    → Read API (FastAPI REST)
                    → Push API (FastAPI WebSocket)
                         → UI / future React
```

Write path and read path are **different processes**:

| Path | Command | JWT? |
|---|---|---|
| **WRITE** | `python -m app.workers.ingest` | No HTTP, no JWT |
| **READ** | `uvicorn main:app` | JWT on GET and WS |

### Folder map (ingest-related only)

```
volta.ai/
  docker-compose.yml                 rabbitmq :5672  timescaledb :5433  redis :6379

  simulator/
    main.py                          live clock loop; optional --rabbitmq-url
    clients/rabbitmq.py              declare EXCHANGE, publish persistent JSON
    clients/ingest.py                legacy HTTP POST /energy/ingest (tests)

  backend/app/
    core/config.py                   every URL / queue name from .env
    core/database.py                 Neon auth DB — NOT used for ticks
    modules/energy/
      schemas.py                     TelemetryRecord (same keys as simulator)
      pipeline.py                    bytes → ack | reject
      service.py                     normalize + live / history projections
      store.py                       in-memory ring (pytest + HTTP ingest)
      timescale_store.py             upsert / latest / range
      redis_live.py                  SET + PUBLISH + subscribe
      router.py                      REST /energy/*
      ws.py                          WS  /ws/energy
    workers/ingest.py                consume: validate → Timescale → Redis → ACK
    main.py                          lifespan connects Timescale+Redis; /health /ready

  frontend/public/suryaa-dashboard.html
      no query         = simulator SSE (port 8765)
      source=backend   = FastAPI WebSocket (port 8000)
```

---

## 2. System diagram

```mermaid
flowchart TB
  subgraph publish [Publish path — no login]
    Sim[Simulator TelemetryRecord JSON persistent]
    RMQ[RabbitMQ vhost volta]
    Sim -->|topic telemetry / routing_key telemetry.ingest| RMQ
    RMQ --> Q[Q telemetry.ingest]
    RMQ --> DLQ[DLQ telemetry.ingest.dead]
  end

  subgraph worker [Ingest worker prefetch=50]
    W[1 parse/valid · 2 Timescale · 3 Redis · 4 ACK]
    Q --> W
  end

  subgraph stores [Stores]
    TS[(TimescaleDB volta_ts :5433<br/>telemetry_ticks PK household,time)]
    RD[(Redis :6379<br/>KEY live:id · CH live:ch:id · TTL 120s)]
    W --> TS
    W --> RD
  end

  subgraph api [FastAPI uvicorn :8000]
    REST[GET /energy/id/live|history|daily|hourly Bearer JWT]
    WS[WS /ws/energy?token=JWT&household_id=home_001]
    TS --> REST
    RD --> REST
    RD --> WS
  end

  subgraph ui [UI]
    SSE[8765 generator SSE — no JWT]
    BE[8765/?source=backend product WebSocket — JWT]
  end
```

**Trust boundary.** The simulator is untrusted I/O. The worker re-validates every field and recomputes energy-balance. A producer cannot mark its own tick “valid”.

---

## 3. Data contract

One message = one `TelemetryRecord`. Field names match `simulator/models.py` so the same JSON is valid on both sides. `extra="allow"` keeps new keys (`scenario`, `location`, `warnings`) without a backend deploy.

### Identity

| Field | Rules |
|---|---|
| `timestamp` | Timezone-aware datetime |
| `household_id` | e.g. `home_001` — regex `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` |
| `data_source` | `"simulator"` |
| `data_quality` | `simulated` · `fallback` · `live` · `degraded` |

### Power (kW, never negative)

| Field | Rule |
|---|---|
| `solar_power_kw` | ≥ 0 |
| `home_load_power_kw` | ≥ 0 |
| `battery_charge_power_kw` + `battery_discharge_power_kw` | **Never** one signed `battery_power` |
| `grid_import_power_kw` + `grid_export_power_kw` | **Never** both &gt; 0.02 in the same interval |

### Energy (kWh)

Solar / home: interval + today + total.  
Grid / battery interval fields — worker fills them if the producer omitted them.

### State

`battery_soc_percent`, `battery_soh_percent`, `battery_status`, `grid_status`, `energy_balance_status` / `valid` / `error_kw`, `devices[]`, `weather{}`.

### AC-bus check (worker recomputes, tolerance 0.05 kW)

```
solar + grid_import + battery_discharge
    ≈  home + grid_export + battery_charge
```

| Outcome | Action |
|---|---|
| Balance fail | `status="warning"`, **still stored** (same as HTTP 202) |
| Schema fail (negative kW, import+export) | **Not stored**, dead-lettered |

### AMQP envelope (`simulator/clients/rabbitmq.py`)

| Field | Value |
|---|---|
| `body` | `record.model_dump_json()` |
| `content_type` | `application/json` |
| `delivery_mode` | `PERSISTENT` (survives broker restart) |
| `app_id` | `suryaa-simulator` |
| `headers` | `household_id`, `data_source` |

### Timescale row (`telemetry_ticks`)

| Column | Role |
|---|---|
| `time` | `TIMESTAMPTZ` |
| `household_id` | `TEXT` |
| `payload` | `JSONB` — full record, source of truth for reads |
| Denormalized kW + interval kWh | Cheap filters |
| PK | `(household_id, time)` |
| Hypertable | on `time` |
| Conflict | `ON CONFLICT DO UPDATE` — retries are idempotent |

### Redis

```
SET     volta:live:{household_id}      JSON    EX 120
PUBLISH volta:live:ch:{household_id}   JSON
```

### WebSocket frame

```json
{
  "type": "hello | telemetry | ping | error",
  "record": { },
  "live": { }
}
```

`live` is a nested `LiveEnergyOut` (solar / load / battery / grid).

---

## 4. Endpoints + broker

### Write (not a user JWT)

| Channel | Auth | Notes |
|---|---|---|
| AMQP `telemetry` / `telemetry.ingest` | Broker user | Simulator → worker |
| `POST /energy/ingest` | `X-Ingest-Token` | Tests / dev only. `202` normalized · `400` · `401`. `404` if `INGEST_HTTP_ENABLED=false` |

### Read (`Authorization: Bearer <access_token>`)

| Method | Path |
|---|---|
| `GET` | `/energy/{household_id}/live` |
| `GET` | `/energy/{household_id}/battery` |
| `GET` | `/energy/{household_id}/grid` |
| `GET` | `/energy/{household_id}/devices` |
| `GET` | `/energy/{household_id}/weather` |
| `GET` | `/energy/{household_id}/history?limit=120` |
| `GET` | `/energy/{household_id}/daily?date=YYYY-MM-DD` |
| `GET` | `/energy/{household_id}/hourly?date=YYYY-MM-DD` |

### Push

`WS /ws/energy?token=<access_jwt>&household_id=home_001`

### Ops

| Path | Meaning |
|---|---|
| `GET /health` | Process up |
| `GET /ready` | `{ timescale, redis, telemetry_io }` |

### Broker (vhost `volta`)

| Object | Spec |
|---|---|
| Exchange `telemetry` | Topic, durable |
| Exchange `telemetry.dlx` | Fanout, durable |
| Queue `telemetry.ingest` | Durable, `x-dead-letter-exchange=telemetry.dlx` |
| Queue `telemetry.ingest.dead` | Durable |
| Bind | `telemetry --[telemetry.ingest]--> telemetry.ingest` |
| Bind | `telemetry.dlx --> telemetry.ingest.dead` |

---

## 5. Why each piece exists

### 5.1 RabbitMQ — buffer, not a database

HTTP ingest cannot survive a simulator at 1 Hz and a restarting worker. POST fails, ticks are gone. API rate limits and worker crashes become the generator’s problem.

| Role | Owns | Behavior |
|---|---|---|
| **Publisher** (simulator) | Exchange only | `publish(routing_key="telemetry.ingest")`, publisher confirms, best-effort (broker down → log, do not kill the generator) |
| **Consumer** (worker) | Exchange + queue + DLX + binding | `set_qos(prefetch=50)`, manual ack |

**Why the publisher must not declare the queue.** Queue arguments are immutable. If the simulator declared the queue **without** `x-dead-letter-exchange` and the worker declares it **with** that arg → `PRECONDITION_FAILED`. Rule: **owner of the binding owns the queue.**

Persistent message + durable queue = **at-least-once** after broker restart. We are **not** exactly-once. Duplicate `(household_id, time)` is absorbed by Timescale `ON CONFLICT DO UPDATE`.

#### Ack matrix

| Condition | Action | Where the tick goes |
|---|---|---|
| Valid + Timescale OK + Redis OK | ACK | History + live |
| Valid + Timescale OK + Redis FAIL | ACK | History only (live heals) |
| Valid + Timescale FAIL | NACK requeue | Stays on ingest queue |
| Invalid JSON / schema | Reject `requeue=false` | DLQ |
| Energy-balance warning | ACK (as valid row) | History, flagged warning |

`prefetch=50`: worker can hold 50 un-acked messages. Limits RAM if Timescale is slow. Raise only after measuring insert latency.

### 5.2 Ingest worker — not inside uvicorn

Why a separate process:

- `uvicorn --reload` respawns workers; each would compete as a consumer
- HTTP scale (many readers) ≠ ingest scale (one sequential writer)
- A crash of the API must not stop ingest, and the reverse

Entry: `python -m app.workers.ingest` (`cwd = backend/`)

Order inside `handle_message`:

1. `process_telemetry_body(bytes)` → `IngestDecision`
2. reject → `message.reject(requeue=False)`
3. `timescale.upsert(record)` — **must succeed**
4. `redis.set_and_publish(record)` — try / except
5. `message.ack()`

`pipeline.py` never talks to the network. That keeps pytest offline.

### 5.3 TimescaleDB — history

**Why not Neon** (`DATABASE_URL`): Neon = OLTP (users, tokens, onboarding). No hypertables there. Ticks are append-mostly, time-ordered, huge over months. Mixing them couples “login is down” to “ingest is down” and blocks compression / retention.

A **hypertable** is an ordinary Postgres table + Timescale chunks by time range. Time filters skip old chunks. Compression turns old chunks columnar. Retention drops chunks older than N days.

Policies (`ensure_schema` on worker / API start):

| Policy | Value |
|---|---|
| Compress after | 7 days |
| Retain raw ticks | 90 days |
| `segmentby` | `household_id` so one home compresses together |

Read strategy in `EnergyService`:

| Query | Source |
|---|---|
| Latest | Redis GET, else Timescale `ORDER BY time DESC LIMIT 1` |
| History | Timescale last N, chronological |
| Daily | `time >= day_start AND time < day_end` |
| Hourly | Same rows, bucketed in Python into 24 hours |

Second async SQLAlchemy engine (`TIMESCALE_DATABASE_URL`). SSL is `TIMESCALE_SSL_REQUIRED`, same pattern as `DB_SSL_REQUIRED` on Neon. `echo=False` — 1 Hz inserts must not flood logs.

### 5.4 Redis — live + fan-out

Two jobs, one server:

1. **Snapshot** — `SET volta:live:home_001 {json} EX 120`. GET is O(1). TTL means a stopped home goes stale instead of looking “live” forever.
2. **Fan-out** — `PUBLISH volta:live:ch:home_001 {json}`. Every API replica that subscribed that channel gets the tick and writes it to its own WebSocket clients. Without pub/sub, WS would only work on the replica that happened to hold the connection.

Redis is not the diary. If Redis restarts, Timescale still has rows; the next tick (or GET latest from Timescale) rebuilds the key.

### 5.5 FastAPI read side

| Mode | Job |
|---|---|
| REST | Pull. Charts, first paint, React Query |
| WS | Push. Live kW without polling |

Browsers cannot send `Authorization` easily on WebSocket, so:

```
?token=ACCESS_JWT&household_id=home_001
```

Server decodes JWT (`type` must be `access`), loads user from Neon, rejects inactive / unknown. Origin must be in `CORS_ORIGINS`.

| Close code | Meaning |
|---|---|
| `4401` | Bad / missing token |
| `1008` | Bad origin or `household_id` |

Heartbeat `{type: ping}` every 30s so proxies do not idle-drop the socket.

`GET /energy/{id}/live` projection (`to_live`) nests solar / load / battery / grid for UI cards. The WS also sends the flat `record` so the existing dashboard `applyTelemetry()` keeps working.

### 5.6 Two UIs, one generator

| URL | Pipe | JWT? | Purpose |
|---|---|---|---|
| Port 8765, no query | EventSource `/api/stream` on the **simulator** | No | Debug the generator |
| Port 8765, `?source=backend&token=…&household=home_001` | WebSocket to FastAPI `:8000` | Yes | Prove the product path |

Same kW is expected (same household, same ticks). They are **not** the same pipe. Proof: stop the worker — only the backend UI freezes. Network tab: SSE `:8765` vs WS `:8000`.

React later uses the backend pipe only. No ingest code change. Add the Vite origin to `CORS_ORIGINS` if it is not already `5173` / `3000`.

---

## 6. Code workflows

### A. Happy path — one live tick

1. Simulator live clock (no `--start-time`) calls `generator.step()`.
2. `RabbitMQPublisher.connect` if needed; declare exchange `telemetry`.
3. Publish persistent JSON, `routing_key=telemetry.ingest`.
4. Worker receives, `process_telemetry_body`.
5. `normalize_record` fills missing interval kWh, recomputes AC-bus.
6. `INSERT … ON CONFLICT (household_id, time) DO UPDATE`.
7. Redis `SET` + `PUBLISH`.
8. ACK.
9. Each WS subscriber for that household gets `{type: telemetry, record}`.
10. `GET /live` reads Redis first.

### B. Worker start

1. Connect Timescale, `ensure_schema` (extension, table, hypertable, policies).
2. Connect Redis, `PING` (if Redis down: log, continue history-only).
3. Connect AMQP, declare topology (DLX first, then ingest queue).
4. Consume until SIGINT / SIGTERM.

If the queue already exists without DLX:

```bash
docker compose exec rabbitmq rabbitmqctl delete_queue telemetry.ingest -p volta
```

Then restart the worker (test messages on that queue are dropped).

### C. WebSocket session

1. Check `Origin` against `CORS_ORIGINS`.
2. `accept()`.
3. Validate `household_id` regex.
4. Decode access JWT, load User from Neon.
5. Send `hello`.
6. Send latest snapshot if Redis / Timescale has one.
7. Parallel tasks: Redis subscribe · heartbeat ping · client close watch.
8. On disconnect: cancel tasks, unsubscribe.

### D. HTTP ingest (pytest / leftover)

1. `require_ingest_token` (constant-time compare of SHA-256 digests).
2. FastAPI validates `TelemetryRecord`.
3. `EnergyService.ingest` → normalize → in-memory store.
4. If `TELEMETRY_IO_ENABLED`, also Timescale + Redis (off in pytest).

Lifespan refuses to boot when `ENVIRONMENT=production` and:

- `INGEST_TOKEN` is still the documented default, **or**
- `INGEST_HTTP_ENABLED` is still `true`

### E. Read fallback chain

| Query | Order |
|---|---|
| Latest | Redis → Timescale → memory store → `404 EnergyNotFound` |
| History | Timescale last N if any rows, else memory |
| Daily / hourly | Timescale time range if configured, else filter memory |

pytest sets `TELEMETRY_IO_ENABLED=false` so leftover Docker rows cannot leak into `GET /history` assertions.

---

## 7. Security checklist

- [ ] Simulator does not hold Timescale or Redis credentials
- [ ] Production AMQP: publish-only user vs consume user, TLS `amqps://`
- [ ] Persistent messages + durable queue
- [ ] ACK only after Timescale upsert
- [ ] Schema failures → DLQ, not infinite requeue
- [ ] Balance warning is **not** a DLQ reason
- [ ] WS: access JWT + origin allow-list + household regex
- [ ] REST energy GETs: Bearer access token ([auth.md](./auth.md))
- [ ] Neon `DATABASE_URL` never pointed at `telemetry_ticks`
- [ ] `INGEST_HTTP_ENABLED=false` in production
- [ ] CORS explicit list, never `*`
- [ ] One local worker process (two consumers + an old binary that ACKs without writing will “lose” ticks)
- [ ] Secrets in `.env`, not committed

---

## 8. Config

No code change to switch hosting. Cloud later = same keys, TLS URLs.

### Already present (auth)

| Key | Role |
|---|---|
| `DATABASE_URL` / `DB_SSL_REQUIRED` | Neon users |
| `JWT_SECRET_KEY` | Signs REST + WS |
| `GOOGLE_*` | Login only |

### Simulator

| Key | Default / notes |
|---|---|
| `RABBITMQ_URL` | Empty = disabled |
| `RABBITMQ_EXCHANGE` | `telemetry` |
| `RABBITMQ_ROUTING_KEY` | `telemetry.ingest` |
| `HOUSEHOLD_ID` | `home_001` |

### Backend

| Key | Notes |
|---|---|
| `TIMESCALE_DATABASE_URL` | asyncpg URL, port `5433` locally |
| `TIMESCALE_SSL_REQUIRED` | `false` local, `true` cloud |
| `TIMESCALE_COMPRESS_AFTER_DAYS` | `7` |
| `TIMESCALE_RETENTION_DAYS` | `90` |
| `REDIS_URL` | `redis://` local, `rediss://` cloud |
| `REDIS_KEY_PREFIX` | `volta:live:` |
| `REDIS_LIVE_TTL_SECONDS` | `120` |
| `RABBITMQ_URL` / `QUEUE` / `DLX` / `DLQ` / `PREFETCH` | Broker topology |
| `WS_PATH` | `/ws/energy` |
| `WS_HEARTBEAT_SECONDS` | `30` |
| `INGEST_HTTP_ENABLED` | `true` dev, `false` prod |
| `TELEMETRY_IO_ENABLED` | `true` app, `false` pytest |
| `CORS_ORIGINS` | Include `http://127.0.0.1:8765` |

### Local ports (`docker compose`)

| Port | Service |
|---|---|
| `5672` / `15672` | RabbitMQ + management UI (`volta` / `volta`) |
| `5433` | Timescale (not `5432` — leave that for other Postgres) |
| `6379` | Redis |

---

## 9. Run, check, stop

Four processes:

```bash
docker compose up -d

cd backend && source venv/bin/activate
python -m app.workers.ingest
uvicorn main:app --reload

# repo root
python -m simulator.main \
  --rabbitmq-url amqp://volta:volta@localhost:5672/volta \
  --weather-mode live --dashboard --quiet
```

Live clock = omit `--start-time`, `--ticks`, `--speed 0`. `--dashboard` forces 1 reading per real second.

### Prove the pipe

```bash
rabbitmqctl list_queues -p volta name messages_ready consumers
# ingest ready=0  consumers=1

psql -U volta -d volta_ts -c "SELECT count(*) FROM telemetry_ticks;"

redis-cli GET volta:live:home_001

curl -s http://127.0.0.1:8000/ready
```

Then `POST /auth/login` and:

```
GET /energy/home_001/live
Authorization: Bearer <access_token>
```

Browser chip **Live · backend** + Network WS to `:8000`.

Stop the worker: only the backend UI freezes. Simulator `:8765` keeps moving.

| Stop | Effect |
|---|---|
| Ctrl+C worker and uvicorn | Processes exit |
| `docker compose stop` | Keeps volumes |
| `docker compose down -v` | Wipes queues and ticks |

---

## 10. Testing

### Simulator — `pytest simulator/tests`

- Persistent JSON message
- Broker-down publish does not raise

### Backend — `TELEMETRY_IO_ENABLED=false` (set in `tests/conftest.py`)

- Pipeline: ack / empty / invalid JSON / negative kW / import+export
- Unbalanced tick → ack + warning
- `record_to_params` ↔ `payload_to_record`
- Redis key and channel names
- WS invalid token → error; patched user → hello
- HTTP energy tests stay on the in-memory store

No pytest against live Timescale (dirty history rows).

---

## 11. Trade-offs

<dl>

<dt>Why RabbitMQ, not HTTP ingest, in production?</dt>
<dd>Different uptime. The queue absorbs bursts and worker restarts. HTTP ties the generator to API health and rate limits.</dd>

<dt>Why not declare the queue on the simulator?</dt>
<dd>Publisher owns the exchange. Consumer owns queue arguments (DLX). Two declares with different args → <code>PRECONDITION_FAILED</code>.</dd>

<dt>Why ACK after Timescale, not after Redis?</dt>
<dd>Timescale is durable history. Redis is a cache. History loss is worse than a one-tick live gap.</dd>

<dt>Why ACK a balance warning?</dt>
<dd>Same as HTTP 202. A 0.06 kW rounding error must not stall the home.</dd>

<dt>Why Timescale, not Neon?</dt>
<dd>Hypertables, compression, retention. Separate failure domain from login.</dd>

<dt>Why Redis and Timescale together?</dt>
<dd>Redis = latest + pub/sub across API replicas. Timescale = months of ticks. One store cannot do both well.</dd>

<dt>Why not consume inside FastAPI lifespan?</dt>
<dd>Reload and multi-worker uvicorn would steal or duplicate consumes. Ingest load scales independently of HTTP.</dd>

<dt>At-least-once or exactly-once?</dt>
<dd>At-least-once (requeue on Timescale error). Idempotent upsert makes duplicates safe.</dd>

<dt>Why query-string JWT on WebSocket?</dt>
<dd>Browsers cannot set <code>Authorization</code> on the WS handshake. Origin check + short-lived access token is the trade-off. Prefer a cookie later if the SPA is same-site. Do not log the token.</dd>

<dt>Why do both dashboards show the same kW?</dt>
<dd>One generator, two readers. Stop the worker: only backend freezes.</dd>

<dt>How does React plug in?</dt>
<dd>Same WS and REST. Hold the access token, <code>onmessage</code> → <code>setState(record)</code>. No worker or schema change.</dd>

<dt>Why does the in-memory store still exist?</dt>
<dd>Pytest and HTTP ingest without Docker. Production reads Redis / Timescale.</dd>

</dl>

---

## 12. Elevator pitch

> The simulator is only a generator. It publishes one JSON tick to a RabbitMQ topic exchange. A worker process — not uvicorn — consumes the queue, validates the record, recomputes the energy-balance check, upserts a Timescale hypertable for history, updates Redis for the live snapshot and pub/sub, then ACKs. Timescale failure requeues; poison JSON goes to a DLQ; Redis failure does not block the ACK. The API serves JWT-protected REST history and a WebSocket so the dashboard, and later React, push live kW without polling. Auth stays on Neon. Telemetry never shares that database. HTTP ingest remains for tests.

---

## 13. Cheat sheet

| Topic | File |
|---|---|
| Architecture | This file |
| Brokers | `docker-compose.yml` |
| Publisher | `simulator/clients/rabbitmq.py` |
| Loop | `simulator/main.py` |
| Validate | `backend/app/modules/energy/pipeline.py` |
| Normalize / reads | `backend/app/modules/energy/service.py` |
| Worker ACK policy | `backend/app/workers/ingest.py` |
| Hypertable | `backend/app/modules/energy/timescale_store.py` |
| Live + pub/sub | `backend/app/modules/energy/redis_live.py` |
| WebSocket | `backend/app/modules/energy/ws.py` |
| REST | `backend/app/modules/energy/router.py` |
| Env | `backend/app/core/config.py` |
| Lifespan /ready | `backend/app/main.py` |
| Two UI modes | `frontend/public/suryaa-dashboard.html` |
| JWT | [auth.md](./auth.md) |
| Tick physics | [simulator.md](./simulator.md) |

---

## 14. End-to-end flow

One solar-home tick from a measured photon to a number changing on the live dashboard.

### Step 1 — Simulator generates a tick

**Tech:** Python (aio-pika, Pydantic, httpx)  
**Files:** `simulator/telemetry_generator.py` + `simulator/clients/rabbitmq.py`

The simulator is running with `--dashboard --weather-mode live`. Its clock fires once per second. `TelemetryGenerator.step()` is called.

| Engine | Example |
|---|---|
| Solar | Cached Open-Meteo GHI → cloud, temperature derate, shading → `solar_power_kw = 2.14` |
| Load | Base household + appliances → `home_load_power_kw = 1.60` |
| Battery | Surplus solar charges → `battery_charge_power_kw = 0.54` |
| Balance | `2.14 = 1.60 + 0.54`. Valid. |

Output: `TelemetryRecord` as JSON. Then `RabbitMQPublisher.publish()`:

- Declares exchange `telemetry` (topic, durable) — **only the exchange, never the queue**
- AMQP Message: JSON bytes, `delivery_mode = PERSISTENT`, headers `{ household_id, data_source }`
- `exchange.publish(message, routing_key="telemetry.ingest")`
- Waits for a publisher confirm

If the broker is down: `_note_failure()` logs a warning. The generator keeps running. The tick still prints to the local dashboard SSE stream.

### Step 2 — RabbitMQ buffers the message

**Tech:** RabbitMQ 3.13 (AMQP 0-9-1)  
**Files:** `docker-compose.yml`, `backend/app/workers/ingest.py`

The exchange routes `telemetry.ingest` to the durable queue `telemetry.ingest` (bound by the worker at startup).

| If HTTP | If queue |
|---|---|
| Worker restart = ticks lost | Ticks wait safely |

Queue properties that matter: `durable = true`, `x-dead-letter-exchange = "telemetry.dlx"`, persistent messages (`delivery_mode=2`).

Role: decoupling, durability, back-pressure relief, dead-letter routing for poison.

### Step 3 — Ingest worker validates the tick

**Tech:** Python (aio-pika consumer, Pydantic v2)  
**Files:** `backend/app/workers/ingest.py` + `backend/app/modules/energy/pipeline.py`

`prefetch_count = 50`. Inside `pipeline.py`:

1. `json.loads(body)` — fail → reject to DLQ
2. `TelemetryRecord.model_validate(payload)` — negative kW or import **and** export → reject to DLQ
3. `normalize_record()` — fill missing interval kWh, recompute AC-bus (producer cannot self-certify). `|error| > 0.05 kW` → `energy_balance_status = "warning"`, still ACK’d
4. Return `IngestDecision(action="ack", record=normalized_record)`

The worker itself never touches the network in `pipeline.py`. Pydantic validates in pure Python. No Docker dependency in tests.

### Step 4 — TimescaleDB stores history

**Tech:** TimescaleDB 2.x (PostgreSQL 16 + time-series extension)  
**File:** `backend/app/modules/energy/timescale_store.py`

```sql
INSERT INTO telemetry_ticks
  (time, household_id, payload, solar_power_kw, ...)
VALUES (:time, :household_id, :json_payload, :solar_kw, ...)
ON CONFLICT (household_id, time)
DO UPDATE SET payload = EXCLUDED.payload, ...
```

| Why | Because |
|---|---|
| `ON CONFLICT` | At-least-once redelivery after a dropped ACK |
| Not Neon | OLTP vs append-mostly ~86 400 rows/day |
| Compression after 7 days | Columnar form, storage drops ~90% |
| Retention 90 days | Chunks dropped automatically — no cron |
| Hypertable | App sees a normal table; time queries skip old chunks |
| JSONB + denormalized kW | New fields without `ALTER TABLE`; cheap aggregates |

The worker does **not** ACK yet. If this `INSERT` throws, the message is nacked and requeued.

### Step 5 — Redis holds the live snapshot and fans it out

**Tech:** Redis 7 (pub/sub + key-value)  
**File:** `backend/app/modules/energy/redis_live.py`

```
SET     volta:live:home_001     <json>  EX 120
PUBLISH volta:live:ch:home_001  <json>
```

- **SET:** any client can GET the latest tick in O(1). TTL 120 s means a stopped simulator does not look “live” forever.
- **PUBLISH:** if the API runs as 3 uvicorn workers, all subscribers get the same tick. Without pub/sub, a tick only reaches one replica.

Redis failure is try / except. The message is still ACK’d because Timescale already has the row. Now the worker calls `message.ack()`.

### Step 6 — FastAPI WebSocket pushes to the browser

**Tech:** FastAPI + Starlette WebSocket + asyncio + Redis subscriber  
**File:** `backend/app/modules/energy/ws.py`

```
ws://127.0.0.1:8000/ws/energy?token=<access_jwt>&household_id=home_001
```

At connect: Origin check → `accept()` → household regex → JWT decode → Neon user → `{ type: "hello" }` → latest snapshot.

Three asyncio tasks in parallel:

| Task | Job |
|---|---|
| `_heartbeat()` | Every 30 s: `{ type: "ping" }` — keep proxies alive |
| `_pump_redis()` | Subscribe `volta:live:ch:home_001` → send `{ type: "telemetry", record, live }` |
| `_client_watch()` | Browser `"close"` or disconnect → `stop` |

Why not HTTP long-polling? At 1 Hz that is 3 600 requests/hour per client. Why not SSE for the product? SSE is the simulator dashboard (port 8765). WebSocket is bidirectional and the standard when both directions may be needed. Query-string JWT because browsers cannot set custom headers on the WS handshake.

### Step 7 — Dashboard renders the data

**Tech:** JavaScript (vanilla today, React later)  
**File:** `frontend/public/suryaa-dashboard.html`

`applyTelemetry(msg.record)` writes DOM fields (`solarKw`, `socPct`, battery status, SOC ring, mini-charts, device table). One function call per tick — no polling.

| URL | Pipe |
|---|---|
| No query | EventSource `/api/stream` → simulator SSE |
| `source=backend&…` | WebSocket `ws://:8000` → ingest pipeline |

React later: same WebSocket, same record fields, `setState(msg.record)`. Pipeline unchanged.

### Technology role summary

| Technology | Role | Port / URL |
|---|---|---|
| Simulator | Generates one JSON tick per interval. Publishes to AMQP exchange only | Publisher, no server |
| aio-pika | Async AMQP 0-9-1 client. Publisher confirms + durable consumer | Library |
| RabbitMQ | Persistent buffer. Routes, stores, dead-letters | `:5672` AMQP · `:15672` Mgmt |
| Python worker | Consume, validate, Timescale, Redis, ACK after Timescale | Process, no HTTP |
| Pydantic v2 | Schema + range validation. `normalize_record` | Library |
| TimescaleDB | Hypertable history, compression, retention, idempotent upsert | `:5433` · DB `volta_ts` |
| Redis 7 | Latest snapshot (TTL) + pub/sub fan-out | `:6379` · `volta:live:{id}` |
| FastAPI | REST pull + WS push + JWT + `/ready` | `:8000` |
| Starlette WS | Framing, async tasks, heartbeat | Framework |
| Neon Postgres | Auth only — never telemetry rows | SSL (cloud) |
| HTML / JS | Two URL modes. React later: same WS | `:8765` |
| JWT | Stateless identity on REST + WS | Bearer / `?token=` · 15 min |
| RabbitMQ DLX | Poison messages | `telemetry.ingest.dead` |

### Why WebSocket specifically

| Option | Frequency at 1 Hz | Connection | Direction |
|---|---|---|---|
| Polling (`fetch`) | 1 HTTP request/s/tab | High (TCP + headers) | Pull only |
| SSE | 1 frame/s | 1 connection, push | Server → client only |
| **WebSocket** | 1 frame/s | 1 connection, push + pull | Bidirectional |

Chosen because: one persistent TCP connection, server-initiated push from Redis `PUBLISH`, bidirectional, works with Redis pub/sub across replicas, React-ready (`new WebSocket(url)`).

The simulator dashboard uses SSE because it is simpler for a single-process debug tool with no auth. The product uses WebSocket because it needs auth, multi-replica, and bidirectional potential.

```
Redis PUBLISH volta:live:ch:home_001  {json}
     → ws.py _pump_redis()
     → TelemetryRecord.model_validate_json(body)
     → websocket.send_json({ type, record, live })
     → Browser onmessage → applyTelemetry(msg.record)
     → DOM update
```

Heartbeat every 30 s: `{ type: "ping" }`. Browser ignores it. Proxy keepalive timers are satisfied.

---

## 15. Timescale vs Redis

Two different jobs at the same time.

| Store | Metaphor | Remembers |
|---|---|---|
| **TimescaleDB** | The accountant | Every tick, up to 90 days |
| **Redis** | The notice board | Only the latest tick per home |

### TimescaleDB — long-term memory

Every second the worker says: *“At 7:35:01 PM, home_001 produced 2.14 kW solar.”* The accountant writes one row. That row stays until retention drops it after 90 days.

Open the app at 8 PM and ask for the last 2 hours → Timescale answers with ~7 200 rows. Redis cannot do this — it only remembers the **latest** value.

**Hypertable:** hidden chunks by time (e.g. one per week). “Last 2 hours” opens the current chunk, not years of old data.

```sql
ON CONFLICT (household_id, time) DO UPDATE SET ...
```

Duplicate delivery overwrites the same value. No error, no duplicate row.

### Redis — real-time memory

After the accountant writes, the worker:

1. **Pins a sticky note** — `SET volta:live:home_001 {json} EX 120` (auto-deletes in 120 seconds if no new tick)
2. **Rings a bell** — `PUBLISH volta:live:ch:home_001 {json}` so every WebSocket listening gets the JSON

A new browser tab needs the current value *right now*. Redis answers in under 1 ms. Timescale would need a full SQL query.

Three users, two API servers: one `PUBLISH` and **both servers hear it**. Without the bell, only one server would know about the new tick.

### Sequence

```
Simulator publishes tick
        │
        ▼
RabbitMQ queues it (durable buffer)
        │
        ▼
Worker picks it up → Pydantic validates
        │
        ├──► TimescaleDB.upsert()     permanent row — history charts
        ├──► Redis.set_and_publish()  sticky note + bell — live WS
        └──► message.ack()            only AFTER both are handled
```

### Why not just one of them?

| Question | Answer |
|---|---|
| Why not Timescale for live too? | SQL ~5–50 ms. Redis ~0.1 ms. At 1 tick/s across many users, Timescale would get crushed. |
| Why not Redis for history too? | Redis lives in RAM. 90 days × 86 400 ticks = millions of rows, huge memory, gone on restart. |
| Why not pub/sub without SET? | New users connecting mid-stream would get nothing until the next tick. |
| Why not Timescale pub/sub? | No native pub/sub. Postgres `LISTEN/NOTIFY` does not fan out across replicas like Redis. |

### TTL

```
SET volta:live:home_001  {json}  EX 120
```

If the simulator stops, the sticky note disappears after 120 seconds. The dashboard sees **no live data** instead of stale 3-hour-old data labelled “live”.

> **One line.** TimescaleDB remembers everything so you can look back. Redis remembers only right now so the live dashboard is instant and every open tab gets the update in the same millisecond.

---

## 16. How RabbitMQ works

The broker sits **between** the simulator and the worker. Timescale and Redis sit **after** the worker ACKs. Think of it as a post office that never throws a letter away until the recipient signs for it.

### Post office, not a database

The simulator never waits for the dashboard. The ingest worker may be restarting, slow, or briefly down. RabbitMQ stores the envelope, routes it to the right pigeonhole, and only removes it when the recipient signs.

It does not understand solar power. It understands envelopes (messages), counters (exchanges), pigeonholes (queues), and signatures (acknowledgements).

### Protocol and library

Wire protocol: **AMQP 0-9-1** — binary TCP, not HTTP. Long-lived connection on port `5672`, many logical conversations (channels) on one socket.

```
amqp://volta:volta@localhost:5672/volta
```

| Part | Meaning |
|---|---|
| `amqp` | Protocol (`amqps` would be TLS on `5671`) |
| `volta` / `volta` | Username / password |
| `localhost:5672` | AMQP port (`15672` is only the management UI) |
| `/volta` | Virtual host — isolated namespace of exchanges and queues |

Both sides use **aio-pika**. `connect_robust()` reconnects if TCP drops. Connection names: `suryaa-simulator` and `volta-ingest-worker` (visible in the management UI). One connection per process, one channel per process.

### Background internals

RabbitMQ is Erlang on the BEAM VM — millions of tiny isolated processes, cheap message passing, crash isolation. On `docker compose` start, the node restores durable topology from `rabbitmq_data`. Persistent un-ACKed bodies are replayed. Each queue is its own Erlang process. Persistent publishes are written to disk before the publisher is told “I have it.” QoS prefetch limits how many deliveries sit unacked.

You never talk to those Erlang processes. AMQP is the public API. `:15672` is a read-only window onto the same state.

### Topology — furniture the worker builds

The worker owns this. The simulator must **not** create the queue. `declare_topology()` order:

1. Exchange `telemetry.dlx` — **fanout**, durable (every poison message to the same graveyard)
2. Queue `telemetry.ingest.dead` — durable, bound to the DLX
3. Exchange `telemetry` — **topic**, durable (routing key `telemetry.ingest`; future-proof for `telemetry.alerts`)
4. Queue `telemetry.ingest` — durable, `x-dead-letter-exchange = telemetry.dlx`, bound with key `telemetry.ingest`
5. `set_qos(prefetch_count=50)` — back-pressure if Timescale is slow

**Durable** = exchange / queue survive broker restart (disk metadata).  
**Persistent** = one message survives restart (`delivery_mode = 2`). You need **both**.

Publisher owns the exchange. Consumer owns the queue, the binding, and the dead-letter arguments.

### One tick as an AMQP envelope

| Field | Value |
|---|---|
| `body` | UTF-8 `record.model_dump_json()` |
| `content_type` | `application/json` |
| `delivery_mode` | `PERSISTENT` (`2`) |
| `app_id` | `suryaa-simulator` |
| `type` | `telemetry.ingest` |
| `headers` | `household_id`, `data_source` |
| `routing_key` | `telemetry.ingest` — **not** inside the JSON |

Publisher confirms (`publisher_confirms=True`) mean the **broker** accepted the letter (and wrote it to disk). That is **not** the worker saying “I stored the tick.”

If the broker is down, the simulator logs a warning and keeps ticking. Local SSE on `:8765` still updates. Best-effort publish: generator uptime &gt; one lost second of ingest.

### After publish

Exchange matches the binding → queue appends. No consumer → `messages_ready` grows. Worker connected → delivery is **unacked** until ACK. Worker crash with 12 unacked → those 12 return to ready. That is at-least-once. `prefetch=50` caps unacked; the rest stay ready.

`handle_message()` does **not** ACK first. Parse → validate → Timescale → try Redis → ACK.

### ACK, NACK, reject

| Verb | Meaning | When |
|---|---|---|
| `message.ack()` | Broker deletes the message | Schema valid **and** Timescale upsert succeeded. Redis failure still ACKs. |
| `message.nack(requeue=True)` | Temporary fail; deliver again | Timescale down, upsert threw, unexpected exception |
| `message.reject(requeue=False)` | Poison → DLX → `telemetry.ingest.dead` | Empty body, invalid JSON, Pydantic `ValidationError` |

An energy-balance warning is **not** a reject. Store with `status=warning` and ACK (HTTP 202).

No exactly-once. Crash after Timescale write but before ACK → redelivery. `ON CONFLICT DO UPDATE` makes the second write a no-op overwrite.

### Dead-lettering

`x-dead-letter-exchange` is a **queue argument**, not a field on the message. Dead-lettered messages get `x-death` headers (original queue, reason, count). This project only uses **rejected**. No per-message TTL, no `x-max-length`. Unread ticks wait.

### Back-pressure and a separate process

The simulator publishes at 1 Hz and never slows down for the database. Stopped worker → ready messages pile up on disk. Slow Timescale → prefetch caps Python RAM. The consumer is `python -m app.workers.ingest`, not a FastAPI route and not a lifespan task.

### What RabbitMQ is not doing

| Not doing | Who does |
|---|---|
| Validate `TelemetryRecord` | Pydantic in `pipeline.py` |
| Write history | Timescale |
| Update the live dashboard | Redis SET + PUBLISH, then FastAPI WS |
| Authenticate the homeowner | JWT on the read API. AMQP user `volta` is a broker login |

It is not a source of truth. `docker compose down -v` kills unread messages. Timescale rows already ACKed are safe. That is why ACK happens **after** the upsert.

### Glossary

| Term | Meaning in this project |
|---|---|
| Broker | The RabbitMQ server process |
| Vhost | Isolated namespace — `volta` |
| Connection | TCP socket (AMQP `:5672`) |
| Channel | Logical session on that socket |
| Exchange | Named router. Receives publishes. Does not store |
| Topic exchange | Routes by dotted routing key (`telemetry.ingest`) |
| Fanout exchange | Copies every message to every bound queue (our DLX) |
| Routing key | String the exchange matches against bindings |
| Binding | Rule: this exchange + this key → this queue |
| Queue | Ordered buffer that stores messages |
| Durable | Exchange / queue survive broker restart |
| Persistent | One message survives broker restart (`delivery_mode=2`) |
| Publisher confirm | Broker ACK of a publish. Not worker ACK |
| Consumer | Ingest worker |
| Prefetch / QoS | Max unacked messages (50) |
| Ready | In the queue, not yet delivered |
| Unacked | Delivered, still owned by the broker |
| ACK | Consumer done; broker deletes |
| NACK requeue | Temporary fail; deliver again |
| Reject no-requeue | Poison; dead-letter |
| DLX / DLQ | `telemetry.dlx` / `telemetry.ingest.dead` |
| At-least-once | A tick may be delivered more than once. Upsert absorbs it |
| Exactly-once | **Not provided.** Do not claim it in an interview |
| aio-pika | Async Python AMQP client |
| Robust connection | Reconnects after TCP failure |
| Best-effort | Simulator logs and continues if publish fails |
| Decoupling | Publisher and consumer have independent uptime |
| Back-pressure | Prefetch + queue depth slow the consumer, not the sender |
| `PRECONDITION_FAILED` | Second declare of a queue with different arguments. Delete the queue once, restart the worker |

### Sequence in one paragraph

Every second the simulator builds a `TelemetryRecord`, wraps it as a persistent AMQP message, and publishes it to the durable topic exchange `telemetry` with routing key `telemetry.ingest`. The broker confirms the publish, matches the binding, and appends the envelope to the durable queue `telemetry.ingest`. The ingest worker, running as its own process with prefetch 50 and manual acknowledgement, receives the body, validates it in pure Python, upserts Timescale, updates Redis if it can, and only then ACKs. A Timescale error NACKs and requeues. Invalid JSON or schema is rejected without requeue and lands on `telemetry.ingest.dead` via the fanout DLX. RabbitMQ never sees solar physics, never talks to Redis, and never serves the dashboard. It only keeps the letter safe until the worker signs for it.

> **One line.** RabbitMQ is the durable, restart-safe buffer between a generator that must keep ticking and a worker that must not lose a valid tick: topic route in, queue hold, prefetch limit, ACK after Timescale, DLQ for poison, at-least-once plus idempotent upsert.
