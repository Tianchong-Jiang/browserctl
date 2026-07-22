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

## Use

```bash
./client.sh list
./client.sh call browser_navigate '{"url":"https://example.com"}'
./client.sh call browser_snapshot '{}'
```

The client defaults to `http://localhost:47883/mcp`. Override `BROWSERCTL_MCP_URL`, the outer `BROWSERCTL_TIMEOUT` (45 seconds), or per-request `BROWSERCTL_HTTP_TIMEOUT` (30 seconds) when needed. Transport failures, timeouts, malformed responses, JSON-RPC errors, and Playwright `isError` results all exit nonzero.

## Concurrency warning

**This initial snapshot has no workflow lease. Never use one controller/browser from two agents or workflows at the same time.** Concurrent controllers or clients can interrupt navigation and observe another agent's page. Serialize the entire workflow externally, from its first browser action through verification and submission.
