#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
: "${BROWSERCTL_CDP_ENDPOINT:=http://127.0.0.1:9222}"
: "${BROWSERCTL_HOST:=127.0.0.1}"
: "${BROWSERCTL_PORT:=47883}"
: "${BROWSERCTL_NPM_CACHE:=${XDG_CACHE_HOME:-$HOME/.cache}/browserctl/npm}"

if [[ "$BROWSERCTL_HOST" != 127.0.0.1 && "$BROWSERCTL_HOST" != localhost && "$BROWSERCTL_HOST" != ::1 ]]; then
  echo "start-controller: refusing non-loopback BROWSERCTL_HOST=$BROWSERCTL_HOST" >&2
  exit 2
fi
if [[ "$BROWSERCTL_HOST" == localhost ]]; then
  BROWSERCTL_HOST=127.0.0.1
fi

if [[ -z "${BROWSERCTL_MCP_URL:-}" ]]; then
  if [[ "$BROWSERCTL_HOST" == "::1" ]]; then
    BROWSERCTL_MCP_URL="http://[::1]:${BROWSERCTL_PORT}/mcp"
  else
    BROWSERCTL_MCP_URL="http://${BROWSERCTL_HOST}:${BROWSERCTL_PORT}/mcp"
  fi
fi
export BROWSERCTL_MCP_URL

BROWSERCTL_CONTROLLER_HOST="$BROWSERCTL_HOST" \
BROWSERCTL_CONTROLLER_PORT="$BROWSERCTL_PORT" \
  python3 "$ROOT/mcp_client.py" controller register "$$" "$BROWSERCTL_MCP_URL"

exec env PLAYWRIGHT_MCP_PING_TIMEOUT_MS=0 \
  npm_config_cache="$BROWSERCTL_NPM_CACHE" \
  npx -y @playwright/mcp@0.0.78 \
  --cdp-endpoint "$BROWSERCTL_CDP_ENDPOINT" \
  --shared-browser-context \
  --host "$BROWSERCTL_HOST" \
  --port "$BROWSERCTL_PORT"
