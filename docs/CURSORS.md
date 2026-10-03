# A conversation is a tree; every writer is a cursor

Built 2026-10-02, all six steps of §10; each **Built:** note below records where
the build diverged from the design. It replaces the store-owned
leaf, `BranchView`, the name "lane" and the frame-only agent config with one model:
a **tree** of entries, and **cursors** that extend it. It is the first of three
records. *Durable writes* (incomplete → finalize) and *the web head* (a fourth
head with cursor sharing) are written in its terms and are not covered here.

## 1. What is wrong

τ already has two cursor implementations, and neither of them is called a cursor.

- **The store owns one leaf.** Each `SessionLog` keeps a `_leaf_id` (31 source
  lines across `session_log.py`, `session_store.py` and the JMFTS `store.py`). Every
  `append_*` call parents the new entry at that leaf and then moves it.
- **`BranchView` is the second implementation, built backwards.** It is an
  imitation `SessionLog` wrapped around the real one (`session_log.py:672-843`). It
  owns a leaf, writes through `append_at`, and moves only itself.

  `spawn_branch` gives it to a whole second `AgentSession`
  (`extension_types.py:1362-1370`). That session copies `api_key`, the model, the
  prompt and the filtered tools. It drops extensions, compaction settings and the
  parent's abort. It also drops the reasoning level, and that drop is undocumented.

Three defects follow from this, and each one is measured.

1. **A sub-agent can move the conversation's reload point.** The reload rule
   (`resolve_cursor`, `session_log.py:245-275`) takes the last entry of any kind.
   A branch's `append_navigate` writes into the shared log.

   Run: on a log with messages `a → b`, open a branch at `a`, append one message
   to it, then navigate the branch back to `a`. Reload resolved to `a`, which is
   neither `b` nor the branch's newest message. Fixed by step 1: a move writes
   nothing, and `test_a_cursor_move_is_not_a_reload_point` (`test_cursor.py`) pins it.
2. **A session swap discards queued messages without a trace.** The steer,
   followUp and nextTurn queues live on `AgentSession`, and so does the head's
   position. `_reset_transient_state` clears all three queues on every
   `new_session`, `fork` and `switch_session`. Strict xfail
   `test_a_swap_never_discards_a_queued_message_without_a_trace` pinned this
   (`9a16771`). Fixed by step 2 (§8); the test is no longer an xfail.
3. **One extension context serves every cursor.** The context's `_signal` is
   overwritten when each turn starts (`agent_session.py:2798`). A hook running for
   one cursor would therefore read another cursor's abort. This is inferred, not
   run: `spawn_branch` avoids the case only because sub-agents start with no
   extensions (`extension_types.py:1330-1337`). Fixed by step 5 (§7).

The vocabulary did not follow the model. `LANE-REMOVAL.md` (2026-08-21) took the
lane tag off disk, but "lane" is still on these lines:

| Where | Lines with "lane" |
|---|---|
| five `src` trees | 289 |
| tests | 198 |
| `docs/` | 108 |

`backends.py` alone has 95 such lines. There, a "lane" is either a submission's
render stream or a sub-agent's cursor, depending on the call site.

"Cursor" itself has two meanings. 74 source lines read `.cursor` as an **entry id**.
"A cursor" in design talk means the thing that extends the tree.

## 2. The model

A **tree** is the log plus everything that must be shared by whoever writes to it:

- the single append path;
- the extension runner;
- the registry of live cursors.

One tree is one session. A `session_id` names a tree.

A **cursor** is a position the tree is extended from, plus the per-turn machinery
for extending it there.

| Owned by | State |
|---|---|
| Tree | the `SessionLog`; append serialization; extension runner and its registrations; live-cursor registry; the event bus |
| Cursor | `id` (runtime only); `leaf`; turn lock; abort signal; steer, followUp and nextTurn queues; turn persistence; `pre_turn_leaf`; usage and calibrator; pending config; `owner` (the cursor that opened it, or `None`) |

The two terms are defined as follows.

- **`leaf`** is the entry a cursor's next append is parented to. It is a leaf of the
  cursor's path, but it may be an interior node of the tree, for example after a
  move. `cursor.leaf` replaces every reading of `.cursor` as an entry id.
