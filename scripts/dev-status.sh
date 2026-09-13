#!/usr/bin/env bash
# Print whether Docker, ingest, API, and simulator are up.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

ensure_dirs

alive() {
  local name="$1" pidfile="$2" pattern="$3"
  local pid=""
  [[ -f "$pidfile" ]] && pid="$(tr -d '[:space:]' < "$pidfile" || true)"
  if is_pid_alive "$pid"; then
    printf '  %-12s running  pid=%s\n' "$name" "$pid"
    return 0
  fi
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    printf '  %-12s running  (not started by dev-up.sh)\n' "$name"
    return 0
  fi
  printf '  %-12s down\n' "$name"
  return 1
}

echo "Processes"
alive "ingest" "$INGEST_PID_FILE" '[p]ython -m app.workers.ingest' || true
alive "uvicorn" "$UVICORN_PID_FILE" '[u]vicorn main:app' || true
alive "simulator" "$SIM_PID_FILE" '[p]ython -m simulator.main' || true

echo
echo "HTTP"
curl -fsS -m 4 "http://${API_HOST}:${API_PORT}/health" >/dev/null 2>&1 \
  && echo "  /health      ok" \
  || echo "  /health      down"
if body="$(curl -fsS -m 4 "http://${API_HOST}:${API_PORT}/ready" 2>/dev/null)"; then
  echo "  /ready       $body"
else
  echo "  /ready       down"
fi
curl -fsS -m 3 "http://${DASH_HOST}:${DASH_PORT}/" >/dev/null 2>&1 \
  && echo "  dashboard    http://${DASH_HOST}:${DASH_PORT}" \
  || echo "  dashboard    down"

echo
echo "Docker"
if command -v docker >/dev/null 2>&1; then
  docker compose -f "$ROOT/docker-compose.yml" --project-directory "$ROOT" ps || true
else
  echo "  docker not on PATH"
fi

if [[ -f "$URL_FILE" ]]; then
  echo
  echo "Last backend dashboard URL"
  echo "  $(cat "$URL_FILE")"
  echo "  (token expires ~15 min — run ./scripts/dev-url.sh)"
fi
