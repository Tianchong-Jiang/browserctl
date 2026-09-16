# browserctl

## Purpose

Control one shared Chrome profile through one persistent Playwright MCP session. This README owns installation, commands, and recovery. Task ownership coordinates cooperating agents; it does not isolate them from human windows.

## Setup

Install official Chrome and Node/npx. Create `~/.config/browserctl/host.json` using first-hand host values; Oak uses:

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

Run startup in the graphical session. The desktop launch command is `/absolute/path/browserctl.py start %U`. It starts Chrome if needed, reuses existing human/agent windows, creates a missing window only when fewer than two exist, and records current process/tab/window IDs in `~/.config/browserctl/agent-window.json`. `start --prepare-only` prepares an already-running Chrome without opening a personal window. Startup refuses a busy controller or an unidentified pair of existing windows. Normal startup focuses the human window; explicit URLs open there. Private browsing must use Chrome's native `--incognito` action with the same profile and CDP flags.

Regions are Chrome window bounds. Oak uses its rightmost third. On the laptop, give the agent distinct normal-window bounds; startup identifies that native window by PID and bounds and moves it to desktop 2 without activating it. The laptop requires the system Python with PyGObject/Wnck. Preparation does not switch desktops. Old Edge configurations and journals are unsupported; the `deprecated` branch preserves that implementation.

Supported hosts are the laptop and Oak on Linux. CDP must use `http://127.0.0.1:PORT`, and the main Chrome process must own its listener; unsupported layouts fail instead of falling back.

The controller pins MCP `0.0.78` and loads `background-page.cjs` to suppress Playwright's window-raising call. It checks the executable, process owner, PID start time, profile, and CDP listener before dispatch.

## Use

```bash
./browserctl.py doctor
./browserctl.py status
./browserctl.py task begin "agent: description"
./browserctl.py agent <<'JS'
async (page) => {
  return await page.locator('body').ariaSnapshot();
}
JS
./browserctl.py task end
```

`agent` supplies only the verified agent Page, checking process identity, target, window, and recorded bounds before running your function. It temporarily emulates page focus without activating the OS window. Missing or moved targets fail; repair them with startup preparation. Controller reconnects retain the binding; browser restarts require preparation. Preserve human tabs and reuse agent tabs. Raw `call TOOL JSON` and `tools` are setup/debugging commands without window restrictions. `browser_close` is blocked. Never take over, recover, or end another agent's task.

Every dispatch is durably `RUNNING` before transmission and `RESULT` before output. Acknowledgement returns it to `READY`. After a disconnect, `result` retrieves saved output without replay. An ordinary tool error exits 1 and returns to `READY`; inspect actual page state before retrying because the action may have occurred. If state is `RECOVERY`, the owning agent inspects what happened, restarts `browserctl-controller.service`, and uses `task recover` or `task end`. Never replay uncertain actions automatically.

Keep Chrome's "Preload pages" setting off. A tab shown from a preloaded page gets a new main frame that MCP `0.0.78` cannot reattach to, so later calls would time out after 90 s; `start` and `agent` instead fail immediately with those tab URLs. Close the tabs or restart Chrome.

## Verify

```bash
python3 -W error::ResourceWarning -m unittest -v test_browserctl.py test_real_integration.py
```

Tests use disposable profiles/state. The integration test requires Chrome, npx, and MCP `0.0.78` already in the npm cache; it runs offline and verifies agent targeting as well as controller lifecycle behavior.