- **A cursor is not durable.** It is the connection between something that can
  become busy and the place its results land. A process exit ends every cursor.
  Reopening a tree places a new cursor (§4).

**Built:** there is no `Tree` class. `AgentSession` holds the tree's half of the
table: the log, the extension runner, the bus, and the registry of live cursors
(`open_cursor`, `close_cursor`, `cursors`). A `Tree` object would have been a
second owner of the same four things. The cursor's half is `tau_agent_core.cursor.Cursor`,
and the turn running now names its cursor through the `TURN_CURSOR` context variable.

The TUI's position, `tau -p`'s position, an RPC client's position and a sub-agent
are all instances of the same `Cursor` class. None of them is "primary":
`LANE-REMOVAL.md` §7 rejected a privileged cursor, and the store-owned leaf kept
one anyway. That leaf is removed.

## 3. `SessionLog` keeps no leaf

The store contract shrinks to the operations that are about storage:

- `id`
- `entries()`
- `append_at(parent_id, entry_type, payload)`
- the finalize write that *durable writes* adds

These operations leave the contract:

- `cursor`
- the eight leaf-moving `append_*` helpers
- `append_navigate`

The typed helpers, for example `append_compaction`, move onto `Cursor`. There they
keep their Fail-Early checks on anchors, and every one of them is `append_at(self.leaf, …)`
followed by moving the leaf.

**The alternative loses.** That alternative is keeping a "default" leaf in the store
for single-cursor callers. Under it, every store implements cursor movement, so the
contract suites test it. `session_log_contract.py` has 39 `.cursor` lines and 15
`append_navigate` lines. Every one of those tests is a test of a policy that does not
belong to storage.

**Single writer** means one append path per tree, inside one process. Concurrent
cursors must not interleave a write or race an id.

- The JSONL store is already safe here, because `_append_at_now` does not await.
- The JMFTS store is not safe. It takes `_next_seq` without a lock inside a thread
  hop (`tau_jmfts/store.py:684-704`). If two cursors race, `load` later raises
  "second writer touched the tree", which leaves the conversation unloadable.
- **Built:** each store is safe under concurrent `append_at` on its own. The JMFTS
  store holds a lock from the seq draw to the mirror update, and the contract
  suite's `test_concurrent_appends_all_land_whole` holds every store to it. That
  puts the guarantee where the hazard is, rather than in a tree-level append path
  every caller would have to remember to go through.

Two processes opening one file is out of scope. This guards against corruption
from τ's own concurrency, not against operator error.

## 4. Where a reopened tree places its cursor

The default leaf is **the newest entry that is not a `navigate`**
(`session_log.default_leaf`). **Built:** it also skips a namespaced kind, one whose
type contains `:` such as `jmfts:document`. The JMFTS store files such entries
under the newest entry, not at a cursor's leaf, so a reopened cursor landed on a
document instead of the conversation.

Every other kind is written at some cursor's leaf, so it lies on that cursor's
path, and a cursor placed on it sees that path's context. A `navigate` is the one
kind that names a different position. The draft of this record also excluded
`customEntry`; the build dropped that, because a `customEntry` is written at a
leaf like anything else and excluding it bought nothing.

**The old rule loses.** It followed a trailing `navigate` to its target, which
§1.1 measured: the trailing entry can be another cursor's move.

**"Newest message" loses.** A compaction, an elide or a branch summary is appended
as a child of the leaf. A cursor placed on that message's parent would walk past the
anchor, and the fold would lose the summary. Those kinds are content; they are not
bookkeeping.

**Existing files load unchanged.** Their `navigate` entries become inert history,
so a session whose last action was a tree move reopens at its newest content
instead. Resuming at a chosen leaf is `Cursor(log, leaf)` in process, or
`SessionCatalog.fork(source, cwd, at=leaf)` across processes, which copies only the
path to `leaf`. That closes the gap recorded in `LANE-REMOVAL.md` §7.

