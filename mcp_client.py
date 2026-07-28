#!/usr/bin/env python3
"""One fail-closed leased Streamable HTTP MCP session."""

import base64
import binascii
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = 3
PROTOCOL = "2025-03-26"
MAX_WAIT = 3600.0
MAX_BODY = 16 * 1024 * 1024
PHASES = {"initializing", "ready", "in_progress", "result_pending",
          "closing", "recovery_required"}
LEASE_KEYS = {"schema_version", "phase", "owner", "acquired_at", "mcp_url",
              "cdp_endpoint", "controller_generation", "session_id",
              "protocol_version", "next_request_id", "client", "operation",
              "receipt", "reason", "snapshot_ready"}
CONTROLLER_KEYS = {
    "schema_version", "phase", "generation", "pid", "start", "boot_id",
    "mcp_url", "cdp_endpoint", "registered_at", "ready_at",
    "anchor_session_id", "anchor_protocol_version", "anchor_next_request_id",
}
FORBIDDEN_TOOLS = {
    "browser_get_config", "browser_close", "browser_run_code_unsafe",
}
REF_TARGET_KEYS = {"target", "startTarget", "endTarget"}
INVALIDATE_TOOLS = {
    "browser_navigate", "browser_reload", "browser_navigate_back",
    "browser_navigate_forward",
}
SYNCED_STATE_DIRS = set()


class Error(Exception):
    pass


def fail(message):
    raise Error(message)


def reject_constant(value):
    fail(f"non-finite JSON number {value} is forbidden")


def validate_json(value):
    if isinstance(value, float) and not math.isfinite(value):
        fail("non-finite JSON numbers are forbidden")
    if isinstance(value, list):
        for item in value:
            validate_json(item)
    elif isinstance(value, dict):
        for item in value.values():
            validate_json(item)


