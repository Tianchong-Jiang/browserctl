# browserctl

Small shell entry points for a host-owned, loopback-only Playwright MCP controller attached to an already-running Edge browser over CDP. Playwright MCP is pinned to `0.0.78`; Codex and Claude invoke `client.sh` and do not register or spawn session-owned MCP servers.

## Install and start

Requirements: Linux, Python 3, Node/npm, and an Edge instance already started with a remote-debugging port for the correct monitor-selected profile. Follow the host's browser policy before starting or restarting Edge; these scripts never manage Edge.

```bash
chmod +x start-controller.sh client.sh mcp_client.py
BROWSERCTL_CDP_ENDPOINT=http://127.0.0.1:9222 ./start-controller.sh
```

The foreground controller defaults to `127.0.0.1:47883`. Run it under a host-owned supervisor or terminal that outlives agent sessions. Configuration:

| Variable | Default | Purpose |
| --- | --- | --- |
| `BROWSERCTL_CDP_ENDPOINT` | `http://127.0.0.1:9222` | Existing Edge CDP endpoint |
| `BROWSERCTL_HOST` | `127.0.0.1` | Controller bind address; non-loopback is refused |
| `BROWSERCTL_PORT` | `47883` | Controller port |
| `BROWSERCTL_NPM_CACHE` | `${XDG_CACHE_HOME:-$HOME/.cache}/browserctl/npm` | npm cache for the pinned controller |

The launcher sets the pinned server's supported `PLAYWRIGHT_MCP_PING_TIMEOUT_MS=0`. Its default Streamable HTTP heartbeat expects a persistent server-to-client channel that this one-command client deliberately does not keep open and can abort tools after roughly five seconds. Completed commands explicitly terminate their MCP session with HTTP `DELETE`; because the pinned server supports deletion, any DELETE error, including HTTP 405, is a failed termination and quarantines the workflow.

## Use

```bash
# Acquire once before the first operation; wait at most 30 seconds, expire in 5 minutes.
export BROWSERCTL_LEASE_TOKEN="$(./client.sh lease acquire "agent-name: task" 30 300)"

./client.sh list
./client.sh call browser_navigate '{"url":"https://example.com"}'
./client.sh call browser_snapshot '{}'

# Refresh during longer workflows, then release only after final verification/submission.
./client.sh lease renew 300
./client.sh lease status
./client.sh lease release
unset BROWSERCTL_LEASE_TOKEN
```

The client defaults to `http://localhost:47883/mcp`. Override `BROWSERCTL_MCP_URL`, the outer `BROWSERCTL_TIMEOUT` (45 seconds), or per-request `BROWSERCTL_HTTP_TIMEOUT` (30 seconds) when needed. Transport failures, timeouts, malformed responses, JSON-RPC errors, and Playwright `isError` results all exit nonzero.

`lease acquire OWNER [WAIT_SECONDS] [TTL_SECONDS]` prints a secret token. `list`, `call`, `lease renew`, and `lease release` require that token in `BROWSERCTL_LEASE_TOKEN`; status deliberately omits it. Wait is bounded to 3600 seconds and TTL to 86400 seconds. Expired leases recover automatically. State defaults to `${XDG_RUNTIME_DIR}/browserctl`, falling back to `${XDG_STATE_HOME:-$HOME/.local/state}/browserctl`, and `BROWSERCTL_STATE_DIR` can select an isolated test/service state directory.

Before a tool request, the client durably marks the lease in flight. It clears that marker only after receiving the complete response and successfully terminating the MCP session. If a timeout, signal, crash, or kill leaves the server-side outcome unknown, it neither sends `DELETE` nor clears the marker; all further calls, release, and stale-lease takeover fail loudly. Recovery requires restarting only the host-owned Playwright controller—not Edge—then explicitly attesting to that restart:

```bash
# Restart the Playwright controller with its host supervisor and confirm it is listening.
# Keep Edge and its selected profile running.
./client.sh lease status
./client.sh lease recover --controller-restarted 300
./client.sh call browser_snapshot '{}'
```

`recover` cannot verify a controller restart; the flag is an operator safety attestation. Never use it merely to clear an error. If the secret token was lost, an operator must restart the controller and remove the lease state manually before a new acquire. The lease and execution lock serialize cooperating `browserctl` clients across processes, but cannot protect against a client that bypasses `browserctl` and calls the MCP endpoint directly. Do not share the controller endpoint with such clients.
