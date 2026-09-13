# Shared helpers for the local one-command stack.
# shellcheck shell=bash

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$ROOT/scripts"
RUN_DIR="$SCRIPTS/.run"
LOG_DIR="$SCRIPTS/.logs"
CREDENTIALS_FILE="$SCRIPTS/dev-credentials.env"

BACKEND_DIR="$ROOT/backend"
BACKEND_PY="$BACKEND_DIR/venv/bin/python"
SIM_PY="$ROOT/simulator/.venv/bin/python"

API_HOST="127.0.0.1"
API_PORT="8000"
DASH_HOST="127.0.0.1"
DASH_PORT="8765"
RABBITMQ_URL="amqp://volta:volta@localhost:5672/volta"

INGEST_PID_FILE="$RUN_DIR/ingest.pid"
UVICORN_PID_FILE="$RUN_DIR/uvicorn.pid"
SIM_PID_FILE="$RUN_DIR/simulator.pid"
URL_FILE="$RUN_DIR/dashboard.url"

INGEST_LOG="$LOG_DIR/ingest.log"
UVICORN_LOG="$LOG_DIR/uvicorn.log"
SIM_LOG="$LOG_DIR/simulator.log"

RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
CYAN=$'\033[0;36m'
BOLD=$'\033[1m'
RESET=$'\033[0m'

log() { printf '%s%s%s %s\n' "$CYAN" "[volta]" "$RESET" "$*"; }
ok() { printf '%s%s%s %s\n' "$GREEN" "[ok]" "$RESET" "$*"; }
warn() { printf '%s%s%s %s\n' "$YELLOW" "[wait]" "$RESET" "$*"; }
fail() { printf '%s%s%s %s\n' "$RED" "[fail]" "$RESET" "$*" >&2; }

die() {
  fail "$*"
  exit 1
}

ensure_dirs() {
  mkdir -p "$RUN_DIR" "$LOG_DIR"
}

load_credentials() {
  [[ -f "$CREDENTIALS_FILE" ]] || return 0
  local saved_email="${VOLTA_DEV_EMAIL:-}"
  local saved_password="${VOLTA_DEV_PASSWORD:-}"
  # shellcheck disable=SC1090
  set -a
  source "$CREDENTIALS_FILE"
  set +a
  if [[ -n "$saved_email" ]]; then
    VOLTA_DEV_EMAIL="$saved_email"
  fi
  if [[ -n "$saved_password" ]]; then
    VOLTA_DEV_PASSWORD="$saved_password"
  fi
}

require_venvs() {
  [[ -x "$BACKEND_PY" ]] || die "Backend venv missing. Create it with:
  cd \"$BACKEND_DIR\" && python3.12 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
  [[ -x "$SIM_PY" ]] || die "Simulator venv missing. Create it with:
  cd \"$ROOT\" && python3.12 -m venv simulator/.venv && source simulator/.venv/bin/activate && pip install -r simulator/requirements.txt"
  [[ -f "$BACKEND_DIR/.env" ]] || die "backend/.env is missing. Copy backend/.env.example and fill DATABASE_URL / JWT_SECRET_KEY."
}

is_pid_alive() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

kill_pidfile() {
  local file="$1"
  [[ -f "$file" ]] || return 0
  local pid
  pid="$(tr -d '[:space:]' < "$file" || true)"
  if is_pid_alive "$pid"; then
    kill "$pid" 2>/dev/null || true
    sleep 0.4
    if is_pid_alive "$pid"; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$file"
}

kill_matching() {
  local pattern="$1"
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    pkill -f "$pattern" 2>/dev/null || true
    sleep 0.4
    pkill -9 -f "$pattern" 2>/dev/null || true
  fi
}

stop_python_stack() {
  kill_pidfile "$SIM_PID_FILE"
  kill_pidfile "$UVICORN_PID_FILE"
  kill_pidfile "$INGEST_PID_FILE"
  # Also stop copies started by hand in other terminals.
  kill_matching '[p]ython -m simulator.main'
  kill_matching '[u]vicorn main:app'
  kill_matching '[p]ython -m app.workers.ingest'
  sleep 0.5
}

