# τ serve protocol

> **Generated — do not hand-edit.** Run `python scripts/generate_serve_protocol.py`
> after changing `tau_coding_agent/serve/protocol.py`, and commit both outputs.
> `tests/test_serve.py` fails if this file or the schema is stale.
>
> Design of record: `docs/TAU-SERVE.md` §5–§7.

- **Protocol version:** `0.6`
- **Default port:** `8256`
- **Counts:** 49 requests (35 of them RPC verbs), 10 event kinds, 193 schema definitions. Cite this line; never copy the numbers into hand-written prose.
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

Every session in the daemon's store, across every cwd, newest first (RPC `list_sessions`).

No fields.

**Answered with:** [SessionList](#sessionlist).

### `new_session`

Create a session in `cwd`, a path on the daemon's machine, and load it (RPC `new_session`).

A connection has no current session to replace, so nothing moves: attach to
the answer's `session.session_id` to drive it. RPC's `persist` is absent,
because every daemon session is stored.

| Field | Type | Required | Description |
|---|---|---|---|
| `cwd` | string | yes | The directory its tools run in; the create fails if it does not exist. |
| `model` | string \| null | no | A model name from the daemon's config, or `None` for its default. |
| `name` | string \| null | no | A session name, or `None`. |

**Answered with:** [SessionOpened](#sessionopened).

### `fork`

Copy the path to a cursor's leaf into a new session in the same cwd, and load it (RPC `fork`).

The source is unchanged and the client stays attached to it. Refused with
`busy` when the copy would carry an entry a turn is still writing.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `cursor_id` | string | yes |  |
| `at` | string \| null | no | The entry to fork at instead of the cursor's leaf. |

**Answered with:** [SessionOpened](#sessionopened).

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

### `answer`

Answer an extension's form (a `request` event); the first answer wins.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `request_id` | string | yes |  |
| `value` | object \| null | yes | The `{field: value}` answers, or `None` to cancel the form. |

**Answered with:** null.

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

## RPC verbs

A request RPC answers too: its verb, at one cursor of one session (docs/TAU-SERVE.md §5, 0.6).

Sent as `{"type": verb, "session_id", "cursor_id", **params}`; `params`
are RPC's own and are checked against its `params_schema`.

Each takes RPC's params as documented in `docs/RPC-PROTOCOL.md`, plus `session_id` and `cursor_id`, and answers RPC's result.

### `submit`

RPC `submit` (docs/RPC-PROTOCOL.md) at one cursor. Answered at admission, as over stdio, and its turn ends with a `submission_end` channel event. `multitask_strategy: "fork"` is accepted. A command answers success with `dispatched`, the arm it resolved to, where stdio refuses a step or a ready flow: the daemon performs a ready flow itself, except `fork` and `switch_session`, which come back for the client.

**Answered with:** [SubmitResult](#submitresult).

### `prompt`

RPC `prompt` (docs/RPC-PROTOCOL.md) at one cursor. `submit` with RPC's provenance defaults.

**Answered with:** [PromptResult](#promptresult).

### `abort`

RPC `abort` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [AbortResult](#abortresult).

### `compact`

RPC `compact` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [CompactResult](#compactresult).

### `get_state`

RPC `get_state` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [GetStateResult](#getstateresult).

### `get_messages`

RPC `get_messages` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [GetMessagesResult](#getmessagesresult).

### `get_commands`

RPC `get_commands` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [GetCommandsResult](#getcommandsresult).

### `get_tools`

RPC `get_tools` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [GetToolsResult](#gettoolsresult).

### `get_models`

RPC `get_models` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [GetModelsResult](#getmodelsresult).

### `get_session_name`

RPC `get_session_name` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [GetSessionNameResult](#getsessionnameresult).

### `get_session_stats`

RPC `get_session_stats` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [GetSessionStatsResult](#getsessionstatsresult).

### `get_last_assistant_text`

RPC `get_last_assistant_text` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [GetLastAssistantTextResult](#getlastassistanttextresult).

### `set_model`

RPC `set_model` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [SetModelResult](#setmodelresult).

### `set_auto_compaction`

RPC `set_auto_compaction` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [SetAutoCompactionResult](#setautocompactionresult).

### `set_session_name`

RPC `set_session_name` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [SetSessionNameResult](#setsessionnameresult).

### `complete_path`

RPC `complete_path` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [CompletePathResult](#completepathresult).

### `next_step`

RPC `next_step` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [NextStepResult](#nextstepresult).

### `enumerate_domain`

RPC `enumerate_domain` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [EnumerateDomainResult](#enumeratedomainresult).

### `complete_message_id`

RPC `complete_message_id` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [CompleteMessageIdResult](#completemessageidresult).

### `get_tree`

RPC `get_tree` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [GetTreeResult](#gettreeresult).

### `get_entry`

RPC `get_entry` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [GetEntryResult](#getentryresult).

### `get_pending_request`

RPC `get_pending_request` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [GetPendingRequestResult](#getpendingrequestresult).

### `answer_request`

RPC `answer_request` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [AnswerRequestResult](#answerrequestresult).

### `list_managed_extensions`

RPC `list_managed_extensions` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [ListManagedExtensionsResult](#listmanagedextensionsresult).

### `get_extension_state`

RPC `get_extension_state` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [GetExtensionStateResult](#getextensionstateresult).

### `get_extension_config`

RPC `get_extension_config` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [GetExtensionConfigResult](#getextensionconfigresult).

### `set_extension_config`

RPC `set_extension_config` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [SetExtensionConfigResult](#setextensionconfigresult).

### `enable_extension`

RPC `enable_extension` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [EnableExtensionResult](#enableextensionresult).

### `disable_extension`

RPC `disable_extension` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [DisableExtensionResult](#disableextensionresult).

### `reload_extension`

RPC `reload_extension` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [ReloadExtensionResult](#reloadextensionresult).

### `navigate`

RPC `navigate` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [NavigateResult](#navigateresult).

### `summarize_and_navigate`

RPC `summarize_and_navigate` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [SummarizeAndNavigateResult](#summarizeandnavigateresult).

### `elide_span`

RPC `elide_span` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [ElideSpanResult](#elidespanresult).

### `commit_branch`

RPC `commit_branch` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [CommitBranchResult](#commitbranchresult).

### `paste_subtree`

RPC `paste_subtree` (docs/RPC-PROTOCOL.md) at one cursor. The daemon runs RPC's own handler at `cursor_id`.

**Answered with:** [PasteSubtreeResult](#pastesubtreeresult).

## Responses

The one answer to a request, matched by `id`.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | integer | yes |  |
| `ok` | boolean | yes |  |
| `result` | any | no | When `ok`, the request's result: `Results[request.type]` in the schema. `null` when not `ok`. |
| `error` | [Error](#error) \| null | no |  |

Error codes: `bad_request`, `unauthorized`, `protocol_mismatch`, `not_found`, `busy`, `failed`, `submission_rejected`, `command_not_supported`, `session_not_persisted`.

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
| `compaction_end` | [CompactionEnd](#compactionend) |

What each event kind carries in `data`.

`agent_event` is RPC's `WireEvent`, built by the same
`tau_agent_core.rpc.wire_events.WireEventProjector`: `message_update`
carries a delta, never the whole message. Unbounded fields (a tool's arguments
and result, a message's content and usage) are left out; the entry events carry
them. It changes no state.

`compaction_end` is RPC's notification of that name, for a `compact` this
session was asked for, and changes no state either: the compaction entry's own
`entry_append` does.

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

### AbortResult

| Field | Type | Required | Description |
|---|---|---|---|
| `status` | `"aborted"` | yes | Always 'aborted'. |
| `compaction_id` | string \| `null` | yes | The compaction this abort's signal was delivered to, or null when none was in flight (finding 5, Tier B review). Present so a host knows to expect a compaction_end carrying cancelled: true for that id. Whether the compaction actually stopped is reported THERE and not here — same signal-vs-outcome split that keeps `leaf` off this response. |

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

### AnswerRequestResult

| Field | Type | Required | Description |
|---|---|---|---|
| `handled` | boolean | yes | Whether the extension that raised the request was loaded and ran its action. FALSE still means the response was appended and the lock released — a lock whose owner cannot answer must not become a session nobody can continue — so a host reports it as a warning and carries on, rather than as a failure to retry. |
| `output` | string \| `null` | yes | What the dispatched command produced, or null. |
| `leaf` | string \| `null` | yes | cursor.leaf after the response was appended (E5 rule 1). The append is what RELEASES the lock — appending moves the cursor and a lock is read at the cursor — so this value is the evidence the session is answerable again. |

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

What a submission's `expand_attachments` did.

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
| `copiedFrom` | string | no |  |
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

### CommandOutput

An extension command's completion, as `submit`'s answer names it.

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string \| null | yes | The command that ran, which an input hook may have rewritten. |
| `output` | string \| null | yes | What it returned, as display text, or `None`. |

### CommitBranchResult

| Field | Type | Required | Description |
|---|---|---|---|
| `messages` | list of [ContextMessage](#contextmessage) | yes | ConversationTree.context_for(cursor) after the mutation — the same flat message array get_messages returns, for the path this call just produced. Returned rather than left for a follow-up get_messages because the mutation's whole product is a different context, and a host that had to fetch it separately could render the old one in between. |
| `leaf` | string \| `null` | yes | cursor.leaf after the mutation (E5 rule 1). |

### CompactResult

| Field | Type | Required | Description |
|---|---|---|---|
| `accepted` | boolean | yes | Always true — the compaction was admitted and is now running in the background. A refusal is an error response instead (TURN_STILL_RUNNING), never accepted: false. |
| `compaction_id` | string | yes | Correlates this acknowledgement to the compaction_end notification that reports the outcome. Server-generated; a host does not supply it. |

### CompactionEnd

| Field | Type | Required | Description |
|---|---|---|---|
| `compaction_id` | string | yes | The id the compact acknowledgement returned (correlation). |
| `request_id` | integer \| `null` | yes | The JSON-RPC id of the compact request that started this compaction — null when that request was a notification (no id), which is the one case compaction_id is the only correlation handle. |
| `is_error` | boolean | yes | True when AgentSession.compact() raised (e.g. CompactionError — summary generation failed and, Fail-Early, nothing was written). `error` carries the detail and `performed` is absent. |
| `error` | string \| `null` | no | The exception's repr when is_error, else null. |
| `cancelled` | boolean | yes | True when a host's `abort` stopped this compaction part-way (finding 5, Tier B review). Nothing was written — the summary is generated before the entry is appended — so `performed` is ABSENT, exactly as it is when is_error is true, and `leaf` is the unchanged tip. False on every other outcome rather than omitted: absence is not this tier's way of saying anything (E5 rule 3). A compaction cancelled by SHUTDOWN never reaches this notification at all — that one reports on stderr (D-5, T4). |
| `performed` | boolean | no | False when AgentSession.compact() returned None — a real outcome (nothing to compact), not an error, and the expected answer under the shipped keep_recent_tokens for any conversation smaller than it, because the cut then removes nothing. Absent entirely when is_error or cancelled is true. Every CompactionResult field below is absent unless this is true. |
| `summary` | string | no | CompactionResult.summary — the generated text. |
| `first_kept_entry_id` | string | no | Session-log entry id of the first entry kept verbatim after the cut. |
| `tokens_before` | integer | no | Estimated context tokens before this compaction. |
| `tokens_saved` | integer | no | Estimated context tokens this compaction removed: the summarized prefix, less the summary that replaces it. NOT tokens_before less the summary — tokens_before includes the recent context the cut keeps. May be negative when the summary is larger than the prefix it replaced; that is reported rather than clamped to 0. |
| `compacted_entry_ids` | list of string | no | Session-log entry ids folded into the summary. |
| `read_files` | list of string | no | CompactionDetails.read_files ([] when details is None). |
| `modified_files` | list of string | no | CompactionDetails.modified_files ([] when details is None). |
| `usage` | [Usage](#usage) | no | What GENERATING this summary cost (CompactionResult.usage) — routinely the priciest single call in a session; distinct from tokens_saved, which is what compaction bought. |
| `leaf` | string \| `null` | yes | cursor.leaf once the compaction finished (E5/F3): the post-compaction tip when performed is true, else the unchanged tip. |

### CompactionEndEvent

A `compaction_end` event.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `epoch` | string | yes |  |
| `seq` | integer | yes |  |
| `kind` | `"compaction_end"` | yes |  |
| `data` | [CompactionEnd](#compactionend) | yes |  |
| `type` | `"event"` | yes |  |

### CompactionEntry

A summary that replaces the path before `firstKeptId` in the context.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | absent | no |  |
| `copiedFrom` | string | no |  |
| `type` | `"compaction"` | yes |  |
| `summary` | string | yes |  |
| `firstKeptId` | string | yes |  |
| `tokensBefore` | integer | yes |  |
| `summarizerModelId` | string | no |  |
| `summaryUsage` | object of integer | no |  |
| `coveredEntries` | integer | no |  |
| `coveredTokens` | integer | no |  |
| `configId` | string \| null | no |  |

### CompactionSettingsRecord

When compaction runs (`compaction.CompactionSettings`).

| Field | Type | Required | Description |
|---|---|---|---|
| `enabled` | boolean | yes |  |
| `reserve_tokens` | integer | yes |  |
| `keep_recent_tokens` | integer | yes |  |

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

### CompleteMessageIdResult

| Field | Type | Required | Description |
|---|---|---|---|
| `matches` | list of [MessageMatch](#messagematch) | yes | The candidates in tree order (root-most first), as [{entry_id, preview}]: `entry_id` is the value every message_id argument takes, and `preview` is the entry's first line — the row the tree browser draws. Bounded by `limit`. |
| `total` | integer | yes | How many entries matched BEFORE `limit` was applied, so a host is told it is seeing a prefix rather than shown one silently (G3, the rule complete_path already follows). |

### CompletePathResult

| Field | Type | Required | Description |
|---|---|---|---|
| `completion` | [AttachmentCompletion](#attachmentcompletion) \| null | yes | `null` when `offset` is not inside an @reference at all — the host shows no popup. Otherwise {start, end, token, matches, total}: `start`/`end` are the character span of the whole @word, so a host replaces that span rather than guessing where the token began; `matches` is a list of {name, detail, is_dir}, `name` being the text that goes AFTER the @ (directories end in '/'); `total` is how many entries matched before the list was bounded, so a host can say '12 of 340' instead of implying it showed everything. An EMPTY `matches` with a non-null completion is the 'this names no file' warning, not an absence of information. |

### ContextEstimate

The context's size, as `get_session_stats` measures it (`compaction.ContextUsageEstimate`).

| Field | Type | Required | Description |
|---|---|---|---|
| `tokens` | integer | yes |  |
| `usage_tokens` | integer | yes |  |
| `trailing_tokens` | integer | yes |  |
| `last_usage_index` | integer \| null | yes |  |

### ContextMessage

A message of model input: a stored message, or a summary rendered as a user message, which alone has no `timestamp`.

One of: [UserMessage](#usermessage), [SummaryMessage](#summarymessage), [AssistantMessage](#assistantmessage), [ToolResultMessage](#toolresultmessage), [SystemMessage](#systemmessage), [CustomRoleMessage](#customrolemessage).

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
| `copiedFrom` | string | no |  |
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
| `copiedFrom` | string | no |  |
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

### DisableExtensionResult

| Field | Type | Required | Description |
|---|---|---|---|
| `action` | `"enable"` \| `"disable"` \| `"reload"` \| `"configure"` | yes | Which action ran — echoes the verb. |
| `path` | string | yes | The managed path the action resolved to. NOT always what was sent: `path` accepts a file stem as well as a full path, and this is the full path it matched. On a failed resolution it is the unresolved string, so a host can quote back what it asked for. |
| `ok` | boolean | yes | Whether the action changed anything. false is a reportable no-op, never an error: an unknown target, an already-enabled extension, an already-disabled one. A hard failure — a file that no longer imports, which only reload can hit — RAISES instead and reaches the host as INTERNAL_ERROR, with the extension left torn down. |
| `message` | string | yes | The human-readable line, the same one the TUI listing shows. |
| `leaf` | string \| `null` | yes | cursor.leaf — E5 rule 1 on a mutator whose whole product is runtime state. It is the live tip reported as a READ, not a claim that this call wrote anything; the same reading set_auto_compaction's cursor already has. |

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

One legal value of a domain: `value` is what is bound, `label` what is shown.

| Field | Type | Required | Description |
|---|---|---|---|
| `value` | string | yes |  |
| `label` | string | yes |  |

### ElideEntry

A splice anchor with no summary: the path before `firstKeptId` leaves the context.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | absent | no |  |
| `copiedFrom` | string | no |  |
| `type` | `"elide"` | yes |  |
| `firstKeptId` | string | yes |  |
| `coveredEntries` | integer | no |  |
| `coveredTokens` | integer | no |  |
| `configId` | string \| null | no |  |

### ElideSpanResult

| Field | Type | Required | Description |
|---|---|---|---|
| `messages` | list of [ContextMessage](#contextmessage) | yes | ConversationTree.context_for(cursor) after the mutation — the same flat message array get_messages returns, for the path this call just produced. Returned rather than left for a follow-up get_messages because the mutation's whole product is a different context, and a host that had to fetch it separately could render the old one in between. |
| `leaf` | string \| `null` | yes | cursor.leaf after the mutation (E5 rule 1). |

### EnableExtensionResult

| Field | Type | Required | Description |
|---|---|---|---|
| `action` | `"enable"` \| `"disable"` \| `"reload"` \| `"configure"` | yes | Which action ran — echoes the verb. |
| `path` | string | yes | The managed path the action resolved to. NOT always what was sent: `path` accepts a file stem as well as a full path, and this is the full path it matched. On a failed resolution it is the unresolved string, so a host can quote back what it asked for. |
| `ok` | boolean | yes | Whether the action changed anything. false is a reportable no-op, never an error: an unknown target, an already-enabled extension, an already-disabled one. A hard failure — a file that no longer imports, which only reload can hit — RAISES instead and reaches the host as INTERNAL_ERROR, with the extension left torn down. |
| `message` | string | yes | The human-readable line, the same one the TUI listing shows. |
| `leaf` | string \| `null` | yes | cursor.leaf — E5 rule 1 on a mutator whose whole product is runtime state. It is the live tip reported as a READ, not a claim that this call wrote anything; the same reading set_auto_compaction's cursor already has. |

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

### EnumerateDomainResult

| Field | Type | Required | Description |
|---|---|---|---|
| `domain` | string | yes | The domain that was enumerated. |
| `values` | list of [DomainChoice](#domainchoice) | yes | A list of {value, label}. `value` is what a host binds into `next_step`'s `bound`; `label` is what it shows. They are equal for a domain whose values already read as text. |
| `total` | integer | yes | How many values matched before `limit` was applied, so a host says '12 of 340' instead of implying it showed everything (G3). An empty `values` with a non-zero `total` cannot happen; an empty one with total 0 means the domain genuinely has none. |

### Error

Why a request failed. `code` is stable; `message` is for a human.

| Field | Type | Required | Description |
|---|---|---|---|
| `code` | `"bad_request"` \| `"unauthorized"` \| `"protocol_mismatch"` \| `"not_found"` \| `"busy"` \| `"failed"` \| `"submission_rejected"` \| `"command_not_supported"` \| `"session_not_persisted"` | yes |  |
| `message` | string | yes |  |
| `data` | object \| null | no | RPC's `error.data` for an RPC verb's refusal (a submission's `lock`, the offending `name`), else `None`. |

### Event

A push for an attached session, numbered per session within an `epoch`.

One of: [EntryOpenEvent](#entryopenevent), [EntryFinalEvent](#entryfinalevent), [EntryAppendEvent](#entryappendevent), [AgentEventEvent](#agenteventevent), [ChannelEvent](#channelevent), [CursorsEvent](#cursorsevent), [RequestEvent](#requestevent), [RequestClosedEvent](#requestclosedevent), [UiEvent](#uievent), [CompactionEndEvent](#compactionendevent).

### ExtensionInfo

One loaded extension and what it registered, as `get_extension_state` lists it.

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

An extension request at a cursor, as `get_pending_request` answers it (docs/EXTENSION-LOCKS.md).

| Field | Type | Required | Description |
|---|---|---|---|
| `entry_id` | string | yes | The request entry's id; `answer_request` names it. |
| `extension` | string | yes |  |
| `extension_name` | string | yes | The display stem of `extension`. |
| `sentence` | string | yes |  |
| `label` | string | yes | τ's framing line for the request (§9). |
| `lock` | boolean | yes | Whether a submission at this cursor is refused. |
| `ask` | [Ask](#ask) \| null | yes | What it asks, or `None` for a bare lock. |
| `release` | string \| null | yes | A command that clears the lock, or `None`. |

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
| `copiedFrom` | string | no |  |
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

### GetCommandsResult

| Field | Type | Required | Description |
|---|---|---|---|
| `commands` | list of object | yes | Every slash command that resolves right now, built-ins first. A `name` has no leading '/' — submit it as ordinary text with expand_commands=true, not as an RPC method. |

### GetEntryResult

| Field | Type | Required | Description |
|---|---|---|---|
| `entry` | [Entry](#entry) | yes | The raw session-log entry, as stored: camelCase `parentId` / `firstKeptId` / `fromId`, a `type`, and whatever payload that type carries — a `message` for the message kinds, a `summary` for a compaction or a branch_summary. Handed over whole rather than projected, because the caller is a detail pane rendering ONE node and a projection would be a second message shape to keep in step with get_messages'. One node per call: get_tree carries a one-line preview per row precisely so a browser does not pull bodies it is not showing. |

### GetExtensionConfigResult

| Field | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes | The managed path the token resolved to, not the token sent. |
| `schema` | [FormSpec](#formspec) \| null | yes | The extension's CONFIG_SCHEMA, normalized at load into {title, fields} — the same spec shape ui.form takes, so a head that can render a form can render a settings screen with no new widget. null for an extension that declares none, which is the answer that tells a head to offer no screen rather than an empty one. |
| `values` | object | yes | The live slice api.config returns for this extension, keyed by file stem: config.json's extensions.<stem> with --ext-config overrides applied, plus any set_extension_config since. {} for an unconfigured extension — never the schema's defaults, which the extension itself supplies. |

### GetExtensionStateResult

| Field | Type | Required | Description |
|---|---|---|---|
| `extensions` | list of [ExtensionInfo](#extensioninfo) | yes | Every loaded extension and what it registered, as [{name, path, tools, commands, shortcuts, hooks, content_hash, subjects}] — sdk.summarize_extensions of the live registry, which is the same projection the TUI's /extensions listing draws. Read LIVE, not from the load-time snapshot, so a reload_extension is reflected here. |
| `errors` | list of [LoadError](#loaderror) | yes | Every discovered file that FAILED to load, as [{path, error}]. Kept from the last load_extensions call, because a failed import leaves nothing to recompute from. This is the half that makes this a read of its own rather than list_managed_extensions with more fields: a file that cannot import can never be a legal extension_name, and is exactly what a listing must show. |

### GetLastAssistantTextResult

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string \| `null` | yes | The last assistant message's concatenated 'text' content blocks, trimmed. null if no qualifying assistant message exists YET, or if one exists but it has no text (e.g. a pure tool-call turn) — the two cases are DELIBERATELY indistinguishable on the wire, matching pi's own `getLastAssistantText(): string \| undefined` (pi agent-session.ts:3092) and its RPC verb (rpc-mode.ts:609-612, docs/rpc.md: 'Returns {"text": null} if no assistant messages exist' — silent on the second null-producing case, because on the wire there is only one representable 'nothing' and pi does not either). |

### GetMessagesResult

| Field | Type | Required | Description |
|---|---|---|---|
| `messages` | list of [ContextMessage](#contextmessage) | yes | AgentSession.messages — the terminal, flat message array (E2's pull side). |

### GetModelsResult

| Field | Type | Required | Description |
|---|---|---|---|
| `models` | list of [ModelRecord](#modelrecord) | yes | Every config model NAME this child can switch to, sorted, as [{name, model}]: `name` is the exact string set_model's `name` param takes, and `model` is the SAME projection get_state publishes for the active model — {id, provider, context_window} — obtained by resolving `name` through the session's bound model resolver, i.e. by asking the one component set_model itself would ask. Empty only when the child's config declares no models; a resolver that cannot be enumerated is an INTERNAL_ERROR, never an empty list. |

### GetPendingRequestResult

| Field | Type | Required | Description |
|---|---|---|---|
| `request` | [ExtensionRequest](#extensionrequest) \| null | yes | The extension request AT THE CURSOR, or null when there is none. The cursor only, never an ancestry walk: the thing a user is looking at and the thing that refused their submission are one entry. {entry_id, extension, extension_name, sentence, label, lock, ask, release} — `label` is τ's own framing of the four states over `lock` and `ask`, `sentence` is the extension's own line, `ask` is a validated `ui.form` spec ({title, fields, actions}) or null, and `release` names a command that clears the lock (advisory: commands are exempt from a lock by placement, not by name). A host renders all four states; three of them draw something and the fourth is this verb answering null. |

### GetSessionNameResult

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string \| `null` | yes | The session's durable display name, or null if never set (extension_types.read_session_name). |

### GetSessionStatsResult

| Field | Type | Required | Description |
|---|---|---|---|
| `context` | [ContextEstimate](#contextestimate) | yes | estimate_context_tokens(session.messages) (compaction.py) projected as {tokens, usage_tokens, trailing_tokens, last_usage_index}: tokens is the total estimate the compaction threshold is checked against; usage_tokens is the anchored provider-reported count up to the last assistant Usage, trailing_tokens the heuristic estimate for messages after it, last_usage_index that message's index (null if no assistant Usage exists yet, in which case tokens==trailing_tokens and the whole list was heuristically estimated). |
| `context_window` | integer | yes | The active model's context_window (get_model()). |
| `context_headroom` | integer | yes | context_window - context.tokens. Can be negative: an honest over-budget number, never clamped to zero. |
| `compaction_settings` | [CompactionSettingsRecord](#compactionsettingsrecord) | yes | The session's EFFECTIVE CompactionSettings — {enabled, reserve_tokens, keep_recent_tokens}, read off AgentSession.compaction_settings, which hands back a COPY so a reader cannot retune a turn already in flight. An RPC session is CONSTRUCTED with enabled=False (backends.py:885) — that is how a host discovers auto-compaction is off (§1.1) — and set_auto_compaction (D-4, shipped in this same tier) is the one thing that changes it, so this reports the session's LIVE effective setting at call time, never a constant. |
| `last_compaction` | [LastCompaction](#lastcompaction) \| null | yes | {id, timestamp, summary, first_kept_id, tokens_before} for the most recent type=='compaction' entry in session_log.entries(), or null if this session has never compacted — an honest absence, never a fabricated entry. |
| `usage` | [Usage](#usage) \| null | yes | AgentSession.get_usage() — null before the first completion. |

### GetStateResult

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes | AgentSession.state.session_id. |
| `status` | `"idle"` \| `"running"` | yes | AgentSession.state.status. |
| `is_streaming` | boolean | yes | AgentSession.is_streaming. |
| `model` | [ModelSpec](#modelspec) | yes | AgentSession.get_model(): {id, provider, context_window}. |
| `usage` | [Usage](#usage) \| null | yes | AgentSession.get_usage() — null before the first completion. |
| `message_count` | integer | yes | len(AgentSession.messages). |
| `leaf` | string \| `null` | yes | cursor.leaf (F3: no host may cache 'the tip'). |
| `addressable` | boolean | yes | Whether the CURRENT session is persisted: true if list_sessions returns it and switch_session can reach it later. The same predicate new_session/fork/switch_session publish on their session tuple, asked about the session this connection is on right now. False means the appending verbs (set_model, set_session_name, compact — D-7) will refuse with -32004 SESSION_NOT_PERSISTED, and nothing this connection does is written to the store. Reachable without a respawn: new_session {"persist": true} moves onto a persisted session. |

### GetToolsResult

| Field | Type | Required | Description |
|---|---|---|---|
| `tools` | list of object | yes | This session's bound AgentTool set, in binding order. |

### GetTreeResult

| Field | Type | Required | Description |
|---|---|---|---|
| `nodes` | list of object | yes | Every entry in the log, in the order a browser draws them — preorder over the parent/child tree, roots in load order, children oldest first. FLAT, with `parent_id` carrying the shape: a nested projection of a long linear conversation is one nesting level per message, which is a serializer's recursion limit rather than a tree anyone wanted. Nothing is filtered out — which rows a browser declines to draw (a `navigate` with one child) is the reader's rule, not the log's. |
| `leaf` | string \| `null` | yes | cursor.leaf at the moment of the read, duplicated out of `nodes` so a host finds it without scanning. Null on a session whose cursor names no entry, which is also the one case in which no node carries is_leaf: true. |
| `count` | integer | yes | len(nodes). Present so a host can check it read a whole tree rather than a truncated one: this read is UNBOUNDED by design — the shape IS the answer and a bounded shape is a different tree — which is why it is a pull and is never pushed (G3). |

### HelloResult

The answer to `Hello`.

| Field | Type | Required | Description |
|---|---|---|---|
| `protocol` | string | yes | The daemon's `PROTOCOL_VERSION`. |
| `client_id` | string | yes | This connection's name in the daemon's log. |
| `pid` | integer | yes | The daemon's process id. |
| `version` | string | yes | τ's package version, as `tau --version` prints it. |
| `cwd` | string | yes | The daemon's working directory at start, absolute; a default for `NewSession`. |

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

### LastCompaction

The newest compaction entry on the path (`agent_session.CompactionRecord`).

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `timestamp` | string | yes | ISO-8601. |
| `summary` | string | yes |  |
| `first_kept_id` | string \| null | yes |  |
| `tokens_before` | integer \| null | yes |  |

### ListManagedExtensionsResult

| Field | Type | Required | Description |
|---|---|---|---|
| `extensions` | list of [ManagedExtension](#managedextension) | yes | Every file extension under management, in load order, as [{path, enabled}]. `path` is the exact string every extension_name argument takes (enable_extension, disable_extension, reload_extension); `enabled` is false exactly when the extension is loaded but its bucket has been removed from the runner, so its hooks, tools and slash commands are not offered. |

### LoadError

An extension file that failed to load, and why.

| Field | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes |  |
| `error` | string | yes |  |

### ManagedExtension

One managed extension file and whether it is enabled.

| Field | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes |  |
| `enabled` | boolean | yes |  |

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
| `copiedFrom` | string | no |  |
| `type` | `"message"` | yes |  |
| `message` | [Message](#message) | yes |  |

### MessageMatch

One entry `complete_message_id` offers: its id, and its first line.

| Field | Type | Required | Description |
|---|---|---|---|
| `entry_id` | string | yes |  |
| `preview` | string | yes |  |

### ModelChangeEntry

Legacy: the config model from here on, before config entries.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | absent | no |  |
| `copiedFrom` | string | no |  |
| `type` | `"model_change"` | yes |  |
| `model` | string \| null | yes |  |
| `backend` | string \| null | no |  |

### ModelRecord

One model the config defines, as `get_models` lists it.

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes |  |
| `model` | [ModelSpec](#modelspec) | yes |  |

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
| `copiedFrom` | string | no |  |
| `type` | `"navigate"` | yes |  |
| `targetId` | string \| null | yes |  |

### NavigateResult

| Field | Type | Required | Description |
|---|---|---|---|
| `messages` | list of [ContextMessage](#contextmessage) | yes | ConversationTree.context_for(cursor) after the mutation — the same flat message array get_messages returns, for the path this call just produced. Returned rather than left for a follow-up get_messages because the mutation's whole product is a different context, and a host that had to fetch it separately could render the old one in between. |
| `leaf` | string \| `null` | yes | cursor.leaf after the mutation (E5 rule 1). |

### NextStepResult

| Field | Type | Required | Description |
|---|---|---|---|
| `status` | `"step"` \| `"ready"` | yes | `step` — one required argument is still unbound and `step` describes it. `ready` — every required argument is bound and `ready` names the mutation to perform and what to perform it with. The two are mutually exclusive and exactly one is present. |
| `step` | [FlowStep](#flowstep) \| null | no | {flow, argument, domain, leaf, bound}. `argument` is {name, domain, description, cardinality, required, scope}; `domain` is the resolved domain record {name, description, free, values, enumerator}, included so a host can render the field without a second call — `values` is non-null for a small fixed set, and `enumerator` non-null means call `enumerate_domain` for the live set. |
| `ready` | [Ready](#ready) \| null | no | {flow, mutation, arguments}. `mutation` is the capability to perform — the named flow's, always, so a host that already knows which flow it stepped can dispatch before this returns. `arguments` is what to perform it with, keyed by the mutation's own parameter names. This is a commitment: τ does not ask a second time, and a host that wants a confirmation renders one from this. |

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

### PasteSubtreeResult

| Field | Type | Required | Description |
|---|---|---|---|
| `minted_ids` | list of string | yes | The ids minted, in the order they were appended. The first is the copy of `source_id` itself. Ids rather than messages because a paste edits the TREE and never moves the leaf: the current context is unchanged, so there is nothing to re-render until someone navigates onto the copy. |
| `leaf` | string \| `null` | yes | cursor.leaf after the paste — E5 rule 1, and here it is the UNCHANGED tip, present because absence is never a signal (rule 3), not because anything moved. |

### PathMatch

One path an `@` token can complete to.

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes |  |
| `detail` | string | yes |  |
| `is_dir` | boolean | yes |  |

### PerformedArm

What a capability produced. The past tense of `Ready`.

| Field | Type | Required | Description |
|---|---|---|---|
| `arm` | `"Performed"` | yes |  |
| `flow` | string \| null | yes | The flow that named the mutation, when a flow did. `None` when a caller performed the capability directly. |
| `mutation` | string | yes | The capability that ran. |
| `data` | object | yes | What it returned, keyed as its `returns` declares. JSON-able. |
| `leaf` | string \| null | no | The acting cursor's leaf after the call, or `None` for a session with no log. It is the promoted copy of `data["leaf"]` wherever the capability declares one, so a head reads the same field for every mutation instead of knowing which ones carry it. |

### PromptResult

| Field | Type | Required | Description |
|---|---|---|---|
| `accepted` | boolean | yes | Always true here — a rejected submission is an RPCError, not this shape. |
| `submission_id` | string | yes | Echoes the request's submission_id (caller-supplied, or a minted uuid4 for prompt). |
| `rejection_reason` | null | yes | Always null on this success shape; a real rejection is SUBMISSION_REJECTED instead. |
| `command` | [CommandOutput](#commandoutput) | no | Present ONLY when this acceptance is also the submission's only completion: a core (extension-registered) slash command resolved synchronously with no turn started, so there is no later agent_end to carry it. {name, output} — `name` is the command that ran, which an input hook may have rewritten. Only an extension-registered command reaches this shape; a built-in resolves to a step, a ready flow or a view, each of which this wire refuses with COMMAND_NOT_SUPPORTED. Absent for an ordinary turn — poll get_messages / watch for agent_end instead. |
| `view` | [View](#view) | no | Present ONLY when this submission resolved to a VIEW command — /tree or /extensions. {name, state, unavailable_because}: `name` is the view asked for, `state` is what a head draws it from, and `unavailable_because` is a sentence saying why no state rides along. Exactly one of the last two is non-null, never both and never neither. τ projects no view state yet (docs/VSCODE-HEAD.md §6), so today every one of these carries the reason; a host with its own browser opens it from its own reads, and a host without one prints the reason. This is a SUCCESS response, not the COMMAND_NOT_SUPPORTED a view used to raise: the wire says what was asked for and what it can supply, and the payload lands in `state` when there is one, with no shape change for a host. |
| `attachments` | [AttachmentReport](#attachmentreport) | no | Present exactly when the request set expand_attachments: true — absent is 'expansion did not run', which is a different statement from 'expansion found nothing'. {expanded: int, images: int, unresolved: [str], failures: [str]}. `unresolved` names the @words that matched no file and were therefore left in the text as prose. `failures` names the ones that resolved but could not be sent, each with the reason; the model is told the same thing through a <reference error="…"> block, so neither side is left believing an attachment landed when it did not. A host that shows neither list turns a visible failure back into a silent one. |
| `admitted` | boolean | no | Whether a turn was admitted for this submission, so a `submission_end` channel event with its id will follow. False for a steer delivered into another turn, and for a command. |
| `dispatched` | [DispatchedCommand](#dispatchedcommand) \| null | no | What a command resolved to, after the daemon performed it; `null` for a prompt. |

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

### ReloadExtensionResult

| Field | Type | Required | Description |
|---|---|---|---|
| `action` | `"enable"` \| `"disable"` \| `"reload"` \| `"configure"` | yes | Which action ran — echoes the verb. |
| `path` | string | yes | The managed path the action resolved to. NOT always what was sent: `path` accepts a file stem as well as a full path, and this is the full path it matched. On a failed resolution it is the unresolved string, so a host can quote back what it asked for. |
| `ok` | boolean | yes | Whether the action changed anything. false is a reportable no-op, never an error: an unknown target, an already-enabled extension, an already-disabled one. A hard failure — a file that no longer imports, which only reload can hit — RAISES instead and reaches the host as INTERNAL_ERROR, with the extension left torn down. |
| `message` | string | yes | The human-readable line, the same one the TUI listing shows. |
| `leaf` | string \| `null` | yes | cursor.leaf — E5 rule 1 on a mutator whose whole product is runtime state. It is the live tip reported as a READ, not a claim that this call wrote anything; the same reading set_auto_compaction's cursor already has. |

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

### SessionInfoEntry

The session's display name from here on; the model never sees it.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes |  |
| `parentId` | string \| null | yes |  |
| `timestamp` | string | yes |  |
| `status` | absent | no |  |
| `copiedFrom` | string | no |  |
| `type` | `"session_info"` | yes |  |
| `name` | string | yes |  |

### SessionList

The answer to `ListSessions`, RPC's shape.

| Field | Type | Required | Description |
|---|---|---|---|
| `sessions` | list of [SessionRow](#sessionrow) | yes |  |
| `scope` | [SessionScope](#sessionscope) | yes |  |

### SessionOpened

The answer to `NewSession` and `Fork`, RPC's lifecycle shape.

| Field | Type | Required | Description |
|---|---|---|---|
| `cancelled` | boolean | yes | Always false: no session is switched away from, so no hook can veto. |
| `session` | [SessionTuple](#sessiontuple) | yes |  |
| `leaf` | string \| null | yes | The new session's head leaf, duplicated out of `session`. |

### SessionRow

One line of `ListSessions`' answer: RPC's row, plus where and whether it is loaded.

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes |  |
| `ref` | string | yes | The store's own handle for the session. |
| `name` | string \| null | yes |  |
| `title` | string | yes | A bounded display label; message text appears nowhere else here. |
| `message_count` | integer | yes |  |
| `created` | string | yes | ISO-8601. |
| `modified` | string | yes | ISO-8601. |
| `parent` | string \| null | yes | The session this one was forked from, or `None`. |
| `error` | string \| null | yes | Why its entries could not be read, or `None`; such a row stays listed. |
| `cwd` | string | yes | The directory its tools run in. |
| `loaded` | boolean | yes | Whether the daemon holds it now. |

### SessionScope

What universe a listing is: a store, and the `cwd` it is scoped to, `None` for every one.

| Field | Type | Required | Description |
|---|---|---|---|
| `store` | string | yes |  |
| `cwd` | string \| null | yes |  |

### SessionTuple

A session a connection drives (F2).

| Field | Type | Required | Description |
|---|---|---|---|
| `store` | string | yes | The backend label of the connection's catalog. |
| `session_id` | string | yes |  |
| `cursor_id` | string | yes | The cursor the connection drives in it. |
| `leaf` | string \| null | yes | That cursor's leaf. |
| `addressable` | boolean | yes | Whether another call can name `session_id`; false for an in-memory session, which `list_sessions` never shows. |

### SetAutoCompactionResult

| Field | Type | Required | Description |
|---|---|---|---|
| `enabled` | boolean | yes | The effective state after this call (D-4: 'a plain, idempotent setter ... returns the effective state') — what AgentSession.set_auto_compaction returns, read back off the settings rather than echoed from the request. |
| `leaf` | string \| `null` | yes | cursor.leaf after this call (E5, rule 1 of 'E5 in Tier B' above). ALWAYS the unchanged tip: this verb mutates an in-memory CompactionSettings and appends no log entry, so there is nothing here that could move it. Returned rather than omitted because absence is not a signal (rule 3) — a host reads the same field from every mutator and never has to infer the tip from a missing key (F3). |

### SetExtensionConfigResult

| Field | Type | Required | Description |
|---|---|---|---|
| `action` | `"enable"` \| `"disable"` \| `"reload"` \| `"configure"` | yes | Which action ran — echoes the verb. |
| `path` | string | yes | The managed path the action resolved to. NOT always what was sent: `path` accepts a file stem as well as a full path, and this is the full path it matched. On a failed resolution it is the unresolved string, so a host can quote back what it asked for. |
| `ok` | boolean | yes | Whether the action changed anything. false is a reportable no-op, never an error: an unknown target, an already-enabled extension, an already-disabled one. A hard failure — a file that no longer imports, which only reload can hit — RAISES instead and reaches the host as INTERNAL_ERROR, with the extension left torn down. |
| `message` | string | yes | The human-readable line, the same one the TUI listing shows. |
| `leaf` | string \| `null` | yes | cursor.leaf — E5 rule 1 on a mutator whose whole product is runtime state. It is the live tip reported as a READ, not a claim that this call wrote anything; the same reading set_auto_compaction's cursor already has. |

### SetModelResult

| Field | Type | Required | Description |
|---|---|---|---|
| `model` | [ModelSpec](#modelspec) | yes | AgentSession.get_model() after the switch: {id, provider, context_window}. |
| `leaf` | string \| `null` | yes | cursor.leaf immediately after the model_change entry this call appended (E5) — that entry's own id, since the append is the last write this handler makes. |

### SetSessionNameResult

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | The name just persisted (echoes params.name). |
| `leaf` | string \| `null` | yes | The resulting cursor.leaf (E5/F3 — every mutating response returns the resulting leaf). |

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

| Field | Type | Required | Description |
|---|---|---|---|
| `accepted` | boolean | yes | Always true here — a rejected submission is an RPCError, not this shape. |
| `submission_id` | string | yes | Echoes the request's submission_id (caller-supplied, or a minted uuid4 for prompt). |
| `rejection_reason` | null | yes | Always null on this success shape; a real rejection is SUBMISSION_REJECTED instead. |
| `command` | [CommandOutput](#commandoutput) | no | Present ONLY when this acceptance is also the submission's only completion: a core (extension-registered) slash command resolved synchronously with no turn started, so there is no later agent_end to carry it. {name, output} — `name` is the command that ran, which an input hook may have rewritten. Only an extension-registered command reaches this shape; a built-in resolves to a step, a ready flow or a view, each of which this wire refuses with COMMAND_NOT_SUPPORTED. Absent for an ordinary turn — poll get_messages / watch for agent_end instead. |
| `view` | [View](#view) | no | Present ONLY when this submission resolved to a VIEW command — /tree or /extensions. {name, state, unavailable_because}: `name` is the view asked for, `state` is what a head draws it from, and `unavailable_because` is a sentence saying why no state rides along. Exactly one of the last two is non-null, never both and never neither. τ projects no view state yet (docs/VSCODE-HEAD.md §6), so today every one of these carries the reason; a host with its own browser opens it from its own reads, and a host without one prints the reason. This is a SUCCESS response, not the COMMAND_NOT_SUPPORTED a view used to raise: the wire says what was asked for and what it can supply, and the payload lands in `state` when there is one, with no shape change for a host. |
| `attachments` | [AttachmentReport](#attachmentreport) | no | Present exactly when the request set expand_attachments: true — absent is 'expansion did not run', which is a different statement from 'expansion found nothing'. {expanded: int, images: int, unresolved: [str], failures: [str]}. `unresolved` names the @words that matched no file and were therefore left in the text as prose. `failures` names the ones that resolved but could not be sent, each with the reason; the model is told the same thing through a <reference error="…"> block, so neither side is left believing an attachment landed when it did not. A host that shows neither list turns a visible failure back into a silent one. |
| `admitted` | boolean | no | Whether a turn was admitted for this submission, so a `submission_end` channel event with its id will follow. False for a steer delivered into another turn, and for a command. |
| `dispatched` | [DispatchedCommand](#dispatchedcommand) \| null | no | What a command resolved to, after the daemon performed it; `null` for a prompt. |

### SummarizeAndNavigateResult

| Field | Type | Required | Description |
|---|---|---|---|
| `messages` | list of [ContextMessage](#contextmessage) | yes | ConversationTree.context_for(cursor) after the mutation — the same flat message array get_messages returns, for the path this call just produced. Returned rather than left for a follow-up get_messages because the mutation's whole product is a different context, and a host that had to fetch it separately could render the old one in between. |
| `leaf` | string \| `null` | yes | cursor.leaf after the mutation (E5 rule 1). |

### SummaryMessage

A compaction or branch summary as the context renders it: a user message with no timestamp.

| Field | Type | Required | Description |
|---|---|---|---|
| `role` | `"user"` | yes |  |
| `content` | list of [TextContent](#textcontent) | yes |  |
| `timestamp` | absent | no |  |

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
| `copiedFrom` | string | no |  |
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