**Moving a cursor writes nothing.** A cursor that is not durable has no position to
persist. The tree browser and RPC `navigate` set `cursor.leaf`. A long-lived host,
such as the web head, keeps its cursors in memory, so swapping between them or
resuming one is a lookup and needs no reload. When a cursor opens, moves or closes,
the bus reports it (`cursor_open`, `cursor_close`). An extension that wants that
history on disk writes its own `customEntry`. Core defines no record type for
starting, stopping, storing or reloading a cursor (decided 2026-10-02).

## 5. Config lives in the tree

The effective config at a leaf is the fold of `config` entries on its path. A
`config` entry is a `customEntry` of type `config`. It carries any of `model`
(`{provider, id}`), `thinking`, `tools`, `cwd`, `extensions` and `system_prompt`.

- `customEntry` never reaches the model. A config entry therefore costs no prompt
  tokens and does not disturb a cached prefix.
- Creating a tree writes one config entry at the root, before the system message.

**Config takes effect where it is recorded.**

- Picking a model, a thinking level or a tool set changes the cursor's **pending**
  config.
- The next append on that cursor first writes a config entry, if the pending
  config differs from the walk at `leaf`.
- Moving a cursor resets its pending config to the walk at the new leaf.

A head shows the effective config. The status line already shows the model.

**Built:** there is no per-cursor pending config. `AgentSession._record_config`
compares the session's frame with `config_at(entries, leaf)` before a turn's first
append, and writes only the keys that differ. The effect matches the design,
because config is recorded where it takes effect. A move needs no reset, because
the walk at the new leaf is the comparison. The keys are `CONFIG_KEYS`
(`session_log.py`). The prompt is recorded as `system_prompt_digest`, a sha256 and
never the text, and the model as `model_spec`. Resume reads `thinking` back
through `resolve_model_config(prior_config=…)`.

**The alternative loses.** That alternative is reading the newest `model_change` in
log order (`Session.model`, `session_store.py:337-350`). It makes a branch's
behaviour depend on edits made in an unrelated branch, because log order is not
conversation order. The answer each model gave is already on the path in
`AssistantMessage.model`, so ancestry is also what the transcript says.

**Legacy entry kinds are read as config by the fold:**

- `model_change` is read as `{model}`;
- `agent_spec` is read as `{model, tools, extensions, cwd}`;
- the header `cwd` is read as the root's `{cwd}`.

No new writer emits these kinds. The `thinking_change` appender has no callers and
is deleted; the fold still reads old `thinking_change` entries as `{thinking}`.

**This reverses three `NODE-ADDRESSABLE-AGENTS.md` decisions:**

- I2: the frame is ephemeral and unpersisted;
- decision 1: reconstruction is not a goal;
- decision 3: `agent_spec` is a record, never a contract.

That record gets a superseded note pointing here. Its I1 (a leaf's context is a pure
function of its ancestors) is unchanged, and now applies to config as well.

**Tools take their `cwd` from the cursor's config.** Today `create_agent_session(cwd=)`
reaches the system prompt but not the tools (`sdk.py:213-219`), which is the defect
this fixes.

## 6. A sub-agent is a cursor

`spawn_branch(parent_id, prompt, tools=…)` is replaced by opening a cursor on the
spawner's own tree. Its owner is the cursor of the turn that asked.

- **Config.** The sub-agent's config is a config entry at its first append. It
  holds the tool allowlist, the model, the prompt and the thinking level, so the
  sub-agent's spec is recorded and not passed as constructor arguments. The
  `tools` allowlist stays required, for the reason the current docstring gives.
- **Abort cascades.** Aborting a cursor aborts every live cursor it owns, owned
  cursors first. Today a branch awaited inside a tool has its own signal, so the
  parent's abort does not reach it.
- **Extensions are opt-in.** By default a sub-agent's cursor runs no extension hooks,
  which matches today. Opting in is allowed, because §7 gives each cursor its own
  context.
- **The result** carries the cursor's `id`, its final `leaf`, its messages, and `ok`
  or `error`. Containment is unchanged: a failed sub-agent returns `ok=False` and
  never aborts its owner.
- **Events.** Every event on the tree's bus carries `cursor_id`. The `branch_event`
  and `branch_end` channels, which re-emit a sub-session's events under a lane key,
  are deleted. A cursor's lifetime is bracketed by `cursor_open` and `cursor_close`.

