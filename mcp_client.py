#!/usr/bin/env python3
"""Minimal tokenless leased Streamable HTTP MCP client with strict failures."""

import base64
import binascii
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

URL = os.environ.get("BROWSERCTL_MCP_URL", "http://127.0.0.1:47883/mcp")
TIMEOUT = float(os.environ.get("BROWSERCTL_HTTP_TIMEOUT", "30"))
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
LOCK_TIMEOUT = None
MAX_WAIT = 3600.0
MAX_TTL = 86400.0
MAX_SEQUENCE_CALLS = 10


def die(message):
    print(f"browserctl: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_duration(value, name, minimum, maximum):
    try:
        result = float(value)
    except ValueError:
        die(f"{name} must be a number")
    if not math.isfinite(result) or not minimum <= result <= maximum:
        die(f"{name} must be between {minimum:g} and {maximum:g} seconds")
    return result


LOCK_TIMEOUT = parse_duration(
    os.environ.get("BROWSERCTL_LOCK_TIMEOUT", "45"),
    "lock timeout", 0.1, MAX_WAIT)


def state_directory():
    configured = os.environ.get("BROWSERCTL_STATE_DIR")
    if configured:
        path = Path(configured)
    elif os.environ.get("XDG_RUNTIME_DIR"):
        path = Path(os.environ["XDG_RUNTIME_DIR"]) / "browserctl"
    else:
        path = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        path /= "browserctl"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path_stat = path.lstat()
    if path.is_symlink() or not path.is_dir() or path_stat.st_uid != os.getuid():
        die(f"unsafe state directory: {path}")
    os.chmod(path, 0o700)
    return path


@contextlib.contextmanager
def locked_file(name):
    path = state_directory() / name
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        die(f"cannot open lock {path}: {exc}")
    try:
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    die(f"timed out waiting for {name}")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def state_path(name):
    return state_directory() / name


def fsync_directory(directory):
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def read_private_json(name, missing_ok=False):
    path = state_path(name)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
        file_stat = os.fstat(fd)
        if (not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.getuid()
                or file_stat.st_mode & 0o077):
            os.close(fd)
            die(f"{name} is not a private owner-controlled regular file")
        with os.fdopen(fd, encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        if missing_ok:
            return None
        die(f"{name} is missing")
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read {name}: {exc}")


def write_private_json(name, value):
    directory = state_directory()
    fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state_path(name))
        fsync_directory(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def remove_state(name):
    try:
        state_path(name).unlink()
        fsync_directory(state_directory())
    except FileNotFoundError:
        pass


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def valid_operation(operation, require_generation):
    required = {"id", "method", "started_at"}
    if require_generation:
        required.add("controller_generation")
    valid = (
        isinstance(operation, dict)
        and set(operation) == required
        and isinstance(operation.get("id"), str)
        and len(operation["id"]) == 32
        and all(char in "0123456789abcdef" for char in operation["id"])
        and operation.get("method") in {"tools/list", "tools/call"}
        and numeric(operation.get("started_at"))
    )
    if require_generation:
        valid = (
            valid
            and isinstance(operation.get("controller_generation"), str)
            and len(operation["controller_generation"]) == 32
            and all(char in "0123456789abcdef"
                    for char in operation["controller_generation"])
        )
    return valid


def valid_common_lease(lease, allowed, required):
    return (
        isinstance(lease, dict)
        and set(lease) <= allowed
        and required <= lease.keys()
        and isinstance(lease.get("owner"), str)
        and 0 < len(lease["owner"]) <= 200
        and numeric(lease.get("acquired_at"))
        and numeric(lease.get("expires_at"))
        and lease["expires_at"] >= lease["acquired_at"]
    )


def read_lease():
    lease = read_private_json("lease.json", missing_ok=True)
    if lease is None:
        return None

    required = {"schema_version", "owner", "acquired_at", "expires_at"}
    allowed = required | {"in_flight"}
    valid = (
        valid_common_lease(lease, allowed, required)
        and lease["schema_version"] == 1
        and ("in_flight" not in lease
             or valid_operation(lease["in_flight"], require_generation=True))
    )
    if valid:
        return lease

    legacy_required = {"owner", "token_hash", "acquired_at", "expires_at"}
    legacy_allowed = legacy_required | {"in_flight"}
    legacy = (
        valid_common_lease(lease, legacy_allowed, legacy_required)
        and isinstance(lease["token_hash"], str)
        and len(lease["token_hash"]) == 64
        and all(char in "0123456789abcdef" for char in lease["token_hash"])
        and ("in_flight" not in lease
             or valid_operation(lease["in_flight"], require_generation=False))
    )
    if not legacy:
        die("lease state is malformed")
    if "in_flight" in lease:
        die("legacy lease has an indeterminate operation but no controller "
            "generation; restart the controller and perform manual operator "
            "recovery before using this state")

    migrated = {
        "schema_version": 1,
        "owner": lease["owner"],
        "acquired_at": lease["acquired_at"],
        "expires_at": lease["expires_at"],
    }
    write_lease(migrated)
    return migrated


def write_lease(lease):
    write_private_json("lease.json", lease)


def remove_lease():
    remove_state("lease.json")


def active_lease(now=None):
    lease = read_lease()
    if (lease and "in_flight" not in lease
            and lease["expires_at"] <= (time.time() if now is None else now)):
        remove_lease()
        return None
    return lease


def require_lease(allow_in_flight=False, allow_expired=False):
    lease = active_lease()
    if lease is None:
        die("no active singleton lease")
    if "in_flight" in lease and not allow_in_flight:
        die("lease has an indeterminate in-flight MCP operation; restart the "
            "controller, then run lease recover")
    if lease["expires_at"] <= time.time() and not allow_expired:
        die("lease is expired")
    return lease


def acquire(owner, wait, ttl):
    if not owner or len(owner) > 200 or any(ord(char) < 32 for char in owner):
        die("lease owner must be 1-200 printable characters")
    deadline = time.monotonic() + wait
    while True:
        with locked_file("state.lock"):
            now = time.time()
            lease = active_lease(now)
            if lease is None:
                lease = {
                    "schema_version": 1,
                    "owner": owner,
                    "acquired_at": now,
                    "expires_at": now + ttl,
                }
                write_lease(lease)
                print(json.dumps({
                    "acquired": True,
                    "owner": owner,
                    "acquired_at": now,
                    "expires_at": now + ttl,
                }, sort_keys=True))
                return
            held_by = lease["owner"]
            in_flight = "in_flight" in lease
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if in_flight:
                die(f"lease held by {held_by!r} requires recovery for an "
                    "in-flight MCP operation")
            die(f"lease is held by {held_by!r}")
        time.sleep(min(0.05, remaining))


def process_identity(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = text.rsplit(")", 1)[1].strip().split()
        return fields[19]
    except (FileNotFoundError, IndexError, OSError):
        return None


def parse_controller(value):
    required = {
        "schema_version", "generation", "pid", "process_start_time",
        "mcp_url", "registered_at",
    }
    valid = (
        isinstance(value, dict)
        and set(value) == required
        and value.get("schema_version") == 1
        and isinstance(value.get("generation"), str)
        and len(value["generation"]) == 32
        and all(char in "0123456789abcdef" for char in value["generation"])
        and type(value.get("pid")) is int
        and value["pid"] > 1
        and isinstance(value.get("process_start_time"), str)
        and value["process_start_time"].isdigit()
        and isinstance(value.get("mcp_url"), str)
        and numeric(value.get("registered_at"))
    )
    if not valid:
        die("controller generation state is malformed")
    return value


def read_controller(missing_ok=False):
    value = read_private_json("controller.json", missing_ok=missing_ok)
    return None if value is None else parse_controller(value)


def controller_process_alive(controller):
    return process_identity(controller["pid"]) == controller["process_start_time"]


def endpoint():
    parsed = urllib.parse.urlsplit(URL)
    if (parsed.scheme != "http" or parsed.path != "/mcp"
            or parsed.query or parsed.fragment
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.port is None):
        die("BROWSERCTL_MCP_URL must use explicit 127.0.0.1 or ::1 with "
            "http://.../mcp and an explicit port")
    return parsed


def listening_inodes(address, port):
    found = set()
    if address == "127.0.0.1":
        table, encoded_address = "/proc/net/tcp", "0100007F"
    else:
        table = "/proc/net/tcp6"
        encoded_address = "00000000000000000000000001000000"
    try:
        lines = Path(table).read_text(encoding="ascii").splitlines()[1:]
    except OSError as exc:
        die(f"cannot inspect controller listeners: {exc}")
    for line in lines:
        fields = line.split()
        if len(fields) > 9:
            local_address, local_port = fields[1].rsplit(":", 1)
            if (local_address == encoded_address
                    and int(local_port, 16) == port
                    and fields[3] == "0A"):
                found.add(fields[9])
    return found


def process_tree(root_pid):
    parents = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text(encoding="utf-8")
            fields = text.rsplit(")", 1)[1].strip().split()
            parents[int(entry.name)] = int(fields[1])
        except (FileNotFoundError, IndexError, OSError, ValueError):
            continue
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return descendants


def process_tree_owns_listener(root_pid, address, port):
    inodes = listening_inodes(address, port)
    if not inodes:
        return False
    for pid in process_tree(root_pid):
        try:
            descriptors = Path(f"/proc/{pid}/fd").iterdir()
            for descriptor in descriptors:
                try:
                    target = os.readlink(descriptor)
                except (FileNotFoundError, PermissionError, OSError):
                    continue
                if (target.startswith("socket:[") and target.endswith("]")
                        and target[8:-1] in inodes):
                    return True
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return False


def validated_controller():
    controller = read_controller()
    parsed = endpoint()
    if controller["mcp_url"] != URL:
        die("controller generation belongs to a different MCP URL")
    if not controller_process_alive(controller):
        die("recorded controller process is missing or was replaced")
    if not process_tree_owns_listener(
            controller["pid"], parsed.hostname, parsed.port):
        die("recorded controller process tree does not own the exact MCP "
            "listener address and port")
    return controller


def register_controller(arguments):
    if len(arguments) != 2:
        die("internal controller registration usage error")
    try:
        pid = int(arguments[0])
    except ValueError:
        die("controller PID is invalid")
    if pid != os.getppid():
        die("controller registration PID must be the registering process parent")
    parsed = endpoint()
    if arguments[1] != URL or parsed.geturl() != URL:
        die("controller registration URL mismatch")
    configured_host = os.environ.get("BROWSERCTL_CONTROLLER_HOST")
    if parsed.hostname != configured_host:
        die("controller registration URL and bind host differ")
    try:
        configured_port = int(os.environ["BROWSERCTL_CONTROLLER_PORT"])
    except (KeyError, ValueError):
        die("controller registration port is invalid")
    if parsed.port != configured_port:
        die("controller registration URL and listening port differ")
    start_time = process_identity(pid)
    if start_time is None:
        die("controller launcher process is missing")
    with locked_file("execution.lock"):
        with locked_file("state.lock"):
            previous = read_controller(missing_ok=True)
            if previous is not None and controller_process_alive(previous):
                die("the previously registered controller process is still alive")
            write_private_json("controller.json", {
                "schema_version": 1,
                "generation": secrets.token_hex(16),
                "pid": pid,
                "process_start_time": start_time,
                "mcp_url": arguments[1],
                "registered_at": time.time(),
            })


def lease_status():
    now = time.time()
    lease = read_lease()
    reclaimed = False
    if lease and "in_flight" not in lease and lease["expires_at"] <= now:
        remove_lease()
        lease = None
        reclaimed = True
    if lease is None:
        return {
            "state": "available",
            "available": True,
            "expired": False,
            "in_flight": False,
            "recovery_required": False,
            "reclaimed_expired_lease": reclaimed,
        }
    expired = lease["expires_at"] <= now
    in_flight = "in_flight" in lease
    status = {
        "state": "recovery_required" if in_flight else "held",
        "available": False,
        "owner": lease["owner"],
        "acquired_at": lease["acquired_at"],
        "expires_at": lease["expires_at"],
        "expired": expired,
        "in_flight": in_flight,
        "recovery_required": in_flight,
    }
    if in_flight:
        status["in_flight_operation"] = lease["in_flight"]
    else:
        status["expires_in"] = max(0, lease["expires_at"] - now)
    return status


def lease_command(arguments):
    if not arguments:
        die("usage: client.sh lease acquire|renew|status|release|recover ...")
    action = arguments[0]
    if action == "acquire" and 2 <= len(arguments) <= 4:
        wait = parse_duration(arguments[2] if len(arguments) > 2 else "0",
                              "wait", 0, MAX_WAIT)
        ttl = parse_duration(arguments[3] if len(arguments) > 3 else "300",
                             "TTL", 0.1, MAX_TTL)
        acquire(arguments[1], wait, ttl)
    elif action == "renew" and len(arguments) <= 2:
        ttl = parse_duration(arguments[1] if len(arguments) == 2 else "300",
                             "TTL", 0.1, MAX_TTL)
        with locked_file("state.lock"):
            lease = require_lease()
            lease["expires_at"] = time.time() + ttl
            write_lease(lease)
        print(json.dumps({
            "renewed": True,
            "owner": lease["owner"],
            "expires_at": lease["expires_at"],
        }, sort_keys=True))
    elif action == "status" and len(arguments) == 1:
        with locked_file("state.lock"):
            status = lease_status()
        print(json.dumps(status, sort_keys=True))
    elif action == "release" and len(arguments) == 1:
        with locked_file("execution.lock"):
            with locked_file("state.lock"):
                lease = require_lease()
                remove_lease()
        print(json.dumps({
            "released": True,
            "owner": lease["owner"],
        }, sort_keys=True))
    elif action == "recover" and len(arguments) <= 2:
        ttl = parse_duration(arguments[1] if len(arguments) == 2 else "300",
                             "TTL", 0.1, MAX_TTL)
        with locked_file("execution.lock"):
            with locked_file("state.lock"):
                lease = require_lease(
                    allow_in_flight=True, allow_expired=True)
                operation = lease.get("in_flight")
                if operation is None:
                    die("lease has no indeterminate operation to recover")
                operation_id = operation["id"]
                old_generation = operation["controller_generation"]
            controller = validated_controller()
            if controller["generation"] == old_generation:
                die("controller generation is unchanged; restart the controller "
                    "that could have executed the operation")
            functional_health_check()
            with locked_file("state.lock"):
                lease = require_lease(
                    allow_in_flight=True, allow_expired=True)
                current = lease.get("in_flight", {})
                if (current.get("id") != operation_id
                        or current.get("controller_generation")
                        != old_generation):
                    die("lease in-flight state changed unexpectedly")
                del lease["in_flight"]
                lease["expires_at"] = time.time() + ttl
                write_lease(lease)
        print(json.dumps({
            "recovered": True,
            "owner": lease["owner"],
            "controller_generation": controller["generation"],
            "expires_at": lease["expires_at"],
        }, sort_keys=True))
    else:
        die("usage: client.sh lease acquire OWNER [WAIT_SECONDS] [TTL_SECONDS] | "
            "lease renew [TTL_SECONDS] | lease status | lease release | "
            "lease recover [TTL_SECONDS]")


def decode(body, content_type):
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        die(f"MCP returned non-UTF-8 output: {exc}")
    payloads = []
    if "text/event-stream" in content_type:
        current = []
        for line in text.splitlines() + [""]:
            if line.startswith("data:"):
                current.append(line[5:].lstrip())
            elif not line and current:
                payloads.append("\n".join(current))
                current = []
    elif text.strip():
        payloads.append(text)
    for payload in reversed(payloads):
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    die("MCP returned empty or malformed JSON")


def post(message, session=None, response_required=True):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(
        URL, json.dumps(message, separators=(",", ":")).encode(), headers
    )
    try:
        with HTTP.open(request, timeout=TIMEOUT) as response:
            body = response.read()
            new_session = response.headers.get("Mcp-Session-Id", session)
            if not response_required:
                return None, new_session
            return decode(body, response.headers.get("Content-Type", "")), new_session
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode(errors="replace")
        die(f"MCP HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        die(f"MCP request failed: {exc}")


def delete_session(session):
    request = urllib.request.Request(
        URL,
        headers={
            "Mcp-Session-Id": session,
            "Mcp-Protocol-Version": "2025-03-26",
        },
        method="DELETE",
    )
    try:
        with HTTP.open(request, timeout=TIMEOUT) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode(errors="replace")
        die(f"MCP session DELETE HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        die(f"MCP session DELETE failed: {exc}")


def valid_list_result(result):
    return (
        isinstance(result, dict)
        and isinstance(result.get("tools"), list)
        and all(isinstance(tool, dict) for tool in result["tools"])
    )


def valid_call_result(result):
    return (
        isinstance(result, dict)
        and isinstance(result.get("content"), list)
        and all(valid_content_block(item) for item in result["content"])
        and ("isError" not in result or type(result["isError"]) is bool)
    )


def valid_content_block(item):
    if not isinstance(item, dict):
        return False
    if item.get("type") == "text":
        return isinstance(item.get("text"), str)
    if item.get("type") == "image":
        if (item.get("mimeType") not in {"image/png", "image/jpeg"}
                or not isinstance(item.get("data"), str)
                or not item["data"]):
            return False
        try:
            base64.b64decode(item["data"], validate=True)
        except (binascii.Error, ValueError):
            return False
        return True
    return False


def checked(response, operation, request_id, result_validator):
    if (not isinstance(response, dict)
            or response.get("jsonrpc") != "2.0"
            or type(response.get("id")) is not type(request_id)
            or response.get("id") != request_id):
        die(f"malformed or mismatched {operation} JSON-RPC response")
    has_result = "result" in response
    has_error = "error" in response
    if has_result == has_error:
        die(f"malformed {operation} JSON-RPC result/error envelope")
    expected = {"jsonrpc", "id", "result" if has_result else "error"}
    if set(response) != expected:
        die(f"malformed {operation} JSON-RPC response fields")
    if has_error:
        error = response["error"]
        if (not isinstance(error, dict)
                or not {"code", "message"} <= error.keys()
                or not set(error) <= {"code", "message", "data"}
                or type(error["code"]) is not int
                or not isinstance(error["message"], str)):
            die(f"malformed {operation} JSON-RPC error")
        return None, error
    if not result_validator(response["result"]):
        die(f"malformed {operation} result")
    return response["result"], None


def fail_rpc_error(operation, error):
    die(f"{operation} JSON-RPC error: {json.dumps(error)}")


def parse_sequence(value):
    try:
        calls = json.loads(value)
    except json.JSONDecodeError as exc:
        die(f"sequence is malformed JSON: {exc}")
    if not isinstance(calls, list):
        die("sequence must be a JSON array")
    if not 1 <= len(calls) <= MAX_SEQUENCE_CALLS:
        die(f"sequence must contain 1-{MAX_SEQUENCE_CALLS} tool calls")
    for index, call in enumerate(calls, 1):
        if not isinstance(call, dict) or set(call) != {"name", "arguments"}:
            die(f"sequence item {index} must contain exactly name and arguments")
        if not isinstance(call["name"], str) or not call["name"]:
            die(f"sequence item {index} name must be a nonempty string")
        if not isinstance(call["arguments"], dict):
            die(f"sequence item {index} arguments must be a JSON object")
    return calls


def initialize_session():
    initialized, session = post({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "browserctl", "version": "0.2.0"},
        },
    })
    _, error = checked(
        initialized, "initialize", 1, lambda result: isinstance(result, dict))
    if error is not None:
        fail_rpc_error("initialize", error)
    if not session:
        die("initialize response omitted Mcp-Session-Id")
    post(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        session,
        response_required=False,
    )
    return session


def functional_health_check():
    session = initialize_session()
    response, _ = post(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        session,
    )
    _, error = checked(
        response, "controller health tools/list", 2, valid_list_result)
    if error is not None:
        delete_session(session)
        fail_rpc_error("controller health tools/list", error)
    response, _ = post({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "browser_tabs",
            "arguments": {"action": "list"},
        },
    }, session)
    result, error = checked(
        response, "controller health browser_tabs", 3, valid_call_result)
    delete_session(session)
    if error is not None:
        fail_rpc_error("controller health browser_tabs", error)
    if result.get("isError") is True:
        die(f"controller browser health probe failed: {json.dumps(result)}")


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "controller":
        if len(sys.argv) >= 3 and sys.argv[2] == "register":
            register_controller(sys.argv[3:])
            return
        die("internal controller command is invalid")
    if len(sys.argv) >= 2 and sys.argv[1] == "lease":
        lease_command(sys.argv[2:])
        return
    if len(sys.argv) == 2 and sys.argv[1] == "list":
        operations = [("tools/list", {})]
        sequence = False
    elif len(sys.argv) == 4 and sys.argv[1] == "call":
        try:
            arguments = json.loads(sys.argv[3])
        except json.JSONDecodeError as exc:
            die(f"tool arguments are malformed JSON: {exc}")
        if not isinstance(arguments, dict):
            die("tool arguments must be a JSON object")
        operations = [(
            "tools/call",
            {"name": sys.argv[2], "arguments": arguments},
        )]
        sequence = False
    elif len(sys.argv) == 3 and sys.argv[1] == "sequence":
        calls = parse_sequence(sys.argv[2])
        operations = [
            ("tools/call", {"name": call["name"], "arguments": call["arguments"]})
            for call in calls
        ]
        sequence = True
    else:
        die("usage: client.sh lease ... | client.sh list | "
            "client.sh call TOOL JSON_OBJECT | "
            "client.sh sequence JSON_ARRAY")

    with locked_file("execution.lock"):
        with locked_file("state.lock"):
            require_lease()
        controller = validated_controller()
        session = initialize_session()
        operation_id = secrets.token_hex(16)
        with locked_file("state.lock"):
            lease = require_lease()
            lease["in_flight"] = {
                "id": operation_id,
                "method": operations[0][0],
                "started_at": time.time(),
                "controller_generation": controller["generation"],
            }
            write_lease(lease)
        results = []
        determinate_error = None
        for item, (method, params) in enumerate(operations, 1):
            request_id = item + 1
            operation = (
                f"sequence item {item} tools/call"
                if sequence else method
            )
            response, response_session = post({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }, session)
            if response_session != session:
                die(f"{operation} changed the MCP session ID")
            validator = (
                valid_call_result if method == "tools/call"
                else valid_list_result
            )
            result, error = checked(
                response, operation, request_id, validator)
            if error is not None:
                determinate_error = ("rpc", operation, error)
                break
            results.append(result)
            if method == "tools/call" and result.get("isError") is True:
                determinate_error = ("tool", operation, result)
                break
        delete_session(session)
        with locked_file("state.lock"):
            lease = require_lease(
                allow_in_flight=True, allow_expired=True)
            if lease.get("in_flight", {}).get("id") != operation_id:
                die("lease in-flight state changed unexpectedly")
            del lease["in_flight"]
            write_lease(lease)
        if determinate_error is not None:
            kind, operation, error = determinate_error
            if kind == "rpc":
                fail_rpc_error(operation, error)
            if sequence:
                die(f"Playwright tool error in {operation}: "
                    f"{json.dumps(error)}")
            die(f"Playwright tool error: {json.dumps(error)}")
    output = results if sequence else results[0]
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
