#!/usr/bin/env python3
"""Disposable exact-MCP integration; never contacts a live browser profile."""

import json
import ast
import http.server
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

import browserctl

ROOT = Path(__file__).parent
CTL = ROOT / "browserctl.py"
UNIT = ROOT / "browserctl-controller.service"
CACHE = Path(os.getenv("npm_config_cache", Path.home() / ".npm"))


def free_port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


def dependencies():
    chrome = next(filter(None, (shutil.which(name) for name in (
        "google-chrome", "google-chrome-stable"))), None)
    npx = shutil.which("npx")
    missing = [
        label for label, path in (("Google Chrome", chrome), ("npx", npx))
        if path is None]
    return chrome, npx, missing


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
    def test_disposable_chrome_and_exact_cached_mcp(self):
        chrome_command, npx_command, missing = dependencies()
        if missing:
            self.fail(
                "qualification dependencies missing from PATH: " +
                ", ".join(missing))
        chrome_launcher = Path(chrome_command).absolute()
        npx = Path(npx_command).absolute()
        chrome = daemon = notify = None
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
                chrome = subprocess.Popen([
                    chrome_launcher, "--headless=new", "--no-first-run",
                    "--no-default-browser-check", "--disable-gpu", "--no-sandbox",
                    f"--remote-debugging-port={cdp}",
                    f"--user-data-dir={human}", "about:blank"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, text=True, start_new_session=True)
                deadline = time.monotonic() + 15
                lock = human / "SingletonLock"
                while time.monotonic() < deadline and not lock.is_symlink():
                    if chrome.poll() is not None:
                        self.fail(f"temporary Chrome failed: {chrome.stderr.read()}")
                    time.sleep(.05)
                self.assertTrue(lock.is_symlink())
                while time.monotonic() < deadline:
                    with socket.socket() as probe:
                        if probe.connect_ex(("127.0.0.1", cdp)) == 0:
                            break
                    time.sleep(.05)
                else:
                    self.fail("temporary Chrome CDP listener did not become ready")
                lock_pid = int(os.readlink(lock).rsplit("-", 1)[1])
                chrome_exe = os.readlink(f"/proc/{lock_pid}/exe")
                host = {
                    "schema_version": 1, "hostname": socket.gethostname(),
                    "user": subprocess.check_output(
                        ["id", "-un"], text=True).strip(),
                    "chrome_exe": chrome_exe, "profile": str(human),
                    "cdp_endpoint": f"http://127.0.0.1:{cdp}",
                    "npx": str(npx),
                    "agent_region": {"left": 0, "top": 0, "width": 800, "height": 600},
                    "human_region": {"left": 0, "top": 0, "width": 800, "height": 600},
                }
                host_path = config / "browserctl/host.json"
                host_path.write_text(json.dumps(host))
                host_path.chmod(0o600)
                # Activating a prerendered page swaps the tab's main frame, which MCP cannot reattach to.
                class Site(http.server.BaseHTTPRequestHandler):
                    def log_message(self, *_): pass
                    def do_GET(self):
                        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
                        self.wfile.write(b"" if self.path == "/b" else
                            b'<script type="speculationrules">{"prerender": [{"source": "list", "urls": ["/b"]}]}</script>'
                            b'<script>setTimeout(() => location.href = "/b", 1000)</script>')
                site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
                threading.Thread(target=site.serve_forever, daemon=True).start()
                try:
                    devtools, page = f"http://127.0.0.1:{cdp}/json", f"http://127.0.0.1:{site.server_address[1]}"
                    self.assertEqual(browserctl.preloaded_tabs(host), [])
                    with urllib.request.urlopen(urllib.request.Request(f"{devtools}/new?{page}/a", method="PUT")) as response:
                        tab = json.load(response)["id"]
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        with urllib.request.urlopen(f"{devtools}/list") as response:
                            if {"id": tab, "url": f"{page}/b"} in [{"id": t["id"], "url": t["url"]} for t in json.load(response)]:
                                break
                        time.sleep(.05)
                    self.assertEqual(browserctl.preloaded_tabs(host), [f"{page}/b"])
                    with urllib.request.urlopen(f"{devtools}/close/{tab}"): pass
                finally:
                    site.shutdown()
                    site.server_close()
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
                    ["call", "browser_tabs", '{"action": "new"}'], ["call", "browser_tabs", '{"action": "list"}'],
                    ["call", "browser_tabs", '{"action": "select", "index": 1}'],
                    ["call", "browser_snapshot", "{}"],
                    ["call", "browser_tabs", '{"action": "close", "index": 1}'], ["task", "end"]):
                    result = subprocess.run(
                        [CTL, *args], env=env, text=True,
                        capture_output=True, timeout=30)
                    self.assertEqual(
                        result.returncode, 0,
                        f"{args}: {result.stdout} {result.stderr}")
                def run(*args, code=None, ok=True):
                    result = subprocess.run([CTL, *args], env=env, text=True,
                        input=code, capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, 0 if ok else 1, result.stderr)
                    return result
                run("task", "begin", "agent identity verification")
                result = run("call", "browser_run_code_unsafe", json.dumps({"code": """async(page)=>{
                  const session=await page.context().newCDPSession(page);
                  try {
                    const {targetInfo}=await session.send('Target.getTargetInfo');
                    const {windowId,bounds}=await session.send('Browser.getWindowForTarget',{targetId:targetInfo.targetId});
                    return {targetId:targetInfo.targetId,windowId,region:bounds};
                  } finally {await session.detach();}
                }"""}))
                content=json.loads(result.stdout)['content']
                text=next(c['text'] for c in content if c.get('text','').startswith('### Result\n'))
                binding=json.loads(text.split('### Result\n',1)[1].split('\n### ',1)[0])
                with mock.patch.dict(os.environ,env):
                    identity=browserctl.chrome_identity(host)
                binding.update(pid=identity['pid'],start=identity['start'])
                binding_path=config/'browserctl/agent-window.json'
                binding_path.write_text(json.dumps(binding))
                valid=run('agent',code='async(page)=>({title:await page.title()})')
                self.assertIn('title',valid.stdout)
                for key,wrong,expected in [('windowId',-1,'moved to another window'),
                        ('targetId','missing-target','Configured agent tab is missing'),
                        ('start','wrong-start','another browser'),
                        ('region',{'left':10000,'top':0,'width':10,'height':10},'outside its assigned region')]:
                    binding_path.write_text(json.dumps({**binding,key:wrong}))
                    failed=run('agent',code='async()=>{throw new Error("USER_CODE_EXECUTED")}',ok=False)
                    self.assertIn(expected,failed.stderr)
                binding_path.write_text(json.dumps(binding))
                # Exercise the actual startup JavaScript against two disposable Chrome windows.
                tree=ast.parse(CTL.read_text())
                startup=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='start_chrome')
                script=next(n.value for n in ast.walk(startup) if isinstance(n,ast.Constant) and
                            isinstance(n.value,str) and 'const {region,human,prepareOnly,previous}' in n.value)
                def raw(code,ok=True):
                    return run('call','browser_run_code_unsafe',json.dumps({'code':code}),ok=ok)
                raw("""async(page)=>{
                  const s=await page.context().browser().newBrowserCDPSession();
                  try {return await s.send('Target.createTarget',{url:'about:blank',newWindow:true,left:850,top:0,width:800,height:600});}
                  finally {await s.detach();}
                }""")
                setup={'region':{k:binding['region'][k] for k in ('left','top','width','height')},
                       'human':{'left':850,'top':0,'width':800,'height':600},
                       'prepareOnly':True,'previous':binding}
                count="""async(page)=>{
                  const s=await page.context().browser().newBrowserCDPSession();
                  try {
                    const windows=new Set();
                    for(const t of (await s.send('Target.getTargets')).targetInfos.filter(t=>t.type==='page'))
                      windows.add((await s.send('Browser.getWindowForTarget',{targetId:t.targetId})).windowId);
                    return {windowCount:windows.size};
                  }finally{await s.detach();}
                }"""
                for prepare in [True,False,True]:
                    raw(script.replace('SETUP',json.dumps({**setup,'prepareOnly':prepare})))
                    self.assertIn('"windowCount":2',json.loads(raw(count).stdout)['content'][0]['text'])
                missing={**setup,'previous':None,'region':{**setup['region'],'left':9000}}
                failed=raw(script.replace('SETUP',json.dumps(missing)),ok=False)
                self.assertIn('no window was created',failed.stderr)
                self.assertIn('"windowCount":2',json.loads(raw(count).stdout)['content'][0]['text'])
                moved={**setup,'region':{**setup['region'],'left':9000}}
                failed=raw(script.replace('SETUP',json.dumps(moved)),ok=False)
                self.assertIn('Recorded agent window moved',failed.stderr)
                self.assertIn('"windowCount":2',json.loads(raw(count).stdout)['content'][0]['text'])
                run('task','end')
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
                if chrome and chrome.poll() is None:
                    os.killpg(chrome.pid, signal.SIGTERM)
                    try:
                        chrome.wait(5)
                    except subprocess.TimeoutExpired:
                        os.killpg(chrome.pid, signal.SIGKILL)
                        chrome.wait(5)
                if chrome:
                    chrome.stderr.close()
                if notify:
                    notify.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