**Built:** the operation is `AgentSession.spawn`. `ctx.spawn_branch` keeps its
name for extensions and delegates to it. The sub-agent's differences from its
session are a `TurnFrame(tools, model, system_prompt, max_turns, hooks=False)` on
its cursor. The frame has no thinking field, so a sub-agent runs at its session's
thinking level, and `_record_config` records that level. `BranchResult.cursor_id`
replaces `lane`.

**A consequence of §4:** a sub-agent writes entries into the shared log. If it is
the last writer, a reopened tree places its cursor on the sub-agent's newest entry.
That entry is a real leaf of the conversation, and §4 is the agreed rule.

`fork` as a `multitask_strategy` is the same operation started by a submission,
rather than by a tool.

## 7. An extension sees the cursor its event happened on

There is one `ExtensionContext` per cursor. It is built when the cursor opens and
handed to every handler that runs for that cursor. The handler-facing surface is
already scoped to a cursor in everything but construction (`extension_types.py:861-1518`):

- `signal`, `abort`, `is_idle`, `get_model`/`set_model`
- `prompt`, `compact`, `entries`, `navigate`, `fork`, `cwd`

Tree-wide operations are reached through `ctx.tree`.

The registrations stay on the tree:

- `api.on`
- `register_tool`
- commands

**Built:** there is still one `ExtensionContext` per session. `ctx.signal` and
`ctx.cursor` resolve through `TURN_CURSOR`, so a handler reads the cursor of the
turn it runs in. That gives the per-cursor reading without building a context per
cursor. There is no `ctx.tree`. Tree-wide operations stay on `ctx` and act on the
current cursor's tree.

An extension's module-level state is shared by every cursor, the way a web
application's globals are shared by concurrent requests. An extension that keeps
per-conversation state keys it by `ctx.cursor.id` or stores it in the tree.

## 8. A swap is a head moving between cursors

A head holds a cursor, and it does not own the queues on that cursor.

- `new_session`, `switch_session` and `fork` detach the head from one cursor and
  attach it to another.
- The old cursor keeps its queues. If it is idle and nothing owns it, it delivers
  what it holds to its own tree, running a turn there with no head attached. That
  is the same thing a sub-agent does. It closes once its queues are empty.
- The §1.2 xfail test therefore passes, by construction. It is deliberately neutral
  about which fix makes it pass.
- **Built:** a swap aborts the old cursor only when it is busy, attaches the head
  to a fresh cursor, and then calls `deliver_queued(old)`. Each queued text runs as
  a turn with source `"extension"` and submitter `"cursor:<id>"`, and the cursor
  closes afterwards. `wait_for_deliveries()` lets a caller wait for those turns.
- `abort()` and `rollback` still clear the steer queue. That behaviour is designed
  and is tested in `test_submit_steer.py` `TestAbortAndRollback`.

## 9. Names

This record is also a hygiene target. The rename covers source, tests, test file
names, comments, docstrings and `docs/`, in the same change as the code. Leaving
"lane" in prose that describes cursors is the defect, and the fact that the tests
still pass does not make it one less.

