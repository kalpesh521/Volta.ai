"""Shared current location + generator hook for the dashboard."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from simulator.models import LocationRecord, default_location

if TYPE_CHECKING:
    from http.server import ThreadingHTTPServer

    from simulator.telemetry_generator import TelemetryGenerator

current_location: LocationRecord = default_location()
active_generator: TelemetryGenerator | None = None
dashboard_server: ThreadingHTTPServer | None = None
stop_requested = threading.Event()


def request_stop() -> None:
    """Stop ticks, SSE waiters, and the dashboard HTTP server."""
    stop_requested.set()
    from simulator.dashboard.live_bus import bus

    bus.close()
    server = dashboard_server
    if server is not None:
        threading.Thread(target=server.shutdown, name="suryaa-http-stop", daemon=True).start()
