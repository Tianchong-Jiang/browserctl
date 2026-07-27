# browserctl

Small shell entry points for a host-owned, loopback-only Playwright MCP controller attached to an already-running Edge browser over CDP. Playwright MCP is pinned to `0.0.78`; Codex and Claude invoke `client.sh` and do not register or spawn session-owned MCP servers.

This design has one explicit trust invariant: exactly one agent/workflow uses a given controller and browser profile. The lease serializes that workflow against other cooperating `browserctl` processes; it is not an authentication credential. A separate concurrent workflow needs its own profile, CDP port, controller port, and state directory.

## Install and start

Requirements: Linux, Python 3, Node/npm, and an Edge instance already started with a remote-debugging port for the correct monitor-selected profile. Follow the host's browser policy before starting or restarting Edge; these scripts never manage Edge.

```bash
chmod +x start-controller.sh client.sh mcp_client.py
BROWSERCTL_CDP_ENDPOINT=http://127.0.0.1:9222 ./start-controller.sh
```

The foreground controller defaults to `127.0.0.1:47883`. Run it under a host-owned supervisor or terminal that outlives agent sessions. `localhost` is normalized to `127.0.0.1`; controller and client URLs use explicit numeric loopback addresses to avoid IPv4/IPv6 ambiguity. Configuration:

| Variable | Default | Purpose |
| --- | --- | --- |
| `BROWSERCTL_CDP_ENDPOINT` | `http://127.0.0.1:9222` | Existing Edge CDP endpoint |
| `BROWSERCTL_HOST` | `127.0.0.1` | Controller bind address; non-loopback is refused |
| `BROWSERCTL_PORT` | `47883` | Controller port |
| `BROWSERCTL_NPM_CACHE` | `${XDG_CACHE_HOME:-$HOME/.cache}/browserctl/npm` | npm cache for the pinned controller |
| `BROWSERCTL_STATE_DIR` | runtime/state default below | Shared lease and controller-generation state |

Always launch the controller through `start-controller.sh`. Before starting MCP it records a new controller generation, launcher PID, Linux `/proc` process-start identity, and MCP URL while holding the same `execution.lock` used by tool calls. Clients require the live registered process tree to own the exact configured loopback address, family, and port. This binds an in-flight operation to the controller that could execute it without adding a proxy or relying on a caller assertion.

The launcher also pins Playwright MCP's allowed host to the exact numeric loopback authority it binds (`127.0.0.1:<port>` or `[::1]:<port>`), never a wildcard. This keeps the server's host-header protection aligned with the client URL.

The launcher sets the pinned server's supported `PLAYWRIGHT_MCP_PING_TIMEOUT_MS=0`. Its default Streamable HTTP heartbeat expects a persistent server-to-client channel that this one-command client deliberately does not keep open and can abort tools after roughly five seconds. Completed commands explicitly terminate their MCP session with HTTP `DELETE`; because the pinned server supports deletion, any DELETE error, including HTTP 405, is a failed termination and quarantines the workflow.

## Use

```bash
# Acquire once before the first operation; wait at most 30 seconds, expire in 5 minutes.
./client.sh lease acquire "agent-name: task" 30 300

./client.sh list
./client.sh call browser_navigate '{"url":"https://example.com"}'
./client.sh call browser_snapshot '{}'

# Refresh during longer workflows, then release only after final verification/submission.
./client.sh lease renew 300
./client.sh lease status
./client.sh lease release
```

The client defaults to `http://127.0.0.1:47883/mcp`. Override `BROWSERCTL_MCP_URL` with an explicit `127.0.0.1` or `::1` URL, the outer `BROWSERCTL_TIMEOUT` (45 seconds), or per-request `BROWSERCTL_HTTP_TIMEOUT` (30 seconds) when needed. MCP requests explicitly bypass ambient proxy configuration. Transport failures, timeouts, malformed or mismatched JSON-RPC responses, JSON-RPC errors, and Playwright `isError` results all exit nonzero.

`lease acquire OWNER [WAIT_SECONDS] [TTL_SECONDS]` prints non-secret JSON describing the lease. Browser operations, renew, release, and recovery require the active singleton lease but need no token or environment credential; old `BROWSERCTL_LEASE_TOKEN` values are ignored. Wait is bounded to 3600 seconds and TTL to 86400 seconds. An expired idle lease is reclaimed automatically. An in-flight lease never auto-expires, even after its nominal TTL.

`lease status` always reports `state` as `available`, `held`, or `recovery_required`, along with explicit `expired`, `in_flight`, and `recovery_required` booleans. Only an ordinary held lease has `expires_in`; recovery-required state deliberately has no misleading expiry countdown.

State defaults to `${XDG_RUNTIME_DIR}/browserctl`, falling back to `${XDG_STATE_HOME:-$HOME/.local/state}/browserctl`, and `BROWSERCTL_STATE_DIR` can select a separate service/test state directory. The controller launcher and every client for that controller must use the same state directory and exact `BROWSERCTL_MCP_URL`.

Before a tool request, the client durably marks the lease in flight. It clears that marker only after validating the correlated JSON-RPC envelope and operation-specific result, then successfully terminating the MCP session. A complete valid JSON-RPC or Playwright `isError` response is determinate and clears after successful session deletion before exiting nonzero. If a timeout, signal, crash, kill, malformed/mismatched response, malformed result, or failed session deletion leaves the server-side outcome unknown, all further calls, release, and stale-lease takeover fail loudly. Recovery requires restarting only the host-owned Playwright controller—not Edge—and then lets the client verify the new generation:

```bash
# Restart the Playwright controller with its host supervisor and confirm it is listening.
# Keep Edge and its selected profile running.
./client.sh lease status
./client.sh lease recover 300
./client.sh call browser_snapshot '{}'
```

`recover [TTL_SECONDS]` is tokenless but fail-closed. It rejects missing, malformed, dead, URL-mismatched, listener-unbound, or unchanged controller generations. For a changed generation, it performs MCP initialize, `tools/list`, a non-mutating `browser_tabs` list probe, and successful session `DELETE` before clearing quarantine. The browser probe must return a fully correlated, correctly shaped, non-`isError` result, so an MCP-only service without a working attached browser cannot clear quarantine. `release` and `renew` reject an in-flight lease.

Controller generations use Linux `/proc`, so this implementation is intentionally Linux-specific. The safety proof also depends on every controller start going through `start-controller.sh`; a process that bypasses the launcher is rejected rather than silently trusted. The lease and execution lock serialize cooperating `browserctl` clients but cannot protect against a client that bypasses `browserctl` and calls MCP directly. Do not share the endpoint with such clients.

After upgrading, a controller that is already running from the old launcher has no registered generation and is rejected; restart only that controller once through the updated `start-controller.sh` before the next browser operation. Edge remains running. On first read, an idle lease from the old token schema is migrated in place by removing its token hash while preserving owner and times. A legacy in-flight lease has no recorded controller generation and therefore cannot be recovered safely by this client; it fails loudly and requires an operator to restart the controller and manually resolve that lease file. Do not remove or rewrite such live state before the restart.
