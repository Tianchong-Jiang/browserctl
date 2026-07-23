#!/usr/bin/env bash
set -euo pipefail

: "${BROWSERCTL_CDP_ENDPOINT:=http://127.0.0.1:9222}"
: "${BROWSERCTL_HOST:=127.0.0.1}"
: "${BROWSERCTL_PORT:=47883}"
: "${BROWSERCTL_NPM_CACHE:=${XDG_CACHE_HOME:-$HOME/.cache}/browserctl/npm}"

if [[ "$BROWSERCTL_HOST" != 127.0.0.1 && "$BROWSERCTL_HOST" != localhost && "$BROWSERCTL_HOST" != ::1 ]]; then
  echo "start-controller: refusing non-loopback BROWSERCTL_HOST=$BROWSERCTL_HOST" >&2
  exit 2
fi

exec env PLAYWRIGHT_MCP_PING_TIMEOUT_MS=0 \
  npm_config_cache="$BROWSERCTL_NPM_CACHE" \
  npx -y @playwright/mcp@0.0.78 \
  --cdp-endpoint "$BROWSERCTL_CDP_ENDPOINT" \
  --shared-browser-context \
  --host "$BROWSERCTL_HOST" \
  --port "$BROWSERCTL_PORT"
