# tau-a2a: an A2A 1.0 server for a τ session

`tau-a2a` puts an [A2A](https://a2a-protocol.org/latest/specification/) 1.0 server (JSON-RPC
binding) in front of one session that `tau serve` holds. It is a client of that session through
τ's public API (`RemoteClient` on the session's socket), so τ itself is unchanged and does not
depend on it. Any standards-compliant A2A client can drive it. Nothing here assumes a particular
gateway, identity provider or orchestrator.

```bash
tau serve -d --socket /run/tau/s.sock          # the session
tau-a2a --socket /run/tau/s.sock --db /var/lib/tau-a2a/tasks.db --auth none   # 127.0.0.1:8080
```

## What A2A sees

| A2A | τ |
|---|---|
| Agent card (`/.well-known/agent-card.json`) | the session at a fork point: `version` is `<session id>@<node>` unless set; `skills` are the session's skills and prompt templates (D24 §6, the commands of its `skills` extension), or one `converse` skill when it has none |
| `contextId` | a cursor, opened at the fork point for each new context (the server's id, or one the client chose); one owner's contexts never meet another's |
| Task | one message through the cursor's next rest; one task at a time per context |
| `TASK_STATE_INPUT_REQUIRED` | an open extension question this client may answer. The status message has its text and a data part `{question, extension, schema}`; the next message on the same task answers it (its first data part, else its text read by the schema). |
| `TASK_STATE_AUTH_REQUIRED` | the step failed for a credential (HTTP 401/403, a missing or expired key); the next message on the task retries |
| `TASK_STATE_COMPLETED` | the step ended `Done`. The artifact `reply` is the last reply, and its metadata `tau.tools` lists the tools the turn called. |
| `TASK_STATE_FAILED` | `Failed`, `Halted`, or a refused reply |
| `TASK_STATE_CANCELED` | `CancelTask`: the step is aborted, its questions declined |
| `TASK_STATE_REJECTED` | a new task on a context whose task is waiting for input or a credential |
| Streaming | the reply's text as `reply` artifact chunks, a `calling <tool>` working status per call, then the final status |

Tasks and push configurations live in the a2a-sdk SQL task store (`--db`, SQLite); contexts and
their cursors in `<db>.contexts`. The τ session's log stays the truth for what was said.

**Extension `https://ffwf.net/a2a/ext/tau-fork/v1`** (`required: false`): request it with the
`A2A-Extensions` header and put `{"<uri>": {"at": "<taskId>"}}` in a new context's first message's
metadata. The context's cursor opens at the node that task ended on: a branch of the
conversation. Only the caller's own tasks can be named.

## The card overlay

`--card-overlay FILE` is a JSON object, part of an A2A `AgentCard` (A2A JSON names: `description`,
`version`, `provider`, `skills`, `documentationUrl`, `capabilities.extensions`, ...), merged over
the card the server states. Each field it gives replaces the server's, except
`capabilities.extensions`, which are added to the server's (one with the URI of a server extension
replaces it). This is how whatever deploys the server says what the agent is, in A2A's own terms:
tau-a2a reads no other schema. The fields only the server can state truly
(`supportedInterfaces`, `securitySchemes`, `securityRequirements`, and `capabilities.streaming`,
`pushNotifications` and `extendedAgentCard`) are refused in an overlay, as is anything that is not
an `AgentCard` field; the server then does not start.

## Auth

- `--auth none`: no credential; every caller is one owner. Allowed on a loopback `--host` only,
  unless `--allow-unauthenticated-network` says a NetworkPolicy or a proxy guards the port.
- `--auth jwt --issuer ISS --audience AUD [--jwks-url URL | --jwks-file FILE] [--scope S ...]`: a
  bearer JWT (RFC 6750, RFC 7519) checked for signature (asymmetric algorithms only), `iss`, `aud`,
  `exp` and `sub`. The keys come from `--jwks-url`, from `--jwks-file` (a JWKS file, re-read when
  it changes: for an issuer that serves its keys to no anonymous caller, such as a Kubernetes
  service-account issuer), or from the issuer's OpenID discovery document. `scope`
  (space-separated, RFC 9068) must hold every `--scope`. The owner of tasks and contexts is
  `sub` (A2A §13.1). No other claim is read. The card stays public, and declares `bearer` (HTTP,
  JWT) and `oidc` (the issuer's discovery URL) schemes.

## Tests

`a2a/tests` runs real processes: `tau serve` on τ's mock model (or a stub OpenAI endpoint
answering 401), and `tau-a2a`, driven by the a2a-sdk client. They need `a2a-sdk[http-server,sqlite]`,
`pyjwt[crypto]`, `uvicorn`, `pytest` and `pytest-asyncio` beside τ.
