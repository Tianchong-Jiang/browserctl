#!/usr/bin/env python3
"""Disposable exact-MCP integration; never contacts a live browser profile."""

import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

import browserctl

ROOT = Path(__file__).parent
CTL = ROOT / "browserctl.py"
UNIT = ROOT / "browserctl-controller.service"
CACHE = Path(os.getenv(
    "XDG_CACHE_HOME", Path.home() / ".cache")) / "browserctl/npm"


def free_port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


def dependencies():
    edge = next(filter(None, (shutil.which(name) for name in (
        "microsoft-edge", "microsoft-edge-stable"))), None)
    npx = shutil.which("npx")
    missing = [
        label for label, path in (("Microsoft Edge", edge), ("npx", npx))
        if path is None]
    return edge, npx, missing


def processes_with(argument):
    found = []
    needle = argument.encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if needle in command:
            found.append(int(entry.name))
    return found


class RealIntegration(unittest.TestCase):
    def test_disposable_edge_and_exact_cached_mcp(self):
        edge_command, npx_command, missing = dependencies()
        if missing:
            self.fail(
                "qualification dependencies missing from PATH: " +
                ", ".join(missing))
        edge_launcher = Path(edge_command).absolute()
        npx = Path(npx_command).absolute()
        edge = daemon = notify = None
        mcp_marker = None
        with tempfile.TemporaryDirectory(prefix="browserctl-real-") as name:
            base = Path(name)
            config, state = base / "config", base / "state"
            runtime, bindir = base / "runtime", base / "bin"
            human, agent = base / "human", base / "agent"
            for path in (config / "browserctl", config / "systemd/user",
                         state, runtime, bindir, human, agent):
                path.mkdir(parents=True, mode=0o700)
            installed = config / "systemd/user/browserctl-controller.service"
            installed.write_bytes(UNIT.read_bytes())
            installed.chmod(0o644)
            xrandr = bindir / "xrandr"
            xrandr.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'eDP-1 connected 1280x720+0+0'\n")
            xrandr.chmod(0o700)
            systemctl = bindir / "systemctl"
            systemctl.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' 'FragmentPath={installed}' 'UnitFileState=static' "
                "'ActiveState=activating' \"MainPID=$PPID\"\n")
            systemctl.chmod(0o700)
            notify_path = runtime / "notify.sock"
            notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            notify.bind(str(notify_path))
            notify.settimeout(.2)
            cdp, unused = free_port(), free_port()
            env = {
                **os.environ, "XDG_CONFIG_HOME": str(config),
                "XDG_STATE_HOME": str(state),
                "XDG_RUNTIME_DIR": str(runtime),
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "NOTIFY_SOCKET": str(notify_path),
                "npm_config_cache": str(CACHE), "npm_config_offline": "true",
            }
            try:
                edge = subprocess.Popen([
                    edge_launcher, "--headless=new", "--no-first-run",
                    "--no-default-browser-check", "--disable-gpu", "--no-sandbox",
                    f"--remote-debugging-port={cdp}",
                    f"--user-data-dir={human}", "about:blank"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, text=True, start_new_session=True)
                deadline = time.monotonic() + 15
                lock = human / "SingletonLock"
                while time.monotonic() < deadline and not lock.is_symlink():
                    if edge.poll() is not None:
                        self.fail(f"temporary Edge failed: {edge.stderr.read()}")
                    time.sleep(.05)
                self.assertTrue(lock.is_symlink())
                while time.monotonic() < deadline:
                    with socket.socket() as probe:
                        if probe.connect_ex(("127.0.0.1", cdp)) == 0:
                            break
                    time.sleep(.05)
                else:
                    self.fail("temporary Edge CDP listener did not become ready")
                lock_pid = int(os.readlink(lock).rsplit("-", 1)[1])
                edge_exe = os.readlink(f"/proc/{lock_pid}/exe")
                host = {
                    "schema_version": 1, "hostname": socket.gethostname(),
                    "user": subprocess.check_output(
                        ["id", "-un"], text=True).strip(),
                    "human": {
                        "edge_exe": edge_exe, "profile": str(human),
                        "cdp_endpoint": f"http://127.0.0.1:{cdp}"},
                    "agent": {
                        "edge_exe": edge_exe, "profile": str(agent),
                        "cdp_endpoint": f"http://127.0.0.1:{unused}"},
                    "mcp_command": [
                        str(npx), "--yes", "@playwright/mcp@0.0.78"],
                }
                host_path = config / "browserctl/host.json"
                host_path.write_text(json.dumps(host))
                host_path.chmod(0o600)
                daemon = subprocess.Popen(
                    [CTL, "daemon"], env=env, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                notice = None
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and notice is None:
                    try:
                        notice = notify.recv(4096)
                    except TimeoutError:
                        if daemon.poll() is not None:
                            break
                if notice is None:
                    if daemon.poll() is None: daemon.terminate()
                    stdout, stderr = daemon.communicate(timeout=5)
                    lock_pid = os.readlink(human / "SingletonLock")
                    pid = int(lock_pid.rsplit("-", 1)[1])
                    argv = Path(f"/proc/{pid}/cmdline").read_bytes().replace(
                        b"\0", b" ").decode()
                    self.fail(
                        f"daemon did not become ready: {stdout} {stderr}; "
                        f"lock={lock_pid} port={cdp} argv={argv}")
                self.assertIn(b"READY=1", notice)
                mcp_marker = f"http://127.0.0.1:{cdp}"
                self.assertTrue(
                    processes_with(mcp_marker), "warm MCP process not found")
                for args in (
                    ["task", "begin", "disposable integration"],
                    ["tab", "new"], ["tab", "list"],
                    ["tab", "select", "1"],
                    ["call", "browser_snapshot", "{}"],
                    ["tab", "close", "1"], ["task", "end"]):
                    result = subprocess.run(
                        [CTL, *args], env=env, text=True,
                        capture_output=True, timeout=30)
                    self.assertEqual(
                        result.returncode, 0,
                        f"{args}: {result.stdout} {result.stderr}")
            finally:
                if daemon and daemon.poll() is None:
                    daemon.terminate()
                    daemon.wait(5)
                if mcp_marker:
                    deadline = time.monotonic() + 2
                    while (processes_with(mcp_marker) and
                           time.monotonic() < deadline):
                        time.sleep(.02)
                    self.assertFalse(
                        processes_with(mcp_marker), "real MCP process leaked")
                if daemon:
                    daemon.stdout.close()
                    daemon.stderr.close()
                if edge and edge.poll() is None:
                    os.killpg(edge.pid, signal.SIGTERM)
                    try:
                        edge.wait(5)
                    except subprocess.TimeoutExpired:
                        os.killpg(edge.pid, signal.SIGKILL)
                        edge.wait(5)
                if edge:
                    edge.stderr.close()
                if notify:
                    notify.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
