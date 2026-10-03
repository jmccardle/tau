# τ serve protocol

> **Generated — do not hand-edit.** Run `python scripts/generate_serve_protocol.py`
> after changing `tau_coding_agent/serve/protocol.py`, and commit both outputs.
> `tests/test_serve.py` fails if this file or the schema is stale.
>
> Design of record: `docs/TAU-SERVE.md` §5–§7.

- **Protocol version:** `0.5`
- **Default port:** `8256`
- **Counts:** 23 requests, 9 event kinds, 140 schema definitions. Cite this line; never copy the numbers into hand-written prose.
- **Schema:** `docs/serve-protocol.schema.json` (JSON Schema 2020-12), also printed by `tau serve --schema` from an installed τ.

## Framing

One JSON object per WebSocket text frame. A client's first request is `hello`; nothing else is served before it. Every request carries an integer `id` the client chose, and gets exactly one `response` with that `id`. Responses and events share one ordered stream per connection, so a client sees an `attach` answer before any event that follows it.

## Open and closed records

Request records are closed (`additionalProperties: false`), because the daemon refuses an unknown field. Everything the daemon sends is open: a client must ignore a field it does not know, so a minor version may add one.

Each request names what it is answered with; in the schema that is the request's `x-result` and the top-level `Results` map. `Event`'s `x-data` names each kind's data.

## Requests

### `hello`

The first request on every connection; nothing else is served before it.

| Field | Type | Required | Description |
|---|---|---|---|
| `protocol` | string | yes | The client's `PROTOCOL_VERSION`; a different one is refused. |
| `client` | string | yes | What the client is, for the daemon's log (`tui`, `tail`, `web`). |
| `token` | string \| null | no | The shared secret, required only when the daemon's config sets `serve.token`. A client reads it from `TAUD_TOKEN`. |

