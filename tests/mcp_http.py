"""A small streamable HTTP MCP server for the tests, on a thread (standard library only).

It answers in JSON, or with `sse=True` in an event stream (a notification event, then the
answer). It gives the session id `s-1` at `initialize` and refuses a later POST without it;
with `token`, a request without `Authorization: Bearer TOKEN` is 401. `log` records each
message's method, whether it carried the session id and the protocol version header, and each
`DELETE`.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SESSION = "s-1"


class HttpMcp:
    def __init__(self, *, sse: bool = False, token: str | None = None) -> None:
        self.sse = sse
        self.token = token
        self.log: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # quiet
                pass

            def do_DELETE(self) -> None:
                owner.log.append({"delete": self.headers.get("Mcp-Session-Id")})
                self.send_response(200)
                self.end_headers()

            def do_POST(self) -> None:
                owner.post(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/mcp"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def post(self, request: BaseHTTPRequestHandler) -> None:
        body = json.loads(request.rfile.read(int(request.headers["Content-Length"])))
        method, ident = body.get("method"), body.get("id")
        session = request.headers.get("Mcp-Session-Id")
        self.log.append(
            {
                "method": method,
                "session": session,
                "version": request.headers.get("MCP-Protocol-Version"),
            }
        )
        if (
            self.token is not None
            and request.headers.get("Authorization") != f"Bearer {self.token}"
        ):
            self._reply(request, 401, None)
            return
        if method != "initialize" and session != SESSION:
            self._reply(request, 400, None)
            return
        if ident is None:  # a notification or a response
            self._reply(request, 202, None)
            return
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "http-test", "version": "1"},
            }
        elif method == "tools/list":
            schema = {"type": "object", "properties": {"text": {"type": "string"}}}
            result = {"tools": [{"name": "echo", "description": "echo it", "inputSchema": schema}]}
        elif method == "tools/call":
            text = body["params"]["arguments"].get("text", "")
            result = {"content": [{"type": "text", "text": f"http: {text}"}]}
        else:
            self._reply(
                request,
                200,
                {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "no"}},
            )
            return
        self._reply(request, 200, {"jsonrpc": "2.0", "id": ident, "result": result})

    def _reply(self, request: BaseHTTPRequestHandler, status: int, message: object) -> None:
        request.send_response(status)
        if message is None:
            request.send_header("Content-Length", "0")
            request.end_headers()
            return
        if isinstance(message, dict) and message.get("result", {}).get("serverInfo"):
            request.send_header("Mcp-Session-Id", SESSION)
        if self.sse:
            note = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "hi"}}
            data = f"data: {json.dumps(note)}\n\nevent: message\ndata: {json.dumps(message)}\n\n"
            raw = data.encode()
            request.send_header("Content-Type", "text/event-stream")
        else:
            raw = json.dumps(message).encode()
            request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(raw)))
        request.end_headers()
        request.wfile.write(raw)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
