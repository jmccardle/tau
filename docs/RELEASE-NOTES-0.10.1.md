# v0.10.1 — the tree a second head can draw, and the lock it can release

Four verbs and one method. 0.10.0 put every declared capability on the wire and
`docs/VSCODE-HEAD.md` §6 recorded what that still did not give an out-of-process
head: a way to SEE what the tree verbs act on. This is that read, plus the three
things building a second head against it found in the first afternoon.

Protocol **1.4 → 1.5**, MINOR because every change is additive: four new verbs,
no field removed, no meaning changed. A host built against 1.4 is unaffected.

One unrelated thing ships in the same version and has its own section below:
`tau --mode repl`, a fourth head, behind a new `[repl]` extra.

---

## The tree, readable

### `get_tree`

`ConversationTree.browse()`, projected. Every entry in the log, in the order a
browser draws them — preorder over the parent/child tree, roots in load order,
children oldest first.

**Flat, with `parent_id` carrying the shape.** A nested projection of a
five-hundred-message linear conversation is five hundred levels of nesting, which
is a serializer's recursion limit rather than a tree anyone wanted. `tree()`
already went iterative for the same reason; this is that decision reaching the
wire.

**Unbounded, and it is the one place G3 is argued rather than applied.** G3
forbids *pushing* something unbounded; this is a pull, the shape IS the answer,
and a bounded shape is a different tree. `count` is there so a host can say it
read a whole one.

### The projection is wider than `TreeNode`, and that is the finding

`docs/VSCODE-HEAD.md` §6 predicted one read over `ConversationTree.tree()`. That
would have been enough to DRAW a tree and not enough to COLOUR one.

Every zone in `docs/TREE-EDITOR-MANUAL.md` §7 reads a fact off the RAW entry, and
no verb hands a raw entry over. So `BrowseNode` carries them beside the shape:

| Field | The zone or gesture that reads it |
|---|---|
| `first_kept_id` | the fold's whole boundary — `folded` and `covered` |
| `tool_call_ids` / `tool_call_id` | the pairing a mark expands over (§6) |
| `copyable` | whether `paste_subtree` takes this row as a source |
| `from_id` | the `branch_summary` and the line it is about (§4.3) |
| `is_system` | the prompt a fold carries across rather than dropping |
| `estimated_tokens` | the mark readout, which says "estimate" beside it |

Without them a head recomputes each from a second reading of the log's shape,
which is the drift the capability registry exists to make impossible.

`COPYABLE_KINDS` moved from `tree_surgery` to `conversation_tree` for the same
reason — the module that reports it per node is the one that should own it.
`tree_surgery` re-imports it and every existing caller is untouched.

### `get_entry`

One entry's full body, by id. What a detail pane draws beside the tree, and the
reason `get_tree` carries a one-line `preview` per row instead of a message.

`get_messages` does not serve this: it answers for the ACTIVE PATH, and the node
a reader has moved a browser's cursor onto is very often not on it. The entry is
handed over RAW, in its stored camelCase shape, because the caller is rendering
one node and a projection would be a second message shape to keep in step with
`get_messages`'.

The pair is the pair the TUI's own browser makes — `tree()` for the rows,
`entry` for the node it is showing (`tree_browser.py`'s `_resolve_entry`).

---

## The lock, releasable

### `get_pending_request` and `answer_request`

0.10.0 replaced `ui.confirm` / `ui.select` / `ui.input` with one persisted
`extension_request` entry and recorded that all four of its states are rendered
by every head. Three heads could. The RPC wire could not: the state was reachable
only as `SUBMISSION_REJECTED` data, so a host learned about a lock by being
REFUSED by one, could not see one that had not refused it yet, and had no verb to
release it at all. **A locked session was a session an out-of-process head could
never continue.**

`get_pending_request` is the read a head polls at every cursor move. Null is the
ordinary answer and is not a failure. `label` rides along rather than being
derived, because tau's four-state framing line is a table
(`docs/EXTENSION-LOCKS.md` §9) and a host deriving it holds a second copy.

`answer_request` appends the response and then dispatches the pressed action, in
that order, so the handler runs on a session that is already unlocked.
`handled: false` is a WARNING: the extension is not loaded, nothing ran, and the
lock is gone anyway.

**Two guards it does not take, stated rather than omitted.** No D-1
`turn_safety_guard`: a request is very often RAISED by a `tool_call` hook inside
a turn, so answering mid-turn is the designed case — and the dispatched action
may itself submit, which under a held `turn_lock` would deadlock against the lock
this verb was holding. The TUI and the REPL call the same method with no lock.
No D-7 `require_durable_session` either, which is the one deliberate exception to
"the verb that appends refuses": the append's product here is a RELEASED LOCK in
this process, and refusing would leave an unpersisted session locked with no way
out at all.

---

## One thing the schema validator could not say

`_assert_supported_schema` accepted `items` only when the element schema was an
object, so no verb could declare an array of strings — and `tool_call_ids` is
one. `_validate_value` now checks a scalar element with `_validate_value` and an
object element with `_validate_object`, decided by the element schema's own
`type`. Additive: nothing that passed before fails now.

---

---

## A fourth head rides along: `tau --mode repl`

Not part of the protocol work, and shipped in the same version. A prompt line:
streamed answers in the scrollback, no screen takeover, `rich` as the only
renderer. It sits beside the TUI, print mode and RPC rather than in front of any
of them, and it is behind a **new extra**, because needing no Textual is the
whole point of it:

```bash
pip install 'ffwf-tau-coding-agent[repl]'
tau --mode repl
```

`rich` renders and `prompt_toolkit` reads. Without the extra, `cli.py` refuses
with `--mode repl needs the 'repl' extra (… is missing)` and the install line,
rather than a traceback. The `ffwf-tau` metapackage pins `[tui]` and does not
pull this in.

It was written as an experiment about the DOCS: specified from `docs/` plus four
source readings, to measure whether they suffice for someone writing an
independent head. `docs/REPL-HEAD.md` §9 is the result — 3 215 lines in three new
modules and 162 tests as shipped (§9's own figures are one commit older), with six
of the seven §1 requirements pinned by a test and the seventh (input history)
resting on inspection alone. §9.2 is the part worth acting on: of the 45 gaps the docs-only reader hit, **35 were real**, and they
cluster on the backend seam — the five `TauBackend` methods a head calls are
documented nowhere, and `subscribe_render`'s own docstring named *five* render
kinds where the router emits **ten**. §9.3 writes the nine sentences that would
close them, each naming the page it belongs on.

The other three heads are unchanged. What this one needed from outside itself is
§9.4, and it is four items: one function moved (`span_seconds`, to
`backends.py`), one copied (`render_panel_body`, because `extension_ui.py`
imports Textual at line 4), one re-derived with an equality test pinning the two
copies against each other (the steering strategy table), and a set of renames in
`headless.py` that made its session selection, its extension-command listing and
its refusal wording reusable without moving any logic.

---

## What this is for

`ffwf-tau-code` 0.4.0 is the first consumer: a conversation-tree browser in a
webview, with the TUI's zones, marks, folds, elide, branch and paste, running in
VS Code, VSCodium and a browser tab. `docs/VSCODE-HEAD.md` §6 item 4 is closed
and says so.

## Breaking changes

None. Every change is additive and the protocol MINOR reflects that.

## Upgrading

```bash
pip install --upgrade "ffwf-tau-coding-agent[tui]==0.10.1"
pip install --upgrade "ffwf-tau-coding-agent[repl]==0.10.1"   # the new head
```

Nothing in the on-disk session format changed.