start_detached() {
  local pidfile="$1"
  local logfile="$2"
  local cwd="$3"
  shift 3
  : > "$logfile"
  (
    cd "$cwd"
    exec env PYTHONUNBUFFERED=1 "$@"
  ) >>"$logfile" 2>&1 &
  echo $! > "$pidfile"
}

wait_for_log() {
  local file="$1"
  local pattern="$2"
  local timeout="${3:-60}"
  local label="${4:-log pattern}"
  local i=0
  while (( i < timeout )); do
    if [[ -f "$file" ]] && grep -Eq "$pattern" "$file"; then
      ok "$label"
      return 0
    fi
    if (( i % 5 == 0 )); then
      warn "waiting for $label (${i}s/${timeout}s)"
    fi
    sleep 1
    ((i += 1))
  done
  fail "timed out waiting for $label"
  if [[ -f "$file" ]]; then
    tail -n 40 "$file" >&2
  fi
  return 1
}

wait_http_ok() {
  local url="$1"
  local timeout="${2:-60}"
  local label="${3:-$url}"
  local i=0
  local body=""
  while (( i < timeout )); do
    if body="$(curl -fsS -m 3 "$url" 2>/dev/null)"; then
      ok "$label → $body"
      return 0
    fi
    if (( i % 5 == 0 )); then
      warn "waiting for $label (${i}s/${timeout}s)"
    fi
    sleep 1
    ((i += 1))
  done
  fail "timed out waiting for $label"
  return 1
}

wait_ready() {
  local timeout="${1:-90}"
  local i=0
  while (( i < timeout )); do
    local body=""
    if body="$(curl -fsS -m 4 "http://${API_HOST}:${API_PORT}/ready" 2>/dev/null)"; then
      if printf '%s' "$body" | grep -q '"timescale":true' && printf '%s' "$body" | grep -q '"redis":true'; then
        ok "/ready → $body"
        return 0
      fi
    fi
    if (( i % 5 == 0 )); then
      warn "waiting for API /ready timescale+redis (${i}s/${timeout}s)"
    fi
    sleep 1
    ((i += 1))
  done
  fail "API /ready did not report timescale+redis"
  curl -sS -m 5 "http://${API_HOST}:${API_PORT}/ready" || true
  echo
  return 1
}

ingest_token() {
  local token="dev-ingest-token"
  local line=""
  if [[ -f "$BACKEND_DIR/.env" ]]; then
    line="$(grep -E '^INGEST_TOKEN=' "$BACKEND_DIR/.env" | tail -n 1 || true)"
    if [[ -n "$line" ]]; then
      token="${line#INGEST_TOKEN=}"
      token="${token%\"}"
      token="${token#\"}"
      token="${token%\'}"
      token="${token#\'}"
    fi
  fi
  printf '%s' "$token"
}

wait_onboarding_profiles() {
  local timeout="${1:-90}"
  local i=0
  local token
  token="$(ingest_token)"
  while (( i < timeout )); do
    local http=""
    http="$(curl -sS -m 20 -o /dev/null -w '%{http_code}' \
      -H "X-Ingest-Token: $token" \
      "http://${API_HOST}:${API_PORT}/energy/onboarding-profiles" || true)"
    if [[ "$http" == "200" ]]; then
      ok "GET /energy/onboarding-profiles"
      return 0
    fi
    if (( i % 5 == 0 )); then
      warn "waiting for onboarding profiles HTTP $http (${i}s/${timeout}s)"
    fi
    sleep 2
    ((i += 2))
  done
  fail "GET /energy/onboarding-profiles did not return 200"
  return 1
}

