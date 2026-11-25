# D29: The HTTP edge (a second transport for `tau serve`)

**Status:** accepted, built, released in 0.17.0 (planned as 0.16.1, which was not released). **Amends** D21 §2 ("No TCP in 0.16") and **closes** D21
Open 4 (TCP for browsers). D21's messages, `PROTOCOL = 1` and `docs/protocol.schema.json` do not
change: this is framing and access only.

**Amended 2026-11-24 (0.17.1, unreleased):** §9. `ffwf-tau` now depends on `ffwf-tau-web`, so the
web client is served by default; `--no-web` and `HttpEdge(web=False)` turn it off; a missing
`tau_web` is a warned, served-as-such broken install, not a startup error. Lockstep versions,
`PROTOCOL` only on a minor bump, and the release order are rules (§9). Open 4 is closed.

## Context

D21 serves a session on a Unix socket, with the file permissions as the only access control, and
deferred TCP to tau-code's web client (Open 4). tau-code's port to 0.16 needs a browser page (served
by a dev server, later by an editor's webview) to drive a session, and a session in a container to
be reached from outside it. A browser cannot dial a Unix socket, and Windows' asyncio serves none.

The choices were made with tau-code's port plan (2026-11-20): SSE plus POST rather than a WebSocket
over TCP, because a WebSocket's upgrade is the part that breaks first across proxies, ingresses and
port forwards; both transports on by default; a bearer token kept in the serve record; machines are
single-user, so the token file's mode (0600) is the whole local secret.

## Decision

### 1. Two transports, both on by default

`tau serve` listens on its Unix socket (unchanged) **and** an HTTP edge. `--no-unix` and `--no-http`
turn one off; both off is an error, as are flags that configure what is off (`--socket` with
`--no-unix`; `--port`, `--host`, `--allow-origin` with `--no-http`). On Windows the default is the
edge alone and `--unix` is an error (see Open 1: that path is not yet built or tested). No
in-tree caller changes transport: the TUI, `-p`, `--stop`, ratatui and the relay keep the socket
whenever it is on, and a client attaches over HTTP only to a server whose record has no socket.

Library: `Server.start(driver, path=None, *, session, cwd, idle=None, http=None)` listens on the
socket `path`, the edge `http` (a `tau.HttpEdge`), or both; `server.url` and `server.token` are the
edge's. `Started.serve(..., http=None)` adds an edge to an embedded server.

### 2. The framing

| request | answer |
|---|---|
| `GET /stream` | `200`, `text/event-stream`: the **stream**. First event `event: connection`, data the **connection id**; then each message the socket would send (responses, `event`, `end`) as one `data:` line, in the socket's order. Comments `: ping` are the heartbeat (§5). |
| `POST /stream/<id>` | the body is one JSON-RPC request. `202` once it is queued on that stream (its response comes on the stream); `404` with a JSON-RPC error body (`InvalidRequest`, the request's `id` if readable) when no open stream has that id; `400` for a body that is not UTF-8; `413` past 64 MiB. |
| `GET /stream/<id>` | `409`: there is no resume, so no second stream may claim an id. |
| any other `GET` or `HEAD` | the **web client**'s files, or the **help page** at `/` (§9). No token. |
| anything else | `405` with `Allow`; `400` (or `431`) for what is not an HTTP/1.1 request. |

The origin and the token are checked on a request's head, before its body is read, so a refused
request costs the server nothing; a head or a body that takes more than 30 seconds to arrive
ends the connection. `--host localhost` is bound as `127.0.0.1`, so the URL names one port.

`hello` is the first POST, after the stream opens, and its snapshot events come before its
response, exactly as on the socket. Calls begin in the order their POSTs are accepted; the one
writer per connection (D21 §3) sends every event a response depends on before the response. `end`,
`CutOff` and a failed driver end the stream as they end a socket connection, and the server then
ends the response. A reconnect is a new stream and a new `hello`. Events carry no SSE `id:`, so a
browser's automatic `Last-Event-ID` reconnection is not offered (there is nothing to resume).

### 3. Access

- **Bearer token.** Every request to the wire (`/stream`, `/stream/<id>`) but a CORS preflight
  carries `Authorization: Bearer <token>`;
  missing or wrong is `401` (`WWW-Authenticate: Bearer`), compared in constant time. The server
  makes a fresh token (`secrets.token_urlsafe(32)`) per start unless the library caller passes one.
- **Origin.** A request with an `Origin` header is refused (`403`) unless it is the edge's **own
  origin** (`http://<host>:<port>`, and on loopback both `127.0.0.1` and `localhost`: the web client
  the edge served, calling home; browsers send `Origin` on a same-origin POST) or `--allow-origin`
  lists it; none is listed by default. So a web page the user merely visits cannot drive a session
  even if it guesses the port. A client served from elsewhere (tau-code's dev server) is either
  listed or reaches τ through a same-origin proxy. An allowed origin gets
  `Access-Control-Allow-Origin` and a preflight answer. Behind `--allow-network` on `0.0.0.0` the
  name a browser uses is not known to the edge: list it with `--allow-origin`.
- **The web client's files** are not secret and carry no session data, so they need no token (§9).
- **Bind.** Loopback (`127.0.0.1`) by default. `--host` that is not loopback needs
  `--allow-network`; then anyone who can reach the port and has the token is in. TLS is not
  offered: across machines, put the edge behind something that terminates it.
- **Threat model.** Single-user machines. The token lives in the serve record (mode 600, in the
  700 serve directory) and on the user's own terminal (§7). Another local user can knock on the
  port and gets `401`. No multi-user hardening beyond that.
- **The Python client** does not take proxies from the environment (`trust_env=False`): the token
  goes only to the URL the record names.

### 4. The port

- `--port N` (N > 0) is strict: bind N, or fail and exit naming the address (`cannot listen on
  127.0.0.1:N: ...`) with the socket unbound and no record written.
- No `--port`, or `--port 0`: try **3393** ("ffwf" on a phone keypad) and, if it is taken, any free
  port. The first session on a desktop gets 3393; tokens are per session, so a client that guesses
  3393 and reaches another session gets `401`, not access. Containers pass `--port 3393` so a port
  mapping is predictable and a conflict fails fast.
- `HttpEdge.port`: `None` is the soft rule above, `0` is any free port (tests), N is strict.

### 5. Liveness

A stream writes `: ping` every 15 seconds (`HttpEdge.heartbeat`), so proxies keep it open and an
idle stream still has writes that can fail. A failed write, a reset, or the client closing its end
is a disconnect: the stream's `LocalClient` closes, so its questions go to the pool (D21 §6), and it
no longer counts as connected for the `-d` idle exit. A client that vanishes without closing (its
host went away) is found by the ping: on Linux the stream's socket has `TCP_USER_TIMEOUT` of two
heartbeats, so an unacknowledged ping fails the connection; elsewhere the kernel's retransmission
limit does, later. A write the client does not take within two heartbeats (it stopped
reading) is a disconnect too, as a WebSocket's keepalive timeout is on the socket, so a stalled
client can neither hold its questions nor keep the server from closing; closing the edge aborts
what is left. Responses send `Cache-Control: no-cache` and `X-Accel-Buffering: no`.

### 6. The serve record carries the edge

The serve record `$TAU_HOME/serve/<session id>.json` already existed beside the socket, written
atomically (scratch file, fsync, rename) with mode 600, removed on every clean exit (signals,
`--stop`, the idle exit) and replaced when stale. So the edge's fields go there rather than in a
second `<id>.tcp` file: `url`, `host`, `port`, `token`, and the session's `name` if it had one at
start. `socket` is written only when the socket is on, and the edge's fields only when the edge is.
A record already present when a server starts is stale (the server holds the session's lease) and
is removed before binding. Readers (`-d`, `--stop`, `tau -c`, `tau -p`) probe the record by the way
it is dialed: the socket if there is one, else the edge. A record whose pid is gone is stale
without asking, and so is one whose endpoint answers `hello` with another session or pid: the
port of a server that crashed may be another session's now (3393 is everyone's first choice),
whose `401` must not read as "busy". A stale record is removed only if the file still holds it.
`-d --json` prints the record, token included; it goes to the user who ran it.

### 7. The banner

Not under `--json`, `tau serve` prints one line naming every endpoint:

```text
serving 20261120-174140-62d20e on /home/u/.tau/serve/20261120-174140-62d20e.sock and http://127.0.0.1:3393/ (pid 410621)
```

and, only when stdout is a terminal and `--quiet` is not given, a second line with the **paste
URL**, the edge's URL with `#token=<token>` (a fragment is not sent to a server): `open: URL#token=…`
when the edge serves a web client (§9, the default), which reads the token from its own address,
and `for a web client: URL#token=…` when it serves only a help page (`--no-web`, or a broken
install). `-d` prints the same lines with
`started` or `already serving`.

### 8. Built on

A private module (`tau._http_edge`) on asyncio streams and `h11` (a direct dependency; httpx already
needs it), so τ adds no web framework: no starlette, uvicorn or aiohttp. The edge hands each stream
to the server as a line with a WebSocket's `recv`, `send` and `close`, so `tau._serve` has one
connection type. The client side is `RemoteClient.connect_http(url, token, client)` on httpx
(`tau._remote_http`); it sends POSTs one at a time, each after the last was accepted, so calls
arrive in the order they were made. The behavior suite runs a third wired mode, `http`.

### 9. The web client and the help page

τ serves a web client itself, so running one needs no Node: Node builds it (tau-code's CI), and a
dev server with live reload stays a development convenience. The page and the wire share an
origin, so the client reads `#token=` from its own address, keeps it in the tab
(`sessionStorage`) and sends it as `Authorization`; no proxy has to inject it.

- **Where the files come from**, first found: `tau serve --web-root DIR` (`HttpEdge.web_root`); else
  the `dist/` of the installed package **`tau_web`**, which is tau-code's built client, published as
  the wheel `ffwf-tau-web` (data only, `py3-none-any`, no dependency on `ffwf-tau`). **`ffwf-tau`
  depends on it** (`ffwf-tau-web>=0.17,<0.18`), so a plain `tau serve` serves the client at `/`
  with nothing more installed. τ's own wheel still carries no JavaScript: the files arrive in the
  other wheel. A web root (or a `tau_web`) without `index.html` fails at start (`ConfigError`
  naming it).
- **`--no-web`** (`HttpEdge(web=False)`) serves no client: `/` is a short page saying the server
  was started with `--no-web` and speaks only the wire at `/stream`. `--no-web` with `--web-root`
  is refused ("one or the other"), and with `--no-http` it is refused too (there is no edge to
  serve from).
- **A broken install** (`tau_web` missing although the dependency says it is there) does not stop
  the server: it warns on stderr (`tau: τ's web client is not installed (...): this install is
  broken, and / serves a page that says so. Reinstall ffwf-tau, or give --web-root DIR, or
  --no-web`) and serves the wire. `/` is then the **help page**: what the URL is, why its `#token=`
  never reached the server, that the install is broken, and how to fix it (reinstall, or
  `--web-root`). This is the gap, reported, not a stand-in and not a startup error, by decision:
  the wire is what the server is for, and the page names the fault to whoever opens it.
- **Versions.** `ffwf-tau-web` goes in lockstep with τ's minor: 0.17.x pairs with 0.17.x. The wire's
  `PROTOCOL` changes only on a τ minor bump (a rule, so the pin `>=0.17,<0.18` is also a promise
  that the client speaks the server's protocol; `scripts/check_tokens_wheel.py` checks the pin
  against the workspace version). The release order is always `ffwf-tau-web` first, then `ffwf-tau`,
  because the pin must resolve on PyPI when `ffwf-tau` publishes. The `ffwf-tau-web` wheel is built
  from tau-code and must speak the current `PROTOCOL`.
- **Routes.** `/stream` and `/stream/<id>` are the wire. Any other `GET` or `HEAD` is a file under
  the root; a path that escapes it (`..`, encoded or not, a backslash, a link out) is `404`; a path
  with no extension is the client's own route and gets `index.html`; a missing file is `404`.
- **Headers.** `Content-Security-Policy: default-src 'self'; connect-src 'self'; img-src 'self'
  data:; style-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-cache`.
- **Not a 0.15 `--web-root` revival:** 0.15 served a client from the daemon over its WebSocket;
  here the files are static, the session is reached only by the wire's token, and the client is
  someone else's package.
- An editor's webview loads the same bundle from the extension and calls the edge by its URL (an
  `--allow-origin` for the webview's origin, or the extension host relaying).

## Alternatives

- **WebSocket over TCP** (one more `ws://` listener): the same messages, fewer moving parts in τ,
  but the upgrade is what proxies and port forwards break. Rejected for the edge; kept on the socket.
- **Responses in the POST's body**: no ordering against events without a watermark. Rejected:
  one ordered channel, as on the socket.
- **Token in `hello`** (D21 Open 4's recommendation): a request before `hello` would be unauthenticated
  and the stream would open to anyone. Rejected for a header on every request.
- **A separate `<id>.tcp` or `<id>.token` file**: a second file with the same lifecycle as the
  record. Rejected once the record was found to be private and atomic already.
- **starlette + uvicorn**: more surface and dependencies than the six routes need.
- **The web client hosted by Node alone** (a Vite server with a token-injecting proxy): what was
  planned first. It needs Node at run time beside a harness that already serves HTTP, and a proxy
  just to move the token. Kept for development only.

## Open

1. **Windows (T2).** The flag rule is in (`--unix` refused, the edge alone), but the serve directory's
   owner check, the signal handlers and `tau -c`'s attach are POSIX-only today, and nothing runs
   the serve tests on Windows. Port them and un-skip the tests in the release workflow.
2. **`tau sessions --json`** could mark sessions with a live endpoint (nice to have).
3. **`name` in the record** is the name at start; a rename while serving is not reflected.
4. ~~**The `ffwf-tau[web]` extra**~~ Closed in 0.17.1: no extra; the client is a plain dependency
   (§9).

## Glossary additions

HTTP edge, `HttpEdge` (with `web`), stream and connection id, heartbeat, paste URL, web client and
web root (`tau_web`, `ffwf-tau-web`, `--no-web`), help page, own origin; the serve record's edge fields (what was planned as the `.tcp`
file).
