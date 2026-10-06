Experiment record (2026-10-05). Nothing here is wired into the five packages.

# A stateful sandbox whose state is in the tree

**Question.** Can a persistent interpreter satisfy tree-as-truth
(`EXTENSIONS-ORCHESTRATION-PLAN.md` §4) if its state is written into the tree as
`customEntry` records that the model never sees? Specifically, does that give
rollback, fork, resume, and a delta a summary can quote?

**Answer.** Yes, for Monty. It ran end to end against τ's real file `Session`,
`AgentSession`, `Cursor` and a real turn. The state at any entry is the newest
`sandbox_state` on that entry's ancestry, so rollback, fork and resume need no new
mechanism: they are the same `parentId` walk `config_at` uses. The live
interpreter becomes a cache of one entry, which is the condition `CODE-MODE.md`
§6.4 sets for a persistent interpreter, met by *loading a dump* rather than by
*replaying scripts*. CPython namespace pickling is a poor substitute (§4). Replay
works, but it is slower and silently wrong on nondeterminism, and it is still
worth keeping as the migration path.

Tags: **[ran]** measured or tested here; **[read]** taken from source or docs;
**[inferred]** reasoned from either; **[assumed]** not checked.

## 1. What was built

| File | What |
|---|---|
| `sandbox_state.py` | `MontySandbox` (run, restore, the cache), the entry format, `BlobStore`, the listing, `diff_listings`/`sandbox_delta_text`, `carry_state`, `summarize_branch_with_sandbox`, `make_python_tool` (a real `AgentTool`) |
| `cpython_alt.py` | dill and cloudpickle namespace pickling; replay with recorded tool results and divergence checks |
| `test_stateful_sandbox.py` | 11 tests on real τ objects; run with `pytest experiments/stateful-sandbox` |
| `resume_probe.py` | The resume test's child process |
| `measure.py` → `results-2026-10-05.json` | Every number below |

Environment: Python 3.11.2, `pydantic-monty` 1.1.0, dill 0.4.1, cloudpickle 3.1.2,
in a venv with τ installed editable from this worktree.

**One run writes one entry** (`customType: "sandbox_state"`), at the cursor's
leaf. A `python` tool call therefore writes assistant(toolCall) → `sandbox_state`
→ toolResult **[ran]**. Writing the state *before* the result is deliberate: a
cursor moved to the toolResult has the state that result describes, and a cursor
moved to the tool call has neither. The entry holds these fields:

```
v, generation (per path), parent_state (entry id), script, script_sha256,
outcome ("ok"|"error"), error, output (≤4 KiB), tool_calls [{name,args,kwargs,result,error,ms}],
state: {engine, engine_version, sha256, raw_bytes, compressed_bytes, inline | blob},
listing: {name: {type, repr (≤120 chars), digest, size}}
```

**Order within a run:** run, then list, then dump. The listing is taken before
the dump, so the bytes and the listing describe the same heap, including any
`__repr__` side effect.

**State storage.** The dump is zlib-compressed. At 64 KiB compressed or less it is
inline as base64. Above that it is a content-addressed side file
(`<session>.jsonl.sandbox/<sha256>.zlib`) that the entry names by hash. A missing
or altered file raises `SandboxError` rather than restoring an empty sandbox
**[ran]**. A dump from another Monty version also raises, and names replay as the
way to rebuild **[ran]**. Monty's docs say dumps carry no authentication
**[read]**, so the sha256 in the entry is the integrity check.

**The listing comes from the host.** Monty 1.1.0 has no `globals()`, `dir()` or
`vars()` **[ran]**. The host parses each script for the module-level names it
binds (`script_bindings`), then evaluates `(type(n).__name__, repr(n))` for every
known name and drops the ones that raise `NameError`. Monty has no `del` or
`exec` **[read: CODE-MODE.md §6.2]**, so for this subset the AST plus the previous
listing name every global **[inferred]**. A value's digest is the sha256 of its
full repr. A function or class is digested by its source, because its repr
(`<function 'f' at 0xc>`) does not change when it is redefined **[ran]**.

