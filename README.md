# browserctl

`browserctl` gives one cooperating agent/workflow exclusive use of one host-owned Playwright MCP controller and its already-running Edge CDP endpoint. The launcher requests exact Playwright MCP version `0.0.78`. The lease is workflow serialization, not authentication; there is no lease token. A concurrent workflow needs a separate profile, CDP port, controller port, and state directory.

## Start the controller

Linux, Python 3, Node/npm, and an already-running Edge CDP endpoint are required. The scripts never start, stop, or select Edge.

```bash
export BROWSERCTL_CDP_ENDPOINT=http://127.0.0.1:9223
./start-controller.sh
```

`BROWSERCTL_CDP_ENDPOINT` has no default and must be a numeric loopback HTTP URL with an explicit port. It identifies the operator-selected browser endpoint; browserctl cannot prove which Edge profile the operator placed there. Follow the host browser policy.

The foreground launcher defaults to `127.0.0.1:47883` and should run in a host-owned terminal or supervisor. It records its generation, Linux PID/start/boot identity, exact MCP listener, and exact CDP endpoint before asking `npx` for exact version `@playwright/mcp@0.0.78`. This is exact-version selection, not package-integrity verification. It restricts allowed hosts to the numeric listener and sets `PLAYWRIGHT_MCP_PING_TIMEOUT_MS=0`, which is required for a persistent Streamable HTTP session without a permanent server-to-client channel.

| Variable | Default | Meaning |
| --- | --- | --- |
| `BROWSERCTL_CDP_ENDPOINT` | none | Required numeric loopback Edge CDP endpoint |
| `BROWSERCTL_HOST` | `127.0.0.1` | Controller bind address (`127.0.0.1` or `::1`) |
| `BROWSERCTL_PORT` | `47883` | Controller port |
| `BROWSERCTL_MCP_URL` | derived | Exact numeric loopback `/mcp` URL |
| `BROWSERCTL_STATE_DIR` | `${XDG_STATE_HOME:-$HOME/.local/state}/browserctl` | Durable private controller/lease state |
| `BROWSERCTL_NPM_CACHE` | `${XDG_CACHE_HOME:-$HOME/.cache}/browserctl/npm` | npm cache used by the pinned launcher |
| `BROWSERCTL_HTTP_TIMEOUT` | `90` | Per-request timeout, 0.1–300 seconds |
| `BROWSERCTL_LOCK_TIMEOUT` | `45` | Lock acquisition timeout, 0.1–3600 seconds |
| `BROWSERCTL_TIMEOUT` | `120` | Outer command timeout, 1–3600 seconds |

The launcher and every client invocation must receive the same explicit `BROWSERCTL_CDP_ENDPOINT` and state-directory configuration. `BROWSERCTL_MCP_URL` is derived by the launcher and has the matching canonical client default; if overridden, the identical override must be supplied to launcher and clients.

## Use

```bash
./client.sh lease acquire "agent-name: task" 30
./client.sh list
./client.sh call browser_navigate '{"url":"https://example.com"}'
./client.sh call browser_snapshot '{}'
./client.sh lease status
./client.sh lease release
```

`lease acquire OWNER [WAIT_SECONDS]` is the readiness transaction. While holding lifecycle exclusion it validates the registered generation and exact listener, durably enters initialization, creates one MCP session, negotiates and records the protocol, checks that `tools/list` contains `browser_tabs`, and requires a nonempty `browser_tabs` listing. In pinned MCP, listing tabs calls `ensureTab()` and may create a blank tab when Edge has no tabs; it otherwise does not navigate or interact with page content. That same session and monotonically increasing request ID are reused by later `list` and `call` processes, preserving current-tab and snapshot-ref state. There is no TTL, renewal, automatic reclaim, sequence command, per-command initialization, or per-command DELETE.

Every call is durably marked `in_progress` with its client PID/start/boot identity before dispatch. Every response must match the request ID and leased MCP session, and the exact registered controller generation/listener is revalidated before and after each HTTP request. Redirects and ambient proxies are rejected. Responses and durable completion receipts are bounded to 16 MiB.

A completed call becomes `result_pending` before output delivery. Successful output flush acknowledges it and returns the lease to `ready`. If the caller dies or its output pipe closes, the browser action is never replayed:

```bash
./client.sh lease status
./client.sh lease result
```

`lease result` reprints the stored stdout/stderr and original exit classification, then clears it only after a successful flush. Repeated retrieval may duplicate output but never repeats the browser call. Release is blocked while a result is pending.

`lease status` is the only command that does not take `execution.lock`, so it remains responsive during a long call. Its public states are `available`, `ready`, `in_progress`, `result_pending`, and `recovery_required`. It reports a live transient client as `in_progress`; the same durable phase with a dead, zombie, PID-reused, or reboot-stale client reports `recovery_required`.

## Failure and recovery

A timeout, signal, crash, malformed/mismatched response, listener/generation change, or ambiguous DELETE leaves durable recovery-required state. Do not retry the browser action, delete state, release, or restart Edge.

1. Restart only the host-owned Playwright controller through `start-controller.sh`, with the same MCP listener and explicit CDP endpoint.
2. Run `./client.sh lease recover`.
3. Reinspect tabs and take a fresh snapshot before deciding how to continue.

Recovery rejects an unchanged generation or changed endpoint. Against the changed generation it creates one fully correlated session, checks `tools/list`, performs the same real nonempty `browser_tabs` probe (including the possible zero-tab blank-tab behavior above), and retains that healthy session as the recovered lease. Session-local tab/ref state from the old controller is reported lost. Recovery never replays the uncertain operation.

`lease release` first durably enters `closing`, then DELETEs the exact persistent session, revalidates the controller, and only then removes the lease. Any DELETE failure or ambiguity remains quarantined.

State and locks are owner-controlled mode `0700`/`0600`, written with replace and filesystem sync, and intentionally live in durable state rather than `XDG_RUNTIME_DIR`. Legacy schemas and unsafe symlinks/modes fail loudly; there is no migration or manual-state fallback.

The serialization boundary covers cooperating browserctl processes only. Any local process able to connect to MCP directly can bypass it, and a same-UID process can additionally rewrite private state; do not share the controller with such callers.

## Tests

```bash
python3 -W error::ResourceWarning -m unittest -v test_browserctl.py
python3 -m unittest -v test_real_integration.py
```

The first suite is deterministic and uses an isolated fake controller. The second is separately skippable when host prerequisites are absent; when available it uses the exact locally cached `@playwright/mcp@0.0.78`, a disposable headless Edge profile, and a local fixture page. It never connects to the human browser.
