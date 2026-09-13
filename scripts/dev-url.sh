#!/usr/bin/env bash
# Login again and print a fresh backend dashboard URL (access token lasts ~15 min).
#
#   ./scripts/dev-url.sh
#   ./scripts/dev-url.sh --email you@example.com --password secret
#   ./scripts/dev-url.sh --open
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

OPEN_BROWSER=0
load_credentials
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      echo "Usage: $0 [--email EMAIL] [--password PASS] [--open]"
      exit 0
      ;;
    --open)
      OPEN_BROWSER=1
      shift
      ;;
    --email)
      VOLTA_DEV_EMAIL="${2:?}"
      shift 2
      ;;
    --password)
      VOLTA_DEV_PASSWORD="${2:?}"
      shift 2
      ;;
    *)
      die "Unknown flag: $1"
      ;;
  esac
done

ensure_dirs
require_venvs

curl -fsS -m 5 "http://${API_HOST}:${API_PORT}/health" >/dev/null \
  || die "API is not up. Run ./scripts/dev-up.sh first."

login_and_write_url || die "Could not issue an access token."

if [[ "$OPEN_BROWSER" -eq 1 && -f "$URL_FILE" ]]; then
  url="$(tr -d '[:space:]' < "$URL_FILE")"
  if command -v wslview >/dev/null 2>&1; then
    wslview "$url" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 || true
  fi
fi
