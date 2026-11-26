# D32: Completion items, attributed notices, the `Commands` event, and extension start/stop

**Status:** accepted 2026-11-25 with John's rulings on the four open questions (Rulings, below),
for 0.18.0 (unreleased). **Amends**
D16 §5 (`complete` answers items, not strings; a `Notice` says who and how loud; a new state
event), D19 (a start that raises is a load error), D21 §3 and §8 (the events, and what `PROTOCOL`
3 holds), D23 (the TUI stops polling `commands`), D31 §4 (`PROTOCOL` 3 holds more than images),
`model.md` §7 (a handle that outlives a hook) and §9 (the events table). Proposal trail: akb #639,
#640 (SC-01), #641 (PC-01..PC-15, CV-01..CV-06), #642 (John's rulings Q-a..Q-e), #643 (PC-16
rejected). John's decisions there are marked **fixed**, and so are his rulings of 2026-11-25 on
this ADR's open questions (Rulings).

## Context

Protocol 3 is unreleased. D29 §9 makes `PROTOCOL` change only on a τ minor bump, so whatever
0.18.x needs on the wire has to be in 3 before 0.18.0 is tagged; after that, the next wire change
is 0.19's protocol 4. The 0.18 discussion sorted the candidate wire changes by one lens (#641):
change the wire only for what a client cannot derive from the tree and the events it already has.
Three survived (**fixed**, #643):

- **PC-01.** A completion is a bare string. A dropdown cannot show "node 42 · fix the parser…" or
  "anthropic · 200k" without a second call per item.
- **PC-02.** A `Notice` is `{text}` alone. A client cannot say which extension spoke, which cursor
  it is about, or whether it is a warning (CV-03's attention needs that).
- **PC-03.** The command forms change at runtime: `skills` rescans its files today, and 0.18 adds
  extension start/stop, MCP tools appearing, and `/reload`. Clients poll `commands()` (the TUI's
  `renew`) or never learn of the change.

P18-04, extension start/stop, is pulled into 0.18.0 so `mcp` and `scheduled` can ship with it
(**fixed**, Q-d). It is no wire change, but it is the main producer of PC-03's event, and it
gives an extension a handle outside a hook, which TG-32 (`images=` on `TreeHandle.send`) rides on.

Extension management stays off the wire (**fixed**, #643): listing, enabling and disabling are the
command line's (`tau ext list|enable|disable`, `load`), because access to the command line is
hook-administrator access. A client sees an extension's effects (nodes, notices, `CursorView`,
commands, options) and nothing of its hooks.

## Decision

### 1. PC-01: completion items

**The value.** `tau.Completion`, a frozen dataclass in `tau.commandline` (exported from `tau`
too):

```python
@dataclass(frozen=True)
class Completion:
    value: str  # what is inserted, or sent: replaces the prefix whole
    label: str | None = None  # what a list shows instead of `value`; None: show `value`
    detail: str | None = None  # one line beside it, dimmed; None: nothing
```

`value` keeps today's meaning exactly: the string that replaces the word being typed (quoted for
a command line, unquoted from `complete` and `values`). A client shows `label or value` and
`detail`, and inserts `value`. Neither `label` nor `detail` ever holds a newline; `detail` is cut
at 80 characters with `…`.

**The calls.** Every completion answer becomes `list[Completion]`:

| call | today | proposed |
|---|---|---|
| `Client.complete(cursor, named, prefix)` | `list[str]` | `list[Completion]` |
| `Client.complete_line(cursor, line)` | `list[str]` | `list[Completion]` |
| `commandline.values(schema, prefix, client, cursor)` | `list[str]` | `list[Completion]` |
| `commandline.completions(line, commands, client, cursor, options)` | `list[str]` | `list[Completion]` |

On the wire each is an array of `{"value", "label", "detail"}`, the server sending all three keys
(`null` for none, as every optional field is encoded today); the schema requires all three:
the server always sends them, and `label` and `detail` are `string | null` (`None` is the Python
default, for building one; the wire never leaves a key out).
Order and prefix matching are unchanged (prefix match on `value` in the harness; fuzzy search is
the client's, over an empty prefix; `path` is still listed one directory at a time). Duplicates
are removed by `value`, first kept.

**Label and detail per kind.**

| kind | `value` | `label` | `detail` |
|---|---|---|---|
| `path` | as today: the directory as typed plus the name, `/` after a directory | the name alone (`parser.py`, `src/`) | a regular file's size (`4.2 kB`); a directory's `None` |
| `model` | the qualified name (`anthropic:sonnet`) | `None` | the provider, the context window, and `images` if it takes them: `anthropic · 200k · images` |
| `cursor` | the id (`"3"`), then `"new"` | the cursor's labels after the id when it has some (`3 main, review`); `None` otherwise | where it is: `node 42 · busy`, from its `CursorView`; for `"new"`, `a new cursor here` |
| `label` | the label | `None` | the cursor it is on: `cursor 3 · node 42`, or `closed cursor 3 · node 42` |
| `node` | the id (`"42"`), newest first as today | `None` | the node's kind and its first line: `User · fix the parser so that…`; `Results · 2 calls`; `Config · model anthropic:sonnet`; `System`; `Custom · todo` |
| an `enum` value | `str(v)` | `None` | `None` |
| `true` / `false` | as today | `None` | `None` |
| a list item | the whole list text so far plus the item (`a,b,c`), as today | the item's own label, or the item alone (`c`), so a list shows items and not the growing text | the item's `detail` |
| a command name (after `/`) | `/name` or `/ext:name`, as today | `None` | the first line of the command's description, then its extension: `Summarize the head of this line · compaction` |
| `/config` extension | the extension's name | `None` | `None` |
| a value on a command line | the word as today (quoted if it needs to be; `name=` before it when typed so) | the item's label, else the unquoted value | the item's `detail` |

The node summary is one new function outside the TUI (the tree pane's text is `tui._treetext`'s,
private and row-based); the tree pane moves onto it where the two say the same thing, so a
client's dropdown and the TUI agree. `detail` for `cursor` and `label` reads the
session as completion already does; nothing here runs extension code (`_complete.py`'s rule
stands for the built-in named types; SC-02's extension completers are a separate ADR and will
answer the same `Completion`).

**Consumers.**

- **TUI.** `tau.tui._editor`'s private `Completion` alias becomes `Completions` (it would shadow
  the public name). The popup shows `label or value` with `detail` dimmed to its right and inserts
  `value`; the model and cursor pickers (`PickDialog`) show `detail`; form fields that complete
  (`complete_value`) take items the same way.
- **Web (tau-code, shipped as `ffwf-tau-web`).** The composer's dropdown renders `label`/`detail`
  from the generated types and inserts `value`.
- **trusty.** Its generated types change; SC-04 (Tab completion through `complete_line`) renders
  labels when it lands.
- **ACP.** Unchanged: ACP has no argument completion.

### 2. PC-02: a notice says who, about which cursor, and how loud

**The value.** `Notice` gains three fields with defaults, so `Notice(text)` still builds:

```python
@dataclass(frozen=True)
class Notice:
    text: str
    cursor: int | None = None  # the cursor it is about; None: the session
    extension: str | None = None  # the extension that said it; None: τ itself
    level: Literal["info", "warning", "error"] = "info"
```

`tau.NoticeLevel` is the `Literal`. The defaults are for building one in Python: on the wire a
`Notice` always carries all of `cursor` (`integer | null`), `extension` (`string | null`) and
`level`, and the schema requires them. The levels mean:

- **`info`**: for the record. Nothing needs doing.
- **`warning`**: the session went on, but not quite as asked, or it costs: a fallback was taken,
  something was skipped, a limit is near.
- **`error`**: what a client asked for was not done (an input refused), or an extension's work
  failed.

Two deliberate departures from #641's sketch (**fixed**, Ruling 1): the field is **`extension`**,
not `source`, because D19 already uses *source* for where extension code comes from, and every other
event names its extension `extension` (`OpenQuestion`, `HookFailure`, `Form`); and the middle
level is **`warning`**, not `warn`, as the config checker's `Severity` (`"error"`, `"warning"`)
and Textual's toast severities spell it.

**Who fills them.** An extension never names itself: `extension` is the handle's, so one extension
cannot speak as another, or as τ. `cursor` is the handle's own where it has one.

| handle or path | `cursor` | `extension` | `level` |
|---|---|---|---|
| `Cursor.tell(text, *, to=None, cursor=None, level="info")` | the argument, else the handle's cursor | the handle's | the argument |
| `NodeHandle.tell(text, *, to=None, cursor=None, level="info")` | the argument, else the batch's cursor | the handle's | the argument |
| `TreeHandle.tell(text, *, to=None, cursor=None, level="info")` (new, §4) | the argument, else `None` (`NoSuchCursor` if not open) | the handle's | the argument |
| a command's `progress(text)` | the cursor it was run on | the command's owner (`tau` for the built-in's) | `info` |
| a `pre_input` hook halts a client's input (core's notice) | the input's cursor | the hook's extension | `error` |
| a task started by `TreeHandle.spawn` raises (§4) | `None` | the handle's | `error` |
| a `stop` that raises or times out during a reload (§4) | `None` | the extension stopped | `error` |
| the image rule halts an input after `pre_input` (D31 §5) | the input's cursor | `None` | `error` |

A `level` outside the three is a `ValueError` at the `tell`. Old call sites keep compiling and
send `info`. (As built, the three `tell`s have one signature, `TreeHandle.tell`'s: `cursor` defaults to the
handle's own, so a hook may also name another cursor; the reference documents one method.)

**Core's notice.** Core pushes `(client, text)` when a `pre_input` hook halts an input. It becomes
`(client, cursor, extension, text)`: `Answer.Hooks` gains `halted_by: str | None`, the extension
whose hook returned the string (`run_hooks` knows it), `None` for the image rule's halt. A small
Rust change in `tau-core` (`step.rs` `on_hook_stop`, `tree.rs` `notices`) and `tau-py`
(`take_notices`).

**Every current producer, mapped.**

| where | text (shortened) | level |
|---|---|---|
| `attachments` | `@word names no regular file … sent as text` (to the author) | `warning` |
| `usage` | `MODEL read nothing from the prompt cache (N tokens at full price)` | `warning` |
| `compaction` | `compacted A → B tokens (…)` | `info` |
| `compaction` | `compaction cannot keep this under its soft limit: …` | `warning` |
| `tokens` | `tokens: the tokenizer wheel is not used: …` | `warning` |
| `tokens` | `MODEL: <problem>` (an observation that failed) | `warning` |
| `tokens` | a model's calibration news (`_news`) | `info` |
| `token_calls` | `token_calls: the count call failed: …` (the step goes on) | `warning` |
| `permissions` | `… so no rule was saved; it ran this once` | `warning` |
| `permissions` | `… is allowed from now on (written to allow …)` | `info` |
| `permissions` | `… allowed for this run … but a project layer … hides it` | `warning` |
| commands | each `progress` line | `info` |
| core | a `pre_input` hook's halt reason, to the author | `error` |
| driver | the image rule's halt, to the author (`N images not sent: …`) | `error` |

The text no longer needs its `extension: ` prefix (`attachments:`, `permissions:`,
`token_calls:`) to be attributed; the prefixes go, so a client that shows `extension` does not
say it twice.

**Consumers.** The TUI's toast takes `extension` as its title and maps `level` to Textual's
severity (`information`, `warning`, `error`), and an `error` stays up longer (10 s, as the switch
toast does). The web client does the same and may filter by `cursor`. trusty shows
`[extension] text`.

### 3. PC-03: the `Commands` event

**The event.** `Commands(forms: tuple[Form, ...])`: every command's form, and `config`'s when
some extension has options, exactly what `Client.commands()` returns at that moment. It is named
for what it carries whole, as `Labels` is.

**Whole, not a nudge (recommended).** The alternative is an empty "commands changed, call
`commands` again" event. Whole wins:

1. It is the pattern for small state: `Labels` and `CursorView` are sent whole whenever they
   change (D16 §5), and a client keeps the last one.
2. No round trip and no race: with a nudge a client calls `commands()` and may meet a later state,
   or a reload between the nudge and the call.
3. It is a state event, so the `Feed` keeps only the latest unsent copy (`_replace`): a burst
   (an MCP server listing tools twice, a reload) costs one event, and it never counts toward the
   `CutOff` backlog. A nudge would be a third kind of event with its own collapsing rule.
4. It goes in the snapshot, so a client never needs a `commands()` call to start; a client that
   drew the palette from the snapshot cannot hold a stale one.

The cost is size: the forms with every skill template's schema may be tens of kilobytes, sent at
connect and on each change, never per step. Acceptable.

**Options forms are not carried.** An options form's defaults are the values at a cursor, so it
cannot be sent once for the session. Which extensions have options is in the `config` form's
`extension` enum, so a change in that set is a change of `forms`. An extension whose options
*type* changed (a reload) also triggers a `Commands`, even if `forms` compare equal, and the
event's rule for clients is: the forms you hold from `options()` are stale; read them again when
you next show one.

**When it is sent.** `Hub.sync` compares the forms (and the identities of the options types) with
what it last sent, as it compares everything else, and sends `Commands` when they differ. So it is
sent after: an extension's start once clients are connected (`TreeHandle.reassemble`, §4), a
reload, a `rescan` that changed a skill's commands, and nothing else. A reload that produces the
same forms sends none. To keep `sync` cheap, the hub compares only after the commands or options
were assembled again (a generation number on `Commands` and `ExtensionOptions`), not on every pass.
`rescan` still runs where it runs today (before `commands`, `run` and the line calls); a
skill file added while nobody types is announced on the next of those.

**Order.** In the snapshot, `Commands` comes **first**: `Commands`, then a `CursorView` per open
cursor, a `CursorGone` per closed one, then `Labels`, which stays last (the hub and the `Feed`
treat `Labels` as the snapshot's end). In a sync it comes after `Labels` and before the notices:
`Appended`, `CursorView`s, `CursorGone`s, `Labels`, `Commands`, `Notice`s, so an extension's
notice about new tools arrives after the forms that hold them.

**Clients.** `LocalClient` and `RemoteClient` change only in yielding it; `Client.commands()`
stays (a line client with no stream, `tau -p`, and `parse_line` need it) and its docstring loses
"fixed until the driver reloads". The TUI drops `renew`'s polling and replaces its command list
on each `Commands`. `tau -p --json` prints it with the rest of the snapshot (D22 §4: nothing is
filtered). The web client replaces its palette.

The private driver class `tau._commands.Commands` is renamed (`CommandTable`) so the public event
owns the name.

### 4. P18-04: extension start and stop

**The contract.** Two optional attributes, beside the hooks:

```python
async def start(self, tree: TreeHandle) -> None: ...
async def stop(self) -> None: ...
```

Either may be absent. `check_extensions` refuses (`DefinitionError`, naming the extension) a
`start` or `stop` that is not a coroutine function, as it refuses a misspelt hook. A third-party
extension that already had a `start` or `stop` attribute meaning something else gets that error:
below 1.0, the name is worth the break.

**The handle.** `start` is handed a `TreeHandle` bound to the extension, and it **stays live until
`stop` returns**: the extension keeps it and uses it from its own tasks, on its own initiative.
This amends `model.md` §7 ("a handle lives exactly as long as its hook coroutine"): a lifecycle
handle lives as long as its extension is started, and is retired after `stop`. It is a tree handle
because outside a hook core lets nothing move or write as a cursor; what an extension does to a
cursor from there is what a tree handle already offers: `send`, `add_item`, `open`, `rest`,
`abort`, `close_cursor`, `reopen`, `set_label`, the reads, and the new `tell` (§2). It is safe
from the loop and, like a tool's handle, from a worker thread; `rest` and `close_cursor` are
loop-only, as now.

The tree handle gains:

- `tell(text, *, to=None, cursor=None, level="info")`, §2.
- `spawn(coro) -> asyncio.Task[None]` (**fixed**, Ruling 2): run `coro` as a task the driver owns
  for this extension. On the loop only (`RuntimeError` off it, as `asyncio` says), on a live handle
  (`NotInHook` on a retired one), and never on a stopped driver (`ConnectionError`). The task is
  the extension's, whichever of its handles spawned it: a hook's handle may spawn one that
  outlives the hook. An exception in it (anything but its cancelling) is reported, never
  swallowed: an `error` `Notice` to every client, `cursor` `None`, `extension` the spawner's, text
  `"task failed: Type: msg"`, and its traceback on stderr with the extension's name, as a hook's
  failure is. It does not fail the driver or the extension: the extension goes on, and it is the
  administrator's call to restart. The tasks are cancelled and awaited when the extension is
  stopped, **before** its `stop` runs (at shutdown, and when a reload stops it), so `stop` never
  races its own tasks; a task that ignores the cancel delays that stop up to its `STOP_TIMEOUT`.
  The returned task may be cancelled by the extension itself; a cancel is not a failure. They are
  not the driver's effects: `DriverState.units` does not count them, so a harness's quiet does not
  wait for them. A coroutine the extension runs with `asyncio.create_task` itself stays its own, as
  before: unwatched.
- `reassemble()`: this extension's `tools` or `commands` changed (an MCP server listed its tools,
  or listed them again). The driver assembles tools and commands again from every extension, from
  the next job it dispatches, as `use_catalog` does for models; hooks, options and hook order are
  not touched. Allowed while cursors are busy: a dispatched job holds the tools it was dispatched
  with, and a call to a tool that has gone is `Err("unknown tool: …")`. `DefinitionError`, with
  nothing changed, if the new set does not fit (two tools of one name). Clients get `Commands`
  (§3) if the forms changed. This is the push form of `rescan`.
- **TG-32:** `send(to, text, images: Sequence[Image] = ())`, the images after the text (D31). At
  most `MAX_INPUT_IMAGES`; each an `Image` with bytes, or one the session keeps. Held to the model
  of `to`'s branch by the same image rule a client's `send` meets (`ValueError` saying why,
  `MissingImage` for one not kept), so nothing is queued that would fail. The rule runs again
  after `pre_input` (D31 §5); an extension's input that fails there halts its step with why, which
  the extension's `rest` returns as `Ending.Halted` (core tells only a `Frontend` author by
  `Notice`).

**When they run.**

- **Driver start.** `Driver.start()` becomes `async`. It runs each extension's `start`, one at a
  time, in load order, **before the pump starts**, so no hook ever runs against an extension
  that has not started, and no client is connected yet (`start_session` returns after it). Then
  tools and commands are assembled again (a `start` may have filled them), then the pump starts.
  Inside `start`, the handle's `rest` and `close_cursor` raise `ValueError` ("the driver has not
  started"): waiting there would never end. An extension that wants to wait starts its own task.
- **Reload** (**fixed**, Ruling 3: stop first). `Driver.reload(extensions)` becomes `async`. It
  refuses, with nothing changed: `Busy` if any cursor is not at rest (busy, or an effect out), or
  another reload is in progress; it does not wait for rest, a caller that wants to retries.
  `DefinitionError` (or `ConfigError` for a hook order that does not fit) if the new set does
  not fit, checked before anything is stopped. Then, in this order:
  1. Dispatching is held: the pump takes no job until the reload ends, so no hook of a new
     extension runs before its `start` has returned, and a client's input meanwhile waits.
  2. The old extensions that leave are **stopped first**, in reverse load order (their spawned
     tasks cancelled, then `stop`). An extension object in both lists (the same object) is
     neither stopped nor started, and keeps its handle and tasks. A `stop` that raises or times
     out is written to stderr and told to clients as an `error` `Notice` from that extension;
     the reload goes on.
  3. The driver adopts the new set (hooks, tools, commands, options).
  4. The new ones are started, in load order, with their `START_TIMEOUT`s. Their handles' `rest`
     and `close_cursor` are refused while the reload holds dispatching, as inside a driver start.
  5. Dispatching is released; the hub publishes (`Commands`, §3).

  **A failed start** (a raise or a timeout) leaves no half-started set and is not hidden: the
  extensions this reload started are stopped (reverse order), and the driver adopts the set the
  reload kept: the extensions in both lists, without any new one. That is what is loaded
  afterwards, and clients see it in `Commands`. `reload` then raises `ExtensionLoadError` naming
  the extension and its failure (`extension 'mcp' did not start: TimeoutError: …`), and that the
  reload left only the kept extensions running. There is no way back to the old instances: they
  were stopped. **Reload is best effort for well-behaved extensions** (a `stop` that lets go of
  what its `start` took); restarting the harness is the safer path, and the one to take after a
  failed reload.
- **Shutdown.** `Driver.stop()` marks the driver stopped and cancels every task as now, the
  extensions' spawned tasks included, then runs each started extension's `stop` in reverse load order, then ends the streams. A `stop` that
  raises or times out is written to stderr with the extension's name and traceback (as a hook's
  failure is) and the next `stop` still runs: shutdown must finish, because the session's lease
  and the providers are released after it. `Driver.stop()` does not raise for it.

