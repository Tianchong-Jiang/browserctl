#!/usr/bin/env python3
"""Minimal leased Streamable HTTP MCP client with strict failures."""

import contextlib
import fcntl
import hashlib
import hmac
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
import urllib.request

URL = os.environ.get("BROWSERCTL_MCP_URL", "http://localhost:47883/mcp")
TIMEOUT = float(os.environ.get("BROWSERCTL_HTTP_TIMEOUT", "30"))
LOCK_TIMEOUT = None
MAX_WAIT = 3600.0
MAX_TTL = 86400.0


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
    stat = path.lstat()
    if path.is_symlink() or not path.is_dir() or stat.st_uid != os.getuid():
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


def lease_path():
    return state_directory() / "lease.json"


def fsync_directory(directory):
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def read_lease():
    path = lease_path()
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
            die("lease state is not a private owner-controlled regular file")
        with os.fdopen(fd, encoding="utf-8") as stream:
            lease = json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read lease state: {exc}")
    required = {"owner", "token_hash", "acquired_at", "expires_at"}
    allowed = required | {"in_flight"}
    numeric = lambda value: type(value) in (int, float) and math.isfinite(value)
    valid = (
        isinstance(lease, dict)
        and set(lease) <= allowed
        and required <= lease.keys()
        and isinstance(lease["owner"], str)
        and 0 < len(lease["owner"]) <= 200
        and isinstance(lease["token_hash"], str)
        and len(lease["token_hash"]) == 64
        and all(char in "0123456789abcdef" for char in lease["token_hash"])
        and numeric(lease["acquired_at"])
        and numeric(lease["expires_at"])
        and lease["expires_at"] >= lease["acquired_at"]
    )
    if valid and "in_flight" in lease:
        operation = lease["in_flight"]
        valid = (
            isinstance(operation, dict)
            and set(operation) == {"id", "method", "started_at"}
            and isinstance(operation["id"], str)
            and len(operation["id"]) == 32
            and all(char in "0123456789abcdef" for char in operation["id"])
            and operation["method"] in {"tools/list", "tools/call"}
            and numeric(operation["started_at"])
        )
    if not valid:
        die("lease state is malformed")
    return lease