**A failed script is transactional.** Monty keeps the assignments a script made
before it raised (`p = 1; q = 1/0` leaves `p`) **[ran]**. So on an error the entry
records the script's nested calls with `state: null`, the live interpreter is
discarded, and the next run restores the last good state **[ran]**. The model is
told in the tool result: "sandbox unchanged: the script's partial effects were
discarded".

## 2. What the tests prove [ran: 11 passed]

| Test | Shows |
|---|---|
| rollback | After `cursor.move(earlier)`, the next run sees `x == 1`, `items == [1]`, and no `y`. The new entry's `parent_state` is the earlier state. `generation` counts per path, so two branches can both be at generation 2. |
| fork | Two cursors opened at one entry run alternately (each switch is a cache miss and a reload). Each sees only its own writes (`n` is 2 on one, 200 on the other), and a third cursor at the fork point still sees `n == 0`. |
| resume | A **separate OS process** loads the session file into a new `AgentSession`, opens a cursor at the default leaf, and restores exact values. These include a dict, a function (`twice(5) == 10`), a recorded `fetch` result, and a 200k-character string held in a side file. Restoring at an earlier leaf in that process gives the earlier state. The host's `fetch` counter does not move, so restore re-runs no tool. |
| real turn | A scripted provider issues a `python` tool call through `AgentSession.submit`, and the tool records on `TURN_CURSOR`. The path is assistant → `customEntry` → toolResult. `context()` contains no entry payload, and the toolResult text carries `[sandbox] +r:dict`. |
| failed script | `p` stays 1, a partially assigned `q` is absent, and the `fetch` call is recorded. |
| memory limit | A `MemoryError` at `max_memory=4 MB` leaves the last good state readable. |
| host functions and files | `getter = fetch` dumps fine but restores as a name, so a sandbox built without `fetch` raises `NameError` on `getter('x')`. `open('/etc/hostname')` raises `PermissionError`, because there is no filesystem without a mount. |
| missing side file | Raises instead of restoring an empty sandbox. |
| branch summary | `summarize_branch_with_sandbox` adds an exact delta to the LLM summary (mocked). With `carry=True` it writes a state entry adopting the abandoned branch's state, so the variables the summary describes exist. |
| compaction | The compaction summary carries `+ acc_3: int = 3` while the messages are gone, and the state still restores, because compaction does not change ancestry. |

The delta text for the branch test, generated from the entries alone:

```
Sandbox: 3 script(s) ran (1 failed, no effect); nested calls fetch×1.
Those scripts changed the sandbox as follows. These changes are NOT in your sandbox now; it is as it was before them:
  + kept: list = [2, 3]
  + page: dict = {'url': 'a', 'status': 200, 'body': '<html>a</html>'}
  ~ rows: list [1, 2, 3] -> list [1, 2, 3, 4]
  ~ threshold: int 2 -> int 3
```

The tool results the model read along that branch were `[sandbox] +kept:list
~threshold:int`, and then `Error: ZeroDivisionError: division by zero (sandbox
unchanged: the script's partial effects were discarded)`.

## 3. Measurements [ran; medians of 7]

### 3.1 Monty: dump size and latency (`results-2026-10-05.json`)

| State | Raw dump | zlib | Dump ms | Checkout + load ms |
|---|---:|---:|---:|---:|
| empty | 875 B | 550 B | 0.13 | 0.58 |
| 1k-row dict of dicts | 209,853 | 23,815 | 3.3 | 5.4 |
| 5 functions | 3,070 | 1,427 | 0.20 | 0.66 |
| 1 MiB string (`'x' * 2**20`) | 1,049,505 | 1,721 | 3.7 | 2.4 |
| 200k random letters | 205,529 | 132,560 | 0.47 | 0.75 |
| rows + functions + 1 MiB string | 1,260,742 | 27,303 | 5.9 | 6.9 |

