# As-built review of the driver, providers and tools, Phase 4

Master at `58260e0`, 2026-10-30. This page is the summary John signs off from.
Detail:

- [metrics/](metrics/README.md): size, complexity, coverage, imports, ratchet
- [traces/](traces/README.md): one turn timed across Rust and Python ([summary.md](traces/summary.md))
- D15's "Review 2026-10-30" and "Live checks 2026-10-30": the providers, reviewed and
  run against every hosted API the same day (not re-reviewed here)

Four independent reviewers, one per layer: the driver and events; config, trust and the
`tau` command; tools; the Python-Rust boundary. Every bug below was reproduced by a
script, and the ones marked † were re-run by the main session before this page was
written. `scripts/check.sh` is green (736 passed, 4 skipped).

## 1. Verdict

**The Python half is in shape to build on, after one fix wave.** 7,184 lines, 455
functions, mean CC 3.7, 94% line-and-branch coverage, no import cycles; providers do not
import the driver and the driver imports no provider. The design holds: no finding calls
for a change to model.md. **What blocks sign-off:**

1. **Eleven bugs** (§3), each with a script that shows it. The worst: a cloned project can
   change every request (`max_tokens`, `extra_body`) and the trust report says it changes
   nothing; `tau trust` can print one config and pin another; a `bash` command outlives a
   killed τ with no timeout; three driver lifecycle edges hang or hide a failure.
2. **The ratchet does not measure Python.** Rule 7 says "max function CC ≤ 15, no file
   over 800 lines" with no language named; `scripts/ratchet.py` reads only `crates/`.
   Python has two functions over 15 (`_config_layers._merge_models` 21,
   `EventStream.offer` 16) and `Driver` has 42 methods.
3. **Rulings** (§5). Most have a recommended default; eight need John.

## 2. Numbers

| | `python/tau_next` | `crates/*/src` (3d) | τ 0.15 `src` |
|---|---:|---:|---:|
| lines | 7,184 | ~7,100 | ~99,000 |
| functions over CC 10 | 25 (5.5%) | 5.4% | 6.7% |
| functions over CC 15 | 2 | 0 (after wave 3) | |
| line + branch coverage | 94.0% | 97-98% (core) | 90.3% |
| largest file | 713 (`_config_layers.py`) | | |

Under 90% coverage: `handles.py` 88.7%, `providers/gemini.py` 88.9%. The traced in-memory
turn costs ~1.2 ms untraced; core is 1.7% of it, the bindings ~30%. Stored turns are
fsync-bound (8 fsyncs, 55%), as in 3d.

## 3. Bugs (fix wave)

