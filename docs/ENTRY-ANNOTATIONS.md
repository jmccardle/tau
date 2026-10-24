# An extension annotates an entry in the same write that records it

Built 2026-10-24 (#82) and the tau-jmfts `at=` fork copy of §5.4 (#84), except
the timeout of §8.1 (#83). An extension's `annotate` hook returns
JSON for each entry the loop writes. τ stores that JSON on the entry as
`ext[<extension name>]`, in the one write that records the entry, and never sends
it to the model. The first user of this is the tau-debugger shadow pass, which
runs the Horopter decider over every step and records what it answered. §8 holds
the one question that is not settled: the hook timeout.

## 1. What is missing

An extension cannot attach data to an entry that already exists. It has two
near-misses, and neither one fits:

- **A `tool_result` hook can patch `details`** (`tool_pipeline.py:423`). That works
  on a tool result only. It writes inside the tool's own `message` payload, which
  the tool owns, and it does nothing for an assistant message.
- **`api.append_entry(custom_type, data)`** (`extension_types.py:2467`) writes a
  new `customEntry`. That costs one more entry per annotated entry. It extends the
  path, so it moves the leaf. And it has to name its target by id, which breaks
  under a JMFTS fork (§5.2).

An assistant message cannot take data after it is written either. The loop
appends it as `status: "incomplete"` (`agent_loop.py:945`), and
`writer.finalize` writes the final version (`agent_loop.py:1014`). Only then does
`message_end` fire, and `finalized_entry` refuses a second finalize
(`session_log.py:118`). Every hook an extension has today, `message_end` and
`turn_end` included, runs after the entry is final.

The JMFTS mirror closes the last workaround. `_finalize_now` PATCHes
`structured_content={"tau": …, "seq": …}` (`tau-jmfts/src/tau_jmfts/store.py:700`),
and a JMFTS PATCH replaces that column. It does not merge. So data written into
the document between create and finalize is wiped. Writing after finalize needs a
read-modify-write, and that races unless it runs under `_append_lock`.

## 2. Where the data lives: `ext`, beside `message`

```json
{"type": "message", "id": "3fa9c1d2", "parentId": "…", "timestamp": "…",
 "message": {"role": "toolResult", "content": […], …},
 "ext": {"tau_debugger.shadow": {"horopter": {"hv-2026-10-20": {"fired": […], "gate": "pass"}}}}}
```

`ext` is an entry-level field, a sibling of `message`. Each key is an extension's
name, and each value is whatever that extension's `annotate` handler returned.
Since #35 that name is the module identifier, so it usually contains dots. A
reader must index `ext["tau_debugger.shadow"]` and must not split the key on
dots as a path.

- **The model never sees it.** `entries_to_messages` (`conversation_tree.py:158`)
  builds model input. It reads `entry["message"]` for `message` and
  `customMessage` entries (`:193-196`), and it builds the `compaction` and
  `branch_summary` messages from `summary`. It reads no other field. So the
  tree-as-truth invariant holds: what the model receives is still exactly the
  path's messages.
  The prompt-cache invariant holds for the same reason, because no byte of `ext`
  is in any request.
- **Not inside `message`.** The message is validated through pydantic models such
  as `ToolResultMessage` (`agent_loop.py:1578`), and its bytes are the model's
  input. Mixing extension data into it would put both of those at risk.
- **Not in `details`.** `details` belongs to the tool that ran. An entry is
  annotated by extensions that are not that tool, and assistant and user messages
  have no `details`.

The value must be JSON: a dict, list, str, int, float, bool or null, nested. A
handler that returns anything else has failed (§4.3). That includes a tuple,
which `json.dumps` would write as a list: the value must already be what a
reload returns, so a list, not a tuple. Dict keys must be str, and a float must
be finite.

## 3. When it is written: the `annotate` hook

### 3.1 One helper, called before every message write

Model-visible entries reach the log at four call sites in `agent_session.py`
(line numbers as built):

| entry | site | path |
|---|---|---|
| user, toolResult, turn-loop custom node | `_CursorWriter.append` (`:471`) | `Cursor.append_message` (one caller, `:493`) or `append_custom_message` |
| assistant | `open()` then `_CursorWriter.finalize` (`:459`) | `Cursor.finalize` |
| `user_turn_end` injection | `:4357` | `acting_cursor.append_custom_message`, bypassing the writer |
| `send_message`, the exposure notice (`:2700`) | `_append_custom_message` (`:4973`), when idle | `acting_cursor.append_custom_message`, bypassing the writer |

A tool result reaches `_CursorWriter.append` through `_record`
(`agent_loop.py:1567`), after the `tool_result` hook has run
(`agent_loop.py:1411` precedes `:1425`).

The annotation logic lives in **one** `AgentSession` method, `_annotation`. It
runs every `annotate` handler through `ExtensionRunner.emit_annotate`. All four
sites call it immediately before their store call. *Built note:* the sketch
returned `dict | None`; the built method returns `(ext, failure)`, because a
site writes the entry first and raises the failure after (§4.3), so it needs
both. `Cursor.append_message`,
`append_custom_message` and `finalize` each take an optional `ext`, which they
write beside `message`. The cursor itself stays unaware of extensions.

The coordinator's sketch placed the hook inside `_record` for tool results and
between `DoneEvent` and `finalize` for assistant messages. Those two are covered
by the writer. The two direct sites matter because §7 routes a steer through
`send_message`, and a steer is an entry the shadow pass must annotate.

**Excluded, by design:** the direct appends that write no message are never
annotated. These are `append_config` (`:2764`), `append_compaction` (`:4581`)
and `append_custom_entry` (`:5084`), along with `branch_summary`, model changes,
labels and navigation records. Compaction and `branch_summary` entries are model
input, but their text is a summary τ generated, not a message a turn produced.
None of the others is model input. None of them has blocks (§6).

An assistant message finalized after a stream error (`agent_loop.py:1159`) is
annotated the same way. Its `stop_reason` is in the message the handler receives.
The incomplete line that `open()` writes carries no `ext`.

When `store_history` is off (`_CursorWriter.persist` is `False`), nothing is
written, and `annotate` is not called. A result with nowhere to go is not
computed.

A branch spawned with `spawn(hooks=False)` dispatches through the
quiet runner (`AgentSession._hooks`), which has no registrations. So its entries
are never annotated, the same as every other hook on that branch.

### 3.2 The contract

```python
async def annotate(event: dict, ctx) -> JSON | None
# event = {"kind": "user" | "assistant" | "toolResult" | "custom",
#          "message": <the message dict about to be written>,
#          "blocks": [Block, …]}          # §6
```

- It is awaited. Extensions run in load order, the order every hook uses
  (`extensions/runner.py:18`).
- `None` means "nothing for this entry", and no key is written.
- Any other value is stored at `ext[<extension name>]`. An extension registers at
  most one `annotate` handler, so the name is unique. A second registration
  raises at load.
- If no extension returns a value, the entry has no `ext` field at all. Sessions
  without annotating extensions are byte-identical to today's.
- The handler cannot change the message. A handler that mutates
  `event["message"]` changes a copy.

### 3.3 Cost to the turn

The hook holds the write. So it also holds everything after the write: the next
sample, and for an assistant message the `message_end` event. The streamed text
is on screen before then. For the shadow pass, holding the next sample is the
blocking-gate behaviour it asks for. For an extension that only records, it is
latency that buys nothing, and §8.1 is about that case.

## 4. Write-once, by construction

### 4.1 Nothing rewrites an entry

`ext` is written in the entry's only completed write. The file store appends one
more line only for a finalize, and `finalized_entry` refuses an entry that is not
incomplete (`session_log.py:118`). No API that edits a finished entry is added.

### 4.2 Fork and navigate

- **navigate** moves the cursor and copies nothing. The shared prefix is the same
  entries, so it carries the same annotations.
- **`Session.fork`** copies the entry dicts whole
  (`tau-coding-agent/src/tau_coding_agent/session_store.py:451`), so
  `ext` travels.
- **The JMFTS fork** copies `structured_content` verbatim (`store.py:483-563`), so
  `ext` travels there too. The full-matrix children of §5.3 are a separate case,
  covered in §5.4.
- **`copy_of`** (branch and paste surgery) copies `message` plus `copiedFrom` only
  (`tree_surgery.py:474-482`). So `ext` does **not** travel with a copy. That is
  kept on purpose. A copy is a new entry at a new position, and an annotation that
  depends on position (the stall question, the obligation deadline) is false
  there. `copiedFrom` leads back to the original's annotations.

### 4.3 A failed handler

A handler can raise, or return a value that is not JSON. Either way, the entry
**is still written**, with the failure recorded in place of the annotation, and
the turn is then aborted. *Built note:* the blocks can fail too, before any
handler runs: `entry_blocks` raises on a content block of a type the message
does not carry, or on `arguments` or `details` that `json.dumps` cannot render
(a `datetime` a `tool_result` hook put in `details`, say). Then no handler runs,
every annotating extension records that error, and `AnnotationError.extension`
is `None`. The entry is written and the turn ends the same way.

1. The entry is written as usual, with `ext[<extension name>] =
   {"error": "<ExceptionType>: <message>"}`. Other extensions' annotations on the
   same entry are kept.
2. The turn aborts with that error after the write. It ends as an error
   `agent_end`, the way a provider failure does.

Writing the entry keeps the tree valid. Withholding it would wedge the session:

- **A tool result:** the tool has already run, and its call is in the finalized
  assistant entry. An unwritten result leaves the path ending on an unanswered
  call, and the provider rejects the next request on resume. The abort records
  `ABORTED_TOOL_RESULT` for each call in the batch that has no result yet,
  through the existing `_aborted_batch` (`agent_loop.py:1260`). A sequential
  batch runs no further tool; a parallel batch's calls are already running, so
  each writes its own result.
- **An assistant message:** without its finalize, the entry would stay
  `incomplete`. `entries_to_messages` raises `IncompleteEntryError` at it
  (`conversation_tree.py:187`), so the session could not continue from there.
  Its tool calls never run. Each gets a result through `_aborted_batch`, the
  path a failed stream takes with `UNRUN_TOOL_RESULT` (`agent_loop.py:1180`).
  *Built note:* the text written is `ABORTED_TOOL_RESULT`, not
  `UNRUN_TOOL_RESULT`, whose words ("the response stream failed") would tell the
  model something false here.
- **A user message or custom node:** the prompt or steer would be lost.

**No re-entry.** Only the first failure ends the turn. The aborted results, and
the `turn_error` node the session writes for a failed turn, go through
`annotate` like any entry. A handler that fails on one of them records its
`{"error": …}` there and does not abort a second time. A failure on an
assistant message finalized after a stream error is the same case: the stream
error ends the turn.

*Built note, how the loop expresses it.* The writer raises `AnnotationError`
after the write. The loop's `_write` and its two finalize sites note the first
one and do not raise. Checkpoints raise it once the entry is written: before
each request, after the assistant message (answering its calls first), after a
tool batch, and at the loop's end. An input written before the loop runs (the
prompt, a seed, a `before_agent_start` message) hands its failure to the loop as
`input_failure`, which raises it after `agent_start`, before any request. So
every one of those cases ends in an error `agent_end`. The two sites outside a
loop cannot: a failed `user_turn_end` node raises from `prompt()` after the
loop's `agent_end`, and a failed idle `send_message` raises from that call.

With the entry written, every case continues normally from the new leaf, once
the faulty extension is fixed or disabled. No separate repair path is needed.

The failure is recorded on the entry, not left as a gap. The shadow pass must see
it, because an unannotated entry reads as a pass. §8.1's timeout policy builds on
this failure state: a timed-out handler is a failed handler.

## 5. JMFTS

### 5.1 The mirror needs no new code

`create_document` and `_finalize_now` both write
`structured_content={"tau": stored, "seq": seq}`, where `stored` is the whole
entry payload (`store.py:752`, `:700`). So `ext` lands at
`structured_content.tau.ext` without mirror changes. The finalize PATCH carries
it, because it is in the finalize payload. So the replace-not-merge behaviour in
§1 never wipes it, and no read-modify-write is needed.

`_content_for` builds the document's text from the message, not from `ext`. So
annotations are not embedded and not BM25-indexed.

### 5.2 An annotation never names its own entry

In the JMFTS store the entry id is the document id (`store.py:756`). A JMFTS fork
re-mints every id. It remaps only the fields `_CROSS_REF_FIELDS` and
`_PROVENANCE_REF_FIELDS` name (`store.py:158-179`). So an entry id written inside
`ext` dangles after a JMFTS fork. The rule follows: position is implied by the
container. A cell inside `ext` is keyed by `{block, start, end, content_hash}`
(§6), never by an entry id.

### 5.3 The full matrix: a child document per (entry, model version)

John ruled out SQL schema changes to JMFTS. jmfts-arecibo confirmed this
design on 2026-10-24.

`ext` carries only what the steer and gate consumed. At a few hundred questions
over several cells, the full matrix is 10–100 KB of JSON per entry. That is
1–10 MB or more per session file. The file loader reads every line
(`keep_last_per_id`, `session_log.py:131`), and the TUI reopens sessions, so the
cost would be paid on every open.

The full matrix goes to JMFTS as one **child document per (entry, model
version)**, under the entry's document. It is created with:

- `usetype="horopter:annotations"`, exactly this literal. Search exclusion
  matches usetypes exactly, and the exclusion list lives in operator config.
- Empty `content`, `auto_embed=False`, `sequential=False`. A POST to
  `/documents` with these enqueues nothing and settles immediately, so no
  pipeline runs.
- `structured_content={"horopter": {"<model_version>": {"cells": […]}}}`.

Two things must never be in that `structured_content`:

- **A `tau` key.** `_is_tau_doc` (`store.py:212`, read at `:446`) would parse the
  document as a session entry.
- **A document id.** `_remap_cross_refs` leaves documents that are not τ's
  untouched, so an id stored inside the annotation dangles after a fork. This is
  the §5.2 rule again, for the child.

A re-annotation under a new model version POSTs a new sibling child. It never
PATCHes an existing one, because a PATCH replaces the whole value. No entry and
no earlier child is changed.

The τ `SessionLog` sees none of these documents. The extension writes them
through the JMFTS client, after the entry exists. So they are not part of the
one-write rule in §3. They are a sink, written by the extension that owns them.

**Ops note: search exclusion.** An annotation document has empty content and no
vector, so it cannot score in a search. Listing `horopter:annotations` in
`JMFTS_SEARCH_EXCLUDE_USETYPES` and `JMFTS_BM25_EXCLUDE_USETYPES` is optional
defence in depth. Either variable, when set, REPLACES the server's defaults, so
an operator who sets one must list the defaults plus `horopter:annotations`.

**Storage.** For JMFTS, the full matrix is roughly 10–100 MB of TOAST per
session (inferred, not measured). Measuring it on one real session is a P0 exit
item.

### 5.4 A fork at a point drops the children, unless tau-jmfts copies them

A full JMFTS fork copies the subtree, so the annotation children travel. A fork
with `at=` copies only the documents on the `ConversationTree` path
(`store.py:483-563`), so it drops every annotation child. Fork-and-compare
(RQ2.5) forks at a point and compares each branch against the shared prefix's
annotations. Under today's code, the fork has no annotations to compare against.

**Agreed by the reviewers (tau-search and tau-router, 2026-10-24):** tau-jmfts's `at=` fork also
copies every child of each copied entry that is not a τ document
(`not _is_tau_doc`), re-parented onto the copy. The store does not name
`horopter:annotations` or any other extension's usetype. Naming one would put an
extension in the store layer. §5.2's rule against document ids makes a generic
copy safe, because a copied child holds no reference that needs remapping.

This is a tau-jmfts change (#84), and JMFTS itself does not change. The cost is
one children listing per copied entry, plus one POST per child.

The rejected alternative was to leave the fork alone and have fork-and-compare
read the source session's annotations. That fork would silently depend on its
source still existing, and a fork that outlives its source would lose the
baseline.

**A shadow extension with no sink refuses to start.** If it is configured to
record the full matrix and no JMFTS client is reachable, it raises at load. It
does not drop the matrix silently.

## 6. Cells: what core derives, and what the extension derives

The hook receives the entry's **blocks**, already derived. The proposed P0
`Cell` is a block cut into segments under a token budget. τ has no tokenizer for
Horopter's vocabulary, so the cut is the extension's. That splits the derivation
into two parts:

**Core: `entry_blocks(kind, message) -> list[Block]`**, a pure function in
`tau_agent_core`. It is in scope for this record. `Block` is
`{block, kind, text, content_hash}`:

| kind | text, which offsets index |
|---|---|
| `user`, `assistant_text`, `custom` | the `TextContent.text` string |
| `thinking` | the `ThinkingContent` text |
| `tool_call` | `json.dumps(arguments, ensure_ascii=False)` of the stored `ToolCall.arguments`, with default separators and no `sort_keys` (`tau-llm/src/tau_llm/types.py:87`). |
| `tool_result` | a tool result's text content block |
| `details` | `json.dumps(details, ensure_ascii=False)`, with the same settings. This is its own kind, because `details` is not model-visible (the providers never read it). |

`tool_call` and `details` are renderings, because both fields are parsed
objects. Their offsets index the rendering that `entry_blocks` owns, not the
bytes the model streamed. The rendering keeps the stored key order, which is the
order the arguments were parsed in, so its text and `content_hash` depend on
that order. A JSON round trip preserves the order, so the rendering is stable
across a reload.

`block` is the index into `message.content`, or `"details"` for the details
block. A `content` that is a plain `str` (`UserMessage.content` is
`str | list[…]`, `tau-llm/src/tau_llm/types.py:154`; a custom node's content can
be a `str` too) is normalized to one text block at index 0. An image block
yields no `Block`. *Built note:* `create_custom_message` refuses content that is
not a str or a list of text and image blocks, so a custom node with any other
block fails when it is made, whether or not an `annotate` handler is loaded. Offsets are in code points (Python
`str` indices), because τ stores JSON strings. `content_hash` is the SHA-256 hex
of the text's UTF-8 bytes.

Deriving blocks in core puts the derivation in one place, which all three heads
share. The function costs about 50 lines, and the TUI's debugger view and the RPC
wire can call it without loading the extension.

**The extension: segments.** The shadow extension cuts each block into segments
that fit Horopter's 2k-token context, with Horopter's tokenizer. A cell key is
`{block, start, end, content_hash}`, where `content_hash` is now the hash of the
segment's text. It is Horopter's state-cache key, not an identity, because the
same text recurs (a re-read file, a repeated tool output). The model version is
part of the key in the §5.3 child, because a new tokenizer can cut different
segments.

## 7. What is absent

- **No annotation of an entry that already exists.** Re-annotation goes to a §5.3
  child. Adding a mutating path to the session file would end write-once, and the
  file store has no mechanism for it.
- **No `ext` on a copied entry** (§4.2).
- **No wire field.** `ext` reaches the RPC wire wherever entries already do, as
  part of the entry. No event carries it on its own, because no head renders it
  yet. The debugger view (S5) decides what a head shows.
- **No hook timeout in this change.** §8.1 is the open policy. No hook has a
  timeout today, and none is added here until that question is ruled on.
- **No tokenizer seam in core** (§6).
- **No constraints, steering or blocking from `annotate`.** It returns data and
  nothing else. A gate uses `tool_call`. A steer uses `send_message` with
  `visible_to_model`, or a `turn_end` return, and its metadata goes in that node's
  `details`.

## 8. Open

### 8.1 What a slow or unreachable decider does

No hook has a timeout today. A grep finds no `wait_for` in `extensions/runner.py`
or `tool_pipeline.py`, so a hung handler hangs the agent. The coordinator
proposes:

- A **gating** hook (`tool_call`, or an `annotate` whose result a gate reads)
  fails closed, with a visible "decider unavailable" error.
- An **advisory** hook proceeds with its default behaviour and raises a session
  warning through `ctx.set_warning` (`extension_types.py:2579`).

John has not ruled on this. Two things it must settle: whether the timeout is
per handler and declared at registration, or one global; and whether "advisory"
is the handler's declaration or the hook's kind. Until it is ruled on, `annotate`
behaves like every other hook: no timeout, and a raise is a failed handler (§4.3).

The reviewers so far lean towards one rule for `annotate` and tau-router's
decider pull alike: a per-handler timeout declared at registration, where the
handler declares itself advisory or gating (tau-router). Tectum prefers a
bounded timeout that fails loudly over no timeout. Whatever is ruled, a timed-out
handler fails in the §4.3 state. Tracked in #83.
