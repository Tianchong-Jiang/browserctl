# browserctl

## Purpose

Control one shared Chrome profile through one persistent Playwright MCP session. This README owns installation, commands, and recovery. Task ownership coordinates cooperating agents; it does not isolate them from human windows.

## Setup

Install official Chrome and Node/npx with built-in `fetch` and `WebSocket` support (tested with Node 24.12.0). Create `~/.config/browserctl/host.json` using first-hand host values; Oak uses:

```json
{
  "schema_version": 1, "hostname": "oak", "user": "ripl",
  "chrome_exe": "/opt/google/chrome/chrome",
  "profile": "/home/ripl/.config/chrome-shared",
  "cdp_endpoint": "http://127.0.0.1:9226",
  "npx": "/home/ripl/miniconda3/envs/node20/bin/npx",
  "agent_region": {"left": 3413, "top": 32, "width": 1707, "height": 1408},
  "human_region": {"left": 66, "top": 32, "width": 3347, "height": 1408}
}
```

```bash
install -D -m 0644 browserctl-controller.service ~/.config/systemd/user/browserctl-controller.service
systemctl --user daemon-reload
./browserctl.py start
```

Run startup in the graphical session. The desktop launch command is `/absolute/path/browserctl.py start %U`. Files and URLs open as new tabs in the existing human window, preserving its size and maximized state. An icon launch focuses that window. Only cold startup, absence of a human window, or explicit `start --new-window` creates a human window. The desktop New Window action must use `start --new-window %U`. Launching uses the shared profile and loopback debugging port without contacting MCP or acquiring an agent task. Chrome starts with the port enabled; later agent attachment does not require a browser restart. Private browsing uses Chrome's native `--incognito` action with the same profile and CDP flags.

Window routing uses the small local `extension/` component, installed through Chrome’s debugging connection when first needed. It uses Chrome’s tab/window APIs to select an exact destination; its only permissions are `storage` and `tabGroups`. The `Agent browser` tab group marks the agent window across Chrome session restores; preserve its name. An ambiguous role fails instead of guessing.

The agent window is optional. `prepare` (also available as `start --prepare-only`) prepares an already-running Chrome for default agent use. It reuses the recorded window/tab; if that tab closed, it reuses a remaining tab in that agent window. It recovers the window from the agent tab group when saved IDs are stale and creates a background agent window only when no agent window remains. Multiple human windows are supported and never identified as agent windows by their position.

Regions are initial Chrome window bounds, not window identity. Oak uses its rightmost third. On the laptop, preparation identifies a newly created native window by Chrome PID and a unique temporary title, then moves it to desktop 2 without activating it. The laptop requires `xdotool` and X11. Preparation does not switch desktops. Window movement, resizing, maximizing, and minimizing do not invalidate the recorded IDs. Old Edge configurations and journals are unsupported; the `deprecated` branch preserves that implementation.

Supported hosts are the laptop and Oak on Linux. CDP must use `http://127.0.0.1:PORT`, and the main Chrome process must own its listener; unsupported layouts fail instead of falling back.

Native Chrome launch diagnostics are appended to `~/.local/state/browserctl/chrome-launch.log` (or the corresponding `XDG_STATE_HOME` path). `start` reports that launch was requested; `doctor` verifies that Chrome and its debugging port are ready.

The controller pins MCP `0.0.78` and loads `background-page.cjs` to suppress Playwright's window-raising call. It checks the executable, process owner, PID start time, profile, and CDP listener before dispatch.

On the laptop, `gnome-extension/no-app-workspace-switch@tianchongj` keeps apps, including Chrome, from switching workspaces. Mutter switches whenever a client requests `_NET_ACTIVE_WINDOW` for a window on another workspace or `_NET_CURRENT_DESKTOP`; the extension immediately undoes switches not made through GNOME Shell controls (overview, Alt+Tab, dock, keybindings, notifications) and logs `blocked workspace switch` to the journal. Install it by symlinking the directory into `~/.local/share/gnome-shell/extensions/`, logging in again, then running `gsettings set org.gnome.shell disable-user-extensions false` and `gnome-extensions enable no-app-workspace-switch@tianchongj`.

## Use

```bash
./browserctl.py doctor
./browserctl.py status
./browserctl.py task begin "agent: description"
./browserctl.py prepare
./browserctl.py agent <<'JS'
async (page) => {
  return await page.locator('body').ariaSnapshot();
}
JS
./browserctl.py task end
```

`agent` supplies the verified agent Page, checking Chrome process identity and the recorded target/window IDs before running your function. It temporarily emulates page focus without activating the OS window. Missing or moved-to-another-window targets fail; use `prepare` before continuing. During a task, use `tab new [URL]` to create a background tab in the verified agent window. The command returns its `targetId` and `windowId`; pass that target ID to `agent TARGET_ID` for subsequent operations. Creation is covered by the same durable task journal and recovery rules as other actions. Use this command for tab creation: generic `context.newPage()`, `window.open()`, and raw `browser_tabs` creation do not provide this destination/focus guarantee. Controller reconnects retain the binding; browser restarts require preparation. Never take over, recover, or end another agent's task.

When the user explicitly requests control of an existing human tab, use `tabs` to list `{targetId, windowId, title, url}`, then `agent TARGET_ID < FUNCTION.js`. Explicit targeting does not require an agent window or binding and fails if that exact tab is missing. Acquire and release the controller as usual; omit `prepare` when working only in a human tab. Preserve human tabs. Raw `call TOOL JSON` and `tools` are setup/debugging commands without window restrictions. `browser_close` is blocked.

The established HTTP MCP transport, single controller, and warm attachment are retained across tasks. Do not reconnect per task: the July 2026 history records why warm attachment was introduced, and the integration test verifies healthy-tab control while another tab remains loading.

Every dispatch is durably `RUNNING` before transmission and `RESULT` before output. Acknowledgement returns it to `READY`. After a disconnect, `result` retrieves saved output without replay. An ordinary tool error exits 1 and returns to `READY`; inspect actual page state before retrying because the action may have occurred. If state is `RECOVERY`, the owning agent inspects what happened, restarts `browserctl-controller.service`, and uses `task recover` or `task end`. Never replay uncertain actions automatically.

Keep Chrome's "Preload pages" setting off. A tab shown from a preloaded page gets a new main frame that MCP `0.0.78` cannot reattach to, so later calls would time out after 90 s; `agent` instead fails with those tab URLs. Human launching does not depend on this automation check. Close the tabs or restart Chrome.

## Verify

```bash
python3 -W error::ResourceWarning -m unittest -v test_browserctl.py test_real_integration.py
```

Tests use disposable profiles/state. The integration test requires Chrome, npx, and MCP `0.0.78` already in the npm cache; it runs offline and verifies agent targeting as well as controller lifecycle behavior.

The laptop graphical regression can be run with `node test_graphical.cjs`. It requires Xephyr, GNOME Shell, xdotool, and a graphical X11 session; it creates an isolated desktop/profile under `scratch/` and checks human-tab routing, explicit new windows, agent-tab creation with continuous focus monitoring, maximized-window preservation, restored window roles, busy-controller independence, and frozen-renderer independence. It never uses the personal Chrome profile.
