#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
: "${BROWSERCTL_MCP_URL:=http://localhost:47883/mcp}"
: "${BROWSERCTL_TIMEOUT:=45}"

if [[ "${1:-}" == lease ]]; then
  exec python3 "$ROOT/mcp_client.py" "$@"
fi

exec timeout --signal=TERM --kill-after=3s "${BROWSERCTL_TIMEOUT}s" \
  python3 "$ROOT/mcp_client.py" "$@"
