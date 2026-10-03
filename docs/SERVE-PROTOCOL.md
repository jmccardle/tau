# τ serve protocol

> **Generated — do not hand-edit.** Run `python scripts/generate_serve_protocol.py`
> after changing `tau_coding_agent/serve/protocol.py`, and commit both outputs.
> `tests/test_serve.py` fails if this file or the schema is stale.
>
> Design of record: `docs/TAU-SERVE.md` §5–§7.

- **Protocol version:** `0.2`
- **Default port:** `8256`
- **Counts:** 22 requests, 9 event kinds. Cite this line; never copy the numbers into hand-written prose.
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

Call one of the session backend's operations, acting at ``cursor_id``.

The TUI's commands reach the backend by method name; under ``--connect`` that
backend is the daemon's. Answered with a :class:`Performed`-shaped record or a
plain value, tagged by ``kind``.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `cursor_id` | string | yes |
| `method` | string | yes |
| `arguments` | object | no |

### `describe`

Re-read a session's extension surface, after an extension was enabled or reloaded.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |

### `compare`

Open one cursor per model at ``leaf`` and send each the same text (docs/TAU-SERVE.md §8).

Answered at once with ``{comparison_id, cursors: [{cursor_id, model}], message}``;
the turns run on. Each turn's ``submission_start`` carries the comparison in
``submission.correlation["compare"]`` as ``{id, models, index, cursor_id}``, so
every attached client can draw the columns. An unknown model fails before
anything is opened.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `models` | list of string | yes |
| `text` | string | yes |
| `leaf` | string \| null | no |

### `end_compare`

End a comparison: the head cursor moves onto ``keep``'s leaf, and every compare cursor closes.

Turns still running on the others are aborted first; every branch stays in
the tree. ``keep`` ``None`` keeps none and leaves the head where it is. Fails
with ``busy`` while the kept turn or the head's turn is running, changing
nothing. Answered with ``{"leaf": <the head's leaf>}``.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `comparison_id` | string | yes |
| `keep` | string \| null | yes |

### `next_step`

The next argument a flow needs, or the mutation it is ready for (RPC ``next_step``).

Answered with ``{status: "step"|"ready", step, ready}``: ``step`` is a
``FlowStep`` as JSON ``{flow, argument, domain, cursor, bound}``, ``ready`` a
``Ready`` ``{flow, mutation, arguments}``.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `flow` | string | yes |
| `bound` | object \| null | no |
| `leaf` | string \| null | no |

### `enumerate_domain`

The values legal for a domain right now (RPC ``enumerate_domain``).

Answered with ``{domain, values: [{value, label}], total}``; ``total`` counts
past ``limit``. ``path`` and ``session_id`` are read in the session's cwd.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `domain` | string | yes |
| `scope` | `"in_session"` \| `"ancestors_of_cursor"` \| `"descendants_of_cursor"` \| null | no |
| `leaf` | string \| null | no |
| `query` | string | no |
| `limit` | integer | no |

### `complete_path`

Complete the ``@path`` at ``offset`` in ``text`` against the session's cwd.

Answered as RPC ``complete_path``: ``{completion: null}`` outside an ``@``
token, else ``{completion: {start, end, token, matches: [{name, detail,
is_dir}], total}}``. The paths are on the daemon's machine.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `text` | string | yes |
| `offset` | integer | yes |

### `get_tree`

Every entry of a session's tree as a browser row, seen from one cursor.

Answered with ``{nodes: [TreeRow, ...], leaf, count}``: ``leaf`` is the
cursor's, and the one row whose ``is_leaf`` is true.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `cursor_id` | string | yes |

### `fork_session`

Copy a session into a new one in the same cwd; answered with ``{session_id}``.

The source is unchanged and the client stays attached to it; it attaches to
the new session to continue there.

| Field | Type | Required |
|---|---|---|
| `session_id` | string | yes |
| `at` | string \| null | yes |

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
| `models` | list of ModelRecord | yes |
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

### `Surface`

What a session answers beyond its tree, which a head reads without a round trip.

| Field | Type | Required |
|---|---|---|
| `commands` | list of CommandInfo | yes |
| `command_args` | object | yes |
| `shortcuts` | list of list of string | yes |
| `extensions` | list of list of any | yes |
| `loaded` | list of ExtensionInfo | yes |
| `load_errors` | list of list of string | yes |

### `CommandInfo`

One slash command a session answers, as RPC ``get_commands`` lists it.

| Field | Type | Required |
|---|---|---|
| `name` | string | yes |
| `description` | string | yes |
| `origin` | `"builtin"` \| `"extension"` | yes |
| `flow` | boolean | yes |
| `hidden` | boolean | yes |

### `ExtensionInfo`

One loaded extension and what it registered, as RPC ``get_extension_state`` lists it.

| Field | Type | Required |
|---|---|---|
| `name` | string | yes |
| `path` | string | yes |
| `tools` | list of string | yes |
| `commands` | list of string | yes |
| `shortcuts` | list of string | yes |
| `hooks` | list of string | yes |
| `content_hash` | string | yes |
| `subjects` | list of string | yes |

### `ModelRecord`

One model the daemon's config defines, as RPC ``get_models`` lists it.

| Field | Type | Required |
|---|---|---|
| `name` | string | yes |
| `model` | ModelSpec | yes |

### `ModelSpec`

What a config model name resolves to.

| Field | Type | Required |
|---|---|---|
| `id` | string | yes |
| `provider` | string | yes |
| `context_window` | integer | yes |

### `TreeRow`

One :class:`GetTree` row: RPC ``get_tree``'s node, with ``is_cursor`` named ``is_leaf``.

| Field | Type | Required |
|---|---|---|
| `entry_id` | string | yes |
| `parent_id` | string \| null | yes |
| `kind` | string | yes |
| `role` | string \| null | yes |
| `preview` | string | yes |
| `is_leaf` | boolean | yes |
| `timestamp` | integer \| null | yes |
| `first_kept_id` | string \| null | yes |
| `from_id` | string \| null | yes |
| `is_system` | boolean | yes |
| `tool_call_ids` | list of string | yes |
| `tool_call_id` | string \| null | yes |
| `copyable` | boolean | yes |
| `estimated_tokens` | integer | yes |