wait_compose_healthy() {
  local timeout="${1:-90}"
  local i=0
  while (( i < timeout )); do
    local ids
    ids="$(docker compose -f "$ROOT/docker-compose.yml" --project-directory "$ROOT" ps -q 2>/dev/null || true)"
    if [[ -z "$ids" ]]; then
      warn "waiting for compose containers (${i}s/${timeout}s)"
      sleep 1
      ((i += 1))
      continue
    fi
    local all_ok=1
    local id health
    for id in $ids; do
      health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || echo missing)"
      if [[ "$health" != "healthy" ]]; then
        all_ok=0
        break
      fi
    done
    if (( all_ok == 1 )); then
      ok "Docker: rabbitmq, timescaledb, redis are healthy"
      return 0
    fi
    if (( i % 5 == 0 )); then
      warn "waiting for Docker healthchecks (${i}s/${timeout}s)"
    fi
    sleep 1
    ((i += 1))
  done
  docker compose -f "$ROOT/docker-compose.yml" --project-directory "$ROOT" ps >&2 || true
  die "Docker services did not become healthy. Is Docker Desktop / the daemon running?"
}

extract_access_token() {
  "$BACKEND_PY" - "$1" <<'PY'
import json, sys
raw = sys.argv[1]
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    print(raw, file=sys.stderr)
    sys.exit(1)
token = data.get("access_token") or (data.get("data") or {}).get("access_token")
if not token:
    print(raw, file=sys.stderr)
    sys.exit(1)
print(token)
PY
}

login_and_write_url() {
  local email="${VOLTA_DEV_EMAIL:-}"
  local password="${VOLTA_DEV_PASSWORD:-}"
  if [[ -z "$email" || -z "$password" ]]; then
    warn "No login credentials. Set scripts/dev-credentials.env or pass --email / --password."
    printf 'http://%s:%s\n' "$DASH_HOST" "$DASH_PORT" > "$URL_FILE"
    return 1
  fi

  local tmp http body payload
  payload="$(VOLTA_DEV_EMAIL="$email" VOLTA_DEV_PASSWORD="$password" "$BACKEND_PY" - <<'PY'
import json, os
print(json.dumps({"email": os.environ["VOLTA_DEV_EMAIL"], "password": os.environ["VOLTA_DEV_PASSWORD"]}))
PY
)"
  tmp="$(mktemp)"
  http="$(curl -sS -m 20 -o "$tmp" -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    -d "$payload" \
    "http://${API_HOST}:${API_PORT}/auth/login" || true)"
  body="$(cat "$tmp")"
  rm -f "$tmp"

  if [[ "$http" != "200" ]]; then
    fail "Login failed HTTP $http for $email"
    printf '%s\n' "$body" >&2
    printf 'http://%s:%s\n' "$DASH_HOST" "$DASH_PORT" > "$URL_FILE"
    return 1
  fi

  local token
  token="$(extract_access_token "$body")" || {
    fail "Login JSON had no access_token"
    return 1
  }

  local backend_url sim_url console_url
  backend_url="http://${DASH_HOST}:${DASH_PORT}/?source=backend&token=${token}"
  sim_url="http://${DASH_HOST}:${DASH_PORT}"
  console_url="http://${API_HOST}:${API_PORT}/ui/energy?token=${token}"
  printf '%s\n' "$console_url" > "$URL_FILE"
  printf '%s\n' "$token" > "$RUN_DIR/access_token"

  echo
  printf '%sEnergy console (API :8000, login + /energy/*)%s\n  %s\n' "$BOLD" "$RESET" "$console_url"
  printf '%sDashboard (simulator SSE, no login)%s\n  %s\n' "$BOLD" "$RESET" "$sim_url"
  printf '%sDashboard (backend WebSocket)%s\n  %s\n' "$BOLD" "$RESET" "$backend_url"
  printf '%sLogged in as%s %s  %s(access token ~15 min)%s\n' "$BOLD" "$RESET" "$email" "$YELLOW" "$RESET"
  echo
  ok "Refresh the URL later with: $SCRIPTS/dev-url.sh"
  return 0
}
