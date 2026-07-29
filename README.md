# browserctl

`browserctl` gives one cooperating workflow fail-closed access to an already-running Edge profile. It never launches, stops, or selects Edge. The stable manually started user service owns one pinned-version Playwright MCP child and one warm session across tasks.

## Configure

Create `${XDG_CONFIG_HOME:-$HOME/.config}/browserctl/host.json`, owned by the user and mode `0600`. Its exact schema has no defaults or unknown keys:

```json
{
  "schema_version": 1,
  "hostname": "HOST",
  "user": "USER",
  "human": {"edge_exe": "/absolute/edge", "profile": "/absolute/human", "cdp_endpoint": "http://127.0.0.1:9224"},
  "agent": {"edge_exe": "/absolute/edge", "profile": "/absolute/agent", "cdp_endpoint": "http://127.0.0.1:9225"},
  "mcp_command": ["/absolute/npx", "--yes", "@playwright/mcp@0.0.78"]
}
```

The command selects exactly MCP version `0.0.78`; npm cache/package integrity is outside browserctl and is not guaranteed. With only internal `eDP`/`LVDS`/`DSI` outputs, `human` is selected; any external output selects `agent`. Both profiles may run simultaneously. The selected profile's `SingletonLock`, exact top-level PID/executable/profile descriptors, and sole numeric-loopback CDP listener must agree.

## Install and start manually

```bash
install -d -m 0700 "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
install -m 0644 browserctl-controller.service \
  "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/browserctl-controller.service"
systemctl --user daemon-reload
./browserctl.py doctor
systemctl --user start browserctl-controller.service
```

The unit is static, `Type=notify`, and manual-only. Direct/transient launch is rejected. READY is sent only after stable-unit identity, legacy/foreign-controller absence, selected Edge identity, and the warm MCP session are validated.

## Task workflow

```bash
./browserctl.py task begin "agent: description"
./browserctl.py tab new
./browserctl.py tab list
./browserctl.py tab select INDEX
./browserctl.py call browser_snapshot '{}'
./browserctl.py tools
./browserctl.py call browser_navigate '{"url":"https://example.com"}'
./browserctl.py task end
```

The enforced phase order is `new → list → select → snapshot → ARMED`. From ARMED, `tab list` deliberately returns to SELECT so a popup or tab switch can be corrected with select and snapshot. Raw tabs/config/close/unsafe tools are forbidden. General calls require ARMED. Ref targets require a fresh nonempty snapshot; every attempted non-snapshot call conservatively invalidates it.

Each dispatch is durably `RUNNING` before transmission and `RESULT` before output. A completed result too large to persist enters `RECOVERY` with explicit do-not-replay evidence. A client ACK returns a normal result to `READY`; after disconnect, use `./browserctl.py result` without replaying. Daemon replacement changes old `READY`/`RUNNING` work to `RECOVERY`; an old `RESULT` stays retrievable and then becomes `RECOVERY`.

After daemon replacement, recovery enforces `task recover → tab list → tab select INDEX → browser_snapshot`. Recovery never replays. `task end` is local, READY-only, and keeps the warm session.

## Operator-only idle cutover

There is no migration or deletion of legacy `controller.json`, `lease.json`, or other unexpected state. While the legacy controller is explicitly idle, stop only that controller, move the entire old `${XDG_STATE_HOME:-$HOME/.local/state}/browserctl` directory to a recoverable archive, create no replacement files manually, and then start the new stable unit. Never archive active or uncertain work, and never stop Edge.

## Tests

```bash
python3 -W error::ResourceWarning -m unittest -v test_browserctl.py
python3 -W error::ResourceWarning -m unittest -v test_real_integration.py
```

The first suite uses fake MCP, systemd, xrandr, and simultaneous fake profiles. The second uses cached exact MCP `0.0.78`, a temporary Edge profile, temporary ports, and no live browser state.
