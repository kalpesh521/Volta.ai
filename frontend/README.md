# Volta.ai frontend

Static pages only (no build step).

```
frontend/
├── public/
│   ├── suryaa-dashboard.html   Live home-energy UI (served by the simulator)
│   └── suryaa_demo.html        Onboarding / demo walkthrough
└── docs.txt
```

The simulator dashboard server (`python -m simulator.main --dashboard`) serves `public/suryaa-dashboard.html` at http://127.0.0.1:8765. Open `suryaa_demo.html` directly in a browser.
