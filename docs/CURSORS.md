# A conversation is a tree; every writer is a cursor

Position and cost record (2026-10-02), nothing built. It replaces the store-owned
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
   to it, then navigate the branch back to `a`. Reload resolves to `a`. That is
   neither `b` nor the branch's newest message. The strict xfail
   `test_a_cursor_move_is_not_a_reload_point` (`test_branch_view.py`) pins this.
2. **A session swap discards queued messages without a trace.** The steer,
   followUp and nextTurn queues live on `AgentSession`, and so does the head's
   position. `_reset_transient_state` clears all three queues on every
   `new_session`, `fork` and `switch_session`. Strict xfail
   `test_a_swap_never_discards_a_queued_message_without_a_trace` pins this
   (`9a16771`).
3. **One extension context serves every cursor.** The context's `_signal` is
   overwritten when each turn starts (`agent_session.py:2798`). A hook running for
   one cursor would therefore read another cursor's abort. This is inferred, not
   run: `spawn_branch` avoids the case only because sub-agents start with no
   extensions (`extension_types.py:1330-1337`).

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
- The tree's append path serializes writes for every store, so no store relies on
  its callers taking turns.

Two processes opening one file is out of scope. This guards against corruption
from τ's own concurrency, not against operator error.

## 4. Where a reopened tree places its cursor

The default leaf is **the newest entry on a model path**: any kind except
`customEntry` and `navigate`.

**The rule in force today loses.** "Last entry of any kind" is measured in §1.1:
the trailing entry can be another cursor's bookkeeping.

**"Newest message" loses.** A compaction, an elide or a branch summary is appended
as a child of the leaf. A cursor placed on that message's parent would walk past the
anchor, and the fold would lose the summary. Those kinds are content; they are not
bookkeeping.

**Existing files load unchanged.** Their `navigate` entries become inert history.
The current rule follows a trailing navigate to its target, and the new rule ignores
it, so a session whose last action was a tree move reopens at its newest content
instead. Nothing is written to resume at a chosen leaf. `Tree.open_cursor(at=…)` does
that, which closes the gap recorded in `LANE-REMOVAL.md` §7.

**Moving a cursor writes nothing.** A cursor that is not durable has no position to
persist. The tree browser and RPC `navigate` set `cursor.leaf`.

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
is deleted.

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

An extension's module-level state is shared by every cursor, the way a web
application's globals are shared by concurrent requests. An extension that keeps
per-conversation state keys it by `ctx.cursor.id` or stores it in the tree.

## 8. A swap is a head moving between cursors

A head holds a cursor, and it does not own the queues on that cursor.

- `new_session`, `switch_session` and `fork` detach the head from one cursor and
  attach it to another.
- The old cursor keeps its queues. If it is idle and nothing owns it, it closes once
  they are empty and delivers what it holds to its own tree.
- The §1.2 xfail test therefore passes, by construction. It is deliberately neutral
  about which fix makes it pass.
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
| store `_leaf_id`, `SessionLog.cursor` | `Cursor.leaf` | the entry-id meaning of "cursor" is retired |
| `resolve_cursor` | `default_leaf(entries)` | the §4 rule |
| `append_navigate`, `navigate` entries | assigning `cursor.leaf`; no entry | old entries are inert |
| `lane` (sub-agent identity) | `cursor.id`, `cursor_id` on events | |
| `lane` (TUI render stream per submission) | a turn stream keyed by `submission_id` within the cursor's stream | `TurnStream` already names it |
| `RenderRouter`'s lanes, `open_lane`, `lane_start`/`lane_end` | streams keyed by `cursor_id`; `turn_start`/`turn_end` brackets | `test_tui_multi_lane_render.py` is renamed to match |
| `branch_event`, `branch_end` channels | `cursor_id` on every event; `cursor_open`/`cursor_close` | §6 |
| `BranchResult.lane` | the result's `cursor_id` | |
| "primary cursor", "primary leaf" | "the head's cursor" | 42 `src` lines say "primary"; some mean "primary bus" and stay |
| `agent_spec`, `agent_spec_in_force` | `config` entries, `config_at(entries, leaf)` | legacy kind still read, §5 |
| RPC `lane: "primary"`, `get_state.cursor` | `cursor: {id, leaf}` | protocol minor bump |

"Branch" keeps one meaning: the shape of the tree, as in "a branch of the
conversation". It does not name an object.

Records amended, each with a dated note at the top pointing here:

- `LANE-REMOVAL.md` §5 and §7
- `NODE-ADDRESSABLE-AGENTS.md` I2 and decisions 1, 3, 4 and 6
- `SESSION-TREE-IMPLEMENTATION.md` §2.2 (durable navigate)
- `REMOTE-CONTROL.md` F1 and F2 (lanes on the wire)

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

I did not measure the total. The rename alone touches about 600 lines that say
"lane" and about 290 that read `.cursor`. Those are line counts, not edits.

## 11. Absent

- **Durable writes.** Writing an entry as incomplete, finalizing it, the audit, and
  removing the made-up `stop_reason="stop"` (`agent_loop.py:927-940`) all belong to
  the next record, which uses the vocabulary set here.
- **The web head and cursor sharing between clients.** Equal control is the
  default. That is the third record. RPC gets only the rename in §9.
- **Cursor persistence.** Cursors are not durable, by decision.
- **A cross-process lock.** It is out of scope (§3).
- **Hooks in sub-agents by default.** They are opt-in (§6).
- **A cursor ownership tree beyond abort.** That would mean durable tasks,
  background ownership or timers. This is the smallest piece of Pi Durable's task
  tree that fixes a measured defect, and nothing more of it is adopted.
