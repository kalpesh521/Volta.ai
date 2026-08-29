# Volta.ai

Monorepo for the Suryaa / Volta solar-home product: FastAPI backend, static frontend, and a standalone telemetry simulator.

```
volta.ai/
├── backend/          FastAPI API (auth, onboarding) — PostgreSQL + Alembic
├── frontend/         Static HTML (dashboard + demo)
│   └── public/
├── simulator/        Standalone solar-home telemetry generator
└── docs/             Design / interview notes
```

Each package has its own README, dependencies, and virtualenv. They do not share a runtime.

## Telemetry infra (local)

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

Live dashboard over backend WebSocket (API + worker running):

```text
http://127.0.0.1:8765/?source=backend&token=ACCESS_JWT&household=home_001
```

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
