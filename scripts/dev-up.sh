#!/usr/bin/env bash
# Start Docker + ingest worker + FastAPI + simulator, then print a dashboard URL.
#
#   ./scripts/dev-up.sh
#   ./scripts/dev-up.sh --email you@example.com --password secret
#   ./scripts/dev-up.sh --wipe     # also wipe RabbitMQ / Timescale / Redis volumes
#   ./scripts/dev-up.sh --open     # open the backend dashboard in a browser
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

WIPE=0
OPEN_BROWSER=0
load_credentials

usage() {
  cat <<'EOF'
Start the full local Volta stack with one command.

  ./scripts/dev-up.sh
  ./scripts/dev-up.sh --email you@example.com --password secret
  ./scripts/dev-up.sh --wipe
  ./scripts/dev-up.sh --open

Order: Docker (healthy) → ingest worker → uvicorn /ready → simulator → login.

Credentials (first match wins):
  1. --email / --password
  2. VOLTA_DEV_EMAIL / VOLTA_DEV_PASSWORD
  3. scripts/dev-credentials.env

Stop later with: ./scripts/dev-down.sh
Status:          ./scripts/dev-status.sh
Fresh JWT URL:   ./scripts/dev-url.sh
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --wipe)
      WIPE=1
      shift
      ;;
    --open)
      OPEN_BROWSER=1
      shift
      ;;
    --email)
      VOLTA_DEV_EMAIL="${2:?--email needs a value}"
      shift 2
      ;;
    --password)
      VOLTA_DEV_PASSWORD="${2:?--password needs a value}"
      shift 2
      ;;
    *)
      die "Unknown flag: $1 (try --help)"
      ;;
  esac
done

ensure_dirs
require_venvs
command -v docker >/dev/null || die "docker is not on PATH"
command -v curl >/dev/null || die "curl is not on PATH"

if [[ "$WIPE" -eq 1 ]]; then
  warn "Wiping Docker volumes (queues + Timescale ticks + Redis)"
  stop_python_stack
  docker compose -f "$ROOT/docker-compose.yml" --project-directory "$ROOT" down -v
else
  log "Stopping leftover Python processes (Docker stays up)"
  stop_python_stack
fi

log "Starting Docker (RabbitMQ :5672, Timescale :5433, Redis :6379)"
docker compose -f "$ROOT/docker-compose.yml" --project-directory "$ROOT" up -d
wait_compose_healthy 120

log "Starting ingest worker"
start_detached "$INGEST_PID_FILE" "$INGEST_LOG" "$BACKEND_DIR" \
  "$BACKEND_PY" -m app.workers.ingest
wait_for_log "$INGEST_LOG" "ingest worker consuming" 90 "ingest worker consuming queue" \
  || die "Ingest worker failed. See $INGEST_LOG"

log "Starting FastAPI (uvicorn --reload)"
start_detached "$UVICORN_PID_FILE" "$UVICORN_LOG" "$BACKEND_DIR" \
  "$BACKEND_DIR/venv/bin/uvicorn" main:app --reload --host "$API_HOST" --port "$API_PORT"
wait_http_ok "http://${API_HOST}:${API_PORT}/health" 60 "GET /health" \
  || { tail -n 40 "$UVICORN_LOG" >&2; die "uvicorn failed. See $UVICORN_LOG"; }
wait_ready 90 || { tail -n 40 "$UVICORN_LOG" >&2; die "API not ready"; }
wait_onboarding_profiles 90 || { tail -n 40 "$UVICORN_LOG" >&2; die "onboarding profiles not reachable"; }

log "Starting simulator (live weather + dashboard + RabbitMQ)"
start_detached "$SIM_PID_FILE" "$SIM_LOG" "$ROOT" \
  "$SIM_PY" -m simulator.main \
    --rabbitmq-url "$RABBITMQ_URL" \
    --weather-mode live \
    --dashboard \
    --quiet
wait_for_log "$SIM_LOG" "Dashboard listening" 90 "simulator dashboard :$DASH_PORT" \
  || die "Simulator failed. See $SIM_LOG"

if grep -q "onboarding profiles unavailable" "$SIM_LOG"; then
  warn "Simulator started before profiles loaded; it will retry. Check $SIM_LOG"
else
  ok "Simulator reached the API for onboarding profiles"
fi

login_and_write_url || true

if [[ "$OPEN_BROWSER" -eq 1 && -f "$URL_FILE" ]]; then
  url="$(tr -d '[:space:]' < "$URL_FILE")"
  if command -v wslview >/dev/null 2>&1; then
    wslview "$url" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 || true
  fi
fi

ok "Stack is up. Logs: $LOG_DIR"
ok "Stop with: $SCRIPT_DIR/dev-down.sh"
