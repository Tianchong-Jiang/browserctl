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
                     "browser_run_code","browser_run_code_unsafe",
                     "bad_id","rpc_error","die"]
            result = {"tools":[{"name":item,"inputSchema":{"type":"object"}}
                               for item in names]}
        elif method == "tools/call":
            current = mode()
            if name == "die": os._exit(7)
            if name == "bad_id":
                return self.reply(200, {"jsonrpc":"2.0","id":999,
                    "result":{"content":[{"type":"text","text":"bad"}]}})
            if name == "rpc_error":
                return self.reply(200, {"jsonrpc":"2.0","id":message["id"],
                    "error":{"code":-32001,"message":"determinate failure"}})
            if name == "browser_tabs":
                action = arguments["action"]
                if action == "new" and current == "fail_new_after_create":
                    tab += 1
                    result = {"content":[{"type":"text","text":"created then failed"}],
                              "isError":True}
                elif current == "fail_" + action:
                    result = {"content":[{"type":"text","text":"failed"}],"isError":True}
                else:
                    if action == "new": tab += 1
                    elif action == "select": tab = arguments["index"]
                    elif action == "close": tab = max(0, tab - 1)
                    result = {"content":[{"type":"text","text":f"{action} tab {tab}"}]}
            elif name == "browser_snapshot":
                result = {"content":[] if current == "empty_snapshot" else
                          [{"type":"text","text":f"snapshot tab {tab} ref=e1"}]}
            else:
                result = {"content":[{"type":"text","text":f"{name} ok"}],
                          "isError": current == "fail_call"}
        else: return self.reply(400)
        return self.reply(200, {"jsonrpc":"2.0","id":message["id"],"result":result})
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

    def test_two_profiles_and_doctor_select_only_policy_pid(self):
        result = json.loads(self.run_ctl("doctor").stdout)
        self.assertEqual(result["edge_pid"], self.edge[0].pid)
        self.display.write_text("agent")
        active = json.loads(self.run_ctl("doctor").stdout)
        self.assertEqual(active["edge_pid"], self.edge[1].pid)

    def test_selected_edge_tree_must_own_configured_listener(self):
        self.stop_daemon()
        selected = self.edge.pop(0)
        self.stop_process(selected)
        (self.human / "SingletonLock").unlink()
        (self.base / f"edge-{self.ports[0]}.ready").unlink(missing_ok=True)
        unrelated = self.base / "unrelated"
        unrelated.mkdir()
        self.start_edge(unrelated, self.ports[0])
        selected = subprocess.Popen([
            sys.executable, "-c", "import time; time.sleep(20)",
            f"--remote-debugging-port={self.ports[0]}",
            f"--user-data-dir={self.human}"],
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.edge.append(selected)
        (self.human / "SingletonLock").symlink_to(
            f"{socket.gethostname()}-{selected.pid}")
        failed = self.run_ctl(
            "doctor", check=False, env={"FAKE_SERVICE_STATE": "inactive"})
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("does not own the CDP listener", failed.stderr)

    def test_direct_daemon_rejected_before_mcp(self):
        self.stop_daemon()
        before = len(self.read_events())
        direct = subprocess.run(
            [CTL, "daemon"], env={key: value for key, value in self.env.items()
                                 if key != "NOTIFY_SOCKET"},
            text=True, capture_output=True, timeout=5)
        self.assertNotEqual(direct.returncode, 0)
        self.assertIn("service MainPID", direct.stderr)
        self.assertEqual(len(self.read_events()), before)

    def test_failed_new_can_reconcile_continue_and_end(self):
        self.run_ctl("task", "begin", "reconcile")
        self.mode.write_text("fail_new_after_create")
        failed = self.run_ctl("tab", "new", check=False)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(self.status()["state"], "READY")
        self.mode.write_text("healthy")
        self.assertIn("list tab 1", self.run_ctl("tab", "list").stdout)
        self.run_ctl("tab", "select", "1")
        self.run_ctl("call", "browser_snapshot", "{}")
        self.run_ctl("call", "browser_navigate", '{"url":"https://example.com"}')
        self.run_ctl("task", "end")
        self.assertEqual(self.status()["state"], "IDLE")

    def test_calls_have_no_artificial_tab_snapshot_or_ref_gate(self):
        self.run_ctl("task", "begin", "ungated")
        self.run_ctl("call", "browser_navigate", "{}")
        self.run_ctl("call", "browser_click", '{"target":"e999"}')
        self.mode.write_text("empty_snapshot")
        self.run_ctl("call", "browser_snapshot", "{}")
        self.run_ctl("tab", "list")
        self.run_ctl("tab", "new")
        self.run_ctl("tab", "close", "1")
        self.run_ctl("task", "end")

    def test_raw_tools_and_run_code_allowed_but_browser_close_blocked(self):
        self.run_ctl("task", "begin", "tools")
        listed = json.loads(self.run_ctl("tools").stdout)
        names = {tool["name"] for tool in listed["tools"]}
        self.assertTrue({
            "browser_tabs", "browser_get_config", "browser_run_code",
            "browser_run_code_unsafe"} <= names)
        self.assertNotIn("browser_close", names)
        for name, arguments in (
                ("browser_tabs", '{"action":"list"}'),
                ("browser_get_config", "{}"),
                ("browser_run_code", '{"code":"async page => 1"}'),
                ("browser_run_code_unsafe", '{"code":"async page => 1"}')):
            self.run_ctl("call", name, arguments)
        before = len(self.calls("browser_close"))
        blocked = self.run_ctl("call", "browser_close", "{}", check=False)
        self.assertIn("forbidden", blocked.stderr)
        self.assertEqual(len(self.calls("browser_close")), before)
        self.assertEqual(self.status()["state"], "READY")
        self.run_ctl("task", "end")

    def test_jsonrpc_error_is_durable_exit_one_then_ack_ready(self):
        self.run_ctl("task", "begin", "rpc-error")
        with socket.socket(socket.AF_UNIX) as stream:
            stream.connect(str(self.runtime / "browserctl.sock"))
            browserctl.send_message(stream, {
                "id": "rpc-error", "command": "call",
                "args": ["rpc_error", {}], "build": browserctl.BUILD})
            response = browserctl.recv_message(stream)
            receipt = response["value"]
            self.assertTrue(response["ok"])
            self.assertEqual(receipt["exit_code"], 1)
            self.assertIn("determinate failure", receipt["stderr"])
            self.assertEqual(self.task()["state"], "RESULT")
            browserctl.send_message(stream, {"ack": receipt["id"]})
            self.assertEqual(
                browserctl.recv_message(stream)["acked"], receipt["id"])
        self.assertEqual(self.status()["state"], "READY")
        self.assertIsNone(self.task()["receipt"])
        self.run_ctl("task", "end")

    def test_task_end_from_ready_after_begin_success_and_tool_error(self):
        paths = (
            lambda: None,
            lambda: self.run_ctl("tab", "list"),
            lambda: self.run_ctl("call", "browser_navigate", "{}",
                                 check=False))
        for index, action in enumerate(paths):
            with self.subTest(index=index):
                self.run_ctl("task", "begin", f"end-{index}")
                if index == 2:
                    self.mode.write_text("fail_call")
                action()
                self.assertEqual(self.status()["state"], "READY")
                self.run_ctl("task", "end")
                self.assertEqual(self.status()["state"], "IDLE")
                self.mode.write_text("healthy")

    def test_legacy_ready_new_migrates_once_and_restart_rebinds(self):
        self.run_ctl("task", "begin", "legacy-ready")
        old_generation = self.task()["generation"]
        self.stop_daemon()
        path = self.state / "browserctl/task.json"
        legacy = self.task()
        legacy.update({"phase": "NEW", "snapshot": False})
        path.write_text(json.dumps(legacy))
        path.chmod(0o600)
        with mock.patch.dict(os.environ, self.env):
            migrated = browserctl.read_task(False)
            first = path.read_bytes()
            browserctl.read_task(False)
        self.assertEqual(path.read_bytes(), first)
        self.assertEqual(migrated["owner"], "legacy-ready")
        self.assertEqual(migrated["generation"], old_generation)
        self.assertEqual(set(migrated), browserctl.TASK_KEYS)
        self.start_daemon()
        self.assertEqual(self.status()["state"], "READY")
        self.assertNotEqual(self.task()["generation"], old_generation)
        self.run_ctl("task", "end")

    def test_malformed_legacy_result_is_not_migrated(self):
        self.stop_daemon()
        legacy = browserctl.new_task("malformed", "0" * 32)
        legacy.update({
            "state": "RESULT", "phase": "ARMED", "snapshot": False,
            "receipt": {
                "id": "receipt", "request_id": "request",
                "stdout": "", "stderr": "", "exit_code": 1,
                "success": "not-a-boolean", "post": {}}})
        path = self.state / "browserctl/task.json"
        path.write_text(json.dumps(legacy))
        before = path.read_bytes()
        with mock.patch.dict(os.environ, self.env), \
             self.assertRaisesRegex(browserctl.Error, "legacy result"):
            browserctl.read_task(False)
        self.assertEqual(path.read_bytes(), before)

    def test_old_running_can_recover_or_be_abandoned(self):
        for outcome in ("recover", "end"):
            with self.subTest(outcome=outcome):
                self.run_ctl("task", "begin", f"old-running-{outcome}")
                self.stop_daemon()
                task = self.task()
                task.update({
                    "state": "RUNNING", "operation": {
                        "id": "uncertain", "method": "call browser_navigate"}})
                path = self.state / "browserctl/task.json"
                path.write_text(json.dumps(task))
                path.chmod(0o600)
                self.start_daemon()
                self.assertEqual(self.status()["state"], "RECOVERY")
                if outcome == "recover":
                    self.run_ctl("task", "recover")
                    self.assertEqual(self.status()["state"], "READY")
                    self.run_ctl("call", "browser_snapshot", "{}")
                    self.run_ctl("tab", "list")
                self.run_ctl("task", "end")
                self.assertEqual(self.status()["state"], "IDLE")

    def test_identity_drift_quarantines_without_dispatch(self):
        self.run_ctl("task", "begin", "drift")
        before = len(self.calls("browser_tabs"))
        self.display.write_text("agent")
        failed = self.run_ctl("tab", "new", check=False)
        self.assertIn("identity", failed.stderr)
        self.assertEqual(self.status()["state"], "RECOVERY")
        self.assertEqual(len(self.calls("browser_tabs")), before)

    def test_mcp_session_syntax(self):
        self.assertTrue(browserctl.valid_session("abc-123"))
        for bad in ("", "bad session", "x\n", "x" * 257):
            self.assertFalse(browserctl.valid_session(bad))

    def test_strict_json_rejects_duplicates_and_nonfinite_numbers(self):
        with self.assertRaisesRegex(browserctl.Error, "duplicate key"):
            browserctl.strict_load('{"x":1,"x":2}')
        for value in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(value=value), \
                 self.assertRaisesRegex(browserctl.Error, "number"):
                browserctl.strict_load(value)

    def test_corrupt_task_types_raise_browserctl_error(self):
        self.stop_daemon()
        path = self.state / "browserctl/task.json"
        cases = []
        task = browserctl.new_task("corrupt", "0" * 32)
        task["state"] = []
        cases.append(task)
        task = browserctl.new_task("corrupt", "0" * 32)
        task["generation"] = []
        cases.append(task)
        task = browserctl.new_task("corrupt", "0" * 32)
        task.update({
            "state": "RUNNING",
            "operation": {"id": [], "method": "call"}})
        cases.append(task)
        task = browserctl.new_task("corrupt", "0" * 32)
        task.update({
            "state": "RESULT",
            "receipt": {
                "id": "r", "request_id": "q", "stdout": "", "stderr": "",
                "exit_code": []}})
        cases.append(task)
        with mock.patch.dict(os.environ, self.env):
            for task in cases:
                with self.subTest(task=task), \
                     self.assertRaises(browserctl.Error):
                    path.write_text(json.dumps(task))
                    browserctl.read_task(False)

    def test_tool_success_requires_content_list_not_known_block_types(self):
        mcp = browserctl.MCP.__new__(browserctl.MCP)
        mcp.call_raw = lambda *_: {"content": [{"type": "future-block"}]}
        self.assertEqual(
            mcp.call("future", {})["content"], [{"type": "future-block"}])
        mcp.call_raw = lambda *_: {"content": {"type": "text"}}
        with self.assertRaisesRegex(browserctl.Error, "tool result"):
            mcp.call("malformed", {})

    def test_invalid_mcp_session_is_rejected(self):
        self.stop_daemon()
        error = self.start_daemon(
            {"FAKE_SESSION": "bad session"}, expect_fail=True)
        self.assertIn("session", error)

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
                    lambda: {"content": [{"type": "text", "text": "x" * 500}]})
            journal = self.task()
        self.assertEqual(journal["state"], "RECOVERY")
        self.assertIsNone(journal["receipt"])
        self.assertIn("completed", journal["reason"])
        self.assertIn("do not replay", journal["reason"])

    def test_stale_socket_is_replaced(self):
        self.stop_daemon()
        path = self.runtime / "browserctl.sock"
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(path))
        stale.close()
        self.assertTrue(path.exists())
        browserctl.remove_stale_socket(path)
        self.assertFalse(path.exists())

    def test_result_disconnect_restart_rebind_and_no_replay(self):
        self.run_ctl("task", "begin", "lost-result")
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
        path = self.state / "browserctl/task.json"
        legacy = self.task()
        legacy.update({"phase": "ARMED", "snapshot": False})
        legacy["receipt"].update({"success": True, "post": {}})
        path.write_text(json.dumps(legacy))
        path.chmod(0o600)
        self.start_daemon()
        self.run_ctl("result")
        self.assertEqual(self.status()["state"], "READY")
        self.assertEqual(len(self.calls("browser_navigate")), 1)
        self.assertEqual(set(self.task()), browserctl.TASK_KEYS)
        self.run_ctl("call", "browser_snapshot", "{}")
        self.run_ctl("tab", "new")
        self.run_ctl("task", "end")

    def test_initial_request_ack_and_client_response_stalls_are_bounded(self):
        stream = socket.socket(socket.AF_UNIX)
        stream.connect(str(self.runtime / "browserctl.sock"))
        time.sleep(browserctl.IO_TIMEOUT + .2)
        stream.close()
        self.assertEqual(self.status()["state"], "IDLE")
        self.run_ctl("task", "begin", "ack-stall")
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
