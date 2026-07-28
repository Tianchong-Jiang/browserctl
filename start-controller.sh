#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
: "${BROWSERCTL_CDP_ENDPOINT:?BROWSERCTL_CDP_ENDPOINT is required}"
: "${BROWSERCTL_HOST:=127.0.0.1}"
: "${BROWSERCTL_PORT:=47883}"
: "${BROWSERCTL_NPM_CACHE:=${XDG_CACHE_HOME:-$HOME/.cache}/browserctl/npm}"

if [[ "$BROWSERCTL_HOST" != 127.0.0.1 && "$BROWSERCTL_HOST" != ::1 ]]; then
  echo "start-controller: BROWSERCTL_HOST must be numeric loopback" >&2
  exit 2
fi
if [[ ! "$BROWSERCTL_PORT" =~ ^[0-9]+$ ]] || (( BROWSERCTL_PORT < 1 || BROWSERCTL_PORT > 65535 )); then
  echo "start-controller: BROWSERCTL_PORT must be 1-65535" >&2
  exit 2
fi

if [[ "$BROWSERCTL_HOST" == ::1 ]]; then
  AUTHORITY="[::1]:${BROWSERCTL_PORT}"
else
  AUTHORITY="${BROWSERCTL_HOST}:${BROWSERCTL_PORT}"
fi
: "${BROWSERCTL_MCP_URL:=http://${AUTHORITY}/mcp}"
export BROWSERCTL_MCP_URL

BROWSERCTL_CONTROLLER_HOST="$BROWSERCTL_HOST" \
BROWSERCTL_CONTROLLER_PORT="$BROWSERCTL_PORT" \
  python3 "$ROOT/mcp_client.py" controller register \
  "$$" "$BROWSERCTL_MCP_URL" "$BROWSERCTL_CDP_ENDPOINT"

exec env PLAYWRIGHT_MCP_PING_TIMEOUT_MS=0 \
  npm_config_cache="$BROWSERCTL_NPM_CACHE" \
  npx -y @playwright/mcp@0.0.78 \
  --cdp-endpoint "$BROWSERCTL_CDP_ENDPOINT" \
  --shared-browser-context \
  --host "$BROWSERCTL_HOST" \
  --allowed-hosts "$AUTHORITY" \
  --port "$BROWSERCTL_PORT"
