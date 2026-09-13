# Volta.ai frontend

Static pages only (no build step). React will replace these later; the
backend `/energy/*` and `/ws/energy` contracts stay.

```
frontend/
├── public/
│   ├── energy-console.html     Temporary energy API tester (FastAPI :8000/ui)
│   ├── suryaa-dashboard.html   Live home-energy UI (simulator :8765)
│   └── suryaa_demo.html        Onboarding / demo walkthrough
└── docs.txt
```

## Energy console (use this while building backend / AI)

Same origin as the API — login, REST, and WebSocket without CORS:

```text
http://127.0.0.1:8000/ui/energy
```

Also served on the simulator HTML port at `http://127.0.0.1:8765/energy`.

## Simulator live dashboard

The simulator (`python -m simulator.main --dashboard`) serves
`public/suryaa-dashboard.html` at http://127.0.0.1:8765. Open `suryaa_demo.html`
directly in a browser.
