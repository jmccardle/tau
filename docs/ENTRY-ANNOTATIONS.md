# An extension annotates an entry in the same write that records it

Design record (2026-10-24), nothing built. An extension's `annotate` hook returns
JSON for each entry the loop writes. τ stores that JSON on the entry as
`ext[<extension name>]`, in the one write that records the entry, and never sends
it to the model. The first user of this is the tau-debugger shadow pass, which
runs the Horopter decider over every step and records what it answered. §8 holds
two questions that are not settled.

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
 "ext": {"shadow": {"horopter": {"hv-2026-10-20": {"fired": […], "gate": "pass"}}}}}
```

`ext` is an entry-level field, a sibling of `message`. Each key is an extension's
name, and each value is whatever that extension's `annotate` handler returned.

- **The model never sees it.** Model input is built from `entry["message"]` only
  (`conversation_tree.py:95`, through `entries_to_messages`). So the tree-as-truth
  invariant holds: what the model receives is still exactly the path's messages.
  The prompt-cache invariant holds for the same reason, because no byte of `ext`
  is in any request.
- **Not inside `message`.** The message is validated through pydantic models such
  as `ToolResultMessage` (`agent_loop.py:1505`), and its bytes are the model's
  input. Mixing extension data into it would put both of those at risk.
- **Not in `details`.** `details` belongs to the tool that ran. An entry is
  annotated by extensions that are not that tool, and assistant and user messages
  have no `details`.

The value must be JSON: a dict, list, str, int, float, bool or null, nested. A
handler that returns anything else fails the write (§4.3).

## 3. When it is written: the `annotate` hook

### 3.1 The site is the turn writer, not the loop

Every message a turn records reaches the log through `_CursorWriter`
(`agent_session.py:425`). `append` takes the user message, each tool result
(`_record`, `agent_loop.py:1491`), steers and custom nodes. `finalize` takes the
assistant message. `Cursor.append_message` has one caller, at
`agent_session.py:472`.

So `annotate` runs inside the writer, immediately before the store call:

| entry | today's write | where `annotate` runs |
|---|---|---|
| user | `_CursorWriter.append` | before `append_message` |
| toolResult | `_record` → `_CursorWriter.append` | before `append_message`; the `tool_result` hook has already run (`agent_loop.py:1336` precedes `:1350`) |
| custom node | `_CursorWriter.append` | before `append_custom_message` |
| assistant | `open()` then `_CursorWriter.finalize` | before `Cursor.finalize`, so its return joins the finalize payload |

The coordinator's sketch placed the hook inside `_record` for tool results and
between `DoneEvent` and `finalize` for assistant messages. The writer is the
common point under both of those, and it covers user messages and custom nodes
with no third site. That is the one place this record departs from the sketch.

An assistant message finalized after a stream error (`agent_loop.py:1087`) is
annotated the same way. Its `stop_reason` is in the message the handler receives.
The incomplete line that `open()` writes carries no `ext`.

When `store_history` is off (`_CursorWriter.persist` is `False`), nothing is
written, and `annotate` is not called. A result with nowhere to go is not
computed.

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
- **`Session.fork`** copies the entry dicts whole (`session_store.py:~480`), so
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

A handler that raises, or returns a value that is not JSON, fails the write. It
does not fail quietly, and the entry is not written without its annotation. That
is a fault the shadow pass must see (a gap in the record looks like a pass). It
is also the boundary §8.1 has to place for a slow handler.

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

**Default:** tau-jmfts's `at=` fork also copies the `horopter:annotations`
children of each entry it copies, re-parented onto the copy. This is a tau-jmfts
change. JMFTS itself does not change. The cost is one children listing per
copied entry, plus one POST per annotation child.

**Alternative, for the reviewers:** leave the fork as it is, and have
fork-and-compare read the prefix's annotations from the source session's
documents. That avoids the copy, but the comparison then depends on the source
session still existing. A fork that outlives its source loses the baseline.

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
| `user`, `assistant_text` | the `TextContent.text` string |
| `thinking` | the `ThinkingContent` text |
| `tool_call` | `json.dumps(arguments, ensure_ascii=False)` of the stored `ToolCall.arguments`. It is a rendering, because `arguments` is a parsed object (`tau-llm/src/tau_llm/types.py:87`). The function owns the rendering, so it is stable. |
| `tool_result` | a tool result's text content block |
| `details` | `json.dumps(details, ensure_ascii=False)`. This is its own kind, because `details` is not model-visible (the providers never read it). |

`block` is the index into `message.content`, or `"details"` for the details
block. An image block yields no `Block`. Offsets are in code points (Python
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
behaves like every other hook: no timeout, and a raise fails the write (§4.3).

### 8.2 The `at=` fork

§5.4's default copies annotation children on a fork at a point. Its alternative
keeps the fork unchanged and reads the source session. The reviewers choose
between them. If no reviewer objects, the default is built.
