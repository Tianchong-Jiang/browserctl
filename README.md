# browserctl

`browserctl` serializes cooperating browser workflows through one host-owned Playwright MCP controller connected to an already-running Edge CDP endpoint. It never starts, stops, or selects Edge. The launcher requests exact `@playwright/mcp@0.0.78`; this pins package selection, not package integrity.

## Controller environment

Linux, Python 3, Node, `npx`, `setsid`, and an operator-verified numeric-loopback Edge CDP endpoint are required. First find the absolute directory that contains `npx`:

```bash
dirname "$(command -v npx)"
```

Create `~/.config/browserctl/controller.env` mode `0600`. Replace `USER`, `VERSION`, and ports for the host; environment files do not expand `$HOME`.

```ini
PATH=/home/USER/.nvm/versions/node/VERSION/bin:/usr/local/bin:/usr/bin:/bin
BROWSERCTL_CDP_ENDPOINT=http://127.0.0.1:9333
BROWSERCTL_HOST=127.0.0.1
BROWSERCTL_PORT=47883
BROWSERCTL_MCP_URL=http://127.0.0.1:47883/mcp
BROWSERCTL_STATE_DIR=/home/USER/.local/state/browserctl
BROWSERCTL_NPM_CACHE=/home/USER/.cache/browserctl/npm
```

The `PATH` entry must contain the exact `npx` found above. The launcher and every client shell must load and export the same file:

```bash
set -a
. "$HOME/.config/browserctl/controller.env"
set +a
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `BROWSERCTL_CDP_ENDPOINT` | none | Required canonical numeric-loopback Edge CDP URL |
| `BROWSERCTL_HOST` | `127.0.0.1` | MCP bind address (`127.0.0.1` or `::1`) |
| `BROWSERCTL_PORT` | `47883` | MCP port |
| `BROWSERCTL_MCP_URL` | derived | Matching canonical numeric-loopback `/mcp` URL |
| `BROWSERCTL_STATE_DIR` | `${XDG_STATE_HOME:-$HOME/.local/state}/browserctl` | Durable private state |
| `BROWSERCTL_NPM_CACHE` | `${XDG_CACHE_HOME:-$HOME/.cache}/browserctl/npm` | Pinned launcher npm cache |
| `BROWSERCTL_STARTUP_TIMEOUT` | `30` | Listener/startup bound, 0.1–300 seconds |
| `BROWSERCTL_HTTP_TIMEOUT` | `90` | Per-request bound, 0.1–300 seconds |
| `BROWSERCTL_LOCK_TIMEOUT` | `45` | Lock bound, 0.1–3600 seconds |
| `BROWSERCTL_TIMEOUT` | `120` | Outer client bound, 1–3600 seconds |

Any ambient `PLAYWRIGHT_MCP_*` variable is a startup error. After that check, the launcher alone sets `PLAYWRIGHT_MCP_PING_TIMEOUT_MS=0` for its MCP process.

## Start, readiness, and stop

For a host-owned foreground terminal:

```bash
./start-controller.sh
```

For the version-controlled user unit:

```bash
install -d -m 0700 "$HOME/.config/browserctl" "$HOME/.config/systemd/user"
install -m 0644 browserctl-controller.service \
  "$HOME/.config/systemd/user/browserctl-controller.service"