def duration(value, name, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        fail(f"{name} must be a number")
    if not math.isfinite(number) or not low <= number <= high:
        fail(f"{name} must be between {low:g} and {high:g} seconds")
    return number


def timeout():
    return duration(os.getenv("BROWSERCTL_HTTP_TIMEOUT", "90"),
                    "HTTP timeout", .1, 300)


def state_dir():
    value = os.getenv("BROWSERCTL_STATE_DIR")
    path = Path(value) if value else Path(
        os.getenv("XDG_STATE_HOME", Path.home() / ".local/state")) / "browserctl"
    path = path.absolute()
    try:
        missing = []
        current = path
        while not current.exists():
            missing.append(current)
            if current == current.parent:
                fail(f"cannot find an existing parent for state directory {path}")
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
            sync_dir(directory.parent)
        info = path.lstat()
    except OSError as exc:
        fail(f"cannot prepare state directory {path}: {exc}")
    if (path.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        fail(f"state directory must be owner-controlled mode 0700: {path}")
    identity = str(path)
    if identity not in SYNCED_STATE_DIRS:
        for directory in (path, *path.parents):
            sync_dir(directory)
        SYNCED_STATE_DIRS.add(identity)
    return path


def open_private(path, flags):
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        fail(f"cannot open private state file {path}: {exc}")
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        os.close(fd)
        fail(f"state file must be owner-controlled mode 0600: {path}")
    return fd


@contextlib.contextmanager
def lock(name, wait=None):
    fd = open_private(state_dir() / name, os.O_RDWR | os.O_CREAT)
    try:
        budget = duration(
            os.getenv("BROWSERCTL_LOCK_TIMEOUT", "45"),
            "lock timeout", .1, MAX_WAIT)
        if wait is not None:
            budget = min(budget, max(0, wait))
        deadline = time.monotonic() + budget
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    fail(f"timed out waiting for {name}")
                time.sleep(.05)
        yield
    finally:
        os.close(fd)


def read_json(name, missing=False):
    path = state_dir() / name
    flags = os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if missing:
            return None
        fail(f"{name} is missing")
    except OSError as exc:
        fail(f"cannot open {name}: {exc}")
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        os.close(fd)
        fail(f"{name} must be owner-controlled mode 0600")
    try:
        with os.fdopen(fd, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read {name}: {exc}")


def sync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        fail(f"cannot sync state directory: {exc}")


def write_json(name, value):
    directory = state_dir()
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"), ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / name)
        sync_dir(directory)
    except OSError as exc:
        fail(f"cannot durably write {name}: {exc}")
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def remove_lease():
    try:
        (state_dir() / "lease.json").unlink()
        sync_dir(state_dir())
    except OSError as exc:
        fail(f"cannot durably remove lease: {exc}")


def boot_id():
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError as exc:
        fail(f"cannot read Linux boot identity: {exc}")
    if len(value) != 36:
        fail("Linux boot identity is malformed")
    return value


def process(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(
            ")", 1)[1].strip().split()
        return None if fields[0] == "Z" else fields[19]
    except (FileNotFoundError, IndexError, OSError):
        return None


def client():
    start = process(os.getpid())
    if start is None:
        fail("cannot identify client process")
    return {"pid": os.getpid(), "start": start, "boot_id": boot_id()}


def valid_client(value):
    return (isinstance(value, dict) and set(value) == {"pid", "start", "boot_id"}
            and type(value["pid"]) is int and value["pid"] > 1
            and isinstance(value["start"], str) and value["start"].isdigit()
            and isinstance(value["boot_id"], str) and len(value["boot_id"]) == 36)


def alive(value):
    return (valid_client(value) and value["boot_id"] == boot_id()
            and process(value["pid"]) == value["start"])


def valid_operation(value):
    return (isinstance(value, dict)
            and set(value) == {"kind", "method", "request_id", "started_at"}
            and value["kind"] in {"request", "notification", "delete"}
            and isinstance(value["method"], str) and bool(value["method"])
            and (value["request_id"] is None
                 or type(value["request_id"]) is int and value["request_id"] > 0)
            and isinstance(value["started_at"], (int, float))
            and math.isfinite(value["started_at"]))


def valid_receipt(value):
    try:
        stdout_size = len(value.get("stdout", "").encode())
        stderr_size = len(value.get("stderr", "").encode())
    except (AttributeError, UnicodeEncodeError):
        return False
    return (isinstance(value, dict)
            and set(value) == {"id", "request_id", "method", "stdout",
                              "stderr", "exit_code", "completed_at"}
            and isinstance(value["id"], str) and len(value["id"]) == 32
            and type(value["request_id"]) is int and value["request_id"] > 0
            and isinstance(value["method"], str) and bool(value["method"])
            and isinstance(value["stdout"], str)
            and isinstance(value["stderr"], str)
            and value["exit_code"] in {0, 1}
            and isinstance(value["completed_at"], (int, float))
            and math.isfinite(value["completed_at"])
            and stdout_size <= MAX_BODY and stderr_size <= MAX_BODY)


def parse_lease(value):
    scalar = (isinstance(value, dict) and set(value) == LEASE_KEYS
              and value.get("schema_version") == VERSION
              and value.get("phase") in PHASES
              and isinstance(value.get("owner"), str)
              and 0 < len(value["owner"]) <= 200
              and isinstance(value.get("acquired_at"), (int, float))
              and math.isfinite(value["acquired_at"])
              and isinstance(value.get("mcp_url"), str)
              and isinstance(value.get("cdp_endpoint"), str)
              and isinstance(value.get("controller_generation"), str)
              and len(value["controller_generation"]) == 32
              and all(char in "0123456789abcdef"
                      for char in value["controller_generation"])
              and (value.get("session_id") is None
                   or isinstance(value["session_id"], str)
                   and 0 < len(value["session_id"]) <= 256
                   and all(32 <= ord(char) < 127 for char in value["session_id"]))
              and (value.get("protocol_version") is None
                   or value["protocol_version"] == PROTOCOL)
              and type(value.get("next_request_id")) is int
              and value["next_request_id"] > 0
              and (value.get("client") is None or valid_client(value["client"]))
              and (value.get("operation") is None
                   or valid_operation(value["operation"]))
              and (value.get("receipt") is None
                   or valid_receipt(value["receipt"]))
              and (value.get("reason") is None
                   or isinstance(value["reason"], str)
                   and 0 < len(value["reason"]) <= 1000)
              and type(value.get("snapshot_ready")) is bool)
    if not scalar:
        fail("lease state is malformed or unsupported")
    phase = value["phase"]
    if phase == "ready":
        shape = (value["session_id"] and value["protocol_version"]
                 and value["client"] is None and value["operation"] is None
                 and value["receipt"] is None and value["reason"] is None)
    elif phase == "result_pending":
        shape = (value["session_id"] and value["protocol_version"]
                 and value["client"] is None and value["operation"] is None
                 and valid_receipt(value["receipt"]) and value["reason"] is None)
    elif phase in {"initializing", "in_progress", "closing"}:
        shape = (valid_client(value["client"])
                 and valid_operation(value["operation"])
                 and value["receipt"] is None and value["reason"] is None)
    else:
        shape = (value["client"] is None and value["operation"] is None
                 and value["receipt"] is None and bool(value["reason"]))
    if not shape:
        fail("lease phase fields are inconsistent")
    return value


def read_lease(missing=False):
    value = read_json("lease.json", missing)
    return None if value is None else parse_lease(value)


def write_lease(value):
    parse_lease(value)
    write_json("lease.json", value)


def endpoint(value, name, mcp=False):
    if not value:
        fail(f"{name} is required")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        fail(f"{name} is invalid: {exc}")
    paths = {"/mcp"} if mcp else {""}
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or port is None or not 1 <= port <= 65535
            or parsed.username or parsed.password
            or parsed.path not in paths or parsed.query or parsed.fragment):
        fail(f"{name} must be a canonical numeric loopback HTTP URL with port")
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    canonical = f"http://{host}:{port}{'/mcp' if mcp else ''}"
    if value != canonical:
        fail(f"{name} is not canonically spelled")
    return parsed, canonical


def mcp_url():
    return endpoint(os.getenv(
        "BROWSERCTL_MCP_URL", "http://127.0.0.1:47883/mcp"),
        "BROWSERCTL_MCP_URL", True)[1]


def cdp_endpoint():
    return endpoint(os.getenv("BROWSERCTL_CDP_ENDPOINT"),
                    "BROWSERCTL_CDP_ENDPOINT")[1]


def parse_controller(value):
    if not (isinstance(value, dict) and set(value) == CONTROLLER_KEYS
            and value.get("schema_version") == VERSION
            and value.get("phase") in {"starting", "ready"}
            and isinstance(value.get("generation"), str)
            and len(value["generation"]) == 32
            and all(char in "0123456789abcdef" for char in value["generation"])
            and type(value.get("pid")) is int and value["pid"] > 1
            and isinstance(value.get("start"), str) and value["start"].isdigit()
            and isinstance(value.get("boot_id"), str)
            and len(value["boot_id"]) == 36
            and isinstance(value.get("registered_at"), (int, float))
            and math.isfinite(value["registered_at"])
            and (value.get("ready_at") is None
                 or isinstance(value["ready_at"], (int, float))
                 and math.isfinite(value["ready_at"]))
            and (value.get("anchor_session_id") is None
                 or isinstance(value["anchor_session_id"], str)
                 and 0 < len(value["anchor_session_id"]) <= 256
                 and all(32 <= ord(char) < 127
                         for char in value["anchor_session_id"]))
            and (value.get("anchor_protocol_version") is None
                 or value["anchor_protocol_version"] == PROTOCOL)
            and type(value.get("anchor_next_request_id")) is int
            and value["anchor_next_request_id"] > 0):
        fail("controller state is malformed or unsupported")
    endpoint(value["mcp_url"], "recorded MCP URL", True)
    endpoint(value["cdp_endpoint"], "recorded CDP endpoint")
    if value["phase"] == "starting":
        shape = (value["ready_at"] is None
                 and value["anchor_session_id"] is None
                 and value["anchor_protocol_version"] is None
                 and value["anchor_next_request_id"] == 1)
    else:
        shape = (value["ready_at"] is not None
                 and value["ready_at"] >= value["registered_at"]
                 and value["anchor_session_id"]
                 and value["anchor_protocol_version"] == PROTOCOL
                 and value["anchor_next_request_id"] >= 4)
    if not shape:
        fail("controller phase fields are inconsistent")
    return value


def read_controller(missing=False):
    value = read_json("controller.json", missing)
    if value is None:
        return None
    return parse_controller(value)


def write_controller(value):
    parse_controller(value)
    write_json("controller.json", value)


def controller_alive(value):
    return value["boot_id"] == boot_id() and process(value["pid"]) == value["start"]


def listener_inodes(address, port):
    table, encoded = (("/proc/net/tcp", "0100007F")
                      if address == "127.0.0.1" else
                      ("/proc/net/tcp6", "00000000000000000000000001000000"))
    try:
        lines = Path(table).read_text().splitlines()[1:]
    except OSError as exc:
        fail(f"cannot inspect MCP listener: {exc}")
    return {fields[9] for line in lines if len(fields := line.split()) > 9
            and fields[1].rsplit(":", 1)[0] == encoded
            and int(fields[1].rsplit(":", 1)[1], 16) == port
            and fields[3] == "0A"}


def owns_listener(root, address, port):
    inodes = listener_inodes(address, port)
    parents = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(
                ")", 1)[1].strip().split()
            parents[int(entry.name)] = int(fields[1])
        except (FileNotFoundError, IndexError, OSError, ValueError):
            pass
    tree = {root}
    while True:
        new = {pid for pid, parent in parents.items() if parent in tree} - tree
        if not new:
            break
        tree |= new
    for pid in tree:
        try:
            descriptors = Path(f"/proc/{pid}/fd").iterdir()
            for descriptor in descriptors:
                try:
                    target = os.readlink(descriptor)
                except OSError:
                    continue
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    return True
        except OSError:
            pass
    return False


def validate_config(cdp, mcp):
    if mcp_url() != mcp:
        fail("caller MCP URL does not match the lease")
    if cdp_endpoint() != cdp:
        fail("caller CDP endpoint does not match the lease")


def validate_controller(generation=None, cdp=None, mcp=None, ready=True):
    value = read_controller()
    expected_mcp = mcp if mcp is not None else mcp_url()
    expected_cdp = cdp if cdp is not None else cdp_endpoint()
    if value["mcp_url"] != expected_mcp:
        fail("registered controller has a different MCP URL")
    if value["cdp_endpoint"] != expected_cdp:
        fail("registered controller has a different CDP endpoint")
    if generation and value["generation"] != generation:
        fail("registered controller generation changed")
    if ready and value["phase"] != "ready":
        fail("registered controller is not ready")
    parsed = urllib.parse.urlsplit(value["mcp_url"])
    if not controller_alive(value):
        fail("registered controller is dead, replaced, reboot-stale, or a zombie")
    if not owns_listener(value["pid"], parsed.hostname, parsed.port):
        fail("registered controller does not own the exact MCP listener")
    return value


def register(arguments):
    if len(arguments) != 3:
        fail("internal registration usage error")
    try:
        pid = int(arguments[0])
    except ValueError:
        fail("controller PID is invalid")
    mcp, cdp = mcp_url(), cdp_endpoint()
    parsed = urllib.parse.urlsplit(mcp)
    if (pid != os.getppid() or arguments[1:] != [mcp, cdp]
            or os.getenv("BROWSERCTL_CONTROLLER_HOST") != parsed.hostname):
        fail("controller registration identity mismatch")
    try:
        configured_port = int(os.environ["BROWSERCTL_CONTROLLER_PORT"])
    except (KeyError, ValueError):
        fail("controller registration port is invalid")
    start = process(pid)
    if configured_port != parsed.port or start is None:
        fail("controller registration process or port is invalid")
    with lock("execution.lock"), lock("state.lock"):
        previous = read_controller(True)
        if previous and controller_alive(previous):
            fail("previously registered controller is still alive")
        write_json("controller.json", {
            "schema_version": VERSION, "phase": "starting",
            "generation": secrets.token_hex(16),
            "pid": pid, "start": start, "boot_id": boot_id(),
            "mcp_url": mcp, "cdp_endpoint": cdp, "registered_at": time.time(),
            "ready_at": None, "anchor_session_id": None,
            "anchor_protocol_version": None, "anchor_next_request_id": 1})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def read_body(response):
    body = response.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        fail(f"MCP response exceeds {MAX_BODY} bytes")
    return body


def decode(body, content_type):
    try:
        text = body.decode()
    except UnicodeDecodeError as exc:
        fail(f"MCP returned non-UTF-8 output: {exc}")
    media = content_type.split(";", 1)[0].lower()
    if media == "application/json":
        payloads = [text] if text.strip() else []
    elif media == "text/event-stream":
        payloads, current = [], []
        for line in text.splitlines() + [""]:
            if line.startswith("data:"):
                current.append(line[5:].lstrip())
            elif not line and current:
                payloads.append("\n".join(current))
                current = []
    else:
        fail(f"MCP returned unsupported Content-Type {content_type!r}")
    try:
        values = [json.loads(item, parse_constant=reject_constant)
                  for item in payloads]
    except json.JSONDecodeError as exc:
        fail(f"MCP returned malformed JSON: {exc}")
    if len(values) != 1 or not isinstance(values[0], dict):
        fail("MCP returned empty or ambiguous JSON-RPC output")
    validate_json(values[0])
    return values[0]


def encode_message(message):
    try:
        body = json.dumps(
            message, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        fail(f"MCP request is not strict encodable JSON: {exc}")
    if len(body) > MAX_BODY:
        fail("MCP request is too large")
    return body


def post(message, generation, session=None, protocol=None, notification=False,
         body=None, controller_ready=True):
    validate_controller(generation, ready=controller_ready)
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    if session:
        headers.update({"Mcp-Session-Id": session,
                        "Mcp-Protocol-Version": protocol})
    body = encode_message(message) if body is None else body
    request = urllib.request.Request(mcp_url(), body, headers, method="POST")
    try:
        with HTTP.open(request, timeout=timeout()) as response:
            if response.geturl() != mcp_url():
                fail("MCP redirects are forbidden")
            status, response_body = response.status, read_body(response)
            response_session = response.headers.get("Mcp-Session-Id")
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode(errors="replace")
        label = "redirect" if 300 <= exc.code < 400 else "error"
        fail(f"MCP HTTP {label} {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        fail(f"MCP request failed: {exc}")
    validate_controller(generation, ready=controller_ready)
    if notification:
        if status != 202 or response_body:
            fail("initialized notification did not return empty HTTP 202")
        if response_session not in {None, session}:
            fail("initialized notification changed the MCP session")
        return None, session
    if status != 200:
        fail(f"MCP request returned HTTP {status}")
    if session:
        if response_session != session:
            fail("MCP response omitted or changed the exact session")
    elif (not isinstance(response_session, str)
          or not 0 < len(response_session) <= 256):
        fail("initialize omitted a valid MCP session ID")
    return decode(response_body, content_type), response_session


def delete(lease):
    validate_controller(lease["controller_generation"],
                        lease["cdp_endpoint"], lease["mcp_url"])
    request = urllib.request.Request(
        lease["mcp_url"], method="DELETE",
        headers={"Accept": "application/json",
                 "Mcp-Session-Id": lease["session_id"],
                 "Mcp-Protocol-Version": lease["protocol_version"]})
    try:
        with HTTP.open(request, timeout=timeout()) as response:
            if response.geturl() != lease["mcp_url"]:
                fail("MCP DELETE redirects are forbidden")
            status, body = response.status, read_body(response)
            response_session = response.headers.get("Mcp-Session-Id")
    except urllib.error.HTTPError as exc:
        fail(f"MCP session DELETE HTTP {exc.code}: "
             f"{exc.read(2000).decode(errors='replace')}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        fail(f"MCP session DELETE failed: {exc}")
    validate_controller(lease["controller_generation"],
                        lease["cdp_endpoint"], lease["mcp_url"])
    if status != 200 or body:
        fail("MCP session DELETE did not return empty HTTP 200")
    if response_session not in {None, lease["session_id"]}:
        fail("MCP session DELETE returned a different session")


def check(response, label, request_id, validator):
    if (not isinstance(response, dict) or response.get("jsonrpc") != "2.0"
            or type(response.get("id")) is not int
            or response["id"] != request_id):
        fail(f"malformed or mismatched {label} response")
    result, error = response.get("result"), response.get("error")
    if ("result" in response) == ("error" in response) or set(response) != {
            "jsonrpc", "id", "result" if "result" in response else "error"}:
        fail(f"malformed {label} envelope")
    if error is not None:
        if (not isinstance(error, dict) or not {"code", "message"} <= error.keys()
                or not set(error) <= {"code", "message", "data"}
                or type(error["code"]) is not int
                or not isinstance(error["message"], str)):
            fail(f"malformed {label} error")
        return None, error
    if not validator(result):
        fail(f"malformed {label} result")
    return result, None


def valid_init(value):
    return (isinstance(value, dict)
            and {"protocolVersion", "capabilities", "serverInfo"} <= value.keys()
            and value["protocolVersion"] == PROTOCOL
            and isinstance(value["capabilities"], dict)
            and isinstance(value["serverInfo"], dict)
            and isinstance(value["serverInfo"].get("name"), str)
            and isinstance(value["serverInfo"].get("version"), str))


def valid_tools(value):
    return (isinstance(value, dict) and set(value) <= {"tools", "nextCursor"}
            and isinstance(value.get("tools"), list)
            and all(isinstance(tool, dict)
                    and isinstance(tool.get("name"), str)
                    and isinstance(tool.get("inputSchema"), dict)
                    for tool in value["tools"]))


def valid_block(value):
    if not isinstance(value, dict):
        return False
    if value.get("type") == "text":
        return isinstance(value.get("text"), str)
    if (value.get("type") != "image"
            or value.get("mimeType") not in {"image/png", "image/jpeg"}
            or not isinstance(value.get("data"), str)):
        return False
    try:
        base64.b64decode(value["data"], validate=True)
        return bool(value["data"])
    except (binascii.Error, ValueError):
        return False


def valid_call(value):
    return (isinstance(value, dict)
            and set(value) <= {"content", "isError", "structuredContent"}
            and isinstance(value.get("content"), list)
            and all(valid_block(block) for block in value["content"])
            and ("isError" not in value or type(value["isError"]) is bool))


def require_tools(result, label, required):
    names = {tool["name"] for tool in result["tools"]}
    missing = required - names
    if missing:
        fail(f"{label} omitted required tools: {', '.join(sorted(missing))}")


def controller_startup(arguments):
    if len(arguments) != 3:
        fail("internal controller startup usage error")
    try:
        pid = int(arguments[0])
    except ValueError:
        fail("controller PID is invalid")
    if (pid != os.getppid()
            or arguments[1:] != [mcp_url(), cdp_endpoint()]):
        fail("controller startup identity mismatch")
    with lock("execution.lock"):
        with lock("state.lock"):
            controller = read_controller()
            if (controller["phase"] != "starting"
                    or controller["pid"] != pid):
                fail("controller is not the registered starting process")
        parsed = urllib.parse.urlsplit(controller["mcp_url"])
        deadline = time.monotonic() + duration(
            os.getenv("BROWSERCTL_STARTUP_TIMEOUT", "30"),
            "startup timeout", .1, 300)
        while not owns_listener(pid, parsed.hostname, parsed.port):
            if not controller_alive(controller):
                fail("registered starting controller died before listening")
            if time.monotonic() >= deadline:
                fail("timed out waiting for the exact MCP listener")
            time.sleep(.05)
        validate_controller(controller["generation"], ready=False)

        identifier = 1
        response, session = post({
            "jsonrpc": "2.0", "id": identifier, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                       "clientInfo": {
                           "name": "browserctl-anchor", "version": "3.0"}}},
            controller["generation"], controller_ready=False)
        result, error = check(
            response, "anchor initialize", identifier, valid_init)
        if error:
            fail(f"anchor initialize error: {json.dumps(error)}")
        post({"jsonrpc": "2.0", "method": "notifications/initialized",
              "params": {}}, controller["generation"], session, PROTOCOL,
             True, controller_ready=False)

        identifier = 2
        response, _ = post({
            "jsonrpc": "2.0", "id": identifier, "method": "tools/list",
            "params": {}}, controller["generation"], session, PROTOCOL,
            controller_ready=False)
        tools, error = check(
            response, "anchor tools/list", identifier, valid_tools)
        if error:
            fail(f"anchor tools/list error: {json.dumps(error)}")
        require_tools(
            tools, "anchor tools/list", {"browser_tabs", "browser_get_config"})

        identifier = 3
        response, _ = post({
            "jsonrpc": "2.0", "id": identifier, "method": "tools/call",
            "params": {"name": "browser_get_config", "arguments": {}}},
            controller["generation"], session, PROTOCOL,
            controller_ready=False)
        value, error = check(
            response, "anchor browser_get_config", identifier, valid_call)
        if error or value.get("isError"):
            fail("anchor browser_get_config failed: "
                 f"{json.dumps(error or value)}")
        texts = [block["text"] for block in value["content"]
                 if block["type"] == "text"]
        prefix = "### Result\n"
        if len(texts) != 1 or not texts[0].startswith(prefix):
            fail("anchor browser_get_config returned ambiguous text")
        try:
            config = json.loads(
                texts[0][len(prefix):], parse_constant=reject_constant)
        except json.JSONDecodeError as exc:
            fail(f"anchor browser_get_config returned malformed JSON: {exc}")
        validate_json(config)
        browser = config.get("browser") if isinstance(config, dict) else None
        if (not isinstance(browser, dict)
                or config.get("sharedBrowserContext") is not True
                or config.get("capabilities") != ["config"]
                or config.get("extension") not in (None, False)
                or browser.get("browserName") != "chromium"
                or browser.get("cdpEndpoint") != controller["cdp_endpoint"]
                or browser.get("isolated") not in (None, False)
                or browser.get("userDataDir")
                or browser.get("remoteEndpoint")
                or browser.get("initPage")
                or browser.get("initScript")):
            fail("anchor config does not match shared CDP controller identity")
        context = browser.get("contextOptions")
        if (not isinstance(context, dict)
                or set(context) - {"viewport"}
                or context.get("viewport") not in (
                    None, {"width": 1280, "height": 720})):
            fail("anchor config contains context-mutating options")
        validate_controller(controller["generation"], ready=False)
        controller.update({
            "phase": "ready", "ready_at": time.time(),
            "anchor_session_id": session,
            "anchor_protocol_version": PROTOCOL,
            "anchor_next_request_id": 4,
        })
        with lock("state.lock"):
            write_controller(controller)


def validate_anchor(controller):
    with lock("state.lock"):
        current = read_controller()
        if (current["phase"] != "ready"
                or current["generation"] != controller["generation"]
                or current["anchor_session_id"]
                != controller["anchor_session_id"]):
            fail("registered ready anchor changed")
        identifier = current["anchor_next_request_id"]
        current["anchor_next_request_id"] += 1
        write_controller(current)
    response, _ = post({
        "jsonrpc": "2.0", "id": identifier, "method": "tools/list",
        "params": {}}, current["generation"], current["anchor_session_id"],
        current["anchor_protocol_version"])
    tools, error = check(response, "anchor health", identifier, valid_tools)
    if error:
        fail(f"anchor health error: {json.dumps(error)}")
    require_tools(
        tools, "anchor health", {"browser_tabs", "browser_get_config"})
    return current


def operation(kind, method, request_id):
    return {"kind": kind, "method": method, "request_id": request_id,
            "started_at": time.time()}


def quarantine(lease, reason):
    lease.update({"phase": "recovery_required", "client": None,
                  "operation": None, "receipt": None, "reason": reason[:1000]})
    write_lease(lease)


def persist_operation(lease, method, kind="request", request_id=True):
    identifier = lease["next_request_id"] if request_id else None
    if request_id:
        lease["next_request_id"] += 1
    lease["operation"] = operation(kind, method, identifier)
    with lock("state.lock"):
        write_lease(lease)
    return identifier


def initialize(lease):
    identifier = persist_operation(lease, "initialize")
    response, session = post({
        "jsonrpc": "2.0", "id": identifier, "method": "initialize",
        "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                   "clientInfo": {"name": "browserctl", "version": "1.0"}}},
        lease["controller_generation"])
    result, error = check(response, "initialize", identifier, valid_init)
    if error:
        fail(f"initialize error: {json.dumps(error)}")
    lease.update({"session_id": session,
                  "protocol_version": result["protocolVersion"]})
    persist_operation(lease, "notifications/initialized",
                      "notification", False)
    post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
         lease["controller_generation"], session, lease["protocol_version"], True)
    identifier = persist_operation(lease, "tools/list")
    response, _ = post(
        {"jsonrpc": "2.0", "id": identifier, "method": "tools/list", "params": {}},
        lease["controller_generation"], session, lease["protocol_version"])
    tools, error = check(response, "acquire tools/list", identifier, valid_tools)
    if error:
        fail(f"acquire tools/list error: {json.dumps(error)}")
    require_tools(tools, "acquire tools/list", {"browser_tabs"})
    identifier = persist_operation(lease, "tools/call browser_tabs")
    response, _ = post({
        "jsonrpc": "2.0", "id": identifier, "method": "tools/call",
        "params": {"name": "browser_tabs", "arguments": {"action": "list"}}},
        lease["controller_generation"], session, lease["protocol_version"])
    result, error = check(response, "acquire browser_tabs", identifier, valid_call)
    if error or result.get("isError"):
        fail(f"acquire browser_tabs failed: {json.dumps(error or result)}")
    lines = [line.strip() for block in result["content"]
             if block["type"] == "text" for line in block["text"].splitlines()]
    if not any(line.startswith("- ") and line[2:].split(":", 1)[0].isdigit()
               for line in lines):
        fail("acquire browser_tabs returned an empty health result")
    validate_controller(lease["controller_generation"],
                        lease["cdp_endpoint"], lease["mcp_url"])
    lease.update({"phase": "ready", "client": None, "operation": None})
    with lock("state.lock"):
        write_lease(lease)
    return lease


def new_lease(owner, controller):
    return {"schema_version": VERSION, "phase": "initializing", "owner": owner,
            "acquired_at": time.time(), "mcp_url": controller["mcp_url"],
            "cdp_endpoint": controller["cdp_endpoint"],
            "controller_generation": controller["generation"],
            "session_id": None, "protocol_version": None, "next_request_id": 1,
            "client": client(), "operation": operation(
                "request", "initialize", 1),
            "receipt": None, "reason": None, "snapshot_ready": False}


def display(lease):
    if lease["phase"] in {"initializing", "in_progress", "closing"}:
        return "in_progress" if alive(lease["client"]) else "recovery_required"
    return lease["phase"]


def controller_matches(lease):
    try:
        validate_controller(lease["controller_generation"],
                            lease["cdp_endpoint"], lease["mcp_url"])
        return True
    except Error:
        return False


def acquire(owner, wait):
    if (not owner or len(owner) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in owner)):
        fail("lease owner must be 1-200 printable characters")
    deadline = time.monotonic() + wait
    while True:
        with lock("state.lock"):
            existing = read_lease(True)
        if existing is None:
            remaining = max(0, deadline - time.monotonic())
            with lock("execution.lock", remaining):
                with lock("state.lock"):
                    existing = read_lease(True)
                if existing is not None:
                    held, phase = existing["owner"], display(existing)
                    continue
                controller = validate_controller()
                controller = validate_anchor(controller)
                lease = new_lease(owner, controller)
                with lock("state.lock"):
                    if read_lease(True) is not None:
                        fail("lease appeared during acquire")
                    write_lease(lease)
                try:
                    lease = initialize(lease)
                except Error as exc:
                    with lock("state.lock"):
                        quarantine(read_lease(), f"acquire failed: {exc}")
                    raise
                output({"acquired": True, "owner": owner,
                        "controller_generation": lease["controller_generation"],
                        "cdp_endpoint": lease["cdp_endpoint"],
                        "session_id": lease["session_id"],
                        "protocol_version": lease["protocol_version"]})
                return
        held, phase = existing["owner"], display(existing)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail(f"lease held by {held!r} in state {phase!r}")
        time.sleep(min(.05, remaining))


def status():
    with lock("state.lock"):
        lease = read_lease(True)
        if lease is None:
            controller = validate_controller()
            validate_config(
                controller["cdp_endpoint"], controller["mcp_url"])
            value = {"state": "available", "available": True}
        else:
            validate_config(lease["cdp_endpoint"], lease["mcp_url"])
            state = display(lease)
            if state == "ready" and not controller_matches(lease):
                state = "recovery_required"
            value = {"state": state, "available": False, "owner": lease["owner"],
                     "phase": lease["phase"],
                     "controller_generation": lease["controller_generation"],
                     "cdp_endpoint": lease["cdp_endpoint"],
                     "session_id": lease["session_id"],
                     "protocol_version": lease["protocol_version"],
                     "next_request_id": lease["next_request_id"]}
            if lease["client"]:
                value.update({"client": lease["client"],
                              "client_alive": alive(lease["client"]),
                              "operation": lease["operation"]})
            if lease["receipt"]:
                value["result"] = {key: lease["receipt"][key] for key in
                                   ("id", "request_id", "method", "exit_code",
                                    "completed_at")}
            if lease["reason"]:
                value["reason"] = lease["reason"]
    output(value)


def require_ready(lease):
    state = display(lease)
    if state == "result_pending":
        fail("completed result pending; run lease result")
    if state != "ready":
        fail(f"lease is {state}; recovery is required")


def receipt(request_id, method, stdout, stderr, code):
    try:
        too_large = (len(stdout.encode()) > MAX_BODY
                     or len(stderr.encode()) > MAX_BODY)
    except UnicodeEncodeError:
        too_large = True
    if too_large:
        stdout = ""
        stderr = (
            "browserctl: MCP operation completed, but its result was too large "
            "or not UTF-8 encodable for the durable receipt; do not replay the "
            "browser action\n")
        code = 1
    return {"id": secrets.token_hex(16), "request_id": request_id,
            "method": method, "stdout": stdout, "stderr": stderr,
            "exit_code": code, "completed_at": time.time()}


def deliver(value):
    try:
        sys.stdout.write(value["stdout"])
        sys.stderr.write(value["stderr"])
        sys.stdout.flush()
        sys.stderr.flush()
        return True
    except (BrokenPipeError, OSError):
        return False


def result():
    with lock("execution.lock"):
        with lock("state.lock"):
            lease = read_lease()
            if lease["phase"] != "result_pending":
                fail("lease has no pending result")
            pending = lease["receipt"]
        if not deliver(pending):
            raise SystemExit(1)
        with lock("state.lock"):
            lease = read_lease()
            if (lease["phase"] != "result_pending"
                    or lease["receipt"]["id"] != pending["id"]):
                fail("pending result changed unexpectedly")
            lease.update({"phase": "ready", "receipt": None})
            write_lease(lease)
        raise SystemExit(pending["exit_code"])


def has_ref_target(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if (key in REF_TARGET_KEYS and isinstance(item, str)
                    and re.fullmatch(r"(?:f\d+)?e\d+", item)):
                return True
            if has_ref_target(item):
                return True
    elif isinstance(value, list):
        return any(has_ref_target(item) for item in value)
    return False


def invalidates_snapshot(name, arguments):
    return (name in INVALIDATE_TOOLS
            or name == "browser_tabs"
            and arguments.get("action") in {"select", "new", "close"})


def call(name=None, arguments=None):
    listing = name is None
    if not listing and name in FORBIDDEN_TOOLS:
        fail(f"tool {name!r} is forbidden by browserctl")
    with lock("execution.lock"):
        with lock("state.lock"):
            lease = read_lease()
            require_ready(lease)
            validate_config(lease["cdp_endpoint"], lease["mcp_url"])
            if (not listing and not lease["snapshot_ready"]
                    and has_ref_target(arguments)):
                fail("snapshot reference targets require a successful "
                     "browser_snapshot in this lease")
            try:
                validate_controller(lease["controller_generation"],
                                    lease["cdp_endpoint"], lease["mcp_url"])
            except Error as exc:
                quarantine(lease, f"controller validation failed: {exc}")
                raise
            identifier = lease["next_request_id"]
            method = "tools/list" if listing else "tools/call"
            operation_name = method if listing else f"{method} {name}"
            params = {} if listing else {"name": name, "arguments": arguments}
            message = {"jsonrpc": "2.0", "id": identifier,
                       "method": method, "params": params}
            body = encode_message(message)
            if not listing and invalidates_snapshot(name, arguments):
                lease["snapshot_ready"] = False
            lease.update({"phase": "in_progress", "client": client(),
                          "next_request_id": identifier + 1,
                          "operation": operation(
                              "request", operation_name, identifier)})
            write_lease(lease)
        response, _ = post(
            message,
            lease["controller_generation"], lease["session_id"],
            lease["protocol_version"], body=body)
        value, rpc_error = check(
            response, method, identifier, valid_tools if listing else valid_call)
        if listing and not rpc_error:
            value["tools"] = [
                tool for tool in value["tools"]
                if tool["name"] not in FORBIDDEN_TOOLS]
        if rpc_error:
            pending = receipt(
                identifier, operation_name, "",
                f"browserctl: {method} error: {json.dumps(rpc_error)}\n", 1)
        elif not listing and value.get("isError"):
            pending = receipt(
                identifier, operation_name, "",
                f"browserctl: Playwright tool error: {json.dumps(value)}\n", 1)
        else:
            pending = receipt(
                identifier, operation_name,
                json.dumps(value, indent=2, sort_keys=True,
                           ensure_ascii=False) + "\n", "", 0)
        with lock("state.lock"):
            current = read_lease()
            if (current["phase"] != "in_progress"
                    or current["operation"]["request_id"] != identifier):
                fail("in-progress state changed unexpectedly")
            if (not listing and name == "browser_snapshot"
                    and not rpc_error and not value.get("isError")):
                current["snapshot_ready"] = True
            current.update({"phase": "result_pending", "client": None,
                            "operation": None, "receipt": pending})
            write_lease(current)
        if not deliver(pending):
            raise SystemExit(1)
        with lock("state.lock"):
            current = read_lease()
            if current["receipt"]["id"] != pending["id"]:
                fail("pending result changed unexpectedly")
            current.update({"phase": "ready", "receipt": None})
            write_lease(current)
        raise SystemExit(pending["exit_code"])


def release():
    with lock("execution.lock"):
        with lock("state.lock"):
            lease = read_lease()
            require_ready(lease)
            validate_config(lease["cdp_endpoint"], lease["mcp_url"])
            try:
                validate_controller(lease["controller_generation"],
                                    lease["cdp_endpoint"], lease["mcp_url"])
            except Error as exc:
                quarantine(lease, f"release validation failed: {exc}")
                raise
            lease.update({"phase": "closing", "client": client(),
                          "operation": operation("delete", "DELETE", None)})
            write_lease(lease)
        delete(lease)
        with lock("state.lock"):
            current = read_lease()
            if current["phase"] != "closing":
                fail("closing state changed unexpectedly")
            remove_lease()
        output({"released": True, "owner": lease["owner"]})


def recover():
    with lock("execution.lock"):
        with lock("state.lock"):
            lease = read_lease()
            validate_config(lease["cdp_endpoint"], lease["mcp_url"])
            phase = display(lease)
            if phase == "in_progress":
                fail("operation is still live")
            if phase == "result_pending":
                fail("completed result pending; run lease result")
            if phase == "ready" and controller_matches(lease):
                fail("ready lease does not need recovery")
            if phase not in {"ready", "recovery_required"}:
                fail(f"lease in state {phase} cannot be recovered")
            controller = validate_controller()
            if controller["generation"] == lease["controller_generation"]:
                fail("controller generation is unchanged")
            if (controller["cdp_endpoint"] != lease["cdp_endpoint"]
                    or controller["mcp_url"] != lease["mcp_url"]):
                fail("recovery controller does not use the same endpoints")
            old = lease["controller_generation"]
        validate_anchor(controller)
        with lock("state.lock"):
            lease = read_lease()
            if (lease["controller_generation"] != old
                    or display(lease) not in {"ready", "recovery_required"}):
                fail("lease changed during recovery validation")
            lease.update({"phase": "initializing",
                          "controller_generation": controller["generation"],
                          "session_id": None, "protocol_version": None,
                          "next_request_id": 1, "client": client(),
                          "operation": operation("request", "initialize", 1),
                          "receipt": None, "reason": None,
                          "snapshot_ready": False})
            write_lease(lease)
        try:
            lease = initialize(lease)
        except Error as exc:
            with lock("state.lock"):
                quarantine(read_lease(), f"recovery failed: {exc}")
            raise
        output({"recovered": True, "owner": lease["owner"],
                "previous_controller_generation": old,
                "controller_generation": lease["controller_generation"],
                "session_id": lease["session_id"],
                "session_state_lost": True,
                "next_action": "inspect tabs and snapshot; do not replay"})


def output(value):
    try:
        print(json.dumps(value, sort_keys=True, ensure_ascii=False), flush=True)
    except (BrokenPipeError, OSError):
        raise SystemExit(1)


def lease_command(arguments):
    if arguments[:1] == ["status"] and len(arguments) == 1:
        status()
    elif arguments[:1] == ["acquire"] and 2 <= len(arguments) <= 3:
        acquire(arguments[1], duration(
            arguments[2] if len(arguments) == 3 else "0",
            "wait", 0, MAX_WAIT))
    elif arguments == ["result"]:
        result()
    elif arguments == ["release"]:
        release()
    elif arguments == ["recover"]:
        recover()
    else:
        fail("usage: client.sh lease acquire OWNER [WAIT_SECONDS] | "
             "lease status|result|release|recover")


def main():
    outer = duration(os.getenv("BROWSERCTL_TIMEOUT", "120"),
                     "outer timeout", 1, 3600)
    if outer <= timeout():
        fail("outer timeout must exceed HTTP timeout")
    if sys.argv[1:3] == ["controller", "register"]:
        register(sys.argv[3:])
    elif sys.argv[1:3] == ["controller", "startup"]:
        controller_startup(sys.argv[3:])
    elif sys.argv[1:2] == ["lease"]:
        lease_command(sys.argv[2:])
    elif sys.argv[1:] == ["list"]:
        call()
    elif len(sys.argv) == 4 and sys.argv[1] == "call":
        try:
            arguments = json.loads(
                sys.argv[3], parse_constant=reject_constant)
        except json.JSONDecodeError as exc:
            fail(f"tool arguments are malformed JSON: {exc}")
        if not sys.argv[2] or not isinstance(arguments, dict):
            fail("tool name must be nonempty and arguments must be an object")
        validate_json(arguments)
        call(sys.argv[2], arguments)
    else:
        fail("usage: client.sh lease ... | list | call TOOL JSON_OBJECT")


if __name__ == "__main__":
    try:
        main()
    except Error as exc:
        print(f"browserctl: {exc}", file=sys.stderr)
        raise SystemExit(1)