| Today | Becomes | Note |
|---|---|---|
| `BranchView`, `open_branch` | `Cursor`, `Tree.open_cursor(at=, owner=)` | `test_branch_view.py` becomes `test_cursor.py` |
| store `_leaf_id`, `SessionLog.cursor`, `ConversationTree.cursor` | `Cursor.leaf`, `ConversationTree.leaf` | the entry-id meaning of "cursor" is retired in Python. `ConversationTree.navigate` is deleted: a tree is read at one leaf |
| `resolve_cursor` | `default_leaf(entries)` | the §4 rule |
| `append_navigate`, `navigate` entries | assigning `cursor.leaf`; no entry | old entries are inert |
| `lane` (sub-agent identity) | `cursor.id`, `cursor_id` on events | |
| `lane` (TUI render stream per submission) | a turn stream keyed by `submission_id` within the cursor's stream | `TurnStream` already names it |
| `RenderRouter`'s lanes, `open_lane`, `lane_start`/`lane_end` | streams keyed by `submission_id`; `stream_start`/`stream_end` render events; `StreamStrip` | **Built** differently from the draft, which named the brackets after the bus channels. A render dict named `submission_start` would share a name with the channel it is built from and carry a different payload. `turn_start`/`turn_end` are `AgentEvent` types and are not reused. `test_tui_multi_lane_render.py` is now `test_tui_multi_stream_render.py` |
| `branch_event`, `branch_end` channels | `cursor_id` on every event; `cursor_open`/`cursor_close` | §6 |
| `BranchResult.lane` | the result's `cursor_id` | |
| "primary cursor", "primary leaf" | "the head's cursor" | "primary bus", theme tokens and "primary output" stay |
| `agent_spec`, `agent_spec_in_force` | `config` entries, `config_at(entries, leaf)` | legacy kind still read, §5 |
| RPC `lane: "primary"` | `cursor_id` on the session tuple and on `WireEvent` | protocol 1.7 → 1.8. **Built:** the wire keeps `cursor` as an entry id (`get_state.cursor`, `navigate`'s result, `complete_message_id`'s `cursor` and scopes, `is_cursor`). On the wire it names the RPC session's cursor's position. Renaming it would break tau-code for no change in meaning, so the wire rename was postponed |

"Branch" keeps one meaning: the shape of the tree, as in "a branch of the
conversation". It does not name an object.

Records amended, each with a dated note pointing here:

- `LANE-REMOVAL.md` §5 and §7
- `NODE-ADDRESSABLE-AGENTS.md` I2 and decisions 1, 3, 4 and 6
- `SESSION-TREE-IMPLEMENTATION.md` §2.2 (durable navigate)
- `REMOTE-CONTROL.md` F1 and F2 (lanes on the wire)
- stale references in `HEADS-AND-MULTIPLEXER.md`, `SUBMISSION-LIFECYCLE.md`,
  `ASYNC-SESSION-LOG.md`, `EXTENSION-LOCKS.md` and `VSCODE-HEAD.md` §5.1

## 10. Order and cost

Each step leaves the suite green and the gates clean.

1. **`Tree` and `Cursor` exist.** `AgentSession` takes a `Cursor` and delegates
   per-turn state to it. `BranchView` becomes a `Cursor`. The store leaf is still
   present but unused. *Large.*
2. **The leaf leaves the store.** The contract suites drop cursor tests and add
   `default_leaf` tests. The JMFTS append is serialized through the tree. *Medium.*
3. **Config entries and the fold.** Legacy kinds are read. Tools take `cwd` from
   config. *Medium.*
4. **Sub-agents are owned cursors.** Abort cascades, and events carry `cursor_id`.
   *Medium.*
5. **One extension context per cursor.** *Small to medium.*
6. **Rename and amend.** This covers §9 across the TUI, RPC (protocol bump) and
   `docs/`. It is mostly mechanical but large. It lands with steps 1–5 where it
   touches their code, and the remainder lands last.

The build landed as five commits. Step 5 came with step 2, because
`TURN_CURSOR` is what both need. Step 6's rename went into the commit whose code it
touched, and the remainder landed last. After it, "lane" survives in the five
`src` trees only as a citation of `LANE-REMOVAL.md`.

## 11. Absent

- **Durable writes.** Writing an entry as incomplete, finalizing it, the audit, and
  removing the made-up `stop_reason="stop"` (`agent_loop.py:927-940`) all belong to
  the next record, which uses the vocabulary set here. That record is now
  `docs/TAU-SERVE.md` §4 (planned 2026-10-02).
- **The web head and cursor sharing between clients.** Equal control is the
  default. That is the third record, now `docs/TAU-SERVE.md` §5–§7. RPC gets only the rename in §9.
- **Cursor persistence.** Cursors are not durable, by decision.
- **A cross-process lock.** It is out of scope (§3).
- **Hooks in sub-agents by default.** They are opt-in (§6).
- **The wire rename of `cursor` to a leaf name.** §9 records why.
- **A cursor ownership tree beyond abort.** That would mean durable tasks,
  background ownership or timers. This is the smallest piece of Pi Durable's task
  tree that fixes a measured defect, and nothing more of it is adopted.
