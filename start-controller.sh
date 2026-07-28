#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
: "${BROWSERCTL_CDP_ENDPOINT:?BROWSERCTL_CDP_ENDPOINT is required}"
: "${BROWSERCTL_HOST:=127.0.0.1}"
: "${BROWSERCTL_PORT:=47883}"
: "${BROWSERCTL_NPM_CACHE:=${XDG_CACHE_HOME:-$HOME/.cache}/browserctl/npm}"

for EXECUTABLE in python3 node npx setsid; do
  if ! command -v "$EXECUTABLE" >/dev/null; then
    echo "start-controller: required executable is missing: $EXECUTABLE" >&2
    exit 2
  fi
done
if [[ -n "${NOTIFY_SOCKET:-}" ]] && ! command -v systemd-notify >/dev/null; then
  echo "start-controller: NOTIFY_SOCKET requires systemd-notify" >&2
  exit 2
fi
MCP_AMBIENT=("${!PLAYWRIGHT_MCP_@}")
if ((${#MCP_AMBIENT[@]})); then
  echo "start-controller: ambient PLAYWRIGHT_MCP_* variables are forbidden: ${MCP_AMBIENT[*]}" >&2
  exit 2
fi

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

MCP_PID=
MCP_START=
proc_identity() {
  local VALUE
  IFS= read -r VALUE < "/proc/$1/stat" 2>/dev/null || return 1
  set -- ${VALUE##*) }
  (($# >= 20)) || return 1
  printf '%s %s %s\n' "$3" "$4" "${20}"
}
leader_matches() {
  local PGRP SESSION START
  read -r PGRP SESSION START < <(proc_identity "$1") || return 1
  [[ "$PGRP" == "$1" && "$SESSION" == "$1" && "$START" == "$2" ]]
}
cleanup() {
  local PID=$MCP_PID START=$MCP_START
  trap '' HUP INT TERM
  [[ -n "$PID" ]] || return
  kill -0 -- "-$PID" 2>/dev/null || {
    wait "$PID" 2>/dev/null || true
    return
  }
  leader_matches "$PID" "$START" || {
    echo "start-controller: MCP group leader identity changed; refusing to signal" >&2
    return
  }
  kill -TERM -- "-$PID" 2>/dev/null || true
  for _ in {1..20}; do
    sleep .05
    kill -0 -- "-$PID" 2>/dev/null || {
      wait "$PID" 2>/dev/null || true
      return
    }
  done
  leader_matches "$PID" "$START" || {
    echo "start-controller: MCP group leader identity changed; refusing to kill" >&2
    return
  }
  kill -KILL -- "-$PID" 2>/dev/null || true
  wait "$PID" 2>/dev/null || true
  for _ in {1..40}; do
    kill -0 -- "-$PID" 2>/dev/null || return
    sleep .05
  done
  echo "start-controller: MCP process group did not terminate" >&2
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

BROWSERCTL_CONTROLLER_HOST="$BROWSERCTL_HOST" \
BROWSERCTL_CONTROLLER_PORT="$BROWSERCTL_PORT" \
  python3 "$ROOT/mcp_client.py" controller register \
  "$$" "$BROWSERCTL_MCP_URL" "$BROWSERCTL_CDP_ENDPOINT"

setsid env PLAYWRIGHT_MCP_PING_TIMEOUT_MS=0 \
  npm_config_cache="$BROWSERCTL_NPM_CACHE" \
  npx -y @playwright/mcp@0.0.78 \
  --cdp-endpoint "$BROWSERCTL_CDP_ENDPOINT" \
  --shared-browser-context \
  --caps=config \
  --host "$BROWSERCTL_HOST" \
  --allowed-hosts "$AUTHORITY" \
  --port "$BROWSERCTL_PORT" &
MCP_PID=$!
read -r _ _ MCP_START < <(proc_identity "$MCP_PID") || {
  echo "start-controller: cannot identify MCP group leader" >&2
  exit 1
}
for _ in {1..20}; do
  leader_matches "$MCP_PID" "$MCP_START" && break
  sleep .01
done
if ! leader_matches "$MCP_PID" "$MCP_START"; then
  echo "start-controller: MCP did not start as the exact group leader" >&2
  exit 1
fi

python3 "$ROOT/mcp_client.py" controller startup \
  "$$" "$BROWSERCTL_MCP_URL" "$BROWSERCTL_CDP_ENDPOINT"
if [[ -n "${NOTIFY_SOCKET:-}" ]]; then
  systemd-notify --ready
fi
set +e
wait "$MCP_PID"
MCP_STATUS=$?
set -e
if (( MCP_STATUS == 0 )); then
  echo "start-controller: MCP exited unexpectedly" >&2
  exit 1
fi
exit "$MCP_STATUS"