A Monty dump is roughly the heap. zlib turns that into roughly its entropy, so
compression is not optional. Dump bytes differ between two sessions that ran the
same code, so a dump's hash identifies its bytes and not its contents. It cannot
deduplicate equal states, and a diff has to use the listing **[ran]**.

### 3.2 Per-script overhead through `MontySandbox.run` on a real session file

| State | Entry line in .jsonl | Side file | Listing ms | Dump + encode ms | Cache-hit overhead ms | Cache-miss restore ms |
|---|---:|---:|---:|---:|---:|---:|
| empty | 1,305 B | — | 0.01 | 0.2 | 1.3 | 0.3 |
| 1k rows | 32,850 | — | 1.1 | 5.8 | 12.4 | 4.1 |
| 5 functions | 3,417 | — | 0.6 | 0.3 | 1.0 | 0.4 |
| 1 MiB string | 3,165 | — | 8.0 | 8.0 | 17.0 | 5.8 |
| 200k random | 1,045 | 132,634 | 1.7 | 8.7 | 9.6 | 3.0 |
| combined | 38,654 | — | 8.2 | 13.6 | 27.2 | 9.2 |

"Overhead" is everything except the script itself: listing, dump, zlib + base64,
and append. Listing a 1 MiB string costs 8 ms because the full repr crosses the
pipe to be hashed. A digest computed inside the sandbox would remove that, but
Monty has no `hashlib` **[assumed; not checked]**.

### 3.3 Session-file growth

| Sequence of 20 scripts | First entry | 20th entry | File after 20 |
|---|---:|---:|---:|
| each sets one small int | 1,470 B | 5,602 B | — |
| each appends 100 rows to one table | 3,962 B | 34,575 B | 389,161 B |

Every entry is a full snapshot, so the file grows quadratically in the number of
scripts that touch a growing value. **A delta against the parent dump fixes
this:** over the same 20 states, full zlib snapshots total 270,933 B, full lzma
snapshots 114,932 B, and lzma deltas against each parent 14,368 B, which is 19×
smaller than zlib **[ran]**. Not built. Restoring a delta needs the chain back to
a full snapshot, so the design would add a keyframe every k entries.

For scale: `CODE-MODE.md` §1 measured 22.1M characters of message content across
100 sessions, about 221k per session **[read]**. A data-heavy 20-script session
under full snapshots therefore adds roughly 1.8× an average session **[inferred]**.

### 3.4 CPython: pickling the `exec` namespace

| State | dill bytes / zlib / dump ms | cloudpickle bytes / zlib / dump ms | load ms (cp) |
|---|---|---|---:|
| 1k rows | 44,435 / 9,254 / 37.1 | 44,437 / 9,255 / 0.72 | 0.46 |
| 5 functions | 1,504 / 773 / 13.3 | 1,613 / 864 / 0.22 | 0.04 |
| 1 MiB string | 1,048,603 / 1,070 / 0.40 | 1,048,603 / 1,071 / 0.62 | 0.18 |
| 200k random | 200,077 / 127,116 / 0.14 | 200,085 / 127,115 / 0.06 | 0.02 |
| combined | 1,094,983 / 12,674 / 144.1 | 1,094,885 / 12,738 / 1.5 | 0.79 |

These pickles are smaller than Monty dumps for row data (9 KB against 24 KB
zlib), because pickle stores values rather than a heap. cloudpickle is fast. dill
is 50–100× slower on any namespace that holds functions **[ran]**. That figure
pickles each name separately to find the failures and then the whole namespace,
so it overstates the per-name cost by about 2×; both libraries were measured the
same way.

What each refuses, in one namespace holding the listed values **[ran]**:

| Value | dill | cloudpickle |
|---|---|---|
| generator | fails | fails |
| socket | fails | fails |
| `threading.Lock` | pickles | fails |
| open read-mode file | pickles; **reopens the path on load** (`HANDLE_FMODE`) **[read]** | pickles a **snapshot of its contents** into a `StringIO` **[read]** |
| a class and an instance defined by the script | **fails, because `gen` is in the same globals** | pickles |
| a host tool (bound method of an object with a socket) | fails | fails |
| module, lambda, `StringIO` | pickles | pickles |

