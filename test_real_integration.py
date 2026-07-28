#!/usr/bin/env python3
"""Disposable integration against the locally cached exact MCP package."""

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
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
CACHE = Path.home() / ".cache/browserctl/npm/_npx"


def cached_package():
    matches = list(CACHE.glob("*/node_modules/@playwright/mcp"))
    for package in matches:
        try:
            if json.loads((package / "package.json").read_text())["version"] == "0.0.78":
                return package
        except (OSError, KeyError, json.JSONDecodeError):
            pass
    return None


PACKAGE = cached_package()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_port(port, process, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"process exited early with {process.returncode}")
        with socket.socket() as sock:
            sock.settimeout(.1)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(.05)
    raise AssertionError(f"port {port} did not become ready")


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


@unittest.skipUnless(EDGE and PACKAGE, "Edge or cached @playwright/mcp@0.0.78 unavailable")
class RealPackageIntegration(unittest.TestCase):
    def test_persistent_session_ref_survival_and_delete(self):
        with tempfile.TemporaryDirectory(prefix="browserctl-real-") as temporary:
            temp = Path(temporary)
            state = temp / "state"
            state.mkdir(mode=0o700)
            fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
            fixture_thread = threading.Thread(target=fixture.serve_forever, daemon=True)
            fixture_thread.start()
            cdp_port, mcp_port = free_port(), free_port()
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
            try:
                wait_port(cdp_port, edge)
                fake_bin = temp / "bin"
                fake_bin.mkdir()
                npx = fake_bin / "npx"
                npx.write_text(
                    "#!/bin/sh\nshift 2\nexec node "
                    f"'{PACKAGE / 'cli.js'}' \"$@\"\n")
                npx.chmod(0o700)
                env = os.environ.copy()
                env.update({
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "BROWSERCTL_STATE_DIR": str(state),
                    "BROWSERCTL_CDP_ENDPOINT": cdp,
                    "BROWSERCTL_MCP_URL": mcp,
                    "BROWSERCTL_HOST": "127.0.0.1",
                    "BROWSERCTL_PORT": str(mcp_port),
                    "BROWSERCTL_HTTP_TIMEOUT": "15",
                })
                controller = subprocess.Popen(
                    [ROOT / "start-controller.sh"], env=env,
                    cwd=temp, stdout=controller_log, stderr=subprocess.STDOUT)
                wait_port(mcp_port, controller)

                def run(*arguments):
                    result = subprocess.run(
                        [CLIENT, *arguments], env=env, text=True,
                        capture_output=True)
                    self.assertEqual(
                        result.returncode, 0,
                        f"{arguments}: stdout={result.stdout!r} stderr={result.stderr!r}")
                    return result

                acquired = json.loads(run("lease", "acquire", "real-test").stdout)
                session = acquired["session_id"]
                run("call", "browser_navigate", json.dumps({"url": page}))
                snapshot = json.loads(
                    run("call", "browser_snapshot", "{}").stdout)
                text = "\n".join(block.get("text", "")
                                 for block in snapshot["content"])
                match = re.search(r'button "Increment" \[ref=([^\]]+)\]', text)
                self.assertIsNotNone(match, text)
                count = re.search(
                    r'status "Count"(?: \[ref=[^\]]+\])?: "([0-9]+)"', text)
                self.assertIsNotNone(count, text)
                self.assertEqual(count.group(1), "0")
                time.sleep(5.5)
                run("call", "browser_click", json.dumps({
                    "element": "Increment button", "target": match.group(1)}))
                after = json.loads(run("call", "browser_snapshot", "{}").stdout)
                after_text = "\n".join(block.get("text", "")
                                       for block in after["content"])
                count = re.search(
                    r'status "Count"(?: \[ref=[^\]]+\])?: "([0-9]+)"',
                    after_text)
                self.assertIsNotNone(count, after_text)
                self.assertEqual(count.group(1), "1")
                self.assertEqual(
                    json.loads(run("lease", "status").stdout)["session_id"],
                    session)

                controller.terminate()
                controller.wait(5)
                self.assertIsNone(edge.poll())
                status = json.loads(run("lease", "status").stdout)
                self.assertEqual(status["state"], "recovery_required")
                controller = subprocess.Popen(
                    [ROOT / "start-controller.sh"], env=env,
                    cwd=temp, stdout=controller_log, stderr=subprocess.STDOUT)
                wait_port(mcp_port, controller)
                recovered = json.loads(run("lease", "recover").stdout)
                recovered_session = recovered["session_id"]
                self.assertNotEqual(recovered_session, session)
                after_recovery = json.loads(
                    run("call", "browser_snapshot", "{}").stdout)
                recovered_text = "\n".join(
                    block.get("text", "")
                    for block in after_recovery["content"])
                count = re.search(
                    r'status "Count"(?: \[ref=[^\]]+\])?: "([0-9]+)"',
                    recovered_text)
                self.assertIsNotNone(count, recovered_text)
                self.assertEqual(count.group(1), "1")
                run("lease", "release")

                for deleted_session in (session, recovered_session):
                    request = urllib.request.Request(
                        mcp, method="POST",
                        data=json.dumps({
                            "jsonrpc": "2.0", "id": 999,
                            "method": "tools/list", "params": {}}).encode(),
                        headers={
                            "Content-Type": "application/json",
                            "Mcp-Session-Id": deleted_session,
                            "Mcp-Protocol-Version": "2025-03-26"})
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(request, timeout=3)
                    self.assertEqual(caught.exception.code, 404)
                self.assertIsNone(edge.poll())
                second = json.loads(
                    run("lease", "acquire", "real-test-2").stdout)
                self.assertNotEqual(second["session_id"], session)
                self.assertNotEqual(second["session_id"], recovered_session)
                run("lease", "release")
                self.assertIsNone(edge.poll())
            finally:
                if controller and controller.poll() is None:
                    controller.terminate()
                    controller.wait(5)
                if edge.poll() is None:
                    edge.terminate()
                    edge.wait(5)
                fixture.shutdown()
                fixture.server_close()
                edge_log.close()
                controller_log.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
