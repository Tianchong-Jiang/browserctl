#!/usr/bin/env python3
"""Deterministic adversarial tests; no human browser is contacted."""

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).parent
CLIENT = ROOT / "client.sh"
PYTHON_CLIENT = ROOT / "mcp_client.py"
CDP = "http://127.0.0.1:49333"


def fake_server(state_dir, port, ready_file, event_file, mode):
    sessions = {}
    active = 0
    mutex = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def event(self, kind, **values):
            with mutex:
                with open(event_file, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"kind": kind, **values}) + "\n")

        def reply(self, status, body=b"", session=None, content_type=None):
            self.send_response(status)
            if content_type:
                self.send_header("Content-Type", content_type)
            if session:
                self.send_header("Mcp-Session-Id", session)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def do_POST(self):
            nonlocal active
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                message = json.loads(body)
            except json.JSONDecodeError:
                return self.reply(400, b"bad json")
            method = message.get("method")
            params = message.get("params", {})
            name = params.get("name")
            arguments = params.get("arguments", {})
            session = self.headers.get("Mcp-Session-Id")
            self.event("post", method=method, name=name, session=session,
                       request_id=message.get("id"),
                       protocol=self.headers.get("Mcp-Protocol-Version"))
            if name == "redirect":
                self.send_response(307)
                self.send_header("Location", "http://127.0.0.1:1/mcp")
                self.end_headers()
                return
            if method == "initialize":
                session = f"session-{os.getpid()}-{len(sessions) + 1}"
                sessions[session] = {"tab": 0}
                result = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "1"},
                }
                payload = {"jsonrpc": "2.0", "id": message["id"],
                           "result": result}
                return self.reply(
                    200, json.dumps(payload).encode(), session,
                    "application/json")
            if session not in sessions:
                return self.reply(404, b"Session not found")
            if self.headers.get("Mcp-Protocol-Version") != "2025-03-26":
                return self.reply(400, b"bad protocol")
            if method == "notifications/initialized":
                return self.reply(202)

            with mutex:
                active += 1
                count = active
            self.event("start", method=method, name=name, session=session,
                       active=count)
            try:
                if name == "slow":
                    time.sleep(.5)
                elif name == "hang":
                    time.sleep(10)
                if method == "tools/list":
                    if mode == "bad_tools_schema":
                        result = {"tools": "bad"}
                    else:
                        names = [] if mode == "no_browser_tabs" else ["browser_tabs"]
                        result = {"tools": [
                            {"name": item, "inputSchema": {"type": "object"}}
                            for item in names]}
                elif name == "browser_tabs" and arguments.get("action") == "list":
                    text = "" if mode == "empty_health" else (
                        f"- 0: tab {sessions[session]['tab']}")
                    result = {"content": [{"type": "text", "text": text}]}
                elif name == "browser_tabs" and arguments.get("action") == "select":
                    sessions[session]["tab"] = arguments["index"]
                    result = {"content": [
                        {"type": "text", "text": f"selected {arguments['index']}"}]}
                elif name == "browser_snapshot":
                    result = {"content": [{"type": "text",
                                           "text": f"snapshot tab {sessions[session]['tab']}"}]}
                elif name == "malformed":
                    result = {"content": "bad"}
                elif name == "big":
                    result = {"content": [{"type": "text", "text": "x" * 200_000}]}
                elif name == "large_receipt":
                    result = {"content": [
                        {"type": "text", "text": "xxxxxxxxxx"}
                        for _ in range(271_000)]}
                elif name == "surrogate":
                    result = {"content": [{"type": "text", "text": "\ud800"}]}
                elif name == "overflow_number":
                    result = {
                        "content": [{"type": "text", "text": "ok"}],
                        "structuredContent": {"x": "__OVERFLOW__"}}
                else:
                    result = {"content": [{"type": "text", "text": "ok"}],
                              "isError": name == "tool_error"}
                if name == "rpc_error":
                    payload = {"jsonrpc": "2.0", "id": message["id"],
                               "error": {"code": -32000, "message": "fake"}}
                else:
                    payload = {"jsonrpc": "2.0",
                               "id": 999 if name == "wrong_id" else message["id"],
                               "result": result}
                if name == "replace_generation":
                    path = Path(state_dir) / "controller.json"
                    controller = json.loads(path.read_text())
                    controller["generation"] = "f" * 32
                    path.write_text(json.dumps(controller))
                    path.chmod(0o600)
                response_session = "wrong-session" if name == "wrong_session" else session
                encoded = json.dumps(payload)
                if name == "overflow_number":
                    encoded = encoded.replace('"__OVERFLOW__"', "1e999")
                self.reply(200, encoded.encode(), response_session,
                           "application/json")
            finally:
                with mutex:
                    active -= 1
                    count = active
                self.event("end", method=method, name=name, active=count)

        def do_DELETE(self):
            session = self.headers.get("Mcp-Session-Id")
            self.event("delete", session=session,
                       protocol=self.headers.get("Mcp-Protocol-Version"))
            if mode == "delete_hang":
                time.sleep(10)
            if mode == "delete_500":
                return self.reply(500, b"refused")
            if session not in sessions:
                return self.reply(404, b"Session not found")
            del sessions[session]
            self.reply(200)

    server = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_port}/mcp"
    env = os.environ.copy()
    env.update({
        "BROWSERCTL_STATE_DIR": state_dir,
        "BROWSERCTL_MCP_URL": url,
        "BROWSERCTL_CDP_ENDPOINT": CDP,
        "BROWSERCTL_CONTROLLER_HOST": "127.0.0.1",
        "BROWSERCTL_CONTROLLER_PORT": str(server.server_port),
    })
    subprocess.run(
        [PYTHON_CLIENT, "controller", "register", str(os.getpid()), url, CDP],
        env=env, check=True)
    Path(ready_file).write_text(str(server.server_port))
    server.serve_forever()


class BrowserctlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="browserctl-test-")
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir(mode=0o700)
        self.ready = Path(self.temp.name) / "ready"
        self.events_file = Path(self.temp.name) / "events"
        self.env = os.environ.copy()
        self.env.update({
            "BROWSERCTL_STATE_DIR": str(self.state),
            "BROWSERCTL_CDP_ENDPOINT": CDP,
            "BROWSERCTL_HTTP_TIMEOUT": "2",
            "BROWSERCTL_LOCK_TIMEOUT": "2",
        })
        self.server = None
        self.port = 0
        self.start_server()

    def tearDown(self):
        self.stop_server()
        self.temp.cleanup()

    def start_server(self, mode="healthy"):
        self.ready.unlink(missing_ok=True)
        self.server = subprocess.Popen(
            [sys.executable, __file__, "--fake-server", str(self.state),
             str(self.port), str(self.ready), str(self.events_file), mode],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.ready.exists():
                try:
                    self.port = int(self.ready.read_text())
                except ValueError:
                    pass
                else:
                    self.env["BROWSERCTL_MCP_URL"] = (
                        f"http://127.0.0.1:{self.port}/mcp")
                    return
            if self.server.poll() is not None:
                _, error = self.server.communicate()
                self.fail(f"fake server failed: {error}")
            time.sleep(.01)
        self.fail("fake server did not start")

    def stop_server(self):
        if not self.server:
            return
        if self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(2)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(2)
        self.server.stdout.close()
        self.server.stderr.close()
        self.server = None

    def restart_server(self, mode="healthy"):
        self.stop_server()
        self.start_server(mode)

    def run_client(self, *args, check=True, env=None, **kwargs):
        merged = self.env.copy()
        if env:
            merged.update(env)
        return subprocess.run(
            [CLIENT, *args], env=merged, text=True, capture_output=True,
            check=check, **kwargs)

    def acquire(self, owner="test"):
        return json.loads(self.run_client("lease", "acquire", owner).stdout)

    def status(self):
        return json.loads(self.run_client("lease", "status").stdout)

    def events(self, kind=None):
        if not self.events_file.exists():
            return []
        values = [json.loads(line) for line in self.events_file.read_text().splitlines()]
        return [item for item in values if item["kind"] == kind] if kind else values

    def wait_event(self, name, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(item["kind"] == "start" and item.get("name") == name
                   for item in self.events()):
                return
            time.sleep(.01)
        self.fail(f"no event for {name}")

    def kill_in_flight(self, name="hang"):
        self.acquire()
        process = subprocess.Popen(
            [sys.executable, PYTHON_CLIENT, "call", name, "{}"], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.wait_event(name)
        process.kill()
        process.wait(2)
        process.stdout.close()
        process.stderr.close()
        return process

    def test_persistent_session_cross_process_and_release(self):
        acquired = self.acquire()
        session = acquired["session_id"]
        self.run_client("call", "browser_tabs", '{"action":"select","index":7}')
        snapshot = json.loads(self.run_client(
            "call", "browser_snapshot", "{}").stdout)
        self.assertEqual(snapshot["content"][0]["text"], "snapshot tab 7")
        posts = self.events("post")
        self.assertEqual(sum(item["method"] == "initialize" for item in posts), 1)
        requests = [item for item in posts if item["request_id"] is not None]
        self.assertEqual([item["request_id"] for item in requests],
                         [1, 2, 3, 4, 5])
        self.assertEqual({item["session"] for item in posts
                          if item["method"] == "tools/call"}, {session})
        self.assertTrue(all(
            item["protocol"] == "2025-03-26" for item in posts
            if item["method"] != "initialize"))
        self.run_client("lease", "release")
        deletes = self.events("delete")
        self.assertEqual(deletes[-1]["session"], session)
        self.assertEqual(deletes[-1]["protocol"], "2025-03-26")
        request = urllib.request.Request(
            self.env["BROWSERCTL_MCP_URL"],
            json.dumps({"jsonrpc": "2.0", "id": 99,
                        "method": "tools/list", "params": {}}).encode(),
            {"Content-Type": "application/json", "Mcp-Session-Id": session,
             "Mcp-Protocol-Version": "2025-03-26"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 404)

    def test_no_expiry_or_renew_interface(self):
        self.acquire()
        time.sleep(.2)
        self.assertEqual(self.status()["state"], "ready")
        blocked = self.run_client("lease", "acquire", "other", "0.1", check=False)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("held", blocked.stderr)
        renew = self.run_client("lease", "renew", check=False)
        self.assertNotEqual(renew.returncode, 0)

    def test_execution_exclusion_and_live_status(self):
        self.acquire()
        first = subprocess.Popen(
            [CLIENT, "call", "slow", "{}"], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.wait_event("slow")
        status = self.status()
        self.assertEqual(status["state"], "in_progress")
        self.assertTrue(status["client_alive"])
        started = time.monotonic()
        blocked = self.run_client(
            "lease", "acquire", "other", "0", check=False)
        self.assertLess(time.monotonic() - started, .2)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("held", blocked.stderr)
        second = subprocess.Popen(
            [CLIENT, "call", "browser_snapshot", "{}"], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        first.communicate(timeout=2)
        second.communicate(timeout=2)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertTrue(all(item["active"] == 1 for item in self.events("start")))

    def test_killed_client_is_recovery_required(self):
        self.kill_in_flight()
        status = self.status()
        self.assertEqual(status["phase"], "in_progress")
        self.assertEqual(status["state"], "recovery_required")
        self.assertFalse(status["client_alive"])
        self.assertNotEqual(
            self.run_client("lease", "release", check=False).returncode, 0)

    def test_wrong_id_session_schema_and_controller_replacement_quarantine(self):
        for tool, text in (
            ("wrong_id", "mismatched"),
            ("wrong_session", "exact session"),
            ("malformed", "malformed tools/call result"),
            ("replace_generation", "generation changed"),
        ):
            with self.subTest(tool=tool):
                if (self.state / "lease.json").exists():
                    self.stop_server()
                    (self.state / "lease.json").unlink()
                    (self.state / "controller.json").unlink(missing_ok=True)
                    self.port = 0
                    self.start_server()
                self.acquire()
                failed = self.run_client("call", tool, "{}", check=False)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn(text, failed.stderr)
                self.assertEqual(self.status()["state"], "recovery_required")

    def test_nonfinite_response_requires_recovery(self):
        self.acquire()
        failed = self.run_client(
            "call", "overflow_number", "{}", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed.stdout, "")
        self.assertIn("non-finite JSON numbers", failed.stderr)
        self.assertNotIn("Infinity", failed.stdout + failed.stderr)
        self.assertEqual(self.status()["state"], "recovery_required")

    def test_health_schema_empty_and_missing_tool_rejected(self):
        for mode, text in (
            ("bad_tools_schema", "malformed acquire tools/list"),
            ("no_browser_tabs", "omitted browser_tabs"),
            ("empty_health", "empty health result"),
        ):
            with self.subTest(mode=mode):
                self.stop_server()
                (self.state / "controller.json").unlink(missing_ok=True)
                (self.state / "lease.json").unlink(missing_ok=True)
                self.port = 0
                self.start_server(mode)
                failed = self.run_client("lease", "acquire", "test", check=False)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn(text, failed.stderr)
                self.assertEqual(self.status()["state"], "recovery_required")

    def test_redirects_and_ambient_proxies(self):
        self.acquire()
        self.run_client("list", env={"HTTP_PROXY": "http://127.0.0.1:1",
                              "HTTPS_PROXY": "http://127.0.0.1:1",
                              "ALL_PROXY": "http://127.0.0.1:1",
                              "NO_PROXY": ""})
        failed = self.run_client("call", "redirect", "{}", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("redirect", failed.stderr)
        self.assertEqual(self.status()["state"], "recovery_required")

    def test_wrong_cdp_and_listener_rejected(self):
        self.acquire()
        mismatches = (
            {"BROWSERCTL_CDP_ENDPOINT": "http://127.0.0.1:49334"},
            {"BROWSERCTL_MCP_URL": "http://127.0.0.1:49334/mcp"},
        )
        for updates in mismatches:
            with self.subTest(updates=updates):
                starts = len(self.events("start"))
                deletes = len(self.events("delete"))
                failed = self.run_client("list", check=False, env=updates)
                self.assertNotEqual(failed.returncode, 0)
                self.assertEqual(len(self.events("start")), starts)
                self.assertEqual(len(self.events("delete")), deletes)
                bad_status = self.run_client(
                    "lease", "status", check=False, env=updates)
                self.assertNotEqual(bad_status.returncode, 0)
                self.assertEqual(self.status()["state"], "ready")
                self.run_client("list")

        starts = len(self.events("start"))
        deletes = len(self.events("delete"))
        wrong_release = self.run_client(
            "lease", "release", check=False, env=mismatches[0])
        self.assertNotEqual(wrong_release.returncode, 0)
        self.assertEqual(len(self.events("start")), starts)
        self.assertEqual(len(self.events("delete")), deletes)
        self.assertEqual(self.status()["state"], "ready")

        self.stop_server()
        (self.state / "lease.json").unlink()
        self.port = 0
        self.start_server()
        self.acquire()
        sleeper = subprocess.Popen(["sleep", "5"])
        try:
            path = self.state / "controller.json"
            controller = json.loads(path.read_text())
            controller["pid"] = sleeper.pid
            fields = Path(f"/proc/{sleeper.pid}/stat").read_text().rsplit(
                ")", 1)[1].strip().split()
            controller["start"] = fields[19]
            path.write_text(json.dumps(controller))
            path.chmod(0o600)
            failed = self.run_client("list", check=False)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("exact MCP listener", failed.stderr)
        finally:
            sleeper.terminate()
            sleeper.wait(2)

    def test_delete_failure_and_timeout_quarantine(self):
        for mode, env, text in (
            ("delete_500", {}, "DELETE HTTP 500"),
            ("delete_hang", {"BROWSERCTL_HTTP_TIMEOUT": ".1"},
             "DELETE failed"),
        ):
            with self.subTest(mode=mode):
                self.stop_server()
                (self.state / "controller.json").unlink(missing_ok=True)
                (self.state / "lease.json").unlink(missing_ok=True)
                self.port = 0
                self.start_server(mode)
                self.acquire()
                failed = self.run_client("lease", "release", check=False, env=env)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn(text, failed.stderr)
                self.assertEqual(self.status()["state"], "recovery_required")

    def test_recovery_requires_changed_generation_and_retains_health_session(self):
        self.kill_in_flight()
        unchanged = self.run_client("lease", "recover", check=False)
        self.assertNotEqual(unchanged.returncode, 0)
        self.assertIn("unchanged", unchanged.stderr)
        self.restart_server()
        before = len([event for event in self.events("post")
                      if event["method"] == "initialize"])
        recovered = json.loads(self.run_client("lease", "recover").stdout)
        self.assertTrue(recovered["session_state_lost"])
        self.assertIn("do not replay", recovered["next_action"])
        self.run_client("call", "browser_snapshot", "{}")
        after = len([event for event in self.events("post")
                     if event["method"] == "initialize"])
        self.assertEqual(after, before + 1)
        calls = [item for item in self.events("post")
                 if item.get("name") == "browser_snapshot"]
        self.assertEqual(calls[-1]["session"], recovered["session_id"])

    def test_recovery_rejects_changed_cdp(self):
        self.kill_in_flight()
        self.restart_server()
        path = self.state / "controller.json"
        controller = json.loads(path.read_text())
        controller["cdp_endpoint"] = "http://127.0.0.1:49334"
        path.write_text(json.dumps(controller))
        path.chmod(0o600)
        failed = self.run_client(
            "lease", "recover", check=False,
            env={"BROWSERCTL_CDP_ENDPOINT": "http://127.0.0.1:49334"})
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("caller CDP endpoint", failed.stderr)

    def test_pending_result_survives_closed_pipe_and_is_not_replayed(self):
        self.acquire()
        process = subprocess.Popen(
            [CLIENT, "call", "big", "{}"], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.stdout.close()
        process.wait(3)
        process.stderr.close()
        self.assertNotEqual(process.returncode, 0)
        status = self.status()
        self.assertEqual(status["state"], "result_pending")
        self.assertEqual(status["result"]["request_id"], 4)
        self.assertEqual(status["result"]["method"], "tools/call big")
        self.assertEqual(len([event for event in self.events("start")
                              if event.get("name") == "big"]), 1)
        recovered = self.run_client("lease", "result")
        self.assertIn("x" * 100, recovered.stdout)
        self.assertEqual(self.status()["state"], "ready")
        self.assertEqual(len([event for event in self.events("start")
                              if event.get("name") == "big"]), 1)

    def test_kill_after_response_before_receipt_ack(self):
        self.acquire()
        process = subprocess.Popen(
            [CLIENT, "call", "big", "{}"], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.status()["state"] == "result_pending":
                break
            time.sleep(.01)
        else:
            self.fail("result receipt was not persisted")
        process.kill()
        process.wait(2)
        process.stdout.close()
        process.stderr.close()
        status = self.status()
        self.assertEqual(status["state"], "result_pending")
        self.assertEqual(status["result"]["request_id"], 4)
        self.assertEqual(status["result"]["method"], "tools/call big")
        self.run_client("lease", "result")
        self.assertEqual(self.status()["state"], "ready")
        self.assertEqual(len([event for event in self.events("start")
                              if event.get("name") == "big"]), 1)

    def test_normal_delivery_clears_receipt_and_errors_are_determinate(self):
        self.acquire()
        self.run_client("call", "browser_snapshot", "{}")
        self.assertEqual(self.status()["state"], "ready")
        failed = self.run_client("call", "tool_error", "{}", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.status()["state"], "ready")
        rpc = self.run_client("call", "rpc_error", "{}", check=False)
        self.assertNotEqual(rpc.returncode, 0)
        self.assertEqual(self.status()["state"], "ready")

    def test_malformed_symlink_and_modes_fail_loudly(self):
        lease = self.state / "lease.json"
        lease.write_text("{}")
        lease.chmod(0o600)
        self.assertNotEqual(
            self.run_client("lease", "status", check=False).returncode, 0)
        lease.unlink()
        target = Path(self.temp.name) / "target"
        target.write_text('{"sentinel":true}')
        target.chmod(0o600)
        lease.symlink_to(target)
        self.assertNotEqual(
            self.run_client("lease", "status", check=False).returncode, 0)
        self.assertEqual(target.read_text(), '{"sentinel":true}')
        lease.unlink()
        self.state.chmod(0o755)
        self.assertNotEqual(
            self.run_client("lease", "status", check=False).returncode, 0)

    def test_new_state_directory_parent_entries_are_synced(self):
        target = Path(self.temp.name) / "durable" / "nested" / "browserctl"
        script = (
            "import json, os\n"
            "import mcp_client as m\n"
            "seen=[]\n"
            "real=m.os.fsync\n"
            "def observed(fd):\n"
            " seen.append(os.readlink(f'/proc/self/fd/{fd}'))\n"
            " real(fd)\n"
            "m.os.fsync=observed\n"
            "m.state_dir()\n"
            "print(json.dumps(seen))\n"
        )
        env = self.env.copy()
        env["BROWSERCTL_STATE_DIR"] = str(target)
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, env=env,
            text=True, capture_output=True, check=True)
        synced = json.loads(result.stdout)
        base = str(Path(self.temp.name))
        self.assertEqual(
            synced[:4],
            [base, str(target.parent.parent), str(target.parent), str(target)])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_invalid_environment_fails_before_http(self):
        for updates in (
            {"BROWSERCTL_CDP_ENDPOINT": ""},
            {"BROWSERCTL_CDP_ENDPOINT": "http://localhost:9222"},
            {"BROWSERCTL_CDP_ENDPOINT": "http://127.0.0.1:9222/"},
            {"BROWSERCTL_MCP_URL": "https://127.0.0.1:1/mcp"},
            {"BROWSERCTL_HTTP_TIMEOUT": "nan"},
            {"BROWSERCTL_TIMEOUT": "0"},
            {"BROWSERCTL_TIMEOUT": "inf"},
            {"BROWSERCTL_TIMEOUT": "3601"},
            {"BROWSERCTL_TIMEOUT": "2", "BROWSERCTL_HTTP_TIMEOUT": "2"},
        ):
            with self.subTest(updates=updates):
                failed = self.run_client(
                    "lease", "acquire", "test", check=False, env=updates)
                self.assertNotEqual(failed.returncode, 0)

    def test_invalid_and_oversized_requests_fail_before_dispatch(self):
        self.acquire()
        for arguments in ('{"value":NaN}', '{"value":Infinity}',
                          '{"value":1e999}'):
            with self.subTest(arguments=arguments):
                starts = len(self.events("start"))
                failed = self.run_client(
                    "call", "ok", arguments, check=False)
                self.assertNotEqual(failed.returncode, 0)
                self.assertEqual(len(self.events("start")), starts)
                self.assertEqual(self.status()["state"], "ready")
        script = (
            "import mcp_client as m\n"
            "m.MAX_BODY=200\n"
            "try: m.call('oversized', {'value':'x'*1000})\n"
            "except m.Error as e: print(e)\n"
        )
        starts = len(self.events("start"))
        oversized = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, env=self.env,
            text=True, capture_output=True)
        self.assertEqual(oversized.returncode, 0)
        self.assertIn("request is too large", oversized.stdout)
        self.assertEqual(len(self.events("start")), starts)
        self.assertEqual(self.status()["state"], "ready")

    def test_unretainable_completed_results_are_determinate(self):
        self.acquire()
        for tool in ("large_receipt", "surrogate"):
            with self.subTest(tool=tool):
                starts = len(self.events("start"))
                failed = self.run_client("call", tool, "{}", check=False)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn("operation completed", failed.stderr)
                self.assertIn("do not replay", failed.stderr)
                self.assertEqual(len(self.events("start")), starts + 1)
                self.assertEqual(self.status()["state"], "ready")

    def test_launcher_requires_cdp_pins_package_and_disables_heartbeat(self):
        self.stop_server()
        fake_bin = Path(self.temp.name) / "bin"
        fake_bin.mkdir()
        npx = fake_bin / "npx"
        npx.write_text("#!/bin/sh\nprintf '%s\\n' \"$PLAYWRIGHT_MCP_PING_TIMEOUT_MS\" \"$@\"\n")
        npx.chmod(0o700)
        (self.state / "controller.json").unlink(missing_ok=True)
        missing = subprocess.run(
            [ROOT / "start-controller.sh"],
            env={**self.env, "PATH": f"{fake_bin}:{self.env['PATH']}",
                 "BROWSERCTL_CDP_ENDPOINT": ""},
            text=True, capture_output=True)
        self.assertNotEqual(missing.returncode, 0)
        result = subprocess.run(
            [ROOT / "start-controller.sh"],
            env={**self.env, "PATH": f"{fake_bin}:{self.env['PATH']}",
                 "BROWSERCTL_HOST": "127.0.0.1", "BROWSERCTL_PORT": "47883",
                 "BROWSERCTL_MCP_URL": "http://127.0.0.1:47883/mcp"},
            text=True, capture_output=True, check=True)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "0")
        self.assertIn("@playwright/mcp@0.0.78", lines)
        self.assertEqual(lines[lines.index("--allowed-hosts") + 1],
                         "127.0.0.1:47883")
        controller = json.loads(
            (self.state / "controller.json").read_text())
        self.assertEqual(controller["cdp_endpoint"], CDP)
        self.assertIn("boot_id", controller)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fake-server"]:
        fake_server(*sys.argv[2:])
    else:
        unittest.main(verbosity=2)
