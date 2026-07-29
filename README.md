# browserctl

`browserctl` gives one cooperating browser agent at a time access to an already-running Edge profile. It never launches, stops, or selects Edge. A manually started user service owns one pinned Playwright MCP child and keeps one warm session across tasks. The Unix account is the boundary; there are no tokens, authentication, or queues.

## Configure and install

Create `${XDG_CONFIG_HOME:-$HOME/.config}/browserctl/host.json` with this exact schema:

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

Only internal `eDP`/`LVDS`/`DSI` outputs select `human`; any external output selects `agent`. The selected profile's `SingletonLock`, Edge executable and arguments, profile path, CDP port, and CDP availability must agree. An operator or agent may configure and install missing pieces using first-hand host values:

```bash
install -d -m 0700 "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
install -m 0644 browserctl-controller.service "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/browserctl-controller.service"
systemctl --user daemon-reload
./browserctl.py doctor
systemctl --user start browserctl-controller.service
```

The static `Type=notify` unit is manual-only. It announces READY after the selected Edge identity and warm MCP `0.0.78` session are ready.

## Use

```bash
./browserctl.py task begin "agent: description"
./browserctl.py tab new
./browserctl.py tab list
./browserctl.py tab select INDEX
./browserctl.py call browser_snapshot '{}'
# Use tab new/list/select/close, tools, and call as needed.
./browserctl.py tab close INDEX
./browserctl.py task end
```

The owner is descriptive, not authorization. While `READY`, tab commands, `tools`, `call`, and `task end` are available in any order. MCP decides whether arguments and refs are valid. Raw `browser_tabs`, `browser_get_config`, and run-code tools are allowed; only `browser_close` is hidden and blocked because it closes the shared browser.

Every dispatch is durably `RUNNING` before MCP transmission and `RESULT` before output. ACK returns it to `READY`; after a disconnect, `result` retrieves it without replay. A normal MCP/tool error exits 1 and ACKs back to `READY`. Inspect tabs or snapshot and continue from observed state: a failed `tab new`, for example, may still have created a tab.

Restarting rebinds `READY` work to the new daemon generation. An old `RESULT` remains retrievable and ACKs to `READY`; an old `RUNNING` becomes `RECOVERY`. `task recover` adopts the new generation without replay or command restrictions, while `task end` may abandon `READY` or `RECOVERY`.

## Tests

```bash
python3 -W error::ResourceWarning -m unittest -v test_browserctl.py
python3 -W error::ResourceWarning -m unittest -v test_real_integration.py
```

Both suites use disposable profiles and state; the integration uses cached MCP `0.0.78` and temporary Edge/CDP ports.
