"""A small stdio MCP server for the tests (standard library only), from tau-acp's.

Environment: MCP_LOG, a file it appends one JSON line to for each message it receives and for
its start (`{"started": pid}`); PAGE_SIZE, to page `tools/list` over extra tools `tool_0` ..
`tool_4`; TEST_SECRET, which the `env` tool returns; NO_TOOLS, to declare no `tools`
capability; SPAWN_CHILD, to start a `sleep` child and log its pid; FAIL_START, to write it to
stderr and exit 2 before reading anything; HANG_START, to never answer `initialize`. It always
writes a line to stderr when it starts, which τ must not show.
"""

import json
import os
import subprocess
import sys
import threading

LOG = os.environ["MCP_LOG"]
PAGE = int(os.environ.get("PAGE_SIZE", "0"))
NO_TOOLS = bool(os.environ.get("NO_TOOLS"))
WRITE = threading.Lock()
CANCELLED: dict[int, threading.Event] = {}
PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kg"
    "AAAABJRU5ErkJggg=="
)


def log(record):
    with open(LOG, "a") as handle:
        handle.write(json.dumps(record) + "\n")


def send(message):
    with WRITE:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def tool(name, **properties):
    schema = {"type": "object", "properties": {k: {"type": v} for k, v in properties.items()}}
    return {"name": name, "description": f"the {name} tool", "inputSchema": schema}


TOOLS = [
    tool("echo", text="string"),
    tool("fail"),
    tool("slow", seconds="number"),
    tool("image"),
    tool("env"),
    tool("die"),
    tool("structured"),
    tool("ping"),
    tool("grow"),
    tool("dotted.name"),
    tool("garbage"),
    *([tool(f"tool_{n}") for n in range(5)] if PAGE else []),
]


def text(value, error=False):
    return {"content": [{"type": "text", "text": value}], "isError": error}


def slow(ident, seconds):
    event = CANCELLED[ident] = threading.Event()
    if event.wait(seconds):
        return  # cancelled: a server sends no result for a cancelled request
    send({"jsonrpc": "2.0", "id": ident, "result": text("slept")})


def call(ident, name, args):
    if name == "die":
        sys.stderr.write("dying as asked\n")
        sys.stderr.flush()
        os._exit(3)
    if name == "ping":  # a server may ping its client; the answer is in the log
        send({"jsonrpc": "2.0", "id": "ping-1", "method": "ping"})
        return text("pinged")
    if name == "garbage":  # a line that is not JSON, and no result
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
        return None
    if name == "grow":  # a new tool, and the notification that says the list changed
        TOOLS.append(tool("grown"))
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        return text("grew")
    if name == "slow":
        threading.Thread(target=slow, args=(ident, args["seconds"]), daemon=True).start()
        return None
    image = [
        {"type": "text", "text": "before"},
        {"type": "image", "data": PNG, "mimeType": "image/png"},
    ]
    results = {
        "echo": lambda: text(args.get("text", "")),
        "fail": lambda: text("it failed", error=True),
        "image": lambda: {
            "content": [*image, {"type": "audio", "data": "", "mimeType": "audio/wav"}]
        },
        "env": lambda: text(os.environ.get("TEST_SECRET", "unset")),
        "structured": lambda: {"content": [], "structuredContent": {"answer": 42}},
    }
    return results.get(name, lambda: text(f"called {name}"))()


def tools_list(params):
    start = int(params.get("cursor", 0))
    if not PAGE:
        return {"tools": TOOLS}
    more = {"nextCursor": str(start + PAGE)} if start + PAGE < len(TOOLS) else {}
    return {"tools": TOOLS[start : start + PAGE], **more}


def handle(message):
    method, ident, params = message.get("method"), message.get("id"), message.get("params") or {}
    if method is None:  # a response to a request of ours
        return
    if method == "initialize":
        if os.environ.get("HANG_START"):
            return
        result = {
            "protocolVersion": params["protocolVersion"],
            "capabilities": {} if NO_TOOLS else {"tools": {"listChanged": True}},
            "serverInfo": {"name": "test", "version": "1"},
        }
    elif method == "tools/list":
        result = tools_list(params)
    elif method == "tools/call":
        result = call(ident, params["name"], params.get("arguments") or {})
        if result is None:
            return
    elif method == "notifications/cancelled":
        CANCELLED[params["requestId"]].set()
        return
    elif ident is None:
        return
    else:
        send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "no"}})
        return
    send({"jsonrpc": "2.0", "id": ident, "result": result})


sys.stderr.write("test server: starting up\n")
sys.stderr.flush()
if os.environ.get("FAIL_START"):
    sys.stderr.write(os.environ["FAIL_START"] + "\n")
    sys.exit(2)
log({"started": os.getpid(), "cwd": os.getcwd()})
if os.environ.get("SPAWN_CHILD"):  # a grandchild, in our process group
    log({"child": subprocess.Popen(["sleep", "300"]).pid})
for line in sys.stdin:
    message = json.loads(line)
    log(
        {
            "method": message.get("method"),
            "params": message.get("params") or {},
            "id": message.get("id"),
        }
    )
    handle(message)
log({"stdin": "closed"})
