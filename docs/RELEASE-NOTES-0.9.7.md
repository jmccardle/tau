# v0.9.7 — the tree becomes an editor, and the transcript stops growing

Written from the commits between `v0.9.6-fullhistory` (`025aed1`) and master, so
the release commit, the GitHub release body and the site all say the same thing.

**Ten commits**, of which two are the version bump and these notes. Two of the
rest are the kind this project keeps finding: a thing that looked like it
worked, did not, and said nothing.

**The system prompt was being folded away.** Every compaction and every elide
dropped it out of the context — and nothing looked wrong, because the agent loop
re-inserts the *config's* prompt whenever the context does not start with one.
So the model kept getting a prompt. Not the tree's.

**A live transcript grew its widget tree without bound.** A chat trimmed when it
was opened climbed straight back past the cap as the reader worked in it, and
every turn made the next one slower: 200 deltas that a perfect renderer finishes
in 5.0s took 18.9s behind a 60-turn backlog.

The rest is new capability. The tree browser can now build a path that keeps one
message and skips the next, which is the thing an elide could never do. The RPC
wire can attach a file. And the release gate itself stopped lying.

---

## The tree

### A splice carries the system prompt instead of folding it away

Reproduced on an in-memory session with a prompt and eight messages:

```
before:                     [system, u0, a0, u1, a1, u2, a2, u3, a3]
after elide(first_kept=u2):                        [u2, a2, u3, a3]
```

Not elide-specific. `compaction` and `elide` share one fold, and that fold drops
everything on the path before `firstKeptId` — which in τ includes the system
prompt, because `Session._init_state` writes it as the first `message` entry. pi
keeps its prompt in the request frame, so pi's splice never sees one; this is the
second-order cost of a deliberate divergence, not a port fault.

**Nothing appeared to be broken, and that is the actual defect.**
`AgentLoop._call_llm` re-inserts `config.system_prompt` at index 0 whenever the
context does not already start with a system message. For a session running under
its creation-time config the two strings match and nothing shows. For a *resumed*
session, or one whose config moved, the fold and the wire disagreed and neither
said so — the tree-as-truth invariant failing silently, which is worse than a
missing prompt.

`_active_path_entries` now carries system messages across the splice and emits
them first. The span counts follow: a row saying "folds 3 entries" no longer
counts one the reader can still see, and elide's "would hide nothing" refusal
means what it says. `docs/SYSTEM-PROMPT-IN-THE-FOLD.md` has the diagnosis.

### Branch from marked messages, and copy/paste a subtree

`ctrl+B` branches, `c` copies, `v` pastes. The browser could already navigate,
revise and elide; it could not keep `m1` and skip `m2..m3`, because an elide only
ever removes a **prefix**. A prefix and a suffix with a gap between them can only
be had by minting new entries.

Which is why there is a *plan* and not a sequence of edits. Minting one entry per
gesture either re-parents existing nodes — breaking the invariant that an entry's
ancestor chain is fixed at append, which is what makes "what did this message
see" answerable forever — or litters a fresh copy of the tail per keystroke.
`tau_agent_core/tree_surgery.py` is the pure algebra: it takes a
`ConversationTree` and returns a plan or the reason there is not one, holds no
`SessionLog`, and writes nothing. `TauBackend.commit_branch` / `paste_subtree`
perform it.

A mark takes its tool group with it, at *selection* time rather than by refusing
a half-selection at the commit: an assistant message and the results answering
its calls are one unit to every provider.

`copiedFrom` is provenance, not structure. It deliberately did **not** join
`_CROSS_REF_FIELDS`, whose rule is raise-on-unresolvable: a copy's source is
normally outside the subtree being copied — that is what makes it a copy rather
than a move — so putting it there would have made `JmftsSessionLog.fork` refuse
to fork any tree anyone had ever pasted into.

43 new tests. `docs/TREE-BROWSER-AS-EDITOR.md` §6 is the design;
`docs/TREE-EDITOR-MANUAL.md` §9a/§9b are the gestures.

## The TUI

### The transcript is a moving window, not a growing one

Measured on this tree, Textual 8.2.7, feeding 200 deltas at 40/s:

