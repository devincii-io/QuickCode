"""A scriptable MCP server over stdio, for the client tests.

Run as ``python tests/mcp_stub.py``. Behaviour is chosen by environment
variables, so a test states the misbehaviour it wants in the server config
exactly the way a user's settings.json would:

``STUB_TOOLS``      JSON list of tool specs for tools/list (default: one "echo")
``STUB_MANY_TOOLS`` a count: list that many tools, 400-char description each
``STUB_BIG``        bytes of text a call returns (tests the 64 KiB line limit)
``STUB_STDERR``     bytes written to stderr before the handshake is answered
``STUB_PING``       "1": send a server->client ``ping`` reusing the client's id
                    before answering each request, and require the reply
``STUB_CRASH``      "1": exit on every tools/call; a path: exit on the first
                    call only while that file does not exist (then create it)
``STUB_HANG``       "1": never answer tools/call
``STUB_LEAK``       an env var name: print its value to stderr and exit 2
``STUB_CONTENT``    JSON list: the ``content`` a call returns verbatim
``STUB_STRUCTURED`` JSON value: a ``structuredContent`` returned with no content
``STUB_PIDFILE``    a path: write this process's pid there, then spawn a child
                    that sleeps and write the child's pid on the next line
``STUB_VERSION``    the protocolVersion to answer initialize with
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def send(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def ping_first(rid) -> None:
    """A request from the server that reuses the id the client is waiting on."""
    send({"jsonrpc": "2.0", "id": rid, "method": "ping"})
    reply = read()
    if reply.get("id") != rid or "result" not in reply:
        sys.stderr.write(f"bad ping reply: {reply}\n")
        sys.exit(3)


def main() -> None:
    if os.environ.get("STUB_PIDFILE"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open(os.environ["STUB_PIDFILE"], "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}\n{child.pid}\n")
    if os.environ.get("STUB_LEAK"):
        sys.stderr.write(f"auth failed with key {os.environ[os.environ['STUB_LEAK']]}\n")
        sys.exit(2)
    if os.environ.get("STUB_STDERR"):
        sys.stderr.write("e" * int(os.environ["STUB_STDERR"]))
        sys.stderr.flush()
    tools = json.loads(os.environ.get("STUB_TOOLS") or '[{"name": "echo"}]')
    if os.environ.get("STUB_MANY_TOOLS"):
        tools = [{"name": f"t{i}", "description": "d" * 400}
                 for i in range(int(os.environ["STUB_MANY_TOOLS"]))]
    for tool in tools:
        tool.setdefault("inputSchema", {"type": "object", "properties": {}})
    while True:
        msg = read()
        if "id" not in msg:
            continue  # a notification
        rid, method = msg["id"], msg.get("method")
        if os.environ.get("STUB_PING") == "1":
            ping_first(rid)
        if method == "initialize":
            version = os.environ.get("STUB_VERSION") or msg["params"]["protocolVersion"]
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": version, "capabilities": {"tools": {}},
                "serverInfo": {"name": "stub", "version": "1"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}})
        elif method == "tools/call":
            crash = os.environ.get("STUB_CRASH", "")
            if crash == "1":
                sys.exit(1)
            if crash and not os.path.exists(crash):
                open(crash, "w").close()
                sys.exit(1)
            if os.environ.get("STUB_HANG") == "1":
                continue
            result: dict = {"content": [{"type": "text", "text": json.dumps(
                {"tool": msg["params"]["name"], "args": msg["params"].get("arguments"),
                 "cwd": os.getcwd(), "pid": os.getpid()})}]}
            if os.environ.get("STUB_BIG"):
                result = {"content": [{"type": "text", "text": "x" * int(os.environ["STUB_BIG"])}]}
            if os.environ.get("STUB_CONTENT"):
                result = {"content": json.loads(os.environ["STUB_CONTENT"])}
            if os.environ.get("STUB_STRUCTURED"):
                result = {"content": [],
                          "structuredContent": json.loads(os.environ["STUB_STRUCTURED"])}
            send({"jsonrpc": "2.0", "id": rid, "result": result})
        else:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": f"no method {method}"}})


if __name__ == "__main__":
    main()
