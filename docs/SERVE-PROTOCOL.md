# τ serve protocol

> **Generated — do not hand-edit.** Run `python scripts/generate_serve_protocol.py`
> after changing `tau_coding_agent/serve/protocol.py`, and commit both outputs.
> `tests/test_serve.py` fails if this file or the schema is stale.
>
> Design of record: `docs/TAU-SERVE.md` §5–§7.

- **Protocol version:** `0.1`
- **Default port:** `8256`
- **Counts:** 16 requests, 9 event kinds. Cite this line; never copy the numbers into hand-written prose.
- **Schema:** `docs/serve-protocol.schema.json` (JSON Schema 2020-12).

## Framing

One JSON object per WebSocket text frame. A client's first request is `hello`; nothing else is served before it. Every request carries an integer `id` the client chose, and gets exactly one `response` with that `id`. Responses and events share one ordered stream per connection, so a client sees an `attach` answer before any event that follows it.

## Requests

### `hello`

The first request on every connection; nothing else is served before it.

| Field | Type | Required |
|---|---|---|
| `protocol` | string | yes |
| `client` | string | yes |
| `token` | string \| null | no |

### `list_sessions`

Every session in the daemon's store, across every cwd, newest first.

No fields.

### `create_session`

Create a session in ``cwd``, a path on the daemon's machine.

| Field | Type | Required |
|---|---|---|
| `cwd` | string | yes |
| `model` | string \| null | no |
| `name` | string \| null | no |

### `attach`

Start receiving a session's events, loading it in the daemon if needed.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `epoch` | string \| null | no |
| `since` | integer \| null | no |

### `detach`

Stop receiving a session's events. The session keeps running.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |

### `submit`

Send text to a cursor: a prompt, or a ``/command`` when ``expand_commands``.

Answered when the submission ends, with its :class:`SubmitResult`.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `cursor_id` | string | yes |
| `text` | string | yes |
| `multitask_strategy` | `"enqueue"` \| `"reject"` \| `"steer"` \| `"follow_up"` | no |
| `expand_commands` | boolean | no |
| `submission_id` | string \| null | no |
| `images` | list of object \| null | no |

### `abort`

Abort the turn running on a cursor, and the cursors it owns.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `cursor_id` | string | yes |

### `open_cursor`

Open a cursor at ``leaf`` (an entry id, or ``None`` before the root).

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `leaf` | string \| null | yes |
| `label` | string | no |
| `owner_id` | string \| null | no |

### `close_cursor`

Close a cursor this session opened; its branch stays in the tree.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `cursor_id` | string | yes |

### `move_cursor`

Move a cursor to ``leaf``. Writes nothing.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `cursor_id` | string | yes |
| `leaf` | string \| null | yes |

### `set_model`

Switch the model a cursor's next turn runs, by a name from the daemon's config.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `cursor_id` | string | yes |
| `model` | string | yes |

### `answer`

Answer an extension's form (a ``request`` event); the first answer wins.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `request_id` | string | yes |
| `value` | object \| null | yes |

### `answer_request`

Answer an extension request written in the tree (docs/EXTENSION-LOCKS.md §3).

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `cursor_id` | string | yes |
| `request_id` | string | yes |
| `action` | string | yes |
| `values` | object | no |

### `perform`

Call one of the session backend's operations on the head cursor's tree.

The TUI's commands reach the backend by method name; under ``--connect`` that
backend is the daemon's. Answered with a :class:`Performed`-shaped record or a
plain value, tagged by ``kind``.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `method` | string | yes |
| `arguments` | object | no |

### `describe`

Re-read a session's extension surface, after an extension was enabled or reloaded.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |

### `compare`

Open one cursor per model at ``leaf`` and send each the same text (docs/TAU-SERVE.md §8).

Answered at once with the opened cursors; the turns run on.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `leaf` | string \| null | yes |
| `models` | list of string | yes |
| `text` | string | yes |

## Responses

The one answer to a request, matched by ``id``.

| Field | Type | Required |
|---|---|---|
| `id` | integer | yes |
| `ok` | boolean | yes |
| `result` | any | no |
| `error` | Error \| null | no |

Error codes: `bad_request`, `unauthorized`, `protocol_mismatch`, `not_found`, `busy`, `failed`.

## Events

A push for an attached session, numbered per session within an ``epoch``.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `epoch` | string | yes |
| `seq` | integer | yes |
| `kind` | `"entry_open"` \| `"entry_final"` \| `"entry_append"` \| `"agent_event"` \| `"channel"` \| `"cursors"` \| `"request"` \| `"request_closed"` \| `"ui"` | yes |
| `data` | object | no |

Kinds:

- ``entry_open`` / ``entry_final`` / ``entry_append``: ``{"entry": {...}}``, a log
  write. Apply by id, last write wins, keeping the first position (§4.1).
- ``agent_event``: an ``AgentEvent`` as JSON. Changes no state.
- ``channel``: ``{"name": ..., "payload": {...}}`` for ``submission_start``,
  ``submission_end`` and ``custom_message``. Changes no state.
- ``cursors``: ``{"cursors": [CursorState, ...]}``, the whole set after a change.
- ``request`` / ``request_closed``: ``{"request_id", "spec"}`` / ``{"request_id"}``,
  an extension form opened, then answered by some client.
- ``ui``: ``{"op": "notify"|"status"|"panel", ...}``, extension display calls.

## Records

### `Attached`

The answer to :class:`Attach`.

With ``entries`` set it is a snapshot: replace the replica with them. With
``entries`` ``None`` the client's replica is current to ``since``, and the
events that follow (``seq > since``) are a replay.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `epoch` | string | yes |
| `seq` | integer | yes |
| `entries` | list of object \| null | yes |
| `cursors` | list of CursorState | yes |
| `head_cursor_id` | string | yes |
| `cwd` | string | yes |
| `models` | list of string | yes |
| `surface` | Surface | yes |

### `CursorState`

A live cursor as clients see it; sent whenever one opens, moves, closes or changes busy.

| Field | Type | Required |
|---|---|---|
| `cursor_id` | string | yes |
| `leaf` | string \| null | yes |
| `label` | string | yes |
| `owner_id` | string \| null | yes |
| `busy` | boolean | yes |
| `model` | string | yes |

### `SessionRow`

One line of :class:`ListSessions`' answer.

| Field | Type | Required |
|---|---|---|
| `id` | string | yes |
| `cwd` | string | yes |
| `name` | string \| null | yes |
| `modified` | string | yes |
| `message_count` | integer | yes |
| `first_message` | string | yes |
| `loaded` | boolean | yes |

### `SubmitResult`

How a submission ended; a refusal is an answer, not an error.

| Field | Type | Required |
|---|---|---|
| `accepted` | boolean | yes |
| `submission_id` | string | yes |
| `reason` | string \| null | no |
| `command` | object \| null | no |