| mounted turns | widgets | wall clock |
|---|---|---|
| 0 | 1 | 5.2 s |
| 20 | 1321 | 7.7 s |
| 40 | 2641 | 11.7 s |
| 60 | 3961 | 18.9 s |

**The cost is not τ's own code.** Every `MarkdownBlock.update` reaches
`_compositor.full_map`, which arranges the entire screen tree with
`visible_only=False`; a delta invalidates its cache, so the next block update
rebuilds it over every widget in the transcript — 8.5s of a 17.6s profile inside
`add_widget`.

Which is why the window **evicts** rather than hides. Per delta at a 60-exchange
backlog: `display: none` 417 → 301 ms, dropping collapsed interiors 417 → 286 ms,
flattening each finished `Markdown` to one `Static` 436 → 225 ms, coalescing
writes to 10 Hz 18.9 → 15.5 s. Evicting whole turns: **417 → 58 ms**, which is
the empty-transcript cost. Only removal is what `full_map` counts.

And the window **moves**: scrolling against either edge slides it one turn, so
reading history costs nothing. A moving window is free; only a growing one is
not.

Two ownership bugs surfaced from a report that "the window only ever lands at the
top or bottom" — a reload that landed at the top of any real transcript
(`scroll_to_tail` against a `virtual_size` the batch had not recomputed), and
every `Collapsible` toggle stealing the view, because Textual's
`_watch_collapsed` ends with `call_after_refresh(scroll_visible)` while this
transcript folds boxes on its own account. `docs/TRANSCRIPT-WINDOW.md` has the
measurements and the five things that did *not* work.

## The RPC wire

### Protocol 1.4 — `complete_path`, and `expand_attachments`

`@notes.txt` sent over the wire reached the model as those eleven literal
characters. Attachment expansion is a **frontend** job in τ
(`docs/FILE-ATTACHMENTS.md` §2: the core decides, the frontend performs) and this
wire had no frontend, so a head had to choose between re-implementing the
`<attachment>` / `<reference>` vocabulary in its own language and shipping an
editor whose `@` did nothing.

Two additive changes, both thin wrappers over pure functions that already
existed:

* **`complete_path`** wraps `attachments.complete_attachment`. A host cannot
  compute this itself: a browser has no filesystem, and one that does — a VS Code
  extension — would be listing the wrong machine under Remote SSH. It answers
  from `Path.cwd()`, the same directory the agent's tools resolve against, so the
  listing, the expansion and the tools cannot disagree about which file
  `@notes.txt` names.
* **`expand_attachments`** on `submit`/`prompt`, mirroring `expand_commands`,
  with a matching optional `attachments` key on the result:
  `{expanded, images, unresolved, failures}`. It rides the **acceptance**,
  because expansion happens before admission — a host told at `agent_end` that a
  file failed would learn it too late to say so beside the message it typed.

The key is **absent** when the flag was not set: "expansion did not run" is a
different statement from "expansion found nothing".

MINOR rather than PATCH: a host that does not set the flag gets byte-identical
1.3 behaviour, but `protocol_version` is how a head decides whether `@` works at
all, instead of discovering it by watching a model fail to see a file it was told
about.

**Scope, stated rather than hidden.** An absolute or `../` token lists outside
the working directory, as it does in the TUI. Not a new hole — the same
connection can run `bash` through the agent — but a host serving this to a
browser is publishing a directory lister to whoever holds the token.

## The release gate

### Four tests failed in every release container, against correct code

`docs/RELEASING.md` §2 listed four failures as harness artifacts and told the
releaser to read `4 failed` as clean. That was wrong twice: it asked a human to
match four names by eye, which is the check that quietly stops being done, and in
both cases the *test* was making a claim its own probe could not answer.

* **`os.kill(pid, 0)` succeeds on a zombie.** An orphan is reparented to PID 1 and
  only PID 1 can reap it. On a normal host that is an init which reaps within
  milliseconds, so the flaw is invisible; in a container PID 1 is the test runner
  itself, nothing reaps the orphan, and a `sleep 300` that *was* killed reads as
  a survivor forever. The two `TestBashToolProcessGroupKill` tests read the
  process state now and treat `Z` as dead — which is also the substantively right
  answer, since a zombie has released its file descriptors and cannot hold open
  the pipe that class exists to protect.
