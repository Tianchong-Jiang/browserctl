#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
: "${BROWSERCTL_TIMEOUT:=120}"
if [[ ! "$BROWSERCTL_TIMEOUT" =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]] || \
  ! python3 -c 'import math,sys; x=float(sys.argv[1]); raise SystemExit(not (math.isfinite(x) and 1 <= x <= 3600))' "$BROWSERCTL_TIMEOUT"; then
  echo "browserctl: BROWSERCTL_TIMEOUT must be between 1 and 3600 seconds" >&2
  exit 2
fi
exec timeout --signal=TERM --kill-after=3s "${BROWSERCTL_TIMEOUT}s" \
  python3 "$ROOT/mcp_client.py" "$@"