systemctl --user daemon-reload
systemctl --user start browserctl-controller.service
systemctl --user is-active browserctl-controller.service
./client.sh lease status
```

The unit is `Type=notify`, `NotifyAccess=all`, `TimeoutStartSec=infinity`, and `Restart=no`. The launcher owns finite listener and HTTP step timeouts; systemd does not impose a competing aggregate startup deadline. `systemctl start` completes only after the launcher has found its exact descendant listener, initialized the anchor session, checked the required tools, and validated the resolved shared-CDP config. Ready proves those configured semantics; it does not prove which Edge process or profile the operator placed at the CDP endpoint. Verify Edge/profile identity separately under the host browser policy.

Normal stop is allowed only when no lease exists. Load the same environment in the operator shell, require `lease status` to report `available`, then stop the exact foreground launcher with Ctrl-C or the service explicitly:

```bash
./client.sh lease status
systemctl --user stop browserctl-controller.service
```

Do not normally stop the controller while status is `in_progress`, `result_pending`, or a lease is `ready`; finish or resolve that workflow first. The unit has no install target and is not automatically enabled at login. Start it only after verifying the selected Edge endpoint.

## Safe task-tab workflow

Acquire one whole-task lease, create a task tab, and leave unrelated tabs untouched:

```bash
./client.sh lease acquire "agent-name: task" 30
./client.sh call browser_tabs '{"action":"list"}'
./client.sh call browser_tabs '{"action":"new"}'
./client.sh call browser_tabs '{"action":"list"}'
./client.sh call browser_tabs '{"action":"select","index":0}' # replace 0 with the task-tab index
./client.sh call browser_navigate '{"url":"https://example.com"}'
./client.sh call browser_snapshot '{}'
# perform task actions, then reload/read back durable state
./client.sh lease release
```

Record the task-tab index from the fresh list and reselect it whenever tab identity could be ambiguous. Do not navigate, close, or reuse unrelated human tabs. After every mutation, inspect its postcondition; before release, wait for saves and perform a fresh server-backed readback.

Acquire validates the ready controller and live anchor before creating one persistent workflow session. Later foreground commands reuse that session. Public `list` hides `browser_get_config`, `browser_close`, and `browser_run_code_unsafe`; calls to them fail before dispatch. Ref-shaped targets require a successful snapshot in the current lease, and navigation or tab select/new/close invalidates that proof.

Each operation is durably marked `in_progress`. A correlated response becomes `result_pending` before output delivery:

```bash
./client.sh lease status
./client.sh lease result
```

Never replay an action when a result is pending. There is no TTL, renewal, automatic reclaim, action retry, per-command session, redirect, proxy, migration, or fallback path.

## Recovery

A timeout, client crash, malformed response, generation/listener change, or ambiguous workflow DELETE requires controller-only recovery. Do not replay the uncertain action, delete lease state, or restart Edge.

```bash
./client.sh lease status # must report recovery_required
systemctl --user stop browserctl-controller.service
systemctl --user start browserctl-controller.service
./client.sh lease status
./client.sh lease recover
./client.sh call browser_tabs '{"action":"list"}'
./client.sh call browser_tabs '{"action":"select","index":0}' # replace 0 with the task-tab index
./client.sh call browser_snapshot '{}'
```

The replacement must use the same MCP and CDP endpoints and establish a different validated generation with a new anchor. `lease status` remains `recovery_required` until `lease recover` creates a new workflow session. Freshly list, select, and snapshot before deciding whether any action remains; recovery never replays the uncertain operation.

Actual notification acceptance by the host user manager is a deployment gate: the repository tests validate launcher notification ordering and unit syntax, but do not substitute for the `systemctl start`, `is-active`, and `lease status` checks above on each deployed host.

## Schema-2 cutover

Schema 2 is not parsed, deleted, or migrated by schema-3 code. Cut over only while explicitly idle:

1. Using the exact old checkout and controller, confirm there is no active workflow and normally release any lease.
2. Stop that exact old controller and verify its MCP listener is closed; do not stop Edge.
3. Move the old state directory to a recoverable archive such as `/home/USER/.local/state/browserctl.schema2.archive`.
4. Start schema 3 with a fresh `/home/USER/.local/state/browserctl`.

Keep the archive until the new controller is qualified. Never point schema 3 at live schema-2 state or auto-delete the old directory.

## Tests

```bash
python3 -W error::ResourceWarning -m unittest -v test_browserctl.py
python3 -W error::ResourceWarning -m unittest -v test_real_integration.py
```

The deterministic suite uses a private fake server. The non-skipped qualification requires Microsoft Edge and the exact cached `@playwright/mcp@0.0.78`; it uses disposable ports/profile/state and never contacts the human browser. For an explicitly skippable developer convenience only:

```bash
python3 test_real_integration.py --developer-skip-missing
```