* **Two tests asked `git ls-files`** and raised in an export with no `.git`. They
  walk the shipped trees now, excluding `__pycache__` and `*.egg-info`. Walking
  is also the stricter question: a host address is just as baked in while the
  file is still untracked, which is the state every one of those files passes
  through.

Both walks gained a companion that asserts the scan actually reached the trees,
because an enumeration that silently returns nothing passes a
scan-for-offenders test with the same green dot as a real pass. A clean matrix is
**zero failures** now, on all four Pythons.

### And the gate could not name what it caught

Applying the new standard immediately found the next hole in it. One 3.14 run of
this release reported `1 failed, 5268 passed` — and the test's identity was
gone, because the matrix command this document had carried since 0.9.2 ended in
`python -m pytest -q | tail -3`. That prints the count and discards the name.

A gate that says "something broke" but not "what broke" leaves the releaser one
move: run it again and hope. Which is how an intermittent failure becomes a
pass. The command now passes `-rf --tb=line`, keeps 40 lines, and builds its
tarball with `git archive master` — the tree step 4 actually publishes, rather
than a `tar --exclude=…` of the working directory that swept up every untracked
tree in the checkout (measured once at 370 MB against 11 MB of tracked source).

The failure itself is **unresolved and recorded as such**, in
`docs/RELEASING.md` §2. Four later 3.14 runs on the byte-identical tarball were
clean; no ordering plugin is installed, so collection order is identical in all
five; and the failing run was not the one that overlapped a second matrix on the
same host. That leaves the wall clock, and 31 test files sleep against it. The
instruction written down for next time is to name it and fix the test — not to
add a retry, and not to read a re-run's pass as a refutation.

## Upgrade notes

**If you drive τ over `--mode rpc`.** `get_capabilities` reports
`protocol_version: "1.4"` and lists `complete_path`. Both additions are opt-in;
an existing 1.3 client that sets neither flag is unaffected.

**If your head sends `expand_attachments`, it requires this release.** τ
validates params strictly and rejects an unknown one, so a 1.4 client talking to
a 0.9.6 τ gets `Invalid params for 'submit': unexpected param(s):
expand_attachments` (-32602) on **every message**. Read `protocol_version` from
`get_capabilities`, or require `ffwf-tau >= 0.9.7`.

**If you resume sessions that have been compacted or elided.** The system prompt
now comes from the tree rather than from your config. If those two ever differed
— a config edited after the session started — the model's instructions change
with this release. That is the fix, not a regression.

**If you implement `SessionLog`.** The contract suite gains a pass-through case
for `copiedFrom`: the field has to survive a round trip. It is provenance, so
remap it when you can resolve it and leave it alone when you cannot; nothing
folds on it.

**If you drive the TUI.** Three new keys in the tree browser (`ctrl+B`, `c`,
`v`), and the transcript holds four turns on screen instead of all of them.
Scrolling against either edge slides the window.

## Known open

Unchanged from 0.9.6:

* `repeat_tool_call_limit` is reachable from `AgentLoop` only.
* Persistence still blocks the UI thread — `docs/BLOCKING-PERSISTENCE.md`.
* An image has no byte budget, only a 2000px dimension cap.
* `/tree extra words` runs and discards the arguments —
  `docs/SLASH-COMMANDS.md` §4.

New with the tree editor, and stated in its own doc: the branch plan is
**derived** from the marks and cannot be edited, so a message that *could* be
kept cannot be forced into a copy. There is no archive gesture and no fold
header.

## Verification

Python 3.11, 3.12, 3.13 and 3.14 in clean `python:<v>-bookworm` containers before
the tag, per `docs/RELEASING.md` §2. Zero failures on all four — a standard this
release is the first that could meet — with the one exception recorded above and
in §2: an unnamed 3.14 failure on the first run, and `5269 passed, 0 failed` on
the four 3.14 runs after it.

`mypy` clean across all five `src` trees in one invocation. `ruff check` and
`ruff format --check` clean.

Ten distribution artifacts, all `twine check --strict` PASSED, and three smoke
installs from local wheels: `[tui,jmfts]` starts and carries both runtime data
files; the `ffwf-tau` metapackage resolves `textual` in and `tau_jmfts` out; the
no-extras install refuses the TUI with a named error that names a real project.