| # | layer | what a user sees | fix |
|---|---|---|---|
| 1 † | trust | A project sets `max_tokens: 64000` and `options.extra_body: {metadata: …}`; `tau trust` reports "(nothing: it sets no model that differs from your config)" and trusts it. D13: approval is consent to everything the project sets. | Show the redacted project config itself; delete `_override_lines`, `_shown`, `_FIELDS` (~40 lines). |
| 2 | trust | `tau trust` reads the config three times; a file flipping during the run was pinned "evil" while "nothing" was printed (45 of 400 runs). | One read: `_trust` goes through `ensure_trusted(ask=…)`; delete `approve_project`, `trust_report`, `is_trusted`. |
| 3 | trust | A `trusted.json` that is a directory or mode 000 gives a traceback, not one line. | Catch `OSError`. |
| 4 | trust | "Trust it with: tau trust /tmp/my proj" breaks when pasted. | `shlex.quote`. |
| 5 | tools | τ killed by SIGTERM/SIGHUP (terminal closed): a running `bash` command lives on and its timeout never fires (`sleep` alive after Python exit 143). SIGINT is fine. | SIGTERM/SIGHUP cancel the main task like SIGINT; one line in the `base_tools` docs for library users. |
| 6 | tools | (plausible: needs a root-owned child) one unkillable process stops the kill sweep and the model gets a bare EPERM instead of the output. | Signal every member, keep the first error, append it to the output. |
| 7 † | driver | A hook that wraps `cursor.ask` in `asyncio.timeout` (model.md Q9) leaves the question open for good; `respond` and a later `abort`/`go_to` raise `InvalidStateError`. | `try/finally` in `ask` removes the question. |
| 8 † | driver | After a driver failure, the natural `await driver.stop()` makes every open event stream end cleanly: the frontend never learns the driver died. | `end()` on an ended stream does nothing. |
| 9 | driver | `rest(cursor)` waits forever if `stop()` runs meanwhile. | After `stop()`, `rest` raises. |
| 10 † | driver | `remove_item`, `remove_label` and a label move announce the hook's own cursor, not the one that changed; a subscriber to the other cursor hears nothing. | Emit for the affected cursor. |
| 11 | boundary | `trace.stop()` while another thread opens a span freezes the process (hung on iteration 2 of 200). | Release the tracer lock before waiting. |
| 12 † | boundary | `repr(cursor_state)` prints the whole live session (every node's text, the store path), including nodes added after the snapshot. | A `repr` without the session handle. |
| 13 | boundary | `Stop.Tools == 1` is true but `Stop.Tools in {1}` is false (equal values, unequal hashes); also `Next`, `Hook`. Nothing relies on int comparison. | Delete `eq_int`. |

Risks worth fixing in the same wave (one line or a deletion each):

- **Hook exceptions vanish** (†). A `pre_tool` with a typo makes every call
  `Err("hook error")` with no type and no traceback anywhere; a cursor hook's `KeyError('x')`
  becomes the message `'x'`. Providers already print `Type: msg` and the traceback to
  stderr; hooks should do the same.
- **`CallDraft.tool` and `.id` accept edits that are ignored** (†): an alias extension's
  `call.tool = "real"` still runs the old name. Make them read-only.
- **A driver failure leaves sibling samples and tool calls running** (and billing) until
  `stop()`. `_fail` cancels them.
- **`read` writes an overflow copy of a file already on disk**, and nothing ever cleans
  `/tmp/tau-<uid>` (up to 64 MiB per `bash`/`grep` call; RAM on tmpfs). `read` stops at the
  limit and says the offset to continue from.
- **Another local user can stop τ from starting** by creating `/tmp/tau-<uid>` first. See
  ruling T2.
- **OS errors from the store lose `errno` and the path** (`FileNotFoundError('No such file
  or directory (os error 2)')`, `filename=None`).
- **A record over 4 GiB panics** (plausible; read, not run) instead of a typed error.

## 4. Subtraction

The reviewers found about as much to delete as to fix. The main items:

- **trust:** with content pinning deciding, the owner checks on paths `find_project` already
  filtered, the `lstat` pre-checks before `O_NOFOLLOW` + `fstat`, the second
  `_refuse_special(root)`, and the second upward walk (`skipped_project`, added today) can
  go; one walk returns the root or why it skipped one.
- **config:** "objects merge by key, null drops" is written three times in
  `_config_layers.py`, and the resolver repeats `ModelConfig`'s own checks. Folding them is
  the way to bring `_merge_models` (CC 21) under the limit.
- **driver:** `Outstanding.units` and the `Outstanding` export, `_DriverBreak` (guards an
  `offer` documented never to raise), a redundant clause in `_deliver`, `takes_progress`
  computed twice, `Saved` emitted for frontend calls that changed nothing.
- **boundary:** ~13 flat read methods on handles re-implement `CursorState` one field at a
  time; `Config()`'s empty default; `Cancel.cancelled`, never read.
- **tools:** defaults written in three places (Rust, PyO3 signatures, Python); the PyO3 ones
  are never used.
- **glossary (rule 2):** `CursorHook`, `ToolHook`, `CallKey`, `ProgressTool`, `check_meta`
  and four `Driver` reads have no entry; the glossary names a `tau_next.Hook` that is not
  exported.

## 5. Rulings

### For John

| # | question | recommendation |
|---|---|---|
| R1 | **What does a broken extension or tool cost?** Today a tool returning a non-`Outcome` or a hook returning the wrong type kills the whole driver; a provider bug costs one sample. | The same as providers: it costs one call (`Err("Type: msg")`) or one step (`HookFailed`), traceback on stderr. Still fail-early: the error is saved and shown. |
| R2 | **Does an outcome set in `pre_tool` skip later `pre_tool` hooks?** Today yes, so a cache extension listed first hides calls from a permissions extension listed after it. `pre_sample` runs every hook. | Run every `pre_tool` hook (one rule; permissions can't be bypassed by list order). |
| R3 | **Should trust pin `.env`?** Today a `git pull` that changes `.env` goes through silently. Low impact while `.env` only holds keys; high once it feeds tool environments. | Pin its bytes in the same digest (one line). |
| R4 | **How does a project start?** Nothing creates `.tau/`; D13 promises "a `.tau/` tau creates is trusted". | `tau trust DIR` creates `.tau/` (700, `.gitignore`) when none is found below `$HOME`. |
| R5 | **Background jobs in `bash`.** `sleep 30 & echo started` answers `started` with the sleep already dead (no child outlives a call). A model can't start a dev server. | Keep it and say so in the tool's description now; a background-process tool later, as an extension. |
| R6 | **Is the working directory a boundary?** `read`/`write`/`grep` take absolute paths and `..`; `write` follows symlinks anywhere. | A default, not a sandbox: say so in the descriptions; containment is the Phase 5 permissions extension's. |
| R7 | **Who is the driver contract for?** `Session.next_job`/`answer` are public but `Job`, `Answer`, `SampleEnd`, `RunCall`, `Hook` are not exported. | Export them: other drivers become possible and `WrongAnswer` becomes testable through the public API. |
| R8 | **Rule 7 for Python.** | Extend `ratchet.py` to `python/` now; fix `_merge_models` and `EventStream.offer` in the wave rather than allowlisting; `Driver` (42 methods) splits or gets a one-line `[justified]`. |

### Defaults (taken unless John objects)

- **trust:** the report shows the redacted project file (fixes bug 1 by construction);
  `tau trust` keeps approving without a y/N (typing it is the consent, the refusal already
  showed the report); D13 is amended so content pinning decides (refuse symlinks and FIFOs,
  no owner check on `config.json`); `tau trust --forget` waits for Phase 6. Before Phase 5
  adds files under `.tau/`: decide whether the hash covers the directory or a file list.
- **tools:** overflow files under `$XDG_RUNTIME_DIR/tau` (private per user, cleared at
  logout), falling back to `$TMPDIR/tau-<euid>`; files older than a day removed when the
  directory is opened; on non-Linux, `base_tools()` raises at construction instead of every
  `bash` call failing; a test pins "the driver never writes `os.environ`" (so `.env` keys
  never reach `bash`).
- **driver:** a complete sample answered after an abort stays a complete reply (D15's
  "an abort always saves Cancelled" is corrected); extensions are checked at construction
  (non-empty unique `name`, no misspelt `pre_*`/`post_*`); `Driver.add_item` goes (tests add
  items through a test extension); the harness-only reads (`units`, `queued`,
  `open_questions`, `check`, `subscribers`) become one `Driver.state()` snapshot;
  `Driver.open(at, client)` like `input` and `go_to`.
- **boundary:** a panic stays a `PanicException` on the first call and `Poisoned` after (a
  crash looks like one; the glossary says so); `hook=` moves off the public `Session`
  methods; handles expose `.tree` and `.cursor(c)` instead of flat reads; `_core.pyi` stays
  hand-written with stubtest; a node over 4 GiB is refused with a typed error.
- **Still open, needed before Phase 5:** how `config_at` names extensions (decides whether
  hook order can differ per branch).

## 6. Exit criteria (plan.md, Phase 4)

| criterion | state |
|---|---|
| behavior suite green except later-phase tests | **met**: 736 passed; the coverage list is ticked, with one item for Phase 5 (approval in `pre_tool`) and one for Phase 6 (client detach) |
| as-built review on `python/` | **this page** |
| a traced turn across both languages | **met**: `traces/summary.md` |
| sign-off | after the fix wave and R1-R8 |