dill pickles a script's functions and classes by value together with their
globals, so one unpicklable name can make every function or class defined
alongside it fail too. And the host tool is the case that matters most. In CPython
the script's tool handles are host closures, and pickling one either fails or
drags host state into the tree.

### 3.5 CPython: replay with recorded tool results

| | |
|---|---|
| Replay 10 light scripts (rows, functions, `fib(15)` ×7) | 1.7 ms |
| Replay one `for i in range(2_000_000)` script | 185 ms, and it grows with compute, never with state |
| Recorded material for those 10 scripts | 783 B of JSON |
| Nondeterminism that reaches a tool call | detected: `ReplayDivergence: called fetch('0.86…'), recorded fetch('0.70…')` |
| Nondeterminism that does not reach a tool call | **silent**: `k = random.random()` replays to a different value |

Replay is what `CODE-MODE.md` §6.4 names as the admissible lifetime. It records
the least, and it carries across interpreter versions. It costs a full re-run per
cache miss, and it is correct only for deterministic scripts. That needs seeded
`random` and a recorded clock, which Monty's `OSPolicy` provides
(`random_start: {'seed': …}`, a frozen `datetime`) **[read]** and plain CPython
does not.

## 4. What breaks

**Monty**

- **A host function is a name, not a value.** `g = fetch` survives a dump and
  calls whatever `fetch` the restoring sandbox binds, or raises `NameError`
  **[ran]**. That is the right behaviour for tree-as-truth: the tool set belongs in
  `config`, and a restore under a different tool set fails loudly.
- **Host objects** (`ClassInstance`) are absent from a dump. Restored values
  become `MontyClassProxy` stand-ins whose methods fail **[read: `load_session`
  docstring]**. Not exercised, because these tools return plain data.
- **An un-awaited async tool call is a dead value.** Under `AsyncMonty`, an async
  host function gives the script a future, so `page = fetch(u)` without `await`
  stores `<coroutine external_future(0)>`, and that survives the dump. Awaiting it
  in a later feed raised `No pending async tasks but ResolveFutures requested` and
  **ended the checkout** (`this checkout has already been finished`). The same
  thing happens after a restore **[ran]**. τ's tools are async, so scripts must
  `await` them. A sandbox should refuse to write a state whose listing holds a
  `coroutine`, rather than persisting a time bomb **[inferred]**.
- **Memory limit.** In 1.1.0, a `MemoryError` at `max_memory` left the session
  readable **[ran]**. Monty's docs still say to discard a session after a limit trip
  **[read]**. The transactional design does that whether or not it is needed.
- **The suspension budget** (`max_suspensions`, default 1000 per checkout) resets
  when a dump is loaded **[read]**. A cached session that lives across many runs
  spends it; the restore path does not. After a trip the session stayed usable
  **[ran]**.
- **A dump loads only into a fresh session** (`RuntimeError` otherwise) **[ran]**.
  Every cache miss costs a checkout, 0.3–9 ms in §3.2.
- **Classes** with methods restore and work (`p.dbl() == 6`) **[ran]**.
  Inheritance was not tested.

**CPython.** See §3.4 and §3.5: generators, sockets and locks; host closures; dill
reopening files; cloudpickle freezing them; and replay's silent nondeterminism.
In-process `exec` is not a boundary in any case (`CODE-MODE.md` §6.2).

## 5. Does this satisfy tree-as-truth?

**The rule is about model input**, and this design adds none: `context()` carries
no part of an entry **[ran]**. What it changes is the *tool environment*. Before,
a persistent interpreter's heap was a function of every script ever run in the
process, which is the hidden channel §6.4 forbids and smolagents shows. With
these entries, the environment at an entry is a pure function of that entry's
path: one `sandbox_state` lookup, verified by hash. The three tests that would
expose a hidden channel (rollback, fork, a cold process) pass.

