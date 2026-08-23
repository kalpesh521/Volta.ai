"""
Tiny stdlib HTTP + SSE server for the live dashboard.

No FastAPI. Serves frontend/suryaa-dashboard.html and streams telemetry
from `live_bus`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from simulator.config import PROJECT_ROOT, SimulatorConfig
from simulator.geocoding_client import GeocodingClient
from simulator.live_bus import bus
from simulator import location_state
from simulator.models import LocationRecord, default_location

logger = logging.getLogger("suryaa.dashboard")

DASHBOARD_HTML = PROJECT_ROOT / "frontend" / "suryaa-dashboard.html"


def _json_bytes(payload: object, status: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(payload, default=str).encode("utf-8")
    return status, body, "application/json; charset=utf-8"


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "SuryaaDashboard/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path in ("/", "/dashboard", "/suryaa-dashboard.html"):
            self._serve_html()
            return
        if path == "/api/latest":
            latest = bus.latest()
            self._send(*_json_bytes({"ok": latest is not None, "record": latest}))
            return
        if path == "/api/history":
            query = parse_qs(parsed.query)
            limit = int((query.get("limit") or ["120"])[0])
            self._send(*_json_bytes({"records": bus.history(limit)}))
            return
        if path == "/api/stream":
            self._stream()
            return
        if path == "/api/geocode":
            self._geocode(parse_qs(parsed.query))
            return
        if path == "/api/location":
            loc = location_state.current_location or default_location()
            self._send(*_json_bytes({"location": loc.model_dump(mode="json")}))
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/api/stop":
            self._stop()
            return
        if path != "/api/location":
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send(400, b'{"error":"invalid json"}', "application/json")
            return
        self._apply_location(body)

    def _serve_html(self) -> None:
        if not DASHBOARD_HTML.exists():
            self._send(404, b"dashboard html missing", "text/plain")
            return
        body = DASHBOARD_HTML.read_bytes()
        self._send(200, body, "text/html; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self) -> None:
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        seq, latest = bus.snapshot()
        if latest is not None:
            self._write_event(latest)

        try:
            while not bus.closed:
                seq, record = bus.wait_next(seq, timeout=15.0)
                if bus.closed:
                    return
                if record is None:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._write_event(record)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _active_config(self) -> SimulatorConfig:
        gen = location_state.active_generator
        return gen.config if gen is not None else SimulatorConfig()

    def _geocode(self, query: dict[str, list[str]]) -> None:
        name = (query.get("q") or query.get("name") or [""])[0]
        country = (query.get("country") or query.get("country_code") or [""])[0] or None
        try:
            matches = asyncio.run(GeocodingClient(self._active_config()).search(name, country_code=country))
        except Exception as exc:  # noqa: BLE001
            self._send(*_json_bytes({"ok": False, "error": str(exc), "results": []}, status=502))
            return
        self._send(*_json_bytes({
            "ok": True,
            "results": [item.model_dump(mode="json") for item in matches],
        }))

    def _apply_location(self, body: dict) -> None:
        gen = location_state.active_generator
        config = self._active_config()
        name = str(body.get("name") or body.get("query") or "").strip()
        try:
            lat = body.get("latitude")
            lon = body.get("longitude")
            source = str(body.get("source") or "")
            coords_only = lat is not None and lon is not None and (
                source == "browser-gps" or not (name and body.get("timezone"))
            )
            if coords_only:
                location = asyncio.run(
                    GeocodingClient(config).from_coordinates(float(lat), float(lon))
                )
            elif lat is not None and lon is not None:
                location = LocationRecord.model_validate(body)
            elif name:
                location = asyncio.run(GeocodingClient(config).resolve(name))
            else:
                self._send(400, b'{"error":"name or coordinates required"}', "application/json")
                return
            if gen is not None:
                tz = ZoneInfo(location.timezone)
                asyncio.run(gen.apply_location(location, datetime.now(tz=tz)))
            else:
                location_state.current_location = location
        except Exception as exc:  # noqa: BLE001
            self._send(*_json_bytes({"ok": False, "error": str(exc)}, status=500))
            return
        self._send(*_json_bytes({
            "ok": True,
            "location": location_state.current_location.model_dump(mode="json"),
        }))

    def _client_is_local(self) -> bool:
        host = self.client_address[0] if self.client_address else ""
        return host in {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}

    def _stop(self) -> None:
        if not self._client_is_local():
            self._send(*_json_bytes({"ok": False, "error": "stop is local-only"}, status=403))
            return
        self._send(*_json_bytes({
            "ok": True,
            "stopped": True,
            "message": "Simulator stopping",
        }))
        threading.Thread(target=_halt_process, name="suryaa-stop", daemon=True).start()

    def _write_event(self, record: dict) -> None:
        payload = json.dumps(record, default=str)
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()


def _halt_process() -> None:
    time.sleep(0.2)
    logger.info("Stop requested from dashboard; shutting down")
    location_state.request_stop()
    try:
        os.kill(os.getpid(), signal.SIGINT)
    except OSError:
        os._exit(0)


def start_dashboard_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    location_state.dashboard_server = server
    thread = threading.Thread(target=server.serve_forever, name="suryaa-dashboard", daemon=True)
    thread.start()
    logger.info("Dashboard listening on http://%s:%s", host, port)
    return server
