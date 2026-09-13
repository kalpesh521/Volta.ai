#!/usr/bin/env bash
# Stop ingest worker, FastAPI, and simulator. Docker stays up unless --wipe.
#
#   ./scripts/dev-down.sh
#   ./scripts/dev-down.sh --wipe   # also docker compose down -v
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

WIPE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      echo "Usage: $0 [--wipe]"
      exit 0
      ;;
    --wipe)
      WIPE=1
      shift
      ;;
    *)
      die "Unknown flag: $1"
      ;;
  esac
done

ensure_dirs
log "Stopping simulator, uvicorn, ingest worker"
stop_python_stack

if [[ "$WIPE" -eq 1 ]]; then
  warn "Stopping Docker and deleting volumes"
  docker compose -f "$ROOT/docker-compose.yml" --project-directory "$ROOT" down -v
else
  log "Leaving Docker running (use --wipe to remove volumes)"
fi

rm -f "$URL_FILE" "$RUN_DIR/access_token"
ok "Python stack stopped"