**Opaque bytes alone are enough for correctness, but not for inspection.** The
bytes guarantee that a restore reproduces the heap. They tell a reader, a head or
a summarizer nothing. Three things in the entry make it inspectable without
loading the bytes:

1. the **listing** (name, type, short repr, digest);
2. the **script**;
3. the **nested calls with their results**.

Items 2 and 3 also make the bytes *replaceable*. If a Monty upgrade breaks the
dump format, replaying the path's scripts against the recorded results rebuilds
the state. That turns an opaque, versioned blob into a cache of something the tree
can still regenerate, so the design meets §6.4 in its strict sense as well.

**What the model needs to see is the delta, on its own path.** The model needs
no bytes and no full listing each turn. It needs its beliefs about the sandbox to
come from its path. The tool result carries the printed output plus one line,
`[sandbox] +page:dict ~threshold:int -old`. That line moves with the path: roll
back and the result that announced a variable goes too. Beliefs and state diverge
only when context is cut while state is not:

- **Compaction** keeps the state (ancestry) and drops the results that described
  it. The summary needs the delta (shown in §2).
- **Branch summary without carry** drops both. The summary must say the changes
  are gone, otherwise the model writes `kept` and gets a `NameError`.
- **Branch summary with carry** brings the state back. The summary must say it is
  there.

τ's summarizer currently cannot see any of this. `ConversationTree.subtree_text`
skips `customEntry`, and in the test the summarizer received only `[assistant]:
loaded\n[user]: try a filter` **[ran]**. So the delta must be computed from the
entries and attached deterministically, as `summarize_branch_with_sandbox` does.
It cannot be left to the LLM. That makes it a reading that belongs in
`tau_agent_core`, so all three heads and compaction get it (CLAUDE.md, "three
heads").

**The filesystem is the remaining channel, and Monty makes it opt-in.** With no
mount, `open()` raises `PermissionError` **[ran]**. An `overlay` mount discards its
writes when the feed ends **[read]**, so it adds nothing outside the tree. A
read-write mount, or an `os=` handler, writes host state that no entry records.
Tree-as-truth then holds only if the mount set is part of `config` and treated as
an explicit exception, as `bash` already is. In CPython the filesystem is always
reachable, and dill's file pickling turns it into a live channel at load time.

**Nor does rollback rewind the world.** It rewinds the heap. A `fetch` or `write`
a script made is recorded but not undone, so a delta or summary should name the
nested calls as well as the variables (`sandbox_delta_text` does).

## 6. What the core would need (not built)

1. A `sandbox_state` reading in `tau_agent_core`: `state_entry_at`, `diff_listings`
   and `sandbox_delta_text`, as pure functions on entries.
2. `subtree_text`, and the compaction summarizer's input, made aware of
   `sandbox_state`: render the delta rather than skipping the entry.
3. The sandbox's tool set and mounts recorded as `config` keys, so a restore runs
   under the declared set or fails.
4. Parallel `python` calls serialized per cursor. The PoC marks the tool
   `sequential` and locks the sandbox, because two runs from one state would
   otherwise chain as if one followed the other.
5. Storage: inline at 64 KiB compressed or less, side files above that, then
   parent-relative lzma deltas with periodic keyframes (§3.3). The JMFTS store's
   tolerance for 30–40 KB `customEntry` lines is unmeasured **[assumed fine;
   `customEntry` projects to `""` there per CODE-MODE.md §4]**.
6. A multi-slot cache keyed by state entry id. A single slot thrashes when cursors
   alternate (§3.2, miss column).

## 7. Absent

- `feed_start` snapshots of a script **paused** mid-call (a permission prompt
  resumed from the tree). The same entry format would hold one; not tried.
- An LLM writing the scripts. Every script here was hand-written.
- The JMFTS store. Only the JSONL `Session` was exercised.
