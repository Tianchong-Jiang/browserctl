#!/usr/bin/env python3
"""Minimal Streamable HTTP MCP client with strict failure propagation."""

import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("BROWSERCTL_MCP_URL", "http://localhost:47883/mcp")
TIMEOUT = float(os.environ.get("BROWSERCTL_HTTP_TIMEOUT", "30"))


def die(message):
    print(f"browserctl: {message}", file=sys.stderr)
    raise SystemExit(1)


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
            if not response_required and not body:
                return None, new_session
            return decode(body, response.headers.get("Content-Type", "")), new_session
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode(errors="replace")
        die(f"MCP HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        die(f"MCP request failed: {exc}")


def checked(response, operation):
    if not isinstance(response, dict):
        die(f"malformed {operation} response")
    if "error" in response:
        die(f"{operation} JSON-RPC error: {json.dumps(response['error'])}")
    if "result" not in response:
        die(f"malformed {operation} response: missing result")
    return response["result"]


def main():
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
        die("usage: client.sh list | client.sh call TOOL JSON_OBJECT")

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
    response, _ = post(
        {"jsonrpc": "2.0", "id": 2, "method": method, "params": params}, session
    )
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
