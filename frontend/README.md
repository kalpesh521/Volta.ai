# Volta.ai frontend

Static HTML only — **no npm, Vite, or React yet**. These pages exist so
backend and AI work can be tested in a browser. A React app will replace
them later. **Do not change the API contract to suit this HTML.**

```
frontend/
├── public/
│   ├── energy-console.html     Temporary energy API tester
│   ├── suryaa-dashboard.html   Live home-energy UI (simulator)
│   └── suryaa_demo.html        Onboarding / demo walkthrough
└── docs.txt
```

---

## Now vs React later

| Now | Later |
|---|---|
| One HTML file, no build step | React SPA |
| Talks to `/auth/login`, `/energy/me/*`, `/ws/energy` | Same JSON and WebSocket |
| Served by FastAPI on **`:8000`** (same origin as the API) | React still calls `:8000` (or a reverse proxy) |
| Simulator `:8765` stays a generator debug UI | Unchanged |

APIs are the product. HTML is only a probe.

The console is **not served** when `ENVIRONMENT=production`
(`backend/app/webui.py` returns 404).

---

## Energy console (use this while building backend / AI)

Same origin as FastAPI — login, REST, and WebSocket **without CORS**.
This is the page to use for energy-module checks (not port `8766`).

```text
http://127.0.0.1:8000/ui/energy
```

Aliases (same file): `/ui`, `/ui/energy-console.html`.

Also served on the simulator HTML port (after the simulator process is
started with the current code):

```text
http://127.0.0.1:8765/energy
```

Prefer **`:8000/ui/energy`**. Opening `:8766` used to 403 the WebSocket
because that origin was not trusted.

### Open it

1. Start the stack (`./scripts/dev-up.sh` from the repo root), **or** at
   least `uvicorn main:app --reload` from `backend/`.
2. Either:
   - Open `/ui/energy` and log in (email + password), or
   - Print a URL with a fresh access token (~15 min):

```bash
./scripts/dev-url.sh
```

That prints `http://127.0.0.1:8000/ui/energy?token=<access_token>`.

Local test user lives in `scripts/dev-credentials.env` (gitignored).

### What it calls

| Tab | Backend |
|---|---|
| Live + WS | `GET /energy/me/live` then `WS /ws/energy` |
| History | `GET /energy/me/history` |
| Daily | `GET /energy/me/daily` |
| Hourly | `GET /energy/me/hourly` |
| Profile | `GET /energy/me/profile` |
| Battery / grid / devices / weather | `GET /energy/{id}/battery` (and `/grid`, `/devices`, `/weather`) |
| Raw JSON | last payload for debugging |

The home dropdown is `GET /energy/me/homes`. Omit `household_id` on `/me/*`
to use the primary completed home from the JWT. Do not put `home_001` in
the URL.

Live solar **0.00 kW** after sunset is expected when `--weather-mode live`
is on. Load, SOC, and battery status should still tick.

### Code

| Piece | Path |
|---|---|
| HTML | `frontend/public/energy-console.html` |
| FastAPI routes | `backend/app/webui.py` (`GET /ui`, `/ui/energy`) |
| Wired in | `backend/app/main.py` |
| Simulator alias | `simulator/dashboard/server.py` (`/energy`) |

When the page is served from `:8000`, `fetch` and WebSocket are same-origin.
When it is served from `:8765`, the script calls `http://127.0.0.1:8000`.

---

## Simulator live dashboard

Generator SSE (no login) — debug ticks only, not the product path:

```text
http://127.0.0.1:8765
```

Product WebSocket (JWT, ingest pipeline must be running):

```text
http://127.0.0.1:8765/?source=backend&token=<access_token>
```

Served by `python -m simulator.main --dashboard` from the **repo root**.
File: `public/suryaa-dashboard.html`.

---

## Onboarding demo

Open `public/suryaa_demo.html` directly in a browser (no server required).