**Failure (Fail Early).** A `start` that raises, or does not return within its start timeout
(`START_TIMEOUT`, 30 s, unless configured), is a load failure: the starts already done are stopped in reverse order, nothing is
served, and `Driver.start()` raises `ExtensionLoadError` naming the extension and its failure
(`extension 'mcp' did not start: TimeoutError: …`). `start_session` adds the source row as every
load failure has it (D19: the key, the origin, and the command that gets past it: `tau ext disable
mcp`, or `tau --no-extensions` for a discovered source), so `tau`, `tau -p` and `tau serve` stop
the same way they stop for an import that fails. `STOP_TIMEOUT` is 5 s. (If importing
`ExtensionLoadError` into the driver makes a cycle, it moves to a leaf module; it stays exported
from `tau`.)

**Timeouts** (**fixed**, Ruling 4). `START_TIMEOUT` (30 s) and `STOP_TIMEOUT` (5 s) are the
defaults, exported from `tau`. The harness's config files override them per extension:
`extensions.NAME.start_timeout` and `extensions.NAME.stop_timeout`, a positive number of seconds.
They sit beside `extensions.NAME.order` (D28) and are read the same way: from the config layers
(user, project, `--set`/`TAU_SETTINGS`), popped from the extension's options before the options
are built, so they are never options, never in the `config` form and never settable by a client
(`set_options`, `/config`, a `Config` node): a client configures what an extension offers it, an
administrator how the harness runs it (akb #643). A value that is not a positive number is a
config `error`; one for an extension that has no `start` (or no `stop`) is a `warning`, ignored,
as other configuration that names something the start does not have. `ModelCatalog.timeouts`
carries them, keyed by extension name, `(start, stop)` with `None` for the default; the driver
reads them where it starts or stops an extension, so a `use_catalog` applies to the next one.

**Concurrency.** Starts are sequential, so the order and the failing extension are deterministic.
Work an extension does after `start` returns is its own: tasks it creates are its to cancel in
`stop`, and a failure in one is its to report, with `tree.tell(…, level="error")`. The driver
does not watch tasks it did not start; `spawn` (above) is how an extension hands a task to the
driver to watch. A hook of the extension and its lifecycle
handle may write at once; core serializes the writes, as it does for two hooks today.

**PC-03 from the lifecycle.** At driver start there is no client, so the first `Commands` a client
gets is its snapshot's. After start, `reassemble` and `reload` change the forms, and the hub sends
`Commands` on its next sync.

**No wire.** Start and stop have no call and no event of their own. Enabling and disabling stay
`tau ext enable|disable` and `load` (**fixed**, #643); a running harness is reconfigured by
restarting it. `/reload` (P18-18) reaches `Driver.reload` in 0.18.x as a command, with no new call.

### 5. Non-goals

Moved to documented conventions over what protocol 3 already carries (#641 §1, **fixed**):

- **CV-01 undo markers**: `Custom {kind: "undo", data}` written by the extension that made the
  change; `/undo` is the `undo` extension, not a call (ruling 1).
- **CV-02 usage**: `Assistant.meta.usage` is the documented shape; clients sum per node.
- **CV-03 attention**: derived from `CursorView` (`busy` to rest, `questions` non-empty), helped
  by PC-02's `level`.
- **CV-04 sub-agent parentage**: `agents` labels its cursors (`agent:<parent>:<n>`).
- **CV-05 todo**: `Custom {kind: "todo", data: [...]}` on the walk.

Rejected for protocol 3:

| id | change | why not |
|---|---|---|
| PC-04 | an attention event | derivable from `CursorView` (CV-03) |
| PC-05 | structured usage on the wire | already in `Assistant.meta.usage` (CV-02) |
| PC-06 | sub-agent parent on `CursorView` | a label convention (CV-04) |
| PC-07 | structured UI items (todo, plan, progress) | a `Custom` convention (CV-05); revisit if it proves too weak |
| PC-08 | an `undo` call, markers on the wire | ruling 1: undo is an extension over markers (CV-01) |
| PC-09 | capability exchange at `hello` | the server does not care how a client presents (ruling 4) |
| PC-10 | a client-directed result for `/new`, `/resume`, `/fork` | ruling 3: those are client moves; a gateway is another design |
| PC-11 | a background task entity | no design yet; the lifecycle (§4) comes first |
| PC-12 | a "leaves resting" field on `Form` | a command cannot promise it; clients read `CursorView` after `run` (#640) |
| PC-13 | extension completers on the wire | needs none: `format` and `named` are open strings (SC-02, its own ADR) |
| PC-14 | the extension list in `Welcome` | Q-e: no |
| PC-15 | a separate author on a refused input's notice | covered by PC-02's `extension` and `cursor` |
| PC-16 | `extensions` and `set_enabled` calls | #643: no new holes to manage; the command line is hook-administrator access |

### 6. Protocol 3, and who must follow

All of §1 to §3 lands in **`PROTOCOL` 3**, which is unreleased, beside D31's images. Within 3
they are additive for a lenient reader, but a client generated from the schema meets a new event
kind and new result shapes, so every generated client is regenerated before 0.18.0 (there is no
released 3 to stay compatible with). `PROTOCOL`'s docstring gains: "`complete` and
`complete_line` answer `Completion`s, a `Notice` names its cursor, extension and level, and the
event `Commands` carries the command forms."

**The freeze.** Once 0.18.0 is tagged, 3 is frozen: by D29 §9 no 0.18.x release changes the
schema, and the next change is protocol 4 in 0.19. So nothing else that 0.18.x needs may wait:
`/reload`, MCP's tools, `scheduled`, `tau ext install` all reach clients through `Commands` and
the existing calls, which is why PC-03 is in 3 (Q-c, **fixed**).

**ffwf-tau-web 0.18** is released first (P18-01; D29 §9's order), from tau-code built against
this schema: it renders completion items, attributes and colours notices, and replaces its
palette on `Commands`. Its decoder treats an unknown event kind as fatal, so it must be built from
the schema after this lands, never before. `ffwf-tau` 0.18.0 pins `ffwf-tau-web>=0.18,<0.19`.

**trusty** (`examples/ratatui`) generates `wire.rs` from the schema in `build.rs`; its event match
fails to compile until it handles `Commands` and the new `Notice` fields, which is the check we
want. It ships with τ's tag, so it cannot drift. **tau-acp** re-pins after 0.18.0 and adapts to
`complete`'s items if it calls it.

### 7. Test plan, size, files

Every behavior test runs in the harness's four modes (`local`, `remote`, `http`, `eager`), so each
wire shape is checked by a `RemoteClient` decoding exactly what a `LocalClient` returns.

| item | tests | size | files |
|---|---|---|---|
| PC-01 | behavior: each named type's items (label and detail as §1's table; a `path` directory with no size; a `cursor` with labels; a `node` summary for each body kind); `complete_line` command names with description detail; list items (value the whole text, label the item); enum and bool items bare; quoting in `value`, none in `label`. `tests/behavior/test_commandline.py` and `test_line_calls.py`. TUI pilot: the popup shows label and detail and inserts value (`tests/tui/test_slash.py`). Schema: `complete`'s `x-result` is an array of `Completion`, all three fields required. | M | `_complete.py`, `commandline.py`, `_client.py`, `_remote.py`, `_commands.py`, a shared node summary (with the tree pane's), `tui/_editor.py`, `tui/_line.py`, the pickers, `__init__.py`, schema, `surface.txt`, `docs/guide/frontends.md`, glossary |
| PC-02 | behavior: `Cursor.tell`, `NodeHandle.tell`, `TreeHandle.tell` (with and without `cursor`) each give the right `cursor`, `extension`, `level`; a bad `level` is `ValueError`; a command's `progress`; a hook-halted input tells only its author, with the hook's extension and `error`; the image rule's halt has `extension` `None`. `tests/behavior/test_events.py`, `test_asking.py`, `test_user_images.py`. TUI: severity and title. Schema: `Notice`'s fields, `level`'s enum. | S–M (Rust S) | `_events.py`, `_handles.py`, `_hub.py`, `_commands.py`, `_effects.py`, `crates/tau-core/src/step.rs`, `tree.rs`, `crates/tau-py/src/session.rs`, `_core.pyi`, the eight producer call sites (§2), `tui/_app.py`, `tui/_view.py`, schema, glossary |
| PC-03 | behavior: the snapshot opens with `Commands` equal to `commands()`; a reload to other forms sends one `Commands` after `Labels` and before its notices; a reload to equal forms sends none; a reload that changes only an options type sends one; a skill file added is announced on the next `commands` call; a slow client holds one `Commands` however many changes; `Commands` never cuts a client off. `tests/behavior/test_events.py`, `test_event_order.py`, `test_reload.py`, `test_skills.py`. Schema: `Event`'s `oneOf` has `Commands`. TUI: the palette follows a reload without `renew`. | S | `_events.py`, `_hub.py` (`Feed._is_state`, `_publish`, `connect`), `_commands.py` (rename, generation), `_options.py` (generation), `_client.py` docstring, `tui/_line.py`, `tui/_app.py`, schema, glossary, D16/D21 cross-references |
| P18-04 | behavior: `start` runs once per extension, in load order, before any hook; a `start` that raises or times out is `ExtensionLoadError` naming it, the earlier ones stopped in reverse, the pump never started; `rest` inside `start` is `ValueError`; the handle works from a task after `start` (`send`, `add_item`, `open`, `tell`) and is retired after `stop` (`NotInHook`); `stop` runs in reverse at `Driver.stop`, after effects are cancelled, and one that raises does not stop the rest; reload stops the old before starting the new, refuses with `Busy` while a cursor is busy, and a failed new start raises `ExtensionLoadError` naming it and leaves the kept extensions alone; `spawn`'s failure is an `error` `Notice` from the extension, and its tasks are cancelled before `stop`; a configured `start_timeout` applies, and a bad one is a config error; `reassemble` changes the tools from the next sample and sends `Commands`. `tests/behavior/test_lifecycle.py` (new), `test_reload.py`, `test_driver_contracts.py`. CLI and serve: a drop-in whose `start` raises stops `tau -p` and `tau serve` with D19's message and exit status (`tests/cli`, `tests/serve/test_serve_command.py`). | M | `_protocols.py` (contract, check), `_driver.py` (`start`, `reload`, `stop` async; reassemble), `_handles.py` (lifecycle handle, `tell`, `reassemble`), `_session_start.py`, `_extensions.py` (error enrichment), every `driver.start()` and `reload` call site (about 30 in tests, the harness), `docs/guide/extensions.md`, `model.md` §7, glossary |
| TG-32 | behavior: `TreeHandle.send(images=…)` delivers a `User` with the images; a model without images refuses at the call; 21 images is `ValueError`; an unkept `sha256`-only image is `MissingImage`. `tests/behavior/test_user_images.py`. | S | `_handles.py`, `Handles` (the image gate), glossary |

Each item also regenerates `docs/protocol.schema.json`, `docs/guide/protocol.md`,
`docs/api/surface.txt` and `docs/reference/`, which `check.sh` holds. Order: PC-02's Rust change
and PC-01 first (the widest diffs), then PC-03, then P18-04 with TG-32, then tau-code and trusty
against the frozen schema.

## Alternatives

- **PC-01 as a parallel call** (`complete_items` beside `complete`): two calls that say the same
  thing, and 3 is unreleased. Rejected: change the one call.
- **PC-01 `label` defaulting to `value` on the wire**: twice the bytes for most items, and a
  client would still need the rule for `null`. Rejected: `null` means "show `value`".
- **PC-02 `source`** (#641's name): collides with D19's *source*. Recommended against (§2).
- **PC-02 `to` on the event**: the receiver already knows it received it; whether it was
  broadcast is not something a client acts on. Not added.
- **PC-02 notices from Python only**, taking core's push out: the driver would have to order its
  notice after the answer core applies. Rejected: the cursor and extension ride core's existing
  push.
- **PC-03 as a nudge**: see §3. Rejected.
- **PC-03 carrying the options forms**: their defaults are per cursor. Rejected.
- **Lifecycle as one long `run(tree)` coroutine** the driver owns and cancels: it surfaces a
  failure by itself, but every extension then needs a body that never returns, and "started" has
  no moment to fail at. Rejected for start/stop; `spawn` is the driver-owned task (Ruling 2).
- **Reload starting the new before stopping the old** (this ADR's first proposal): atomic, but two
  instances of one extension run at once and the new one can fail on what the old holds (a port,
  a lock file, an MCP server's process). Rejected (Ruling 3): stop first, and say plainly that a
  failed reload leaves only the kept extensions.
- **Reload waiting for rest**: a reload that waits while a client keeps a cursor busy would block
  its caller for as long. Rejected (Ruling 3): `Busy`, and the caller retries.
- **Timeouts as extension options**: options are what a client may set (akb #643). Rejected
  (Ruling 4): config keys beside `order`.
- **Concurrent starts**: faster with several slow extensions, but the order and the failing one
  stop being deterministic. Rejected for 0.18; `mcp` starts its servers concurrently inside its
  own `start`.

## Rulings (John, 2026-11-25)

1. **Names.** `extension` (not #641's `source`) and `warning` (not `warn`) on `Notice`: confirmed.
2. **Background failures.** `TreeHandle.spawn(coro)` is in 0.18.0 (§4): a driver-owned task for
   an extension; an exception in it becomes an `error` `Notice` attributed to the extension
   (PC-02), and its tasks are cancelled and awaited at that extension's stop (and so before a
   reload starts anything).
3. **Reload overlap.** Stop first: the old extensions are stopped, then the new ones started.
   Reload is refused with `Busy` if any cursor is not at rest; it does not wait. A full restart is
   the safer path; reload is best effort for well-behaved extensions. There is no "a failed reload
   changes nothing" guarantee: a failed start is reported naming the extension, and afterwards the
   kept extensions alone are loaded (§4).
4. **Timeouts.** 30 s to start, 5 s to stop, by default; the harness's config files override them
   per extension (`extensions.NAME.start_timeout`, `extensions.NAME.stop_timeout`), which is
   administrator configuration, not a client-facing option (akb #643).

## Glossary additions

`Completion` (and *completion item*: `value`, `label`, `detail`), the per-kind labels in
*complete*; `Notice.cursor`, `Notice.extension`, `Notice.level`, `NoticeLevel`; the event
`Commands`; *start* and *stop* (an extension's), *lifecycle handle* (amending *retired handle*),
`START_TIMEOUT`, `STOP_TIMEOUT`, `extensions.NAME.start_timeout` and `stop_timeout`,
`ModelCatalog.timeouts`, `TreeHandle.tell`, `TreeHandle.spawn`, `TreeHandle.reassemble`,
`TreeHandle.send(images=)`; `Answer.Hooks.halted_by`; *notice* (core's) gains its cursor and
extension; `PROTOCOL` 3's contents; `CommandTable` (the renamed private class needs none).