def write_lease(lease):
    directory = state_directory()
    fd, temporary = tempfile.mkstemp(prefix=".lease.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(lease, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, lease_path())
        fsync_directory(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def remove_lease():
    try:
        lease_path().unlink()
        fsync_directory(state_directory())
    except FileNotFoundError:
        pass


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def token_matches(lease, token):
    return hmac.compare_digest(lease["token_hash"], token_hash(token))


def active_lease(now=None):
    lease = read_lease()
    if (lease and "in_flight" not in lease
            and lease["expires_at"] <= (time.time() if now is None else now)):
        remove_lease()
        return None
    return lease


def lease_token():
    token = os.environ.get("BROWSERCTL_LEASE_TOKEN")
    if not token:
        die("BROWSERCTL_LEASE_TOKEN is required")
    return token


def acquire(owner, wait, ttl):
    if not owner or len(owner) > 200 or any(ord(char) < 32 for char in owner):
        die("lease owner must be 1-200 printable characters")
    deadline = time.monotonic() + wait
    while True:
        with locked_file("state.lock"):
            now = time.time()
            lease = active_lease(now)
            if lease is None:
                token = secrets.token_urlsafe(32)
                write_lease({
                    "owner": owner,
                    "token_hash": token_hash(token),
                    "acquired_at": now,
                    "expires_at": now + ttl,
                })
                print(token)
                return
            held_by = lease["owner"]
            in_flight = "in_flight" in lease
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if in_flight:
                die(f"lease held by {held_by!r} has an in-flight MCP operation; "
                    "restart the controller, then recover the lease")
            die(f"lease is held by {held_by!r}")
        time.sleep(min(0.05, remaining))


def validate_token(token, allow_in_flight=False, allow_expired=False):
    lease = active_lease()
    if lease is None:
        die("no active lease")
    if not token_matches(lease, token):
        die("lease token is invalid or expired")
    if "in_flight" in lease and not allow_in_flight:
        die("lease has an indeterminate in-flight MCP operation; restart the "
            "controller, then run lease recover --controller-restarted")
    if lease["expires_at"] <= time.time() and not allow_expired:
        die("lease token is invalid or expired")
    return lease


def lease_command(arguments):
    if not arguments:
        die("usage: client.sh lease acquire|renew|status|release ...")
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
        token = lease_token()
        with locked_file("state.lock"):
            lease = validate_token(token)
            lease["expires_at"] = time.time() + ttl
            write_lease(lease)
        print(json.dumps({"owner": lease["owner"], "expires_at": lease["expires_at"]}))
    elif action == "status" and len(arguments) == 1:
        with locked_file("state.lock"):
            lease = active_lease()
        if lease:
            print(json.dumps({
                "available": False,
                "owner": lease["owner"],
                "expires_at": lease["expires_at"],
                "expires_in": max(0, lease["expires_at"] - time.time()),
                "in_flight": "in_flight" in lease,
            }))
        else:
            print(json.dumps({"available": True}))
    elif action == "release" and len(arguments) == 1:
        token = lease_token()
        with locked_file("execution.lock"):
            with locked_file("state.lock"):
                lease = validate_token(token)
                remove_lease()
        print(json.dumps({"released": True, "owner": lease["owner"]}))
    elif (action == "recover" and 2 <= len(arguments) <= 3
          and arguments[1] == "--controller-restarted"):
        ttl = parse_duration(arguments[2] if len(arguments) == 3 else "300",
                             "TTL", 0.1, MAX_TTL)
        token = lease_token()
        with locked_file("execution.lock"):
            with locked_file("state.lock"):
                lease = validate_token(
                    token, allow_in_flight=True, allow_expired=True)
                if "in_flight" not in lease:
                    die("lease has no indeterminate operation to recover")
                del lease["in_flight"]
                lease["expires_at"] = time.time() + ttl
                write_lease(lease)
        print(json.dumps({"recovered": True, "owner": lease["owner"]}))
    else:
        die("usage: client.sh lease acquire OWNER [WAIT_SECONDS] [TTL_SECONDS] | "
            "lease renew [TTL_SECONDS] | lease status | lease release | "
            "lease recover --controller-restarted [TTL_SECONDS]")


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
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
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
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode(errors="replace")
        die(f"MCP session DELETE HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        die(f"MCP session DELETE failed: {exc}")


def checked(response, operation):
    if not isinstance(response, dict):
        die(f"malformed {operation} response")
    if "error" in response:
        die(f"{operation} JSON-RPC error: {json.dumps(response['error'])}")
    if "result" not in response:
        die(f"malformed {operation} response: missing result")
    return response["result"]


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "lease":
        lease_command(sys.argv[2:])
        return
    if len(sys.argv) == 2 and sys.argv[1] == "list":
        method, params = "tools/list", {}
    elif len(sys.argv) == 4 and sys.argv[1] == "call":
        try:
            arguments = json.loads(sys.argv[3])
        except json.JSONDecodeError as exc:
            die(f"tool arguments are malformed JSON: {exc}")
        if not isinstance(arguments, dict):
            die("tool arguments must be a JSON object")
        method = "tools/call"
        params = {"name": sys.argv[2], "arguments": arguments}
    else:
        die("usage: client.sh lease ... | client.sh list | "
            "client.sh call TOOL JSON_OBJECT")

    token = lease_token()
    with locked_file("execution.lock"):
        with locked_file("state.lock"):
            validate_token(token)
        initialized, session = post({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "browserctl", "version": "0.1.0"},
            },
        })
        checked(initialized, "initialize")
        if not session:
            die("initialize response omitted Mcp-Session-Id")
        post(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            session,
            response_required=False,
        )
        operation_id = secrets.token_hex(16)
        with locked_file("state.lock"):
            lease = validate_token(token)
            lease["in_flight"] = {
                "id": operation_id,
                "method": method,
                "started_at": time.time(),
            }
            write_lease(lease)
        response, _ = post(
            {"jsonrpc": "2.0", "id": 2, "method": method, "params": params}, session
        )
        delete_session(session)
        with locked_file("state.lock"):
            lease = validate_token(
                token, allow_in_flight=True, allow_expired=True)
            if lease.get("in_flight", {}).get("id") != operation_id:
                die("lease in-flight state changed unexpectedly")
            del lease["in_flight"]
            write_lease(lease)
        result = checked(response, method)
    if method == "tools/call":
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            die("malformed tools/call result")
        if result.get("isError") is True:
            die(f"Playwright tool error: {json.dumps(result)}")
    elif not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        die("malformed tools/list result")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
