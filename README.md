# Volta.ai

Monorepo for the Suryaa / Volta solar-home product: FastAPI backend, static frontend, and a standalone telemetry simulator.

```
volta.ai/
├── backend/          FastAPI API (auth, onboarding, energy, AI assistant) — PostgreSQL + Alembic
├── frontend/         Static HTML (dashboard + demo)
│   └── public/
├── simulator/        Standalone solar-home telemetry generator
└── docs/             Design / interview notes
```

Each package has its own README, dependencies, and virtualenv. They do not share a runtime.

## Run everything with Docker (one command)

```bash
cp backend/.env.example backend/.env   # once; set JWT_SECRET_KEY and OPENROUTER_API_KEY
docker compose up -d --build --wait    # build, start in dependency order, wait until healthy
docker compose ps
docker compose down                    # stop, keep data   (down -v wipes all data)
```

| Service | What | Host URL (127.0.0.1 only) |
|---|---|---|
| `postgres` | auth / onboarding / knowledge DB (pgvector) | `:5434` |
| `timescaledb`, `redis`, `rabbitmq` | telemetry history, live snapshot, queue | `:5433`, `:6379`, `:5672` / UI `:15672` |
| `migrate` | one-shot `alembic upgrade head` | n/a |
| `api` | FastAPI | http://127.0.0.1:8000/docs, `/ui/energy` |
| `ingest-worker`, `device-worker` | RabbitMQ consumers | n/a |
| `simulator` | telemetry generator + dashboard | http://127.0.0.1:8765 |

Hostnames inside the stack override anything in `backend/.env`, so that file only needs secrets and AI keys.
Ports, passwords and `APP_ENV` can be overridden through a root `.env` (see `.env.example`).
If a port is taken, set e.g. `API_PORT=8010`. Logs: `docker compose logs -f api`.

## Telemetry infra (local, without Docker for the apps)

RabbitMQ, TimescaleDB, and Redis run in Docker. Neon stays the auth database.

```bash
docker compose up -d
```

- RabbitMQ AMQP: `amqp://volta:volta@localhost:5672/volta`
- RabbitMQ UI: http://127.0.0.1:15672 (volta / volta)
- TimescaleDB: `localhost:5433` database `volta_ts`
- Redis: `localhost:6379`

Ingest worker (from `backend/`, after compose is up):

```bash
python -m app.workers.ingest
```

Live dashboard over backend WebSocket (API + worker + simulator running).
The login token is enough — the API binds it to the user's primary home:

```text
http://127.0.0.1:8765/?source=backend&token=ACCESS_JWT
```

Optional `&household=home_…` switches to a non-primary home. Do not put `home_001` in the URL.

## Backend

```bash
cd backend
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then set DATABASE_URL and JWT_SECRET_KEY
alembic upgrade head
uvicorn main:app --reload
```

API docs: http://localhost:8000/docs  
Health: http://localhost:8000/health

```bash
pytest -v
```

AI assistant (read-only, LangChain + LangGraph): add one LLM key to `backend/.env`
(cheapest: `GOOGLE_API_KEY` with `gemini-3.5-flash-lite`), then `POST /assistant/me/ask`.
See [docs/ai_implementation_report.md](docs/ai_implementation_report.md).

## Simulator

Run from the **repository root** so `python -m simulator.main` can import the package:

```bash
python3.12 -m venv simulator/.venv
source simulator/.venv/bin/activate
pip install -r simulator/requirements.txt
python -m simulator.main --dashboard --weather-mode live
```

Dashboard: http://127.0.0.1:8765

## Frontend

Static pages in `frontend/public/`. The live energy dashboard is served by the simulator (`--dashboard`); open `suryaa_demo.html` in a browser for the onboarding demo.