**Answered with:** [HelloResult](#helloresult).

### `list_sessions`

Every session in the daemon's store, across every cwd, newest first.

No fields.

**Answered with:** [SessionList](#sessionlist).

### `create_session`

Create a session in `cwd`, a path on the daemon's machine.

| Field | Type | Required | Description |
|---|---|---|---|
| `cwd` | string | yes | The directory its tools run in; the create fails if it does not exist. |
| `model` | string \| null | no | A model name from the daemon's config, or `None` for its default. |
| `name` | string \| null | no | A session name, or `None`. |

**Answered with:** [SessionCreated](#sessioncreated).

### `attach`

Start receiving a session's events, loading it in the daemon if needed.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes | The session, or an unambiguous prefix of its id. |
| `epoch` | string \| null | no | The `epoch` of an earlier attach, to resume rather than re-read. |
| `since` | integer \| null | no | The last `seq` the client applied under that epoch. |

**Answered with:** [Attached](#attached).

### `detach`

Stop receiving a session's events. The session keeps running.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |

**Answered with:** null.

### `submit`

Send text to a cursor: a prompt, or a `/command` when `expand_commands`.

Answered when the submission ends. The `submission_start` channel event
carries what `expand_attachments` did, at acceptance.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `cursor_id` | string | yes |  |
| `text` | string | yes |  |
| `multitask_strategy` | `"reject"` \| `"enqueue"` \| `"steer"` \| `"rollback"` \| `"fork"` | no | What to do when the cursor is busy (docs/SUBMISSION-LIFECYCLE.md). |
| `expand_commands` | boolean | no |  |
| `submission_id` | string \| null | no | The id the events of this submission carry; the daemon mints one when `None`. A client that renders its own streams sends it. |
| `images` | list of object \| null | no | Image content blocks to send with the text. |
| `expand_attachments` | boolean | no | Resolve `@path` references in `text` against the session's cwd on the daemon's machine, as the TUI's editor does (docs/FILE-ATTACHMENTS.md §2). |

**Answered with:** [SubmitResult](#submitresult).

### `abort`

Abort the turn running on a cursor, and the cursors it owns.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `cursor_id` | string | yes |  |

**Answered with:** null.

### `open_cursor`

Open a cursor at `leaf` (an entry id, or `None` before the root).

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `leaf` | string \| null | yes |  |
| `label` | string | no |  |
| `owner_id` | string \| null | no |  |

**Answered with:** [CursorOpened](#cursoropened).

### `close_cursor`

Close a cursor this session opened; its branch stays in the tree.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `cursor_id` | string | yes |  |

**Answered with:** null.

### `move_cursor`

Move a cursor to `leaf`. Writes nothing.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `cursor_id` | string | yes |  |
| `leaf` | string \| null | yes |  |

**Answered with:** null.

### `set_model`

Switch the model a cursor's next turn runs, by a name from the daemon's config.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `cursor_id` | string | yes |  |
| `model` | string | yes |  |

**Answered with:** [ModelSet](#modelset).

### `answer`

Answer an extension's form (a `request` event); the first answer wins.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `request_id` | string | yes |  |
| `value` | object \| null | yes | The `{field: value}` answers, or `None` to cancel the form. |

**Answered with:** null.

### `answer_request`

Answer an extension request written in the tree (docs/EXTENSION-LOCKS.md §3).

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `cursor_id` | string | yes |  |
| `request_id` | string | yes | The request entry's id. |
| `action` | string | yes | The label of the pressed action. |
| `values` | object | no | The filled fields, keyed by name. |

**Answered with:** [RequestAnswered](#requestanswered).

### `perform`

Call one of the session backend's operations, acting at `cursor_id`.

The TUI's commands reach the backend by method name; under `--connect` that
backend is the daemon's.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `cursor_id` | string | yes | The cursor the operation acts on: the tree edits move and append at it, and `rollback_turn` runs its turn there. |
| `method` | string | yes | One of `PERFORMABLE`. |
| `arguments` | object | no | Its keyword arguments. |

**Answered with:** [PerformResult](#performresult).

### `perform_ready`

Perform a flow's `Ready` at a cursor, as a submit that resolved to it would.

`fork` and `switch_session` move a client to another session, so they come
back unperformed for the client to do.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `cursor_id` | string | yes |  |
| `ready` | [Ready](#ready) | yes | What `NextStep` answered once every argument was bound. Its `mutation` must be its flow's. |

**Answered with:** [DispatchedCommand](#dispatchedcommand).

### `describe`

Re-read a session's extension surface, after an extension was enabled or reloaded.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |

**Answered with:** [Surface](#surface).

### `compare`

Open one cursor per model at `leaf` and send each the same text (docs/TAU-SERVE.md §8).

Answered at once; the turns run on. Each turn's `submission_start` carries
the comparison in `submission.correlation.compare`, so every attached
client can draw the columns. An unknown model fails before anything is opened.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `models` | list of string | yes | Model names from the daemon's config, one cursor each; one may repeat. |
| `text` | string | yes | The prompt every cursor receives, never expanded as a command. |
| `leaf` | string \| null | no | The entry the cursors start from; `None` is the head cursor's leaf. |

**Answered with:** [CompareStarted](#comparestarted).

### `end_compare`

End a comparison: the head cursor moves onto `keep`'s leaf, and every compare cursor closes.

Turns still running on the others are aborted first; every branch stays in
the tree. `keep` `None` keeps none and leaves the head where it is. Fails
with `busy` while the kept turn or the head's turn is running, changing
nothing.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `comparison_id` | string | yes |  |
| `keep` | string \| null | yes |  |

**Answered with:** [CompareEnded](#compareended).

### `next_step`

The next argument a flow needs, or the mutation it is ready for (RPC `next_step`).

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `flow` | string | yes | A command `name` whose `flow` is true in the `Surface`. |
| `bound` | object \| null | no | The arguments bound so far; `None` or `{}` is the first step. |
| `leaf` | string \| null | no | The entry a scoped `message_id` argument is relative to, echoed back as the step's `cursor` for `EnumerateDomain`. |

**Answered with:** [NextStepResult](#nextstepresult).

### `enumerate_domain`

The values legal for a domain right now (RPC `enumerate_domain`).

`path` and `session_id` are read in the session's cwd.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `domain` | string | yes | A domain name, as a step's `domain.name` gives it. |
| `scope` | `"in_session"` \| `"ancestors_of_leaf"` \| `"descendants_of_leaf"` \| null | no | For `message_id`: which entries are candidates; `None` is `in_session`. |
| `leaf` | string \| null | no | The entry a scoped `message_id` is relative to; `None` is the head cursor's leaf. |
| `query` | string | no | A prefix of the value, or a substring of the label; empty matches all. |
| `limit` | integer | no | The most values answered. |

**Answered with:** [DomainListing](#domainlisting).

### `complete_path`

Complete the `@path` at `offset` in `text` against the session's cwd (RPC `complete_path`).

The paths are on the daemon's machine.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `text` | string | yes |  |
| `offset` | integer | yes | The caret's character offset in `text`. |

**Answered with:** [CompletePathResult](#completepathresult).

### `get_tree`

Every entry of a session's tree as a browser row, seen from one cursor.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `cursor_id` | string | yes |  |

**Answered with:** [TreeResult](#treeresult).

### `fork_session`

Copy a session into a new one in the same cwd.

The source is unchanged and the client stays attached to it; it attaches to
the new session to continue there.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `at` | string \| null | yes | The entry to fork at, copying only the path to it; `None` copies the whole tree. |

**Answered with:** [SessionForked](#sessionforked).

## Responses

The one answer to a request, matched by `id`.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | integer | yes |  |
| `ok` | boolean | yes |  |
| `result` | any | no | When `ok`, the request's result: `Results[request.type]` in the schema. `null` when not `ok`. |
| `error` | [Error](#error) \| null | no |  |

Error codes: `bad_request`, `unauthorized`, `protocol_mismatch`, `not_found`, `busy`, `failed`.

## Events

A push for an attached session, numbered per session within an `epoch`.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `epoch` | string | yes |  |
| `seq` | integer | yes |  |

| `kind` | `data` |
|---|---|
| `entry_open` | [EntryEventData](#entryeventdata) |
| `entry_final` | [EntryEventData](#entryeventdata) |
| `entry_append` | [EntryEventData](#entryeventdata) |
| `agent_event` | [WireEvent](#wireevent) |
| `channel` | [ChannelEventData](#channeleventdata) |
| `cursors` | [CursorsEventData](#cursorseventdata) |
| `request` | [RequestEventData](#requesteventdata) |
| `request_closed` | [RequestClosedEventData](#requestclosedeventdata) |
| `ui` | [UiEventData](#uieventdata) |

What each event kind carries in `data`.

`agent_event` is RPC's `WireEvent`, built by the same
`tau_agent_core.rpc.wire_events.WireEventProjector`: `message_update`
carries a delta, never the whole message. Unbounded fields (a tool's arguments
and result, a message's content and usage) are left out; the entry events carry
them. It changes no state.

## `tau serve -d --json`

What `tau serve -d --json` prints: the one daemon at `address`.

Not a frame. `tau serve -d` starts no second daemon where one answers a hello.

| Field | Type | Required | Description |
|---|---|---|---|
| `address` | string | yes | `HOST:PORT` or `unix:/PATH`. |
| `pid` | integer | yes | The daemon's process id. |
| `started` | boolean | yes | Whether this call started it. |
| `log` | string | yes | The background log a daemon started by `-d` writes. |

## Definitions

Every `$defs` entry the requests above do not already show, by name.

### AgentEventEvent

A `agent_event` event.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `epoch` | string | yes |  |
| `seq` | integer | yes |  |
| `kind` | `"agent_event"` | yes |  |
| `data` | [WireEvent](#wireevent) | yes |  |
| `type` | `"event"` | yes |  |

### Argument

One argument a flow needs before it can run.

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | The argument's name, as the bound-argument mapping keys it. |
| `domain` | string | yes | The name of its `Domain`, a key of `DOMAINS`. |
| `description` | string | yes | The prompt a head shows for it. |
| `cardinality` | `"one"` \| `"many"` | no | `"one"` for a single value, `"many"` for a list. |
| `required` | boolean | no | Whether the flow can run without it. An optional argument is offered as a step and may be skipped. |
| `scope` | `"in_session"` \| `"ancestors_of_leaf"` \| `"descendants_of_leaf"` \| null | no | For the `message_id` domain, which entries are candidates — one of `ConversationTree.complete_message_id`'s scopes. `None` everywhere else. |

### Ask

What an extension request asks, normalized (`extension_types.validate_ask_spec`).

| Field | Type | Required | Description |
|---|---|---|---|
| `title` | string | yes |  |
| `body` | [PanelBody](#panelbody) \| null | yes |  |
| `fields` | list of [FormField](#formfield) | yes |  |
| `actions` | list of [AskAction](#askaction) | yes |  |

### AskAction

An ask's button: it runs `command` with the request's id.

| Field | Type | Required | Description |
|---|---|---|---|
| `label` | string | yes |  |
| `command` | string | yes |  |

### AssistantMessage

An assistant message from the LLM.

| Field | Type | Required | Description |
|---|---|---|---|
| `role` | `"assistant"` | yes |  |
| `content` | list of [TextContent](#textcontent) \| [ThinkingContent](#thinkingcontent) \| [ToolCall](#toolcall) | yes |  |
| `api` | string | yes |  |
| `provider` | string | yes |  |
| `model` | string | yes |  |
| `response_id` | string \| null | no |  |
| `usage` | [Usage](#usage) | no |  |
| `stop_reason` | `"stop"` \| `"length"` \| `"toolUse"` \| `"error"` \| `"aborted"` | yes |  |
| `error_message` | string \| null | no |  |
| `timestamp` | integer \| null | no |  |

### Attached

The answer to `Attach`.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `epoch` | string | yes | Changes when the daemon restarts or reloads the session; a client holding another epoch must take a snapshot. |
| `seq` | integer | yes | The newest event number folded into this answer. |
| `entries` | list of [Entry](#entry) \| null | yes |  |
| `cursors` | list of [CursorState](#cursorstate) | yes |  |
| `head_cursor_id` | string | yes | The cursor a client drives unless it opens its own. |
| `cwd` | string | yes |  |
| `models` | list of [ModelRecord](#modelrecord) | yes | Every model the daemon's config defines, for a picker. |
| `surface` | [Surface](#surface) | yes |  |
| `requests` | list of [RequestEventData](#requesteventdata) | yes | The extension forms open now, which an `Answer` closes. |

### AttachmentCompletion

The `@` token at the caret and what it completes to.

| Field | Type | Required | Description |
|---|---|---|---|
| `start` | integer | yes | The token's first character offset in the text. |
| `end` | integer | yes | The offset after its last. |
| `token` | string | yes |  |
| `matches` | list of [PathMatch](#pathmatch) | yes |  |
| `total` | integer | yes | How many paths match, counting past the bound on `matches`. |

### AttachmentReport

What `Submit.expand_attachments` did.

| Field | Type | Required | Description |
|---|---|---|---|
| `expanded` | integer | yes | How many `@` references were sent as attachments. |
| `images` | integer | yes | How many of them were images. |
| `unresolved` | list of string | yes | The `@` tokens that named no file, sent as written. |
| `failures` | list of string | yes | One line per file that resolved and could not be read. |

### BranchSummaryEntry

A summary of the branch left at `fromId`, in the path where it was appended.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | absent | no |  |
| `type` | `"branch_summary"` | yes |  |
| `summary` | string | yes |  |
| `fromId` | string \| null | yes |  |

### ChannelEvent

A `channel` event.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `epoch` | string | yes |  |
| `seq` | integer | yes |  |
| `kind` | `"channel"` | yes |  |
| `data` | [ChannelEventData](#channeleventdata) | yes |  |
| `type` | `"event"` | yes |  |

### ChannelEventData

A session bus channel, told apart by `name`. Changes no state.

One of: [SubmissionStartChannel](#submissionstartchannel), [SubmissionEndChannel](#submissionendchannel), [CustomMessageChannel](#custommessagechannel).

### CommandInfo

One slash command a session answers, as RPC `get_commands` lists it.

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes |  |
| `description` | string | yes |  |
| `origin` | `"builtin"` \| `"extension"` | yes | `builtin` for τ's own, `extension` for one an extension registered. |
| `flow` | boolean | yes | Whether `NextStep` steps it. |
| `hidden` | boolean | yes | A qualified extension name (`ext:pirate.speak`): it resolves, and a completion list leaves it out. |

### CompactionAnswer

A `Perform` whose operation returned a `CompactionResult` (`compact`).

| Field | Type | Required | Description |
|---|---|---|---|
| `fields` | [CompactionResult](#compactionresult) | yes |  |
| `kind` | `"CompactionResult"` | yes |  |

### CompactionDetails

File-operation details stored alongside a compaction (pi: CompactionDetails).

| Field | Type | Required | Description |
|---|---|---|---|
| `read_files` | list of string | no |  |
| `modified_files` | list of string | no |  |

### CompactionEntry

A summary that replaces the path before `firstKeptId` in the context.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | absent | no |  |
| `type` | `"compaction"` | yes |  |
| `summary` | string | yes |  |
| `firstKeptId` | string | yes |  |
| `tokensBefore` | integer | yes |  |
| `summarizerModelId` | string | no |  |
| `summaryUsage` | object of integer | no |  |
| `coveredEntries` | integer | no |  |
| `coveredTokens` | integer | no |  |
| `configId` | string \| null | no |  |

### CompactionResult

Generated compaction data ready to persist (pi: CompactionResult).

| Field | Type | Required | Description |
|---|---|---|---|
| `summary` | string | yes |  |
| `first_kept_entry_id` | string | yes |  |
| `tokens_before` | integer | yes |  |
| `details` | [CompactionDetails](#compactiondetails) \| null | no |  |
| `compacted_entry_ids` | list of string | no |  |
| `tokens_saved` | integer | no |  |
| `usage` | object of integer | no |  |

### CompareCorrelation

A comparison turn's place in its comparison (`tau_agent_core.compare.COMPARE_KEY`).

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `models` | list of string | yes |  |
| `index` | integer | yes |  |
| `cursor_id` | string | yes |  |

### CompareCursor

One column of a comparison.

| Field | Type | Required | Description |
|---|---|---|---|
| `cursor_id` | string | yes |  |
| `model` | string | yes |  |

### CompareEnded

The answer to `EndCompare`.

| Field | Type | Required | Description |
|---|---|---|---|
| `leaf` | string \| null | yes | The head cursor's leaf afterwards. |

### CompareStarted

The answer to `Compare`, sent before the turns end.

| Field | Type | Required | Description |
|---|---|---|---|
| `comparison_id` | string | yes |  |
| `cursors` | list of [CompareCursor](#comparecursor) | yes |  |
| `message` | string | yes | One line naming the models, for a head to show. |

### CompletePathResult

The answer to `CompletePath`; `completion` is `None` outside an `@` token.

| Field | Type | Required | Description |
|---|---|---|---|
| `completion` | [AttachmentCompletion](#attachmentcompletion) \| null | yes |  |

### Correlation

`Submission.correlation`: open, JSON-safe keys a submitter attached; `compare` is τ's.

| Field | Type | Required | Description |
|---|---|---|---|
| `compare` | [CompareCorrelation](#comparecorrelation) | no |  |

### CursorOpened

The answer to `OpenCursor`.

| Field | Type | Required | Description |
|---|---|---|---|
| `cursor_id` | string | yes |  |

### CursorState

A live cursor as clients see it; sent whenever one opens, moves, closes or changes busy.

| Field | Type | Required | Description |
|---|---|---|---|
| `cursor_id` | string | yes |  |
| `leaf` | string \| null | yes |  |
| `label` | string | yes |  |
| `owner_id` | string \| null | yes |  |
| `busy` | boolean | yes |  |
| `model` | string | yes |  |
| `request` | [ExtensionRequest](#extensionrequest) \| null | yes | The extension request at its leaf, or `None`. |

### CursorsEvent

A `cursors` event.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `epoch` | string | yes |  |
| `seq` | integer | yes |  |
| `kind` | `"cursors"` | yes |  |
| `data` | [CursorsEventData](#cursorseventdata) | yes |  |
| `type` | `"event"` | yes |  |

### CursorsEventData

The whole cursor set after a change.

| Field | Type | Required | Description |
|---|---|---|---|
| `cursors` | list of [CursorState](#cursorstate) | yes |  |

### CustomEntryEntry

Durable data the model never sees.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | absent | no |  |
| `type` | `"customEntry"` | yes |  |
| `customType` | string | yes |  |
| `data` | object | yes |  |

### CustomMessageChannel

The `custom_message` channel.

| Field | Type | Required | Description |
|---|---|---|---|
| `payload` | [CustomMessagePayload](#custommessagepayload) | yes |  |
| `name` | `"custom_message"` | yes |  |

### CustomMessageEntry

An extension's message, which reaches the model unless `visibleToModel` is false.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | absent | no |  |
| `type` | `"customMessage"` | yes |  |
| `customType` | string | yes |  |
| `message` | [CustomRoleMessage](#customrolemessage) | yes |  |

### CustomMessagePayload

An extension message appended outside a turn.

| Field | Type | Required | Description |
|---|---|---|---|
| `entry_id` | string | yes |  |
| `message` | [CustomRoleMessage](#customrolemessage) | yes |  |

### CustomRoleMessage

An extension's message (`messages.create_custom_message`); the model sees it as `user`.

| Field | Type | Required | Description |
|---|---|---|---|
| `role` | `"custom"` | yes |  |
| `customType` | string | yes |  |
| `content` | list of [TextContent](#textcontent) \| [ImageContent](#imagecontent) | yes |  |
| `display` | boolean | yes |  |
| `visibleToModel` | boolean | no |  |
| `details` | any | no |  |
| `timestamp` | integer | no |  |

### DispatchedCommand

What a command resolved to, told apart by `arm`: `Performed` (it ran), `FlowStep` (an argument is missing), `Ready` (a `fork` or `switch_session` for the client to perform) or `View` (a surface only a head opens).

One of: [PerformedArm](#performedarm), [FlowStepArm](#flowsteparm), [ReadyArm](#readyarm), [ViewArm](#viewarm).

### Domain

A named type in τ's object model, and how its values are found.

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | The domain's name, as an argument declares it. |
| `description` | string | yes | What a value of this domain means, for a person reading a form. |
| `free` | boolean | no | Whether any value is legal. A free domain has no enumerator and no fixed values, and a head renders it as a plain field. |
| `values` | list of string \| null | no | The fixed legal values, when there are few and they never change. |
| `enumerator` | string \| null | no | The name of the `Capability` that computes the legal values, when they depend on live state. |
| `field_kind` | string | no | Which of `tau_agent_core.extension_types.FORM_FIELD_KINDS` a head renders a SINGLE value of this domain as. `"select"` asserts the whole legal set can be put on screen at once; a domain whose set is unbounded or merely large says `"text"` and is completed against instead. A head may substitute a richer control than the kind names — the TUI answers `session_id` with its filtered picker — and may never substitute a poorer one. |

### DomainChoice

One legal value: `value` is what is bound, `label` what is shown.

| Field | Type | Required | Description |
|---|---|---|---|
| `value` | string | yes |  |
| `label` | string | yes |  |

### DomainListing

The answer to `EnumerateDomain`.

| Field | Type | Required | Description |
|---|---|---|---|
| `domain` | string | yes |  |
| `values` | list of [DomainChoice](#domainchoice) | yes |  |
| `total` | integer | yes | How many values match, counting past `limit`. |

### ElideEntry

A splice anchor with no summary: the path before `firstKeptId` leaves the context.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | absent | no |  |
| `type` | `"elide"` | yes |  |
| `firstKeptId` | string | yes |  |
| `coveredEntries` | integer | no |  |
| `coveredTokens` | integer | no |  |
| `configId` | string \| null | no |  |

### Entry

One session-log entry, told apart by `type`; an unfinished one carries `status: "incomplete"`. Apply by `id`, last write wins (docs/TAU-SERVE.md §4.1).

One of: [MessageEntry](#messageentry), [CustomMessageEntry](#custommessageentry), [CustomEntryEntry](#customentryentry), [CompactionEntry](#compactionentry), [ElideEntry](#elideentry), [BranchSummaryEntry](#branchsummaryentry), [SessionInfoEntry](#sessioninfoentry), [NavigateEntry](#navigateentry), [ModelChangeEntry](#modelchangeentry), [ThinkingChangeEntry](#thinkingchangeentry), [ForeignEntry](#foreignentry), [IncompleteEntry](#incompleteentry).

### EntryAppendEvent

A `entry_append` event.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `epoch` | string | yes |  |
| `seq` | integer | yes |  |
| `kind` | `"entry_append"` | yes |  |
| `data` | [EntryEventData](#entryeventdata) | yes |  |
| `type` | `"event"` | yes |  |

### EntryEventData

An `entry_open`, `entry_final` or `entry_append`: one log write.

| Field | Type | Required | Description |
|---|---|---|---|
| `entry` | [Entry](#entry) | yes | The entry as the log now holds it. |
| `cursor_id` | string \| null | no | The cursor whose turn or request wrote it; an `entry_final` names the cursor that opened the entry. Null for a write no cursor made. A client joins `agent_event` to the entries a turn writes by it. |

### EntryFinalEvent

A `entry_final` event.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `epoch` | string | yes |  |
| `seq` | integer | yes |  |
| `kind` | `"entry_final"` | yes |  |
| `data` | [EntryEventData](#entryeventdata) | yes |  |
| `type` | `"event"` | yes |  |

### EntryOpenEvent

A `entry_open` event.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `epoch` | string | yes |  |
| `seq` | integer | yes |  |
| `kind` | `"entry_open"` | yes |  |
| `data` | [EntryEventData](#entryeventdata) | yes |  |
| `type` | `"event"` | yes |  |

### Error

Why a request failed. `code` is stable; `message` is for a human.

| Field | Type | Required | Description |
|---|---|---|---|
| `code` | `"bad_request"` \| `"unauthorized"` \| `"protocol_mismatch"` \| `"not_found"` \| `"busy"` \| `"failed"` | yes |  |
| `message` | string | yes |  |

### Event

A push for an attached session, numbered per session within an `epoch`.

One of: [EntryOpenEvent](#entryopenevent), [EntryFinalEvent](#entryfinalevent), [EntryAppendEvent](#entryappendevent), [AgentEventEvent](#agenteventevent), [ChannelEvent](#channelevent), [CursorsEvent](#cursorsevent), [RequestEvent](#requestevent), [RequestClosedEvent](#requestclosedevent), [UiEvent](#uievent).

### ExtensionCommandAnswer

A `Perform` whose operation returned an `ExtensionCommandResult`.

| Field | Type | Required | Description |
|---|---|---|---|
| `fields` | [ExtensionCommandResult](#extensioncommandresult) | yes |  |
| `kind` | `"ExtensionCommandResult"` | yes |  |

### ExtensionCommandResult

Outcome of `AgentSession.run_extension_command` (E7 §3 / S46).

| Field | Type | Required | Description |
|---|---|---|---|
| `handled` | boolean | yes |  |
| `output` | any \| null | no |  |

### ExtensionInfo

One loaded extension and what it registered, as RPC `get_extension_state` lists it.

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes |  |
| `path` | string | yes |  |
| `tools` | list of string | yes |  |
| `commands` | list of string | yes |  |
| `shortcuts` | list of string | yes |  |
| `hooks` | list of string | yes |  |
| `content_hash` | string | yes |  |
| `subjects` | list of string | yes |  |

### ExtensionRequest

An extension request at a cursor, as RPC `get_pending_request` answers it (docs/EXTENSION-LOCKS.md).

| Field | Type | Required | Description |
|---|---|---|---|
| `entry_id` | string | yes | The request entry's id; `AnswerRequest` names it. |
| `extension` | string | yes |  |
| `extension_name` | string | yes | The display stem of `extension`. |
| `sentence` | string | yes |  |
| `label` | string | yes | τ's framing line for the request (§9). |
| `lock` | boolean | yes | Whether a submission at this cursor is refused. |
| `ask` | [Ask](#ask) \| null | yes | What it asks, or `None` for a bare lock. |
| `release` | string \| null | yes | A command that clears the lock, or `None`. |

### ExtensionRequestRecord

One reserved `customEntry`, read back off the tree.

| Field | Type | Required | Description |
|---|---|---|---|
| `entry_id` | string | yes | The tree entry's own id. |
| `extension` | string | yes | The appending extension's path, stored rather than looked up (§4) — the case this exists for is a reload where that extension never loaded. |
| `sentence` | string | yes | The extension's own one line. Distinct from `label`, which is τ's framing of it. |
| `lock` | boolean | yes | Whether a submission at this cursor is refused. |
| `ask` | object \| null | yes | The validated ask spec a head renders, or `None`. |
| `release` | string \| null | yes | A command name that clears the lock, or `None`. Advisory — commands are exempt from the lock by placement (§5), not by name. |

### FlowStep

One argument a flow still needs, and everything required to ask for it.

| Field | Type | Required | Description |
|---|---|---|---|
| `flow` | string | yes | The flow's name. |
| `argument` | [Argument](#argument) | yes | The argument being asked for. |
| `domain` | [Domain](#domain) | yes | That argument's `tau_agent_core.capabilities.Domain`, resolved here so a head need not look it up. |
| `leaf` | string \| null | yes | The entry a scoped `message_id` argument is relative to, carried through from the `next_step` call so the head hands it straight back to `enumerate_domain`. |
| `bound` | object | yes | The arguments already bound, so a head redrawing a form has them. |

### FlowStepArm

One argument a flow still needs, and everything required to ask for it.

| Field | Type | Required | Description |
|---|---|---|---|
| `arm` | `"FlowStep"` | yes |  |
| `flow` | string | yes | The flow's name. |
| `argument` | [Argument](#argument) | yes | The argument being asked for. |
| `domain` | [Domain](#domain) | yes | That argument's `tau_agent_core.capabilities.Domain`, resolved here so a head need not look it up. |
| `leaf` | string \| null | yes | The entry a scoped `message_id` argument is relative to, carried through from the `next_step` call so the head hands it straight back to `enumerate_domain`. |
| `bound` | object | yes | The arguments already bound, so a head redrawing a form has them. |

### ForeignEntry

A document another system's store put in the tree, typed `system:kind` (`jmfts:document`).

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | absent | no |  |
| `type` | string matching `^[^:]+:.+$` | yes |  |

### FormField

One field of an extension form (`extension_types.validate_form_spec`).

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes |  |
| `kind` | `"text"` \| `"select"` \| `"multiselect"` \| `"confirm"` \| `"number"` | yes |  |
| `label` | string | no |  |
| `default` | any | no |  |
| `options` | list of string | no |  |

### FormSpec

An extension's `ui.form` spec, as the extension passed it (docs/EXTENSION-LOCKS.md §8.2).

| Field | Type | Required | Description |
|---|---|---|---|
| `title` | string | no |  |
| `fields` | list of [FormField](#formfield) | yes |  |

### HelloResult

The answer to `Hello`.

| Field | Type | Required | Description |
|---|---|---|---|
| `protocol` | string | yes | The daemon's `PROTOCOL_VERSION`. |
| `client_id` | string | yes | This connection's name in the daemon's log. |
| `pid` | integer | yes | The daemon's process id. |
| `version` | string | yes | τ's package version, as `tau --version` prints it. |
| `cwd` | string | yes | The daemon's working directory at start, absolute; a default for `CreateSession`. |

### ImageContent

An image content block in a message.

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | `"image"` | yes |  |
| `data` | string | yes |  |
| `mime_type` | string | yes |  |

### IncompleteEntry

An entry opened and not yet finalized (docs/TAU-SERVE.md §4).

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | string | yes |  |
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | `"incomplete"` | yes |  |

### Message

A message as the log stores it, told apart by `role`.

One of: [UserMessage](#usermessage), [AssistantMessage](#assistantmessage), [ToolResultMessage](#toolresultmessage), [SystemMessage](#systemmessage), [CustomRoleMessage](#customrolemessage).

### MessageEntry

A message on the conversation path.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | absent | no |  |
| `type` | `"message"` | yes |  |
| `message` | [Message](#message) | yes |  |

### ModelChangeEntry

Legacy: the config model from here on, before config entries.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | absent | no |  |
| `type` | `"model_change"` | yes |  |
| `model` | string \| null | yes |  |
| `backend` | string \| null | no |  |

### ModelRecord

One model the daemon's config defines, as RPC `get_models` lists it.

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes |  |
| `model` | [ModelSpec](#modelspec) | yes |  |

### ModelSet

The answer to `SetModel`.

| Field | Type | Required | Description |
|---|---|---|---|
| `model` | string | yes | The model id the cursor's next turn calls. |

### ModelSpec

What a config model name resolves to.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `provider` | string | yes |  |
| `context_window` | integer | yes |  |

### NavigateEntry

Legacy: a recorded move to `targetId`, written before cursors (docs/CURSORS.md §1.1).

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | absent | no |  |
| `type` | `"navigate"` | yes |  |
| `targetId` | string \| null | yes |  |

### NextStepResult

The answer to `NextStep`: exactly one of `step` and `ready` is set, as `status` says.

| Field | Type | Required | Description |
|---|---|---|---|
| `status` | `"step"` \| `"ready"` | yes |  |
| `step` | [FlowStep](#flowstep) \| null | yes |  |
| `ready` | [Ready](#ready) \| null | yes |  |

### PanelAction

A panel button: it runs the extension command `command` with `args`.

| Field | Type | Required | Description |
|---|---|---|---|
| `label` | string | yes |  |
| `command` | string | yes |  |
| `args` | string | yes |  |

### PanelBody

A panel's body, told apart by `kind` (`extension_types.validate_panel_spec`).

One of: [PanelText](#paneltext), [PanelList](#panellist), [PanelTable](#paneltable).

### PanelList

A panel body listing strings.

| Field | Type | Required | Description |
|---|---|---|---|
| `kind` | `"list"` | yes |  |
| `items` | list of string | yes |  |

### PanelSpec

A `ui.panel` spec, normalized (`extension_types.validate_panel_spec`).

| Field | Type | Required | Description |
|---|---|---|---|
| `title` | string | yes |  |
| `body` | [PanelBody](#panelbody) | yes |  |
| `actions` | list of [PanelAction](#panelaction) | yes |  |

### PanelTable

A panel body of string cells; every row has one cell per column.

| Field | Type | Required | Description |
|---|---|---|---|
| `kind` | `"table"` | yes |  |
| `columns` | list of string | yes |  |
| `rows` | list of list of string | yes |  |

### PanelText

A panel body of text.

| Field | Type | Required | Description |
|---|---|---|---|
| `kind` | `"text"` | yes |  |
| `text` | string | yes |  |

### PathMatch

One path an `@` token can complete to.

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes |  |
| `detail` | string | yes |  |
| `is_dir` | boolean | yes |  |

### PerformResult

The answer to `perform`, told apart by `kind`.

One of: [PerformedAnswer](#performedanswer), [ExtensionCommandAnswer](#extensioncommandanswer), [SubmissionAnswer](#submissionanswer), [CompactionAnswer](#compactionanswer), [ValueAnswer](#valueanswer).

### Performed

What a capability produced. The past tense of `Ready`.

| Field | Type | Required | Description |
|---|---|---|---|
| `flow` | string \| null | yes | The flow that named the mutation, when a flow did. `None` when a caller performed the capability directly. |
| `mutation` | string | yes | The capability that ran. |
| `data` | object | yes | What it returned, keyed as its `returns` declares. JSON-able. |
| `leaf` | string \| null | no | The acting cursor's leaf after the call, or `None` for a session with no log. It is the promoted copy of `data["leaf"]` wherever the capability declares one, so a head reads the same field for every mutation instead of knowing which ones carry it. |

### PerformedAnswer

A `Perform` whose operation returned a `Performed`.

| Field | Type | Required | Description |
|---|---|---|---|
| `fields` | [Performed](#performed) | yes |  |
| `kind` | `"Performed"` | yes |  |

### PerformedArm

What a capability produced. The past tense of `Ready`.

| Field | Type | Required | Description |
|---|---|---|---|
| `arm` | `"Performed"` | yes |  |
| `flow` | string \| null | yes | The flow that named the mutation, when a flow did. `None` when a caller performed the capability directly. |
| `mutation` | string | yes | The capability that ran. |
| `data` | object | yes | What it returned, keyed as its `returns` declares. JSON-able. |
| `leaf` | string \| null | no | The acting cursor's leaf after the call, or `None` for a session with no log. It is the promoted copy of `data["leaf"]` wherever the capability declares one, so a head reads the same field for every mutation instead of knowing which ones carry it. |

### Ready

A flow with every required argument bound: the mutation, and what to call it with.

| Field | Type | Required | Description |
|---|---|---|---|
| `flow` | string | yes | The flow's name. |
| `mutation` | string | yes | The capability to perform — the flow's, always. Which mutation runs is a property of which flow was named, never of what was bound. |
| `arguments` | object | yes | What to perform it with, keyed by the mutation's own parameter names, so a caller can splat it. |

### ReadyArm

A flow with every required argument bound: the mutation, and what to call it with.

| Field | Type | Required | Description |
|---|---|---|---|
| `arm` | `"Ready"` | yes |  |
| `flow` | string | yes | The flow's name. |
| `mutation` | string | yes | The capability to perform — the flow's, always. Which mutation runs is a property of which flow was named, never of what was bound. |
| `arguments` | object | yes | What to perform it with, keyed by the mutation's own parameter names, so a caller can splat it. |

### RequestAnswered

The answer to `AnswerRequest`.

| Field | Type | Required | Description |
|---|---|---|---|
| `handled` | boolean | yes | Whether the action's command ran. |
| `output` | string \| null | yes | What the command returned, as text, or `None`. |

### RequestClosedEvent

A `request_closed` event.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `epoch` | string | yes |  |
| `seq` | integer | yes |  |
| `kind` | `"request_closed"` | yes |  |
| `data` | [RequestClosedEventData](#requestclosedeventdata) | yes |  |
| `type` | `"event"` | yes |  |

### RequestClosedEventData

A form some client answered, or its asker gave up on.

| Field | Type | Required | Description |
|---|---|---|---|
| `request_id` | string | yes |  |

### RequestEvent

A `request` event.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `epoch` | string | yes |  |
| `seq` | integer | yes |  |
| `kind` | `"request"` | yes |  |
| `data` | [RequestEventData](#requesteventdata) | yes |  |
| `type` | `"event"` | yes |  |

### RequestEventData

An extension form open now: the `request` event's data, and one of `Attached`'s `requests`.

| Field | Type | Required | Description |
|---|---|---|---|
| `request_id` | string | yes | What `Answer` names. |
| `spec` | [FormSpec](#formspec) | yes |  |

### Response

The one answer to a request, matched by `id`.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | integer | yes |  |
| `ok` | boolean | yes |  |
| `result` | any | no | When `ok`, the request's result: `Results[request.type]` in the schema. `null` when not `ok`. |
| `error` | [Error](#error) \| null | no |  |
| `type` | `"response"` | yes |  |

### ServeStarted

What `tau serve -d --json` prints: the one daemon at `address`.

| Field | Type | Required | Description |
|---|---|---|---|
| `address` | string | yes | `HOST:PORT` or `unix:/PATH`. |
| `pid` | integer | yes | The daemon's process id. |
| `started` | boolean | yes | Whether this call started it. |
| `log` | string | yes | The background log a daemon started by `-d` writes. |

### SessionCreated

The answer to `CreateSession`.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |

### SessionForked

The answer to `ForkSession`.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |

### SessionInfoEntry

The session's display name from here on; the model never sees it.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | absent | no |  |
| `type` | `"session_info"` | yes |  |
| `name` | string | yes |  |

### SessionList

The answer to `ListSessions`.

| Field | Type | Required | Description |
|---|---|---|---|
| `sessions` | list of [SessionRow](#sessionrow) | yes |  |

### SessionRow

One line of `ListSessions`' answer.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `cwd` | string | yes |  |
| `name` | string \| null | yes |  |
| `modified` | string | yes |  |
| `message_count` | integer | yes |  |
| `first_message` | string | yes |  |
| `loaded` | boolean | yes |  |

### SubmissionAnswer

A `Perform` whose operation returned a `SubmissionResult` (`rollback_turn`).

| Field | Type | Required | Description |
|---|---|---|---|
| `fields` | [SubmissionResult](#submissionresult) | yes |  |
| `kind` | `"SubmissionResult"` | yes |  |

### SubmissionEndChannel

The `submission_end` channel.

| Field | Type | Required | Description |
|---|---|---|---|
| `payload` | [SubmissionEndPayload](#submissionendpayload) | yes |  |
| `name` | `"submission_end"` | yes |  |

### SubmissionEndPayload

A submission's turn ended, however it ended.

| Field | Type | Required | Description |
|---|---|---|---|
| `submission` | [SubmissionInfo](#submissioninfo) | yes |  |
| `side_usage` | object of integer \| null | yes | Tokens its side completions (summaries) spent, by usage field. |

### SubmissionInfo

A `Submission` as its channel events carry it (`tau_agent_core.submission`).

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | yes |  |
| `source` | `"interactive"` \| `"rpc"` \| `"extension"` \| `"bus"` \| `"timer"` \| `"webhook"` \| `"voice"` \| `"agent"` | yes |  |
| `submitter` | string | yes |  |
| `submission_id` | string | yes |  |
| `images` | list of object \| null | yes |  |
| `multitask_strategy` | `"reject"` \| `"enqueue"` \| `"steer"` \| `"rollback"` \| `"fork"` | yes |  |
| `expand_commands` | boolean | yes |  |
| `allow_user_input` | boolean | yes |  |
| `store_history` | boolean | yes |  |
| `silent` | boolean | yes |  |
| `correlation` | [Correlation](#correlation) | yes |  |
| `depth` | integer | yes |  |

### SubmissionResult

The outcome of a `Submission` — LSP's `ApplyWorkspaceEditResult` shape: a refusal is a **result**, not an exception (decision-adjacent to the spec's "Five mechanisms" point 5).

| Field | Type | Required | Description |
|---|---|---|---|
| `accepted` | boolean | yes |  |
| `submission_id` | string | yes |  |
| `rejection_reason` | string \| null | no |  |
| `messages` | list of object | no |  |
| `command` | [FlowStep](#flowstep) \| [Ready](#ready) \| [Performed](#performed) \| [View](#view) \| null | no |  |
| `lock` | [ExtensionRequestRecord](#extensionrequestrecord) \| null | no |  |

### SubmissionStartChannel

The `submission_start` channel.

| Field | Type | Required | Description |
|---|---|---|---|
| `payload` | [SubmissionStartPayload](#submissionstartpayload) | yes |  |
| `name` | `"submission_start"` | yes |  |

### SubmissionStartPayload

A submission admitted to run a turn.

| Field | Type | Required | Description |
|---|---|---|---|
| `submission` | [SubmissionInfo](#submissioninfo) | yes |  |
| `text` | string | yes |  |
| `images` | list of object \| null | yes |  |
| `cursor_id` | string \| null | yes | The cursor its turn extends. |
| `owner_id` | string \| null | yes | That cursor's owner, for a sub-agent's turn. |
| `attachments` | [AttachmentReport](#attachmentreport) \| null | yes | What `expand_attachments` did, or `None` when it was not asked. |

### SubmitResult

The answer to `Submit`: how the submission ended. A refusal is an answer, not an error.

| Field | Type | Required | Description |
|---|---|---|---|
| `accepted` | boolean | yes |  |
| `submission_id` | string | yes |  |
| `reason` | string \| null | no |  |
| `command` | [DispatchedCommand](#dispatchedcommand) \| null | no | When the text was a command, what it resolved to. The daemon performs a `Ready` itself, except `fork` and `switch_session`. |

### Surface

What a session answers beyond its tree, which a head reads without a round trip.

| Field | Type | Required | Description |
|---|---|---|---|
| `commands` | list of [CommandInfo](#commandinfo) | yes | Every command, built-in and extension, in resolution order. |
| `command_args` | object of string \| null | yes | Each extension command's argument hint, or `None`. |
| `shortcuts` | list of list of string | yes | `[key, command, args, description]` per extension shortcut. |
| `extensions` | list of list of any | yes | `[path, enabled]` per managed extension. |
| `loaded` | list of [ExtensionInfo](#extensioninfo) | yes | Every loaded extension and what it registered. |
| `load_errors` | list of list of string | yes | `[path, error]` per extension file that failed to load. |

### SystemMessage

The system prompt, which a session stores as its first message entry.

| Field | Type | Required | Description |
|---|---|---|---|
| `role` | `"system"` | yes |  |
| `content` | string | yes |  |

### TextContent

A text content block in a message.

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | `"text"` | yes |  |
| `text` | string | yes |  |

### ThinkingChangeEntry

Legacy: the reasoning level from here on, before config entries.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | absent | no |  |
| `type` | `"thinking_change"` | yes |  |
| `level` | string \| null | yes |  |

### ThinkingContent

A thinking/reasoning content block.

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | `"thinking"` | yes |  |
| `thinking` | string | yes |  |
| `cached_tokens` | integer | no |  |
| `thinking_signature` | string \| object | no |  |

### ToolCall

A tool call content block in a message.

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | `"toolCall"` | yes |  |
| `id` | string | yes |  |
| `name` | string | yes |  |
| `arguments` | object | yes |  |
| `provider_signature` | object | no |  |

### ToolResultMessage

A tool result message.

| Field | Type | Required | Description |
|---|---|---|---|
| `role` | `"toolResult"` | yes |  |
| `tool_call_id` | string | yes |  |
| `tool_name` | string | yes |  |
| `content` | list of [TextContent](#textcontent) \| [ImageContent](#imagecontent) | yes |  |
| `details` | object \| null | no |  |
| `is_error` | boolean | no |  |
| `timestamp` | integer | yes |  |

### TreeResult

The answer to `GetTree`.

| Field | Type | Required | Description |
|---|---|---|---|
| `nodes` | list of [TreeRow](#treerow) | yes |  |
| `leaf` | string \| null | yes | The cursor's leaf: the one row whose `is_leaf` is true. |
| `count` | integer | yes | How many rows. |

### TreeRow

One `GetTree` row: RPC `get_tree`'s node.

| Field | Type | Required | Description |
|---|---|---|---|
| `entry_id` | string | yes |  |
| `parent_id` | string \| null | yes |  |
| `kind` | string | yes | The entry's `type`. |
| `role` | string \| null | yes | The message role; `None` on a bookkeeping entry. |
| `preview` | string | yes | The entry's first line. |
| `is_leaf` | boolean | yes | Whether this entry is the cursor's leaf. |
| `timestamp` | integer \| null | yes | Epoch milliseconds, or `None`. |
| `first_kept_id` | string \| null | yes | On a `compaction` or `elide`, the oldest entry kept. |
| `from_id` | string \| null | yes | On a `branch_summary`, the branch head it summarizes. |
| `is_system` | boolean | yes | Whether this is the system prompt. |
| `tool_call_ids` | list of string | yes | The tool call ids an assistant message declares. |
| `tool_call_id` | string \| null | yes | The call a tool result answers. |
| `copyable` | boolean | yes | Whether `paste_subtree` can take this entry as its source. |
| `estimated_tokens` | integer | yes | An estimate of the entry's tokens, 0 when it holds no message. |

### UiEvent

A `ui` event.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `epoch` | string | yes |  |
| `seq` | integer | yes |  |
| `kind` | `"ui"` | yes |  |
| `data` | [UiEventData](#uieventdata) | yes |  |
| `type` | `"event"` | yes |  |

### UiEventData

An extension display call, told apart by `op`.

One of: [UiNotify](#uinotify), [UiStatus](#uistatus), [UiPanel](#uipanel).

### UiNotify

An extension's notification.

| Field | Type | Required | Description |
|---|---|---|---|
| `message` | string | yes |  |
| `level` | string | yes |  |
| `op` | `"notify"` | yes |  |

### UiPanel

An extension's panel under `key`; `None` closes it.

| Field | Type | Required | Description |
|---|---|---|---|
| `key` | string | yes |  |
| `spec` | [PanelSpec](#panelspec) \| null | yes |  |
| `op` | `"panel"` | yes |  |

### UiStatus

An extension's status-line text under `key`; `None` clears it.

| Field | Type | Required | Description |
|---|---|---|---|
| `key` | string | yes |  |
| `text` | string \| null | yes |  |
| `op` | `"status"` | yes |  |

### Usage

Token usage information for an LLM response.

| Field | Type | Required | Description |
|---|---|---|---|
| `input_tokens` | integer | no |  |
| `output_tokens` | integer | no |  |
| `cache_read_tokens` | integer | no |  |
| `cache_write_tokens` | integer | no |  |
| `cache_reported` | boolean | no |  |
| `total_tokens` | integer | no |  |
| `cost` | object of number | no |  |
| `extra` | object | no |  |

### UserMessage

A user message.

| Field | Type | Required | Description |
|---|---|---|---|
| `role` | `"user"` | yes |  |
| `content` | string \| list of [TextContent](#textcontent) \| [ImageContent](#imagecontent) | yes |  |
| `timestamp` | integer | yes |  |

### ValueAnswer

A `Perform` whose operation returned a plain value.

| Field | Type | Required | Description |
|---|---|---|---|
| `value` | any | yes | `navigate_tree`, `elide_span` and `commit_branch` return the context's messages; `paste_subtree` the minted ids; `list_managed_extensions` `[path, enabled]` pairs; `compact` `null` when there was nothing to compact. |
| `kind` | `"value"` | yes |  |

### View

A named surface only a head can open.

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | The view's name, a key of `tau_agent_core.capabilities.VIEW_COMMANDS`. |
| `state` | object \| null | no | What a head draws the view from. `None` everywhere today — no capability projects the session tree yet (docs/VSCODE-HEAD.md §6), and this is the spot that payload lands in when one does, with no change to the union. |
| `unavailable_because` | string \| null | no | Why no `state` rides with this, in a sentence a head can print. A head that has its own view of that name ignores it and opens it; a head that has none prints it and does nothing else. Not a fallback: it is the same idiom the RPC table's seven `declined_because` entries already use. |

### ViewArm

A named surface only a head can open.

| Field | Type | Required | Description |
|---|---|---|---|
| `arm` | `"View"` | yes |  |
| `name` | string | yes | The view's name, a key of `tau_agent_core.capabilities.VIEW_COMMANDS`. |
| `state` | object \| null | no | What a head draws the view from. `None` everywhere today — no capability projects the session tree yet (docs/VSCODE-HEAD.md §6), and this is the spot that payload lands in when one does, with no change to the union. |
| `unavailable_because` | string \| null | no | Why no `state` rides with this, in a sentence a head can print. A head that has its own view of that name ignores it and opens it; a head that has none prints it and does nothing else. Not a fallback: it is the same idiom the RPC table's seven `declined_because` entries already use. |

### WireEvent

The wire projection of ``AgentEvent`` (D3) — REMOTE-CONTROL.md's designed shape, and (as of unit 2B) what ``rpc/handler.py`` actually sends: ``rpc/wire_events.py`` constructs instances of this class rather than a hand-shaped dict. See the module docstring's Status note and the field-by-field comment above this class.

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | `"agent_start"` \| `"agent_end"` \| `"turn_start"` \| `"turn_end"` \| `"message_start"` \| `"message_update"` \| `"message_end"` \| `"tool_execution_start"` \| `"tool_execution_update"` \| `"tool_execution_end"` \| `"side_completion_start"` \| `"side_completion_update"` \| `"side_completion_end"` | yes | Event type discriminator. |
| `timestamp` | integer | yes | Milliseconds since epoch. |
| `turn_index` | integer \| null | no | Turn number (turn_*). |
| `tool_call_id` | string \| null | no | Tool call id (tool_*). |
| `tool_name` | string \| null | no | Tool name (tool_*). |
| `is_error` | boolean | no | Whether this event represents an error. |
| `error` | string \| null | no | Why an agent_end closed when the loop raised rather than finishing (e.g. 'RuntimeError: Connection refused'). None on a normal close; always paired with is_error=True when set. Without it 'the agent finished' and 'the agent died mid-turn' are the same event on the wire. |
| `end_reason` | `"done"` \| `"terminate"` \| `"aborted"` \| `"max_turns"` \| `"repeat_tool_calls"` \| `"error"` \| null | no | How an agent_end closed: 'done' (the model had nothing more to say), 'terminate' (a tool asked to stop), 'aborted', 'max_turns' (the ceiling truncated the run), 'repeat_tool_calls' (the loop stopped itself because the model kept repeating an identical, wholly-failing batch) or 'error'. None on every other event type. `error` says whether the loop raised; this says how it stopped when it did not, which is what tells a host that an answer is TRUNCATED rather than finished. |
| `blocked` | boolean | no | Whether a tool_execution_end is an extension veto (S50), distinct from a generic errored result. |
| `blocked_by` | string \| null | no | The extension that vetoed the call; paired with blocked. |
| `cursor_id` | string \| null | no | The cursor whose turn emitted this event (docs/CURSORS.md §6). Several cursors run turns on one session at once; a host routes by this. None outside a turn. |
| `submission_id` | string \| null | no | The Submission that drove this turn, if any (E4/G6). None for an event from a call that never went through submit()/prompt() — never a fabricated id. |
| `source` | `"interactive"` \| `"rpc"` \| `"extension"` \| `"bus"` \| `"timer"` \| `"webhook"` \| `"voice"` \| `"agent"` \| null | no | The submission's origin (E4). None alongside submission_id. |
| `submitter` | string \| null | no | WHO submitted (E4). None alongside submission_id. |
| `correlation` | object \| null | no | The submission's free-form origin detail (E4). None alongside submission_id — an empty dict would claim a submission with no correlation data, which is a different statement. |
| `delta` | string \| null | no | One text fragment that arrived. On message_update (E1) it is a diffable content-block's prefix-diff against the previous message_update in the same turn, never the cumulative message; only a diffable block kind sets it (see block_type), and a non-diffable block change (e.g. a growing toolCall) produces no wire event. On side_completion_update it is the next fragment of the summary. Both are applied the same way, which is why they share a field rather than asking a client to keep two accumulators — see `replace`. None for all other event types. |
| `block_type` | `"text"` \| `"thinking"` \| null | no | Which diffable content-block kind `delta` belongs to. Set exactly when `delta` is set — 'text' on a side_completion_update, which has no other kind. |
| `replace` | boolean | no | Only meaningful when delta is set. False (the common case): delta is an incremental suffix — append it to whatever was already accumulated for this block_type this turn. True: the provider replaced rather than extended the block's content — delta is the block's ENTIRE new value, and the receiver must RESET its accumulator to delta rather than appending. Mirrors event_projection.BlockDelta.replace exactly. |
| `message_count` | integer \| null | no | Count of messages produced this turn, on agent_end (E2). The messages themselves are pulled via get_messages, never pushed. None for all other event types. |
| `stop_reason` | `"stop"` \| `"length"` \| `"toolUse"` \| `"error"` \| `"aborted"` \| null | no | Why the model stopped this completion, on the message_end that carries usage. 'length' means the output cap ended it, so the content is a PREFIX and not an answer — the one value an operator has to act on. None on the content-only duplicate message_end (which carries no usage either) and on every other event type. This rides a field of its own because the message it belongs to is excluded from the wire; it is a closed enum, not unbounded content. See docs/TRUNCATED-TOOL-CALLS.md. |
| `dropped_tool_calls` | integer \| null | no | How many tool calls this completion lost because the stream ended mid-argument, on message_end. A truncated or aborted arguments buffer is a prefix, so the provider drops the call rather than running it on a repaired or empty payload, and this is the only record that it existed. Null rather than 0 when none were dropped, so 'none lost' and 'not reported' stay distinguishable. None for all other event types. |
| `cache_notice` | string \| null | no | One sentence saying this turn's prompt cache should have been read and was not, on agent_end. Null is the normal case and says nothing was observed: the cache was read, the server accounts for no cache, the prompt is under the minimum cacheable prefix, or a read earlier in this session already proved caching is on. A host renders it as a warning; see docs/PROMPT-CACHING.md §7 for the three gates. None for all other event types. |
| `purpose` | `"compaction"` \| `"branch_summary"` \| null | no | Which side completion a side_completion_* event reports: 'compaction' or 'branch_summary'. Side work spends tokens and produces text outside any turn, so it stamps no submission_id and a host keys on this instead. The summary TEXT and what it cost are deliberately not on the wire — both land in the session log as a compaction or branch_summary entry, which get_entry serves with tokens_before, covered_tokens and summary_usage attached. Pushing unbounded content through an event is what WireEvent exists to avoid. None for all other event types. See docs/STREAMING-SIDE-WORK.md. |
| `reason` | `"manual"` \| `"threshold"` \| `"navigate"` \| null | no | What asked for a side completion: 'manual' (a person or this host), 'threshold' (the auto-trigger, which nobody asked for) or 'navigate' (the tree browser's summarising move). On all three side_completion_* events, so a host that attached mid-summary still learns whether the work was requested or imposed. None for all other event types. |
| `leaf` | string \| null | no | The emitting cursor's resulting leaf, on agent_end (E5/F3). Filled in by rpc/transport.py's writer immediately before this line is serialized — not by rpc/wire_events.py at event-projection time — because persistence happens strictly AFTER agent_end fires; reading it any earlier reproduces the exact stale-tip bug this field exists to close. None for all other event types. |
