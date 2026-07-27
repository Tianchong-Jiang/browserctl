#!/usr/bin/env python3
"""Isolated cross-process tests for browserctl."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).parent
CLIENT = ROOT / "client.sh"
PYTHON_CLIENT = ROOT / "mcp_client.py"


def run_fake_server(state_dir, port_file, event_file, mode="healthy"):
    class FakeMCP(BaseHTTPRequestHandler):
        delete_status = 200
        active = 0
        mutex = threading.Lock()

        def log_message(self, *_):
            pass

        def event(self, kind, **values):
            entry = {"kind": kind, "at": time.monotonic(), **values}
            with self.mutex:
                with open(event_file, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(entry) + "\n")

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            message = json.loads(body)
            method = message["method"]
            name = message.get("params", {}).get("name")
            self.event("post", method=method, name=name)
            if method == "notifications/initialized":
                payload, status = b"accepted", 202
            elif method == "initialize":
                payload, status = json.dumps({
                    "jsonrpc": "2.0", "id": message["id"], "result": {}
                }).encode(), 200
            else:
                with self.mutex:
                    type(self).active += 1
                    active = type(self).active
                self.event("operation_start", method=method, name=name, active=active)
                if method == "tools/call":
                    if name == "slow":
                        time.sleep(0.35)
                    elif name == "long":
                        time.sleep(5.2)
                    elif name and name.startswith("delete_"):
                        type(self).delete_status = int(name.removeprefix("delete_"))
                if method == "tools/call" and name == "malformed_result":
                    result = {"content": "not-a-list"}
                elif method == "tools/call" and name == "missing_text":
                    result = {"content": [{"type": "text"}]}
                elif method == "tools/call" and name == "unknown_content_type":
                    result = {"content": [{"type": "bogus"}]}
                elif method == "tools/call":
                    result = {
                        "content": [{"type": "text", "text": "fake"}],
                        "isError": (
                            name == "error"
                            or (mode == "bad_browser" and name == "browser_tabs")
                        ),
                    }
                else:
                    result = (
                        {"not_tools": []}
                        if mode == "bad_health"
                        else {"tools": []}
                    )
                payload = json.dumps({
                    "jsonrpc": "2.0",
                    "id": 999 if name == "wrong_id" else message["id"],
                    "result": result,
                }).encode()
                status = 200
                with self.mutex:
                    type(self).active -= 1
                    active = type(self).active
                self.event("operation_end", method=method, name=name, active=active)
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
            self.event(
                "delete",
                session=self.headers.get("Mcp-Session-Id"),
                protocol=self.headers.get("Mcp-Protocol-Version"),
            )
            status = type(self).delete_status
            payload = b"" if status == 200 else b"delete refused"
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except BrokenPipeError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMCP)
    server.daemon_threads = True
    FakeMCP.delete_status = 500 if mode == "bad_delete" else 200
    url = f"http://127.0.0.1:{server.server_port}/mcp"
    env = os.environ.copy()
    env.update({
        "BROWSERCTL_STATE_DIR": state_dir,
        "BROWSERCTL_MCP_URL": url,
        "BROWSERCTL_HTTP_TIMEOUT": "2",
        "BROWSERCTL_CONTROLLER_HOST": "127.0.0.1",
        "BROWSERCTL_CONTROLLER_PORT": str(server.server_port),
    })
    subprocess.run(
        [PYTHON_CLIENT, "controller", "register", str(os.getpid()), url],
        env=env, check=True)
    Path(port_file).write_text(str(server.server_port), encoding="ascii")
    server.serve_forever()


class BrowserctlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="browserctl-test-")
        self.state = Path(self.temporary.name) / "state"
        self.state.mkdir()
        self.port_file = Path(self.temporary.name) / "port"
        self.event_file = Path(self.temporary.name) / "events"
        self.env = os.environ.copy()
        self.env.update({
            "BROWSERCTL_STATE_DIR": str(self.state),
            "BROWSERCTL_HTTP_TIMEOUT": "2",
        })
        self.server = None
        self.start_server()

    def tearDown(self):
        self.stop_server()
        self.temporary.cleanup()

    def start_server(self, mode="healthy"):
        self.port_file.unlink(missing_ok=True)
        self.server = subprocess.Popen(
            [
                sys.executable, __file__, "--fake-server",
                str(self.state), str(self.port_file), str(self.event_file), mode,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.port_file.exists():
                try:
                    port = int(self.port_file.read_text(encoding="ascii"))
                except ValueError:
                    pass
                else:
                    self.env["BROWSERCTL_MCP_URL"] = (
                        f"http://127.0.0.1:{port}/mcp")
                    return
            if self.server.poll() is not None:
                _, error = self.server.communicate()
                self.fail(f"fake controller failed to start: {error}")
            time.sleep(0.01)
        self.fail("fake controller did not publish its port")

    def stop_server(self):
        if self.server is None:
            return
        if self.server.poll() is None:
            self.server.send_signal(signal.SIGTERM)
            try:
                self.server.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(timeout=2)
        self.server.stdout.close()
        self.server.stderr.close()
        self.server = None

    def restart_server(self, mode="healthy"):
        self.stop_server()
        self.start_server(mode)

    def events(self):
        if not self.event_file.exists():
            return []
        return [
            json.loads(line)
            for line in self.event_file.read_text(encoding="utf-8").splitlines()
        ]

    def run_client(self, *arguments, check=True, env_updates=None):
        env = self.env.copy()
        if env_updates:
            env.update(env_updates)
        return subprocess.run(
            [CLIENT, *arguments], env=env, text=True, capture_output=True,
            check=check)

    def acquire(self, owner="test", wait="0", ttl="5"):
        return json.loads(self.run_client(
            "lease", "acquire", owner, wait, ttl).stdout)

    def wait_for_tool(self, name, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(
                event["kind"] == "operation_start" and event.get("name") == name
                for event in self.events()
            ):
                return
            time.sleep(0.01)
        self.fail(f"tool {name!r} did not enter fake controller")

    def quarantine_with_timeout(self, ttl="5"):
        self.acquire(ttl=ttl)
        result = self.run_client(
            "call", "slow", "{}", check=False,
            env_updates={"BROWSERCTL_HTTP_TIMEOUT": "0.05"})
        self.assertNotEqual(result.returncode, 0)
        return result

    def test_acquire_use_renew_release_across_processes(self):
        acquired = self.acquire()
        self.assertEqual(acquired["owner"], "test")
        self.assertTrue(acquired["acquired"])
        self.assertNotIn("token", acquired)
        self.run_client("list")
        self.run_client("lease", "renew", "5")
        status = json.loads(self.run_client("lease", "status").stdout)
        self.assertEqual(status["state"], "held")
        self.assertFalse(status["expired"])
        self.assertFalse(status["in_flight"])
        self.assertFalse(status["recovery_required"])
        self.run_client("lease", "release")
        rejected = self.run_client("list", check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)
        for path in self.state.iterdir():
            self.assertEqual(path.lstat().st_mode & 0o777, 0o600)

    def test_token_environment_is_unneeded_and_ignored(self):
        self.acquire()
        self.run_client(
            "list", env_updates={"BROWSERCTL_LEASE_TOKEN": "wrong-old-token"})
        self.run_client(
            "lease", "renew",
            env_updates={"BROWSERCTL_LEASE_TOKEN": "another-value"})
        self.run_client(
            "lease", "release",
            env_updates={"BROWSERCTL_LEASE_TOKEN": "ignored"})

    def test_loopback_http_ignores_ambient_proxy(self):
        self.acquire()
        self.run_client("list", env_updates={
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "",
            "no_proxy": "",
        })

    def test_commands_require_active_singleton_lease_before_mcp(self):
        before = len(self.events())
        result = self.run_client("list", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no active singleton lease", result.stderr)
        self.assertEqual(len(self.events()), before)

    def test_nonempty_notification_response_is_ignored(self):
        self.acquire()
        result = self.run_client("list")
        self.assertEqual(json.loads(result.stdout), {"tools": []})

    def test_long_tool_survives_heartbeat_window_and_deletes_session(self):
        self.acquire(ttl="8")
        result = self.run_client(
            "call", "long", "{}",
            env_updates={"BROWSERCTL_HTTP_TIMEOUT": "7", "BROWSERCTL_TIMEOUT": "8"})
        self.assertFalse(json.loads(result.stdout)["isError"])
        deletes = [event for event in self.events() if event["kind"] == "delete"]
        self.assertEqual(deletes[-1]["session"], "fake-session")
        self.assertEqual(deletes[-1]["protocol"], "2025-03-26")

    def test_controller_launcher_pins_server_and_registers_generation(self):
        self.stop_server()
        (self.state / "controller.json").unlink()
        fake_bin = Path(self.temporary.name) / "bin"
        fake_bin.mkdir()
        fake_npx = fake_bin / "npx"
        fake_npx.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$PLAYWRIGHT_MCP_PING_TIMEOUT_MS\" \"$@\"\n")
        fake_npx.chmod(0o700)
        result = subprocess.run(
            [ROOT / "start-controller.sh"],
            env={
                **self.env,
                "PATH": f"{fake_bin}:{self.env['PATH']}",
                "BROWSERCTL_PORT": "47883",
                "BROWSERCTL_MCP_URL": "http://127.0.0.1:47883/mcp",
            },
            text=True, capture_output=True, check=True)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "0")
        self.assertIn("@playwright/mcp@0.0.78", lines)
        controller = json.loads(
            (self.state / "controller.json").read_text(encoding="utf-8"))
        self.assertEqual(controller["schema_version"], 1)
        self.assertEqual(len(controller["generation"]), 32)

    def assert_delete_error_quarantines(self, status):
        self.acquire()
        calls_before = len([
            event for event in self.events()
            if event["kind"] == "operation_start"
            and event.get("method") == "tools/call"
        ])
        failed = self.run_client(
            "call", f"delete_{status}", "{}", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn(f"DELETE HTTP {status}", failed.stderr)
        lease_status = json.loads(self.run_client("lease", "status").stdout)
        self.assertEqual(lease_status["state"], "recovery_required")
        self.assertTrue(lease_status["in_flight"])
        self.assertNotIn("expires_in", lease_status)
        blocked = self.run_client("call", "ok", "{}", check=False)
        self.assertNotEqual(blocked.returncode, 0)
        calls = [
            event for event in self.events()
            if event["kind"] == "operation_start" and event.get("method") == "tools/call"
        ]
        self.assertEqual(len(calls), calls_before + 1)

    def test_delete_405_and_500_quarantine(self):
        self.assert_delete_error_quarantines(405)
        self.restart_server()
        self.run_client("lease", "recover")
        self.run_client("lease", "release")
        self.assert_delete_error_quarantines(500)

    def assert_malformed_response_quarantines(self, tool, error_text):
        self.acquire()
        deletes_before = len([
            event for event in self.events() if event["kind"] == "delete"
        ])
        failed = self.run_client("call", tool, "{}", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn(error_text, failed.stderr)
        status = json.loads(self.run_client("lease", "status").stdout)
        self.assertEqual(status["state"], "recovery_required")
        deletes_after = len([
            event for event in self.events() if event["kind"] == "delete"
        ])
        self.assertEqual(deletes_after, deletes_before)

    def test_malformed_tools_call_result_quarantines(self):
        self.assert_malformed_response_quarantines(
            "malformed_result", "malformed tools/call result")

    def test_missing_text_content_quarantines(self):
        self.assert_malformed_response_quarantines(
            "missing_text", "malformed tools/call result")

    def test_unknown_content_type_quarantines(self):
        self.assert_malformed_response_quarantines(
            "unknown_content_type", "malformed tools/call result")

    def test_wrong_response_id_quarantines(self):
        self.assert_malformed_response_quarantines(
            "wrong_id", "malformed or mismatched tools/call JSON-RPC response")

    def test_bounded_contention(self):
        self.acquire(owner="first")
        started = time.monotonic()
        result = self.run_client(
            "lease", "acquire", "second", "0.2", "5", check=False)
        elapsed = time.monotonic() - started
        self.assertNotEqual(result.returncode, 0)
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertLess(elapsed, 0.9)
        self.assertIn("'first'", result.stderr)

    def test_idle_expiry_auto_reclaims(self):
        self.acquire(owner="old", ttl="0.1")
        time.sleep(0.15)
        status = json.loads(self.run_client("lease", "status").stdout)
        self.assertEqual(status["state"], "available")
        self.assertTrue(status["reclaimed_expired_lease"])
        self.assertFalse(status["expired"])
        acquired = self.acquire(owner="new")
        self.assertEqual(acquired["owner"], "new")

    def test_expired_in_flight_never_auto_expires(self):
        self.quarantine_with_timeout(ttl="0.1")
        time.sleep(0.15)
        status = json.loads(self.run_client("lease", "status").stdout)
        self.assertEqual(status["state"], "recovery_required")
        self.assertTrue(status["expired"])
        self.assertTrue(status["in_flight"])
        self.assertTrue(status["recovery_required"])
        self.assertNotIn("expires_in", status)
        contender = self.run_client(
            "lease", "acquire", "new", "0.1", "5", check=False)
        self.assertNotEqual(contender.returncode, 0)
        self.assertIn("requires recovery", contender.stderr)

    def test_http_and_outer_timeouts_quarantine(self):
        self.quarantine_with_timeout()
        blocked = self.run_client("call", "ok", "{}", check=False)
        self.assertIn("indeterminate in-flight", blocked.stderr)
        self.restart_server()
        self.run_client("lease", "recover")
        outer = self.run_client(
            "call", "slow", "{}", check=False,
            env_updates={
                "BROWSERCTL_HTTP_TIMEOUT": "2",
                "BROWSERCTL_TIMEOUT": "0.25",
            })
        self.assertNotEqual(outer.returncode, 0)
        status = json.loads(self.run_client("lease", "status").stdout)
        self.assertTrue(status["recovery_required"])

    def test_killed_client_quarantines_before_second_tool(self):
        self.acquire()
        first = subprocess.Popen(
            [sys.executable, PYTHON_CLIENT, "call", "slow", "{}"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait_for_tool("slow")
        first.kill()
        first.wait(timeout=1)
        blocked = self.run_client("call", "ok", "{}", check=False)
        self.assertNotEqual(blocked.returncode, 0)
        calls = [
            event for event in self.events()
            if event["kind"] == "operation_start"
            and event.get("method") == "tools/call"
        ]
        self.assertEqual([call["name"] for call in calls], ["slow"])

    def test_release_and_renew_reject_in_flight(self):
        self.quarantine_with_timeout()
        for arguments in (("lease", "release"), ("lease", "renew")):
            result = self.run_client(*arguments, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("indeterminate in-flight", result.stderr)

    def test_recovery_requires_changed_healthy_generation(self):
        self.quarantine_with_timeout()
        unchanged = self.run_client("lease", "recover", check=False)
        self.assertNotEqual(unchanged.returncode, 0)
        self.assertIn("generation is unchanged", unchanged.stderr)
        self.restart_server(mode="bad_health")
        unhealthy = self.run_client("lease", "recover", check=False)
        self.assertNotEqual(unhealthy.returncode, 0)
        self.assertIn("malformed controller health tools/list", unhealthy.stderr)
        status = json.loads(self.run_client("lease", "status").stdout)
        self.assertTrue(status["recovery_required"])
        self.restart_server()
        recovered = json.loads(self.run_client(
            "lease", "recover", "5").stdout)
        self.assertTrue(recovered["recovered"])
        self.run_client("call", "ok", "{}")

    def test_recovery_requires_working_browser_probe(self):
        self.quarantine_with_timeout()
        self.restart_server(mode="bad_browser")
        failed = self.run_client("lease", "recover", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("browser health probe failed", failed.stderr)
        status = json.loads(self.run_client("lease", "status").stdout)
        self.assertEqual(status["state"], "recovery_required")

    def test_recovery_requires_successful_health_session_delete(self):
        self.quarantine_with_timeout()
        self.restart_server(mode="bad_delete")
        failed = self.run_client("lease", "recover", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("DELETE HTTP 500", failed.stderr)
        status = json.loads(self.run_client("lease", "status").stdout)
        self.assertEqual(status["state"], "recovery_required")

    def test_recovery_rejects_controller_url_mismatch(self):
        self.quarantine_with_timeout()
        self.restart_server()
        controller_path = self.state / "controller.json"
        controller = json.loads(controller_path.read_text(encoding="utf-8"))
        controller["mcp_url"] = "http://127.0.0.1:1/mcp"
        controller_path.write_text(json.dumps(controller), encoding="utf-8")
        failed = self.run_client("lease", "recover", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("different MCP URL", failed.stderr)

    def test_recovery_rejects_listener_on_wrong_loopback_family(self):
        self.quarantine_with_timeout()
        self.restart_server()
        controller_path = self.state / "controller.json"
        controller = json.loads(controller_path.read_text(encoding="utf-8"))
        port = urllib.parse.urlsplit(self.env["BROWSERCTL_MCP_URL"]).port
        ipv6_url = f"http://[::1]:{port}/mcp"
        controller["mcp_url"] = ipv6_url
        controller_path.write_text(json.dumps(controller), encoding="utf-8")
        failed = self.run_client(
            "lease", "recover", check=False,
            env_updates={"BROWSERCTL_MCP_URL": ipv6_url})
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("exact MCP listener address and port", failed.stderr)

    def test_recovery_rejects_missing_dead_or_wrong_generation_state(self):
        self.quarantine_with_timeout()
        self.stop_server()
        missing = self.state / "controller.json"
        missing.unlink()
        result = self.run_client("lease", "recover", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("controller.json is missing", result.stderr)
        self.start_server()
        controller = json.loads(missing.read_text(encoding="utf-8"))
        controller["process_start_time"] = "0"
        missing.write_text(json.dumps(controller), encoding="utf-8")
        missing.chmod(0o600)
        dead = self.run_client("lease", "recover", check=False)
        self.assertNotEqual(dead.returncode, 0)
        self.assertIn("missing or was replaced", dead.stderr)

    def test_execution_lock_serializes_racing_calls(self):
        self.acquire()
        first = subprocess.Popen(
            [CLIENT, "call", "slow", "{}"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.wait_for_tool("slow")
        second = subprocess.Popen(
            [CLIENT, "call", "ok", "{}"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _, first_error = first.communicate(timeout=2)
        _, second_error = second.communicate(timeout=2)
        self.assertEqual(first.returncode, 0, first_error)
        self.assertEqual(second.returncode, 0, second_error)
        starts = [
            event for event in self.events()
            if event["kind"] == "operation_start"
            and event.get("method") == "tools/call"
        ]
        self.assertTrue(starts)
        self.assertTrue(all(event["active"] == 1 for event in starts))

    def test_malformed_and_symlinked_state_fail_loudly(self):
        lease = self.state / "lease.json"
        lease.write_text('{"owner":"wrong schema"}', encoding="utf-8")
        lease.chmod(0o600)
        malformed = self.run_client("lease", "status", check=False)
        self.assertNotEqual(malformed.returncode, 0)
        lease.unlink()
        target = self.state / "target.json"
        target.write_text('{"sentinel":true}', encoding="utf-8")
        target.chmod(0o600)
        lease.symlink_to(target)
        redirected = self.run_client("lease", "status", check=False)
        self.assertNotEqual(redirected.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), '{"sentinel":true}')
        lease.unlink()
        controller = self.state / "controller.json"
        controller.unlink()
        controller.symlink_to(target)
        self.acquire()
        rejected = self.run_client("list", check=False)
        self.assertNotEqual(rejected.returncode, 0)

    def test_legacy_idle_migrates_but_in_flight_fails_loudly(self):
        now = time.time()
        legacy = {
            "owner": "legacy",
            "token_hash": "a" * 64,
            "acquired_at": now,
            "expires_at": now + 5,
        }
        lease_path = self.state / "lease.json"
        lease_path.write_text(json.dumps(legacy), encoding="utf-8")
        lease_path.chmod(0o600)
        status = json.loads(self.run_client("lease", "status").stdout)
        self.assertEqual(status["state"], "held")
        migrated = json.loads(lease_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["schema_version"], 1)
        self.assertNotIn("token_hash", migrated)
        self.run_client("lease", "release")
        legacy["in_flight"] = {
            "id": "b" * 32,
            "method": "tools/call",
            "started_at": now,
        }
        lease_path.write_text(json.dumps(legacy), encoding="utf-8")
        lease_path.chmod(0o600)
        failed = self.run_client("lease", "status", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("no controller generation", failed.stderr)

    def test_tool_is_error_is_nonzero_without_quarantine(self):
        self.acquire()
        result = self.run_client("call", "error", "{}", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Playwright tool error", result.stderr)
        status = json.loads(self.run_client("lease", "status").stdout)
        self.assertEqual(status["state"], "held")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--fake-server":
        run_fake_server(*sys.argv[2:])
    else:
        unittest.main()
