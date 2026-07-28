#!/usr/bin/env python3
"""Disposable qualification against exact cached @playwright/mcp 0.0.78."""

import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
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
EDGE = shutil.which("microsoft-edge") or shutil.which("microsoft-edge-stable")
NODE = shutil.which("node")
NPX = shutil.which("npx")
SETSID = shutil.which("setsid")
CACHE = Path.home() / ".cache/browserctl/npm/_npx"
CYCLES = 20


def cached_package():
    for package in CACHE.glob("*/node_modules/@playwright/mcp"):
        try:
            metadata = json.loads((package / "package.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if metadata.get("version") == "0.0.78":
            return package
    return None


PACKAGE = cached_package()


def free_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def wait_port(port, process, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"process exited early with {process.returncode}")
        with socket.socket() as sock:
            sock.settimeout(.1)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(.05)
    raise AssertionError(f"port {port} did not become ready")


def wait_port_closed(port, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(.1)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(.05)
    raise AssertionError(f"port {port} remained open")


def process_rows():
    rows = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            pid = int(path.parent.name)
            fields = path.read_text().rsplit(")", 1)[1].strip().split()
            rows[pid] = {
                "state": fields[0],
                "ppid": int(fields[1]),
                "pgrp": int(fields[2]),
                "session": int(fields[3]),
                "start": fields[19],
            }
        except (FileNotFoundError, IndexError, OSError, ValueError):
            pass
    return rows


def find_mcp_group(launcher_pid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = process_rows()
        leaders = [
            (pid, row["start"]) for pid, row in rows.items()
            if row["ppid"] == launcher_pid
            and row["pgrp"] == pid and row["session"] == pid]
        if len(leaders) == 1:
            return leaders[0]
        time.sleep(.05)
    raise AssertionError("launcher did not have one exact MCP session leader")


def group_members(pgid):
    return {
        pid for pid, row in process_rows().items()
        if row["pgrp"] == pgid}


def wait_group_empty(pgid, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not group_members(pgid):
            return
        time.sleep(.05)
    raise AssertionError(
        f"MCP process group {pgid} survived: {sorted(group_members(pgid))}")


def terminate_group_if_same(identity):
    pgid, start = identity
    rows = process_rows()
    if not any(row["pgrp"] == pgid for row in rows.values()):
        return
    leader = rows.get(pgid)
    if (not leader or leader["pgrp"] != pgid
            or leader["session"] != pgid or leader["start"] != start):
        raise AssertionError(
            f"refusing to signal reused MCP process group {pgid}")
    os.killpg(pgid, signal.SIGKILL)
    wait_group_empty(pgid)


def wait_controller_ready(
        state, process, log_path, previous_generation=None, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"controller exited early with {process.returncode}: "
                f"{log_path.read_text(errors='replace')}")
        try:
            value = json.loads((state / "controller.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            value = {}
        if (value.get("phase") == "ready"
                and value.get("generation") != previous_generation):
            return value
        time.sleep(.05)
    raise AssertionError("controller did not atomically publish ready")


def http_json(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.load(response)


def response_text(value):
    return "\n".join(
        block.get("text", "") for block in value["content"])


def page_count(value):
    text = response_text(value)
    match = re.search(
        r'status "Count"(?: \[ref=[^\]]+\])?: "([0-9]+)"', text)
    if not match:
        raise AssertionError(text)
    return int(match.group(1))


def mcp_session_status(url, session):
    request = urllib.request.Request(
        url, method="POST",
        data=json.dumps({
            "jsonrpc": "2.0", "id": 999,
            "method": "tools/list", "params": {}}).encode(),
        headers={
            "Content-Type": "application/json",
            "Mcp-Session-Id": session,
            "Mcp-Protocol-Version": "2025-03-26"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        return code


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        body = b"""<!doctype html><title>browserctl fixture</title>
<button id="increment" onclick="count.textContent=Number(count.textContent)+1">Increment</button>
<div id="count" role="status" aria-label="Count">0</div>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ExactPackageQualification(unittest.TestCase):
    def test_twenty_workflows_group_stop_and_anchored_recovery(self):
        self.assertIsNotNone(
            EDGE, "qualification requires microsoft-edge on PATH")
        self.assertIsNotNone(NODE, "qualification requires node on PATH")
        self.assertIsNotNone(NPX, "qualification requires npx on PATH")
        self.assertIsNotNone(SETSID, "qualification requires setsid on PATH")
        self.assertIsNotNone(
            PACKAGE,
            "qualification requires cached exact @playwright/mcp@0.0.78")
        with tempfile.TemporaryDirectory(
                prefix="browserctl-real-qualification-") as temporary:
            temp = Path(temporary)
            state = temp / "state"
            state.mkdir(mode=0o700)
            fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
            fixture_thread = threading.Thread(
                target=fixture.serve_forever, daemon=True)
            fixture_thread.start()
            cdp_port, mcp_port, contaminated_port = free_ports(3)
            cdp = f"http://127.0.0.1:{cdp_port}"
            mcp = f"http://127.0.0.1:{mcp_port}/mcp"
            page = f"http://127.0.0.1:{fixture.server_port}/"
            edge_log = open(temp / "edge.log", "w")
            controller_log = open(temp / "controller.log", "w")
            edge = subprocess.Popen([
                EDGE, "--headless=new", "--no-sandbox", "--disable-gpu",
                f"--remote-debugging-port={cdp_port}",
                f"--user-data-dir={temp / 'profile'}", "about:blank",
            ], cwd=temp, stdout=edge_log, stderr=subprocess.STDOUT)
            controller = None
            mcp_groups = []
            try:
                wait_port(cdp_port, edge)
                original_pid = edge.pid
                original_tabs = {
                    tab["id"] for tab in http_json(f"{cdp}/json/list")
                    if tab.get("type") == "page"}
                self.assertTrue(original_tabs)

                env = {
                    name: value for name, value in os.environ.items()
                    if not name.startswith("PLAYWRIGHT_MCP_")}
                env.update({
                    "DEBUG": "pw:mcp:test",
                    "BROWSERCTL_STATE_DIR": str(state),
                    "BROWSERCTL_CDP_ENDPOINT": cdp,
                    "BROWSERCTL_MCP_URL": mcp,
                    "BROWSERCTL_HOST": "127.0.0.1",
                    "BROWSERCTL_PORT": str(mcp_port),
                    "BROWSERCTL_HTTP_TIMEOUT": "15",
                    "BROWSERCTL_STARTUP_TIMEOUT": "15",
                    "BROWSERCTL_NPM_CACHE": str(CACHE.parent),
                    "npm_config_offline": "true",
                    "npm_config_update_notifier": "false",
                })

                contaminated_state = temp / "contaminated-state"
                contaminated_state.mkdir(mode=0o700)
                contaminated = subprocess.run(
                    [ROOT / "start-controller.sh"],
                    env={
                        **env,
                        "BROWSERCTL_STATE_DIR": str(contaminated_state),
                        "BROWSERCTL_MCP_URL": (
                            f"http://127.0.0.1:{contaminated_port}/mcp"),
                        "BROWSERCTL_PORT": str(contaminated_port),
                        "PLAYWRIGHT_MCP_ISOLATED": "1",
                    },
                    cwd=temp, text=True, capture_output=True, timeout=5)
                self.assertNotEqual(contaminated.returncode, 0)
                self.assertIn(
                    "ambient PLAYWRIGHT_MCP_* variables are forbidden",
                    contaminated.stderr)
                self.assertIn(
                    "PLAYWRIGHT_MCP_ISOLATED", contaminated.stderr)
                self.assertFalse(
                    (contaminated_state / "controller.json").exists())
                wait_port_closed(contaminated_port)
                self.assertEqual(
                    {tab["id"] for tab in http_json(f"{cdp}/json/list")
                     if tab.get("type") == "page"},
                    original_tabs)

                controller = subprocess.Popen(
                    [ROOT / "start-controller.sh"], env=env, cwd=temp,
                    stdout=controller_log, stderr=subprocess.STDOUT)
                registered = wait_controller_ready(
                    state, controller, temp / "controller.log")
                generation_one = registered["generation"]
                anchor_one = registered["anchor_session_id"]
                self.assertEqual(registered["phase"], "ready")
                self.assertEqual(registered["cdp_endpoint"], cdp)
                identity_one = find_mcp_group(controller.pid)
                group_one = identity_one[0]
                mcp_groups.append(identity_one)
                members_one = group_members(group_one)
                self.assertIn(group_one, members_one)
                self.assertGreaterEqual(
                    len(members_one), 2,
                    f"real npx had no MCP descendant: {members_one}")
                self.assertNotEqual(group_one, os.getpgid(edge.pid))

                def invoke(*arguments):
                    return subprocess.run(
                        [CLIENT, *arguments], env=env, text=True,
                        capture_output=True)

                def run(*arguments):
                    result = invoke(*arguments)
                    self.assertEqual(
                        result.returncode, 0,
                        f"{arguments}: stdout={result.stdout!r} "
                        f"stderr={result.stderr!r}")
                    return result

                workflow_sessions = []
                stable_tabs = None
                for cycle in range(CYCLES):
                    acquired = json.loads(run(
                        "lease", "acquire",
                        f"qualification-cycle-{cycle}").stdout)
                    session = acquired["session_id"]
                    workflow_sessions.append(session)
                    self.assertNotEqual(session, anchor_one)
                    self.assertNotIn("anchor", acquired)
                    tabs = json.loads(run(
                        "call", "browser_tabs",
                        '{"action":"list"}').stdout)
                    tab_text = "\n".join(
                        block.get("text", "")
                        for block in tabs["content"])
                    tab_lines = tuple(
                        line for line in tab_text.splitlines()
                        if re.match(r"- \d+:", line))
                    self.assertTrue(tab_lines)
                    if stable_tabs is None:
                        stable_tabs = len(tab_lines)
                    self.assertEqual(len(tab_lines), stable_tabs)

                    if cycle == 0:
                        run("call", "browser_navigate",
                            json.dumps({"url": page}))
                        snapshot = json.loads(run(
                            "call", "browser_snapshot", "{}").stdout)
                        text = "\n".join(
                            block.get("text", "")
                            for block in snapshot["content"])
                        target = re.search(
                            r'button "Increment" \[ref=(e\d+)\]', text)
                        self.assertIsNotNone(target, text)
                        run("call", "browser_click", json.dumps({
                            "element": "Increment button",
                            "target": target.group(1)}))
                    snapshot = json.loads(run(
                        "call", "browser_snapshot", "{}").stdout)
                    self.assertEqual(page_count(snapshot), 1)

                    run("lease", "release")
                    self.assertIsNone(edge.poll())
                    self.assertEqual(mcp_session_status(mcp, session), 404)

                self.assertEqual(len(set(workflow_sessions)), CYCLES)
                held = json.loads(run(
                    "lease", "acquire", "qualification-held").stdout)
                held_session = held["session_id"]
                held_tabs = json.loads(run(
                    "call", "browser_tabs", '{"action":"list"}').stdout)
                self.assertIn(page, response_text(held_tabs))
                held_snapshot = json.loads(run(
                    "call", "browser_snapshot", "{}").stdout)
                self.assertEqual(page_count(held_snapshot), 1)

                controller.terminate()
                controller.wait(10)
                controller = None
                wait_port_closed(mcp_port)
                wait_group_empty(group_one)
                mcp_groups.remove(identity_one)
                self.assertEqual(edge.pid, original_pid)
                self.assertIsNone(edge.poll())
                stopped_tabs = [
                    tab for tab in http_json(f"{cdp}/json/list")
                    if tab.get("type") == "page"]
                self.assertEqual(
                    {tab["id"] for tab in stopped_tabs}, original_tabs)
                self.assertIn(page, {tab["url"] for tab in stopped_tabs})
                self.assertEqual(
                    json.loads(run("lease", "status").stdout)["state"],
                    "recovery_required")

                controller = subprocess.Popen(
                    [ROOT / "start-controller.sh"], env=env, cwd=temp,
                    stdout=controller_log, stderr=subprocess.STDOUT)
                replacement = wait_controller_ready(
                    state, controller, temp / "controller.log",
                    generation_one)
                generation_two = replacement["generation"]
                anchor_two = replacement["anchor_session_id"]
                self.assertNotEqual(generation_two, generation_one)
                self.assertNotEqual(anchor_two, anchor_one)
                identity_two = find_mcp_group(controller.pid)
                group_two = identity_two[0]
                mcp_groups.append(identity_two)
                self.assertGreaterEqual(len(group_members(group_two)), 2)
                self.assertEqual(
                    json.loads(run("lease", "status").stdout)["state"],
                    "recovery_required")

                recovered = json.loads(run("lease", "recover").stdout)
                recovered_session = recovered["session_id"]
                self.assertNotEqual(recovered_session, held_session)
                self.assertEqual(
                    recovered["previous_controller_generation"],
                    generation_one)
                self.assertEqual(
                    recovered["controller_generation"], generation_two)
                fresh_tabs = json.loads(run(
                    "call", "browser_tabs", '{"action":"list"}').stdout)
                fresh_text = response_text(fresh_tabs)
                matching = [
                    line for line in fresh_text.splitlines()
                    if page in line]
                self.assertEqual(len(matching), 1, fresh_text)
                index = re.match(r"- (\d+):", matching[0].strip())
                self.assertIsNotNone(index, fresh_text)
                run("call", "browser_tabs", json.dumps({
                    "action": "select", "index": int(index.group(1))}))
                recovered_snapshot = json.loads(run(
                    "call", "browser_snapshot", "{}").stdout)
                self.assertEqual(page_count(recovered_snapshot), 1)
                run("lease", "release")

                for old_session in (
                        *workflow_sessions, held_session, anchor_one,
                        recovered_session):
                    self.assertEqual(
                        mcp_session_status(mcp, old_session), 404)

                final_tabs = {
                    tab["id"] for tab in http_json(f"{cdp}/json/list")
                    if tab.get("type") == "page"}
                self.assertEqual(final_tabs, original_tabs)
                self.assertEqual(
                    json.loads((state / "controller.json").read_text())
                    ["anchor_session_id"], anchor_two)
                anchor_request = urllib.request.Request(
                    mcp, method="POST",
                    data=json.dumps({
                        "jsonrpc": "2.0", "id": 999,
                        "method": "tools/list", "params": {}}).encode(),
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                        "Mcp-Session-Id": anchor_two,
                        "Mcp-Protocol-Version": "2025-03-26"})
                with urllib.request.urlopen(
                        anchor_request, timeout=3) as response:
                    self.assertEqual(response.status, 200)

                controller_log.flush()
                log = (temp / "controller.log").read_text()
                self.assertEqual(log.count("create browser (cdp)"), 2, log)
                self.assertEqual(log.count("create http session"),
                                 CYCLES + 4, log)
                self.assertEqual(log.count("delete http session"),
                                 CYCLES + 1, log)
                self.assertIsNone(edge.poll())
            finally:
                if controller and controller.poll() is None:
                    controller.terminate()
                    controller.wait(10)
                for identity in mcp_groups:
                    terminate_group_if_same(identity)
                if edge.poll() is None:
                    edge.terminate()
                    edge.wait(5)
                fixture.shutdown()
                fixture.server_close()
                edge_log.close()
                controller_log.close()


if __name__ == "__main__":
    developer_skip = "--developer-skip-missing" in sys.argv
    if developer_skip:
        sys.argv.remove("--developer-skip-missing")
        if not EDGE or not PACKAGE:
            print("developer convenience: skipped; Edge or exact cache missing")
            raise SystemExit(0)
    unittest.main(verbosity=2)
