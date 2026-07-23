#!/usr/bin/env python3
"""Isolated cross-process tests for browserctl."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).parent
CLIENT = ROOT / "client.sh"


class FakeMCP(BaseHTTPRequestHandler):
    requests = []
    deletes = []
    active = 0
    maximum_active = 0
    delete_status = 200
    mutex = threading.Lock()

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        message = json.loads(body)
        with self.mutex:
            self.requests.append(message)
        method = message["method"]
        if method == "notifications/initialized":
            payload, status = b"accepted", 202
        elif method == "initialize":
            payload, status = json.dumps({
                "jsonrpc": "2.0", "id": 1, "result": {}
            }).encode(), 200
        else:
            with self.mutex:
                type(self).active += 1
                type(self).maximum_active = max(
                    type(self).maximum_active, type(self).active)
            if method == "tools/call":
                name = message["params"]["name"]
                if name == "slow":
                    time.sleep(0.35)
                elif name == "long":
                    time.sleep(5.2)
                elif name.startswith("delete_"):
                    with self.mutex:
                        type(self).delete_status = int(name.removeprefix("delete_"))
            if method == "tools/call":
                result = {
                    "content": [{"type": "text", "text": "fake"}],
                    "isError": message["params"]["name"] == "error",
                }
            else:
                result = {"tools": []}
            payload = json.dumps({
                "jsonrpc": "2.0", "id": 2, "result": result
            }).encode()
            status = 200
            with self.mutex:
                type(self).active -= 1
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", "fake-session")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except BrokenPipeError:
            pass

    def do_DELETE(self):
        with self.mutex:
            self.deletes.append({
                "session": self.headers.get("Mcp-Session-Id"),
                "protocol": self.headers.get("Mcp-Protocol-Version"),
            })
            status = type(self).delete_status
        payload = b"" if status == 200 else b"delete refused"
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class BrowserctlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FakeMCP.requests = []
        FakeMCP.deletes = []
        FakeMCP.active = FakeMCP.maximum_active = 0
        cls.start_server()

    @classmethod
    def start_server(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMCP)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def stop_server(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    @classmethod
    def tearDownClass(cls):
        cls.stop_server()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="browserctl-test-")
        self.env = os.environ.copy()
        self.env.update({
            "BROWSERCTL_STATE_DIR": self.temporary.name,
            "BROWSERCTL_MCP_URL":
                f"http://127.0.0.1:{self.server.server_port}/mcp",
            "BROWSERCTL_HTTP_TIMEOUT": "2",
        })
        with FakeMCP.mutex:
            FakeMCP.requests.clear()
            FakeMCP.deletes.clear()
            FakeMCP.active = FakeMCP.maximum_active = 0
            FakeMCP.delete_status = 200

    def tearDown(self):
        self.temporary.cleanup()

    def run_client(self, *arguments, token=None, check=True, env_updates=None):
        env = self.env.copy()
        if token is not None:
            env["BROWSERCTL_LEASE_TOKEN"] = token
        if env_updates:
            env.update(env_updates)
        return subprocess.run(
            [CLIENT, *arguments], env=env, text=True, capture_output=True,
            check=check)

    def acquire(self, owner="test", wait="0", ttl="5"):
        return self.run_client(
            "lease", "acquire", owner, wait, ttl).stdout.strip()

    def test_acquire_use_release_across_processes(self):
        token = self.acquire()
        self.run_client("list", token=token)
        self.assertEqual(os.stat(self.temporary.name).st_mode & 0o777, 0o700)
        for path in Path(self.temporary.name).iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        status = json.loads(self.run_client("lease", "status").stdout)
        self.assertEqual(status["owner"], "test")
        self.assertNotIn("token", status)
        self.run_client("lease", "renew", "5", token=token)
        self.run_client("lease", "release", token=token)
        rejected = self.run_client("list", token=token, check=False)
        self.assertNotEqual(rejected.returncode, 0)

    def test_nonempty_notification_response_is_ignored(self):
        token = self.acquire()
        result = self.run_client("list", token=token)
        self.assertEqual(json.loads(result.stdout), {"tools": []})

    def test_long_tool_survives_heartbeat_window_and_deletes_session(self):
        token = self.acquire()
        result = self.run_client(
            "call", "long", "{}", token=token,
            env_updates={"BROWSERCTL_HTTP_TIMEOUT": "7"})
        self.assertEqual(json.loads(result.stdout)["isError"], False)
        with FakeMCP.mutex:
            self.assertEqual(FakeMCP.deletes, [{
                "session": "fake-session",
                "protocol": "2025-03-26",
            }])

    def assert_delete_error_quarantines(self, status):
        token = self.acquire()
        failed = self.run_client(
            "call", f"delete_{status}", "{}", token=token, check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn(f"DELETE HTTP {status}", failed.stderr)
        lease_status = json.loads(
            self.run_client("lease", "status").stdout)
        self.assertTrue(lease_status["in_flight"])
        blocked = self.run_client("call", "ok", "{}", token=token, check=False)
        self.assertNotEqual(blocked.returncode, 0)
        with FakeMCP.mutex:
            calls = [request for request in FakeMCP.requests
                     if request["method"] == "tools/call"]
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(FakeMCP.deletes), 1)

    def test_delete_405_quarantines(self):
        self.assert_delete_error_quarantines(405)

    def test_delete_500_quarantines(self):
        self.assert_delete_error_quarantines(500)

    def test_controller_disables_pinned_server_heartbeat(self):
        fake_bin = Path(self.temporary.name) / "bin"
        fake_bin.mkdir()
        fake_npx = fake_bin / "npx"
        fake_npx.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$PLAYWRIGHT_MCP_PING_TIMEOUT_MS\" \"$@\"\n")
        fake_npx.chmod(0o700)
        result = subprocess.run(
            [ROOT / "start-controller.sh"],
            env={**self.env, "PATH": f"{fake_bin}:{self.env['PATH']}"},
            text=True, capture_output=True, check=True)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "0")
        self.assertIn("@playwright/mcp@0.0.78", lines)

    def test_no_and_wrong_token_rejected_before_mcp(self):
        self.acquire()
        for token in (None, "wrong"):
            result = self.run_client("list", token=token, check=False)
            self.assertNotEqual(result.returncode, 0)
        with FakeMCP.mutex:
            self.assertEqual(FakeMCP.requests, [])

    def test_malformed_or_symlinked_state_fails_loudly(self):
        state = Path(self.temporary.name)
        lease = state / "lease.json"
        lease.write_text('{"owner":"wrong schema"}')
        lease.chmod(0o600)
        malformed = self.run_client("lease", "status", check=False)
        self.assertNotEqual(malformed.returncode, 0)
        lease.unlink()
        target = state / "target.json"
        target.write_text('{"sentinel":true}')
        target.chmod(0o600)
        lease.symlink_to(target)
        redirected = self.run_client("lease", "status", check=False)
        self.assertNotEqual(redirected.returncode, 0)
        self.assertEqual(target.read_text(), '{"sentinel":true}')

    def test_bounded_contention(self):
        self.acquire(owner="first")
        started = time.monotonic()
        result = self.run_client(
            "lease", "acquire", "second", "0.2", "5", check=False)
        elapsed = time.monotonic() - started
        self.assertNotEqual(result.returncode, 0)
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertLess(elapsed, 0.8)

    def test_expiry_recovers_and_rejects_old_token(self):
        old = self.acquire(owner="old", ttl="0.15")
        time.sleep(0.2)
        new = self.acquire(owner="new")
        self.assertNotEqual(old, new)
        result = self.run_client("list", token=old, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.run_client("list", token=new)

    def test_expiry_does_not_overlap_calls(self):
        old = self.acquire(owner="old", ttl="0.3")
        first = subprocess.Popen(
            [CLIENT, "call", "slow", "{}"],
            env={**self.env, "BROWSERCTL_LEASE_TOKEN": old},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with FakeMCP.mutex:
                if any(request["method"] == "tools/call"
                       for request in FakeMCP.requests):
                    break
            time.sleep(0.01)
        time.sleep(0.31)
        new = self.acquire(owner="new", wait="0.5")
        second = subprocess.Popen(
            [CLIENT, "call", "ok", "{}"],
            env={**self.env, "BROWSERCTL_LEASE_TOKEN": new},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, first_error = first.communicate(timeout=2)
        _, second_error = second.communicate(timeout=2)
        self.assertEqual(first.returncode, 0, first_error)
        self.assertEqual(second.returncode, 0, second_error)
        with FakeMCP.mutex:
            self.assertEqual(FakeMCP.maximum_active, 1)

    def test_http_timeout_quarantines_until_recovery(self):
        token = self.acquire()
        timed_out = self.run_client(
            "call", "slow", "{}", token=token, check=False,
            env_updates={"BROWSERCTL_HTTP_TIMEOUT": "0.1"})
        self.assertEqual(timed_out.returncode, 1)
        blocked = self.run_client("call", "ok", "{}", token=token, check=False)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("indeterminate in-flight", blocked.stderr)
        release = self.run_client(
            "lease", "release", token=token, check=False)
        self.assertNotEqual(release.returncode, 0)
        contender = self.run_client(
            "lease", "acquire", "contender", "0.1", "5", check=False)
        self.assertNotEqual(contender.returncode, 0)
        self.assertIn("in-flight MCP operation", contender.stderr)
        with FakeMCP.mutex:
            calls = [request for request in FakeMCP.requests
                     if request["method"] == "tools/call"]
            self.assertEqual(len(calls), 1)
            self.assertEqual(FakeMCP.maximum_active, 1)
            self.assertEqual(FakeMCP.deletes, [])
        time.sleep(0.4)
        refused = self.run_client(
            "lease", "recover", "not-restarted", token=token, check=False)
        self.assertNotEqual(refused.returncode, 0)
        old_server = self.server
        self.stop_server()
        self.start_server()
        self.env["BROWSERCTL_MCP_URL"] = (
            f"http://127.0.0.1:{self.server.server_port}/mcp")
        self.assertIsNot(self.server, old_server)
        self.run_client(
            "lease", "recover", "--controller-restarted", "5", token=token)
        self.run_client("call", "ok", "{}", token=token)

    def test_killed_client_quarantines_before_second_tool(self):
        token = self.acquire()
        first = subprocess.Popen(
            [sys.executable, ROOT / "mcp_client.py", "call", "slow", "{}"],
            env={**self.env, "BROWSERCTL_LEASE_TOKEN": token},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with FakeMCP.mutex:
                if any(request["method"] == "tools/call"
                       for request in FakeMCP.requests):
                    break
            time.sleep(0.01)
        else:
            self.fail("first tool call did not enter fake server")
        first.kill()
        first.wait(timeout=1)
        blocked = self.run_client("call", "ok", "{}", token=token, check=False)
        self.assertNotEqual(blocked.returncode, 0)
        with FakeMCP.mutex:
            calls = [request for request in FakeMCP.requests
                     if request["method"] == "tools/call"]
            self.assertEqual(len(calls), 1)
            self.assertEqual(FakeMCP.maximum_active, 1)
            self.assertEqual(FakeMCP.deletes, [])
        time.sleep(0.4)

    def test_tool_is_error_is_nonzero(self):
        token = self.acquire()
        result = self.run_client(
            "call", "error", "{}", token=token, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Playwright tool error", result.stderr)


if __name__ == "__main__":
    unittest.main()
