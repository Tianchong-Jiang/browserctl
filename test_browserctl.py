#!/usr/bin/env python3
"""Deterministic browserctl tests; no real browser or service is touched."""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import browserctl

ROOT = Path(__file__).parent
CTL = ROOT / "browserctl.py"
UNIT = ROOT / "browserctl-controller.service"

EDGE = r'''
import pathlib, signal, socket, sys, time
profile, port, ready = pathlib.Path(sys.argv[1]), int(sys.argv[2]), pathlib.Path(sys.argv[3])
(profile / "Default").mkdir(parents=True, exist_ok=True)
held = open(profile / "Default/state", "w")
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", port))
listener.listen()
ready.write_text("ready")
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True: time.sleep(1)
'''

FAKE_MCP = r'''
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
port = int(sys.argv[sys.argv.index("--port") + 1])
events, mode_file = os.environ["FAKE_EVENTS"], os.environ["FAKE_MODE"]
session = os.environ.get("FAKE_SESSION", "warm-session")
tab = 0
lock = threading.Lock()
def mode(): return open(mode_file).read().strip()
def event(**value):
    value["server_pid"] = os.getpid()
    with lock:
        with open(events, "a") as stream: stream.write(json.dumps(value) + "\n")
class H(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def reply(self, status, value=None, include=True):
        body = b"" if value is None else json.dumps(value).encode()
        self.send_response(status)
        if include: self.send_header("Mcp-Session-Id", session)
        if body: self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try: self.wfile.write(body)
        except BrokenPipeError: pass
    def do_POST(self):
        global tab
        message = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        method, params = message.get("method"), message.get("params", {})
        name, arguments = params.get("name"), params.get("arguments", {})
        event(method=method, name=name, arguments=arguments, id=message.get("id"))
        if method == "initialize":
            return self.reply(200, {"jsonrpc":"2.0","id":message["id"],
                "result":{"protocolVersion":"2025-03-26","capabilities":{},
                          "serverInfo":{"name":"fake","version":"1"}}})
        if self.headers.get("Mcp-Session-Id") != session: return self.reply(404)
        if method == "notifications/initialized": return self.reply(202)
        if method == "tools/list":
            names = ["browser_tabs","browser_snapshot","browser_click",
                     "browser_navigate","browser_get_config","browser_close",
                     "browser_run_code","browser_run_code_unsafe","bad_id","die"]
            result = {"tools":[{"name":item,"inputSchema":{"type":"object"}}
                               for item in names]}
        elif method == "tools/call":
            current = mode()
            if name == "die": os._exit(7)
            if name == "bad_id":
                return self.reply(200, {"jsonrpc":"2.0","id":999,
                    "result":{"content":[{"type":"text","text":"bad"}]}})
            if name == "browser_tabs":
                action = arguments["action"]
                if current == "fail_" + action:
                    result = {"content":[{"type":"text","text":"failed"}],"isError":True}
                else:
                    if action == "new": tab += 1
                    elif action == "select": tab = arguments["index"]
                    result = {"content":[{"type":"text","text":f"{action} tab {tab}"}]}
            elif name == "browser_snapshot":
                result = {"content":[] if current == "empty_snapshot" else
                          [{"type":"text","text":f"snapshot tab {tab} ref=e1"}]}
            else:
                result = {"content":[{"type":"text","text":f"{name} ok"}],
                          "isError": current == "fail_call"}
        else: return self.reply(400)
        return self.reply(200, {"jsonrpc":"2.0","id":message["id"],"result":result})
if os.environ.get("FAKE_FOREIGN_LISTENER"):
    intermediate = os.fork()
    if intermediate == 0:
        parent = os.getpid()
        if os.fork() == 0:
            while os.getppid() == parent: time.sleep(.01)
            ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
        os._exit(0)
    os.waitpid(intermediate, 0)
    while True: time.sleep(1)
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def free_port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


class BrowserctlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="browserctl-test-")
        self.edge, self.daemon, self.notify = [], None, None
        self.addCleanup(self.cleanup_all)
        self.base = Path(self.temp.name)
        self.config = self.base / "config"
        self.state = self.base / "state"
        self.runtime = self.base / "runtime"
        self.bin = self.base / "bin"
        self.human, self.agent = self.base / "human", self.base / "agent"
        for path in (self.config / "browserctl", self.config / "systemd/user",
                     self.state, self.runtime, self.bin, self.human, self.agent):
            path.mkdir(parents=True, mode=0o700)
        self.events, self.mode = self.base / "events", self.base / "mode"
        self.display = self.base / "display"
        self.mode.write_text("healthy")
        self.display.write_text("human")
        self.fake_mcp = self.base / "fake-mcp.py"
        self.fake_mcp.write_text(FAKE_MCP)
        fake_npx = self.bin / "npx"
        fake_npx.write_text(
            f"#!/bin/sh\nexec {sys.executable!s} {self.fake_mcp!s} \"$@\"\n")
        fake_npx.chmod(0o700)
        xrandr = self.bin / "xrandr"
        xrandr.write_text(
            "#!/bin/sh\n"
            f"if [ \"$(cat {self.display!s})\" = agent ]; then\n"
            "  printf '%s\\n' 'eDP-1 connected' 'DP-1 connected'\n"
            "else printf '%s\\n' 'eDP-1 connected'; fi\n")
        xrandr.chmod(0o700)
        systemctl = self.bin / "systemctl"
        systemctl.write_text(
            "#!/bin/sh\n"
            "pid=$PPID\n"
            "if [ \"${FAKE_SERVICE_STATE:-activating}\" = active ]; then "
            f"pid=$(cat {self.base / 'daemon.pid'}); fi\n"
            f"printf '%s\\n' \"FragmentPath=${{FAKE_FRAGMENT:-{self.config / 'systemd/user/browserctl-controller.service'}}}\" "
            "'UnitFileState=static' \"ActiveState=${FAKE_SERVICE_STATE:-activating}\" \"MainPID=$pid\"\n")
        systemctl.chmod(0o700)
        installed = self.config / "systemd/user/browserctl-controller.service"
        installed.write_bytes(UNIT.read_bytes())
        installed.chmod(0o644)
        self.notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.notify_path = self.runtime / "notify.sock"
        self.notify.bind(str(self.notify_path))
        self.notify.settimeout(5)
        self.ports = [free_port(), free_port()]
        self.start_edge(self.human, self.ports[0])
        self.start_edge(self.agent, self.ports[1])
        data = {
            "schema_version": 1, "hostname": socket.gethostname(),
            "user": subprocess.check_output(["id", "-un"], text=True).strip(),
            "human": {"edge_exe": os.path.realpath(sys.executable),
                      "profile": str(self.human),
                      "cdp_endpoint": f"http://127.0.0.1:{self.ports[0]}"},
            "agent": {"edge_exe": os.path.realpath(sys.executable),
                      "profile": str(self.agent),
                      "cdp_endpoint": f"http://127.0.0.1:{self.ports[1]}"},
            "mcp_command": [str(fake_npx), "--yes", "@playwright/mcp@0.0.78"],
        }
        host = self.config / "browserctl/host.json"
        host.write_text(json.dumps(data))
        host.chmod(0o600)
        self.env = {
            **os.environ, "XDG_CONFIG_HOME": str(self.config),
            "XDG_STATE_HOME": str(self.state),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_EVENTS": str(self.events), "FAKE_MODE": str(self.mode),
            "NOTIFY_SOCKET": str(self.notify_path),
        }
        self.start_daemon()

    def cleanup_all(self):
        self.stop_daemon()
        for process in getattr(self, "edge", []):
            self.stop_process(process)
        if self.notify:
            self.notify.close()
        self.temp.cleanup()

    def start_edge(self, profile, port):
        ready = self.base / f"edge-{port}.ready"
        process = subprocess.Popen([
            sys.executable, "-c", EDGE, str(profile), str(port), str(ready),
            f"--remote-debugging-port={port}", f"--user-data-dir={profile}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not ready.exists():
            if process.poll() is not None:
                self.fail(f"fake Edge failed: {process.stderr.read()}")
            time.sleep(.01)
        self.assertTrue(ready.exists())
        (profile / "SingletonLock").symlink_to(
            f"{socket.gethostname()}-{process.pid}")
        self.edge.append(process)

    def stop_process(self, process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(2)
        process.stderr.close()

    def start_daemon(self, env=None, expect_fail=False):
        merged = {**self.env, **(env or {})}
        process = subprocess.Popen(
            [CTL, "daemon"], env=merged, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if expect_fail:
            stdout, stderr = process.communicate(timeout=5)
            self.assertNotEqual(process.returncode, 0, stdout)
            return stderr
        self.daemon = process
        (self.base / "daemon.pid").write_text(str(process.pid))
        try:
            notice = self.notify.recv(4096)
        except TimeoutError:
            if process.poll() is None:
                process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            self.fail(f"daemon did not notify ready: {stdout} {stderr}")
        self.assertIn(b"READY=1", notice)
        self.assertIsNone(process.poll())
        self.assertTrue((self.runtime / "browserctl.sock").exists())
        events = self.read_events()
        self.assertTrue(any(item["method"] == "initialize" for item in events),
                        "READY preceded warm MCP initialize")

    def stop_daemon(self):
        if not self.daemon:
            return
        server_pids = {item["server_pid"] for item in self.read_events()}
        if self.daemon.poll() is None:
            self.daemon.terminate()
            self.daemon.wait(4)
        self.daemon.stdout.close()
        self.daemon.stderr.close()
        self.daemon = None
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(
                Path(f"/proc/{pid}").exists() for pid in server_pids):
            time.sleep(.02)
        self.assertFalse(any(Path(f"/proc/{pid}").exists() for pid in server_pids),
                         "MCP child leaked")

    def run_ctl(self, *args, check=True, env=None, timeout=5):
        merged = {**self.env, "FAKE_SERVICE_STATE": "active", **(env or {})}
        return subprocess.run([CTL, *args], env=merged, text=True,
                              capture_output=True, check=check, timeout=timeout)

    def status(self):
        return json.loads(self.run_ctl("status").stdout)

    def task(self):
        return json.loads((self.state / "browserctl/task.json").read_text())

    def read_events(self):
        if not self.events.exists():
            return []
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def calls(self, name):
        return [item for item in self.read_events() if item.get("name") == name]

    def ack_workflow(self):
        self.run_ctl("task", "begin", "test")
        self.run_ctl("tab", "new")
        self.run_ctl("tab", "list")
        self.run_ctl("tab", "select", "1")
        self.run_ctl("call", "browser_snapshot", "{}")
        self.assertEqual(self.task()["phase"], "ARMED")

    def test_two_profiles_and_doctor_select_only_policy_pid(self):
        result = json.loads(self.run_ctl("doctor").stdout)
        self.assertEqual(result["edge_pid"], self.edge[0].pid)
        self.display.write_text("agent")
        active = json.loads(self.run_ctl("doctor").stdout)
        self.assertEqual(active["edge_pid"], self.edge[1].pid)

    def test_direct_and_transient_daemon_rejected_before_mcp(self):
        self.stop_daemon()
        before = len(self.read_events())
        error = self.start_daemon(
            {"FAKE_FRAGMENT": str(self.base / "transient.service")},
            expect_fail=True)
        self.assertIn("transient", error)
        self.assertEqual(len(self.read_events()), before)
        direct = subprocess.run(
            [CTL, "daemon"], env={key: value for key, value in self.env.items()
                                 if key != "NOTIFY_SOCKET"},
            text=True, capture_output=True, timeout=5)
        self.assertNotEqual(direct.returncode, 0)
        self.assertIn("exact stable service", direct.stderr)

    def test_legacy_state_refused_without_mutation(self):
        self.stop_daemon()
        legacy = self.state / "browserctl/controller.json"
        legacy.write_text('{"schema_version":3}')
        legacy.chmod(0o600)
        before = legacy.read_bytes()
        error = self.start_daemon(expect_fail=True)
        self.assertIn("operator archival", error)
        self.assertEqual(legacy.read_bytes(), before)

    def test_phase_machine_failed_new_select_and_snapshot(self):
        self.run_ctl("task", "begin", "phase")
        blocked = self.run_ctl("tab", "list", check=False)
        self.assertIn("tab list", blocked.stderr)
        self.mode.write_text("fail_new")
        self.assertNotEqual(self.run_ctl("tab", "new", check=False).returncode, 0)
        self.assertEqual(self.task()["phase"], "NEW")
        self.mode.write_text("healthy")
        self.run_ctl("tab", "new")
        self.assertEqual(self.task()["phase"], "LIST")
        self.run_ctl("tab", "list")
        self.mode.write_text("fail_select")
        self.assertNotEqual(self.run_ctl("tab", "select", "1", check=False).returncode, 0)
        self.assertEqual(self.task()["phase"], "SELECT")
        self.mode.write_text("healthy")
        self.run_ctl("tab", "select", "1")
        self.assertEqual(self.task()["phase"], "SNAPSHOT")
        self.mode.write_text("empty_snapshot")
        self.assertNotEqual(self.run_ctl(
            "call", "browser_snapshot", "{}", check=False).returncode, 0)
        self.assertEqual(self.task()["phase"], "SNAPSHOT")
        self.assertFalse(self.task()["snapshot"])
        self.mode.write_text("healthy")
        self.run_ctl("call", "browser_snapshot", "{}")
        self.assertEqual(self.task()["phase"], "ARMED")
        self.run_ctl("tab", "list")
        self.assertEqual(self.task()["phase"], "SELECT")
        self.assertFalse(self.task()["snapshot"])
        blocked = self.run_ctl(
            "call", "browser_navigate", "{}", check=False)
        self.assertIn("new/list/select/snapshot", blocked.stderr)
        self.run_ctl("tab", "select", "1")
        self.run_ctl("call", "browser_snapshot", "{}")
        self.assertEqual(self.task()["phase"], "ARMED")

    def test_refs_and_failed_mutation_conservatively_clear_snapshot(self):
        self.ack_workflow()
        self.mode.write_text("fail_call")
        self.assertNotEqual(self.run_ctl(
            "call", "browser_navigate", "{}", check=False).returncode, 0)
        self.assertFalse(self.task()["snapshot"])
        before = len(self.calls("browser_click"))
        blocked = self.run_ctl(
            "call", "browser_click", '{"target":"e1"}', check=False)
        self.assertIn("fresh browser_snapshot", blocked.stderr)
        self.assertEqual(len(self.calls("browser_click")), before)

    def test_identity_drift_quarantines_without_dispatch(self):
        self.run_ctl("task", "begin", "drift")
        before = len(self.calls("browser_tabs"))
        self.display.write_text("agent")
        failed = self.run_ctl("tab", "new", check=False)
        self.assertIn("identity", failed.stderr)
        self.assertEqual(self.status()["state"], "RECOVERY")
        self.assertEqual(len(self.calls("browser_tabs")), before)

    def test_duplicate_json_and_session_syntax(self):
        self.run_ctl("task", "begin", "strict")
        failed = self.run_ctl(
            "call", "browser_snapshot", '{"x":1,"x":2}', check=False)
        self.assertIn("duplicate key", failed.stderr)
        self.assertTrue(browserctl.valid_session("abc-123"))
        for bad in ("", "bad session", "x\n", "x" * 257):
            self.assertFalse(browserctl.valid_session(bad))

    def test_invalid_mcp_session_and_foreign_controller_refused(self):
        self.stop_daemon()
        error = self.start_daemon(
            {"FAKE_SESSION": "bad session"}, expect_fail=True)
        self.assertIn("session", error)
        cdp = f"http://127.0.0.1:{self.ports[0]}"
        foreign = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)",
             "playwright-mcp", cdp], start_new_session=True)
        try:
            error = self.start_daemon(expect_fail=True)
            self.assertIn("foreign or legacy", error)
        finally:
            os.killpg(foreign.pid, signal.SIGTERM)
            foreign.wait(2)

    def test_mcp_listener_must_belong_to_child_tree(self):
        self.stop_daemon()
        initialized = len([
            item for item in self.read_events()
            if item["method"] == "initialize"])
        error = self.start_daemon(
            {"FAKE_FOREIGN_LISTENER": "1"}, expect_fail=True)
        self.assertIn("listener", error)
        self.assertEqual(len([
            item for item in self.read_events()
            if item["method"] == "initialize"]), initialized)

    def test_group_cleanup_refuses_changed_leader_identity(self):
        child = subprocess.Popen(["sleep", "10"], start_new_session=True)
        mcp = browserctl.MCP.__new__(browserctl.MCP)
        mcp.child, mcp.child_start = child, "wrong"
        try:
            with self.assertRaisesRegex(browserctl.Error, "identity changed"):
                mcp.close()
            self.assertIsNone(child.poll())
        finally:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(2)

    def test_group_cleanup_kills_term_resistant_survivor(self):
        ready = self.base / "survivor.ready"
        survivor = (
            "import os,signal,sys,time,pathlib;"
            "os.setpgrp();"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "pathlib.Path(sys.argv[1]).write_text('ready');"
            "time.sleep(20)")
        leader = subprocess.Popen([
            sys.executable, "-c",
            "import subprocess,sys,time;"
            f"subprocess.Popen([{sys.executable!r},'-c',{survivor!r},sys.argv[1]]);"
            "time.sleep(20)", str(ready)], start_new_session=True)
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists())
        mcp = browserctl.MCP.__new__(browserctl.MCP)
        mcp.child = leader
        mcp.child_start = browserctl.process_info(leader.pid)["start"]
        try:
            with mock.patch.object(browserctl, "IO_TIMEOUT", .1):
                mcp.close()
            self.assertFalse(browserctl.group_members(leader.pid))
        finally:
            if browserctl.group_members(leader.pid):
                os.killpg(leader.pid, signal.SIGKILL)
            leader.wait(2)

    def test_oversized_completed_result_requires_recovery(self):
        self.stop_daemon()
        task = browserctl.new_task("oversized", "0" * 32)
        daemon = browserctl.Daemon.__new__(browserctl.Daemon)
        daemon.validate_identity = lambda _: None
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(browserctl, "MAX", 128):
            self.assertEqual(
                browserctl.state_dir(), self.state / "browserctl")
            with self.assertRaisesRegex(browserctl.Error, "do not replay"):
                daemon.dispatch(
                    task, "oversized-id", "tab new",
                    lambda: {"content": [{"type": "text", "text": "x" * 500}]},
                    {"phase": "LIST"})
            journal = self.task()
        self.assertEqual(journal["state"], "RECOVERY")
        self.assertIsNone(journal["receipt"])
        self.assertIn("completed", journal["reason"])
        self.assertIn("do not replay", journal["reason"])

    def test_stale_socket_probe_timeout_preserves_socket(self):
        self.stop_daemon()
        path = self.runtime / "browserctl.sock"
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(path))
        os.chmod(path, 0o600)
        probe = mock.MagicMock()
        probe.__enter__.return_value = probe
        probe.connect_ex.side_effect = socket.timeout("bounded")
        try:
            with mock.patch.object(browserctl.socket, "socket",
                                   return_value=probe), \
                 mock.patch.object(browserctl, "IO_TIMEOUT", .1):
                with self.assertRaisesRegex(browserctl.Error, "ambiguous"):
                    browserctl.remove_stale_socket(path)
            probe.settimeout.assert_called_once_with(.1)
            self.assertTrue(path.exists())
        finally:
            stale.close()
            path.unlink(missing_ok=True)

    def test_result_disconnect_recovery_and_no_replay(self):
        self.ack_workflow()
        stream = socket.socket(socket.AF_UNIX)
        stream.connect(str(self.runtime / "browserctl.sock"))
        browserctl.send_message(stream, {
            "id": "lost", "command": "call",
            "args": ["browser_navigate", {}], "build": browserctl.BUILD})
        response = browserctl.recv_message(stream)
        self.assertTrue(response["ok"])
        stream.close()
        deadline = time.monotonic() + 3
        while self.task()["state"] != "RESULT" and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(len(self.calls("browser_navigate")), 1)
        self.stop_daemon()
        self.start_daemon()
        self.run_ctl("result")
        self.assertEqual(self.status()["state"], "RECOVERY")
        self.assertEqual(len(self.calls("browser_navigate")), 1)
        self.run_ctl("task", "recover")
        self.assertEqual(self.task()["phase"], "LIST")
        self.assertNotEqual(self.run_ctl("tab", "new", check=False).returncode, 0)
        self.run_ctl("tab", "list")
        self.run_ctl("tab", "select", "1")
        self.run_ctl("call", "browser_snapshot", "{}")
        self.assertEqual(self.task()["phase"], "ARMED")

    def test_initial_request_ack_and_client_response_stalls_are_bounded(self):
        stream = socket.socket(socket.AF_UNIX)
        stream.connect(str(self.runtime / "browserctl.sock"))
        time.sleep(browserctl.IO_TIMEOUT + .2)
        stream.close()
        self.assertEqual(self.status()["state"], "IDLE")
        self.ack_workflow()
        stalled = socket.socket(socket.AF_UNIX)
        stalled.connect(str(self.runtime / "browserctl.sock"))
        browserctl.send_message(stalled, {
            "id": "ack-stall", "command": "call",
            "args": ["browser_navigate", {}], "build": browserctl.BUILD})
        browserctl.recv_message(stalled)
        time.sleep(browserctl.IO_TIMEOUT + .2)
        stalled.close()
        self.assertEqual(self.status()["state"], "RESULT")
        self.run_ctl("result")
        self.stop_daemon()
        path = self.runtime / "browserctl.sock"
        path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        server.listen(1)
        def stall():
            connection, _ = server.accept()
            browserctl.recv_message(connection)
            time.sleep(.5)
            connection.close()
        thread = threading.Thread(target=stall)
        thread.start()
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(browserctl, "HTTP_TIMEOUT", .1), \
             mock.patch.object(browserctl, "IO_TIMEOUT", .1):
            with self.assertRaises(browserctl.Error):
                browserctl.client("status", [])
        thread.join()
        server.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
