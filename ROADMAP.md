# τ Roadmap

Living schedule of open work. Each item cites the evidence (file:line, doc, or
test) it came from so it can be audited against the source of truth (pi) and the
"Fail Early" rule.

**State (2026-08-28):** this file was last edited at `1968e6a` (2026-08-21) and
had drifted 51 commits behind master. Two releases landed in that window: 0.9.3
(`1107d74`, 2026-08-22) and 0.9.4 (`17d0ac0` + tag `v0.9.4`, 2026-08-24).
Re-audited against code, not commit messages. **Now shipped and moved below:**
the whole 0.9.4 cycle (`docs/PLAN-0.9.4.md` §1–§7, every section marked
"Built"), the multi-vendor clients (`anthropic.py`, `google.py`), Session UX
Phases B and C (the picker modal and `--resume` in the TUI — the "Open work"
list below still called both open while this file's own 08-21 header called
them shipped), Tier 10's themes leg, and the agent-facing docs mechanism.
**Still open, confirmed 2026-08-28:** Tier 8's trust gate, Tier 9 (`--export`
HTML, pi-faithful `--mode json`), Tier 10's templates and skills legs, Tier 11
M4/M5, two flags (`--list-models`, `--session-id`), and the `docs/PLAN-0.9.4.md`
§8 debt list. Suite: 4951 passed, 144 skipped, 0 failed. Docs coverage:
316/758 (41.7%), 0 drift.

**Release state (2026-08-28).** `v0.9.4` is public: the tag resolves to
`c5fff1f` on the `github` remote, matching the local tag. **`origin`
(`dev.ffwf.net`) carries no tags at all** — `git ls-remote --tags origin`
returns empty and exits 0, so the release tags live on `github` and in the local
clone only. That is consistent with `docs/RELEASING.md`'s two-repository split
but is worth stating, because an empty tag listing reads like an unreachable
remote and is not one. `origin/master` is at `947918b`, which is behind local
master. I did not check PyPI.

**Three commits sit past that release with no release notes and no plan doc.**
`docs/RELEASE-NOTES-0.9.4.md` was extended once after the tag, by `cf3920a`, so
it does cover the theme crash. The three commits after that are not in it:

- `407befc` — a temperature nobody chose made every `anthropic-messages` call
  fail.
- `dceb44a` — the agent-facing reference docs (see "Shipped" below).
- `947918b` — the Google client read pi's field names off a τ message.

Two of the three are user-visible provider fixes, and both vendor clients
shipped in the release those fixes are missing from. There is no
`docs/PLAN-0.9.5.md`: `docs/PLAN-0.9.4.md` §9 is fully built and its §8 says
explicitly that nothing in it is scheduled. So the next cycle currently has
three commits and no scope. The "Suggested order" section at the end of this
file is the closest thing to one, and its top two items both come out of §8.

**State (2026-08-21):** the 0.9.3 cycle closed most of what the 2026-08-09 audit
left open. Now shipped: context files (Tier 8's loader half), Session UX Phases
B and C, the non-streaming transport, multi-vendor dispatch, backend hardening,
and branch-lane removal — see `docs/PLAN-0.9.3.md`, whose §7 sequencing list is
fully built. **Genuinely still open, confirmed against code on 2026-08-21:**
Tier 8's trust gate, Tier 9 (`--export` HTML, pi-faithful `--mode json`), Tier
10 (themes/templates/skills — untouched), Tier 11's M4/M5 (deliberately
deferred), two flags (`--list-models`, `--session-id`), and from 0.9.3 §4:
retry/backoff, repeat-tool-call detection, and non-OpenAI clients. The "Doc
hygiene" section below is now fully closed.

**State (2026-08-09):** this file was last edited 2026-07-18 and had drifted
152 commits behind master. A full re-audit (5 parallel evidence passes: RPC,
submission-lifecycle, CLI flags, extensions, session UX) found five shipped
arcs this file never mentioned or still called unbuilt — folded into "Shipped"
below with commit-hash evidence.

**State (2026-07-14, W0–W15):** the constrained-decoding + JMFTS-backing-store
workstream is tracked under four overlapping naming vocabularies (W-series
schedule, G-series constrained-gen targets, JMFTS Phases 1–6 + C1/C2, and the
llama.cpp fork Phases A–D in `turboquant_experiments`), reconciled in
**`docs/WORKSTREAM-CROSSWALK.md`** (current as of 2026-07-16, unaffected by
this audit — trust it for that vocabulary). G6 (jump-forward decoding) is
BUILT and GPU-verified; nothing is committed on the llama.cpp fork branch
(`jump-forward`, per the no-AI-PR rule) — G7 builds on it and remains open.

---

## Shipped (compressed)

### Former Tiers 1–4 (unchanged from the 06-22 pass)

- **API key (Tier 1):** no fabricated `sk-fake-…` default; key threaded
  end-to-end, raises `No API key for provider: …` when absent.
- **Loop/prompt quality (Tier 2/3):** restored pi-parity prompt threading;
  removed the fragile loop-level dedup. Tool-call join/parse collapsed to two
  intentionally-divergent sites (WONTFIX).
- **Thinking (Tier 3 #4):** full `reasoning_effort` send-path. *Caveat:*
  silent no-op on the local llama.cpp rig — real local toggle is
  `chat_template_kwargs.enable_thinking`.
- **Headless session continuation (Tier 3 #5):** superseded by the session
  sprint (below).
- **Docs/cleanup (Tier 4):** `COMMAND_LINE.md` corrected (11 fixes).

### Quality gate (Tier 5) — shipped 2026-06-22

`.githooks/pre-commit` (ruff check + ruff format --check + mypy), hard-gating
commits. ruff 31→0, mypy 55→0, no blanket `# type: ignore`. LLM-backed
compaction is a faithful port of pi's `compaction.ts` (no fabricated-summary
fallback — Fail-Early). `SessionManager.summarize_branch` raises rather than
falling back to truncated text on an LLM error.

### Session UX sprint — Phase A (storage layer) — shipped 2026-06-23

Append-only JSONL `Session` store, cwd partitioning, fork, `SessionInfo`
reader, all four Phase-A seams (`session_store.py`). `headless.py`/`app.py`
migrated off the old `Chat` store. **Phase B (picker modal) and Phase C
(command unification + sidebar-closed default) are still open** — see "Open
work" below; do not assume they shipped alongside Phase A.

### Streaming UX — live reasoning + cancellable generation — shipped 2026-06-26

HTTP body streamed instead of buffered (root-caused "no reasoning until
complete"); one stream class (net −184 lines); `AbortSignal` threaded into
the provider for mid-completion cancel; TUI generation runs as a
`@work(exclusive=True)` worker, `Esc` cancels cooperatively.

### Extensions epic (Tier 11) — M0–M3 done, plus an entire undocumented E6–E11 arc — shipped through 2026-07-05

`feat/extensions-e0-e4` (M0–M2, S1–S23) and E5 (M3's session-lifecycle +
Textual UI half, S24–S37) are on master, matching the prior write-up. **New
finding:** a further arc, E6–E11 (S38–S75, `docs/EXTENSIONS-DEMO-ROADMAP.md`),
was *also* fully merged — 13 days before this file's last edit — and this
file never mentioned it. It ships: session-lifecycle hooks
(`registry.py:200` `LIFECYCLE_EVENTS = ("session_start", "session_shutdown")`),
`turn_end`/`input` mutating hooks, per-extension config, `ctx.get_model`/
`set_model`, TUI panels/forms/status slots, runtime enable/disable/reload,
13 base demo extensions (E9) + 5 advanced composed demos (E11: review swarm,
delegate fleet, red-team memory, router ledger, consequence engine). The old
two-loader contradiction is resolved (`extensions/__init__.py:9-10`: old
`ExtensionLoader` "removed in E0/S1").

Still open, **by written decision, not stalled work**: M4 (`registerProvider`
— "provider work is tau-llm's, not an extension's,"
`docs/EXTENSIONS-DEMO-ROADMAP.md:292`) and M5 (package manager). Zero
`registerProvider` symbol or package-manager CLI surface exists anywhere.

The remote ref `origin/feat/extensions-e6` is a **stale, fully-merged
pointer** (0 commits ahead of master, 206 behind, merge-base = its own tip
`e6f9543`) — safe to delete from `origin`.

### RPC / remote control (Tier 12) — shipped 2026-08-01 → 08-07

Reclassify: this tier was "deferred, narrow audience" as of 07-18; it is now
built, reviewed, and tested. `--mode rpc` is wired
(`cli.py:557` → `tau_coding_agent.rpc_mode.run_rpc`); `rpc.py` was split into
a package (`tau-agent-core/src/tau_agent_core/rpc/`: commands, handler,
transport, dialect, capabilities, wire_events — commit `0ea6d2d`, 2026-08-05).
20 command verbs implemented, 7 formally declined with a stated rationale
(`commands.py:3642-3727`) — 27 of pi's 28 accounted for. `docs/RPC-PROTOCOL.md`
is machine-generated from `COMMAND_TABLE` (`scripts/generate_rpc_protocol_doc.py`)
and drift-tested (`test_rpc_protocol_doc.py`). Tier B
(`docs/RPC-TIER-B.md`) adds 6 more verbs (`set_model`, `compact`,
`get_session_stats`, `set_auto_compaction`, `set_session_name`,
`get_last_assistant_text`), each with its own test file. 463 RPC-scoped tests
pass, including 28 true-subprocess conformance tests
(`test_rpc_conformance.py`, real stdin/stdout pipes, no in-process mocking).
Documented, deliberately-open gaps: no reverse channel (extension → host UI),
no socket/TCP transport (stdio-only by design), no notification-payload
schema slot in the capability doc yet.

### Submission lifecycle — one door for every input source — shipped 2026-07-31 → 08-05

`AgentSession.submit()` (`agent_session.py:1754`) is now the one door: TUI
(`app.py:2438` `on_input_submitted`), headless (`headless.py:583` `run_print`,
docstring: "Both modes reach the model through `AgentSession.submit`"), and
SDK (`AgentSession.prompt()`) all funnel through it. `multitask_strategy`
covers `reject`/`enqueue`/`steer`/`rollback`/`fork`. Provenance
(`submission_id`/`source`/`submitter`/`correlation`) on every event
(`5eff135`). `submit_threadsafe` exists (`agent_session.py:1431`) for
cross-loop callers.

### Node-addressable agents — `agent_spec` provenance + rollback — shipped 2026-07-30 → 08-01

`agent_spec` is a real event type, written by `_record_agent_spec()`
(`agent_session.py:558`) as a `customEntry`/`customType: "agent_spec"`, with
a reload-invariance contract test
(`session_log_contract.py:634`). Rollback (`multitask_strategy="rollback"`)
navigates back to `_pre_turn_leaf`; bound to `ctrl+z` in the TUI
(`app.py:2160`, `RollbackPromptModal` at `app.py:612`). Fork reuses
`ctx.spawn_branch`/`BranchView`.

### Tectum NATS bus extension (tau-006/tau-007) — shipped 2026-07-27 → 08-01 — documented 2026-08-10

`tau-006` (`991d880`): extension-boundary declaration —
`ExtensionInfo.content_hash` (sha256) + `TOUCHES_BUS`/`SUBJECTS` module
attributes checked before `register()` runs; `bus_available=False` by default,
raises `ExtensionCapabilityError` on an unauthorized bus-touching extension.
`tau-007` (`4151ca9` + 2 follow-ups): a real NATS extension,
`tau_agent_core/extensions_builtin/nats_bus.py` (843 lines), test suite
1331 lines, real `nats-py>=2.15.0` dependency. Iterated twice against a live
sibling project (`~/Development/tectum`) — once to fix 5 wire-format
mismatches against tectum's actual protocol, once to fix a live-observed
infinite loop (28 turns before `max_turns`) by making the `speak` tool set
`terminate`. `scripts/tectum_responder.py` is the live bring-up demo (real
NATS + tectum's `parley-nats` + this script). Documented in
`docs/NATS-BUS-EXTENSION.md` — the strongest current source for the
ffwfrobotics `integrations/tectum-tau.md` page (see memory
`docs-overhaul-plan-for-ffwfrobotics-site`).

### Multi-vendor clients — τ speaks three wire protocols — shipped 2026-08-22

`tau-llm/src/tau_llm/providers/` now holds `openai.py`, `anthropic.py` and
`google.py` behind `base.py`'s registry. Both new SDKs import lazily, so the
test suite runs without either installed. Design decisions O1/O2/O4 were settled
by measurement, not argument (`3065380`, `4728f35`); the instrument is in
`scripts/`. Follow-up fixes after the 0.9.4 tag: the Anthropic client sent a
temperature nobody chose (`407befc`), and the Google client read pi's field
names off a τ message (`947918b`). `docs/ANTHROPIC-GOOGLE-CLIENTS.md` is the
design record. This closes 0.9.3 §4's "non-OpenAI clients" item. **Remainder
still open:** §4.4 step 5's pluggable auth and model resolver.

### TUI steering — typing while the model generates — shipped 2026-08-29

`docs/TUI-STEERING.md`. The core has had `multitask_strategy="steer"` since the
submission lifecycle's phase 4; nothing in the TUI could reach it, because a
turn disabled the editor and pinned the transcript to the bottom. Both locks are
off: `MessageList` follows the tail only while the reader is at the bottom, and a
line typed during a turn lands in `Parley._pending_steer`, shown in a new
`PendingInput` widget and reclaimable with Up on an empty editor. `config.json`'s
`steering_strategy` picks the delivery point — `steer` (default, the running
turn's next tool call) or `enqueue` (the turn edge). A delivered steering message
is rendered inside the open exchange, which it was not before: `TurnStream`
consumed no `message_start`, so it reached the model and the log without
appearing on screen until a reload.

### Session UX sprint — Phases B and C — shipped, confirmed 2026-08-28

Both were listed as open below and were not. `session_picker.py` defines
`SessionPickerModal` (`session_picker.py:128`); `--resume` is a real TUI flag
that opens it over the first frame (`cli.py:774-787`); the palette carries
"Resume session…" (`app.py:6796` → `app.py:7076`); the sidebar defaults closed
(`parley.tcss:42-43`, `display: none`). `--resume` is now rejected only with
`--print`, where there is no screen to draw a picker on — narrowed from the old
blanket "isn't available headlessly".

### 0.9.4 cycle — the session that got long — shipped 2026-08-23 → 08-24

`docs/PLAN-0.9.4.md` §1–§7, every section marked "Built". The cycle's complaint
was that τ goes silent as a session grows, and two of its four items turned out
to be one defect: 67% of a reload and 91% of streaming CPU were the same
quadratic `Screen._refresh_layout` pass (`efae7af`). Also shipped: `Esc` no
longer destroys the turn (`e8540fe`, verified against a real llama.cpp, not a
mock SSE body — `7ed04c3`); thinking text now reaches a widget that is on screen
(`760bbc5`); a liveness readout so a two-minute thinking turn does not look dead
(`69b9309`); the tree browser gained fork indentation, fold labels, authorship,
branch summaries and eliding as a key rather than a modal (`45635b3`, `a29224c`,
`3e7203e`, `bd64016`); `--max-turns` is a real flag and the silent 50-turn
ceiling is gone (`8edc813`); `pip install ffwf-tau` works (`486088e`); the token
counter stopped re-billing every earlier turn (`b8e26bb`). `04c8003` adds
`docs/TREE-EDITOR-MANUAL.md`.

### Tier 10 — the themes leg only — shipped 2026-08-24

`tau-coding-agent/src/tau_coding_agent/themes.py` + `--theme`. Structure stays
in `parley.tcss`, colour moves to `textual.theme.Theme`, so a theme is a palette
and not a thousand-line stylesheet fork. `tests/test_themes.py` fails if a
colour literal appears in `parley.tcss`, which is what keeps the split real.
Ships three themes, a live swap, a user theme file, and per-run `--theme`;
one bad theme file no longer costs the whole TUI (`eb01f48`). Post-tag fix:
Textual's own 21 built-in themes crashed the app on `$tau-bg` (`cf3920a`).
**Tier 10's templates and skills legs remain untouched** — see "Open work".

### Agent-facing reference docs — shipped 2026-08-26

`dceb44a`. An agent asked to extend τ had 8,359 lines of examples and no
reference. `scripts/build_agent_docs.py` reads `@agent_facing` markers out of
the AST via griffe (it imports nothing, which is what lets it document the
lazily-imported providers and the Textual TUI) and writes
`docs/library/reference/`. `scripts/check_docs_coverage.py` is the gate.
Measured 2026-08-28: **316/758 marked objects complete (41.7%), 0 drift** —
unchanged from the 08-26 baseline. The gate is still not in the pre-commit hook,
by decision: a hook that always fails is a hook people switch off. See
`docs/AGENT-DOCS.md` §7.

### System-prompt fields — shipped 2026-08-22

`bf8d2fc` adds `{{fields}}` expansion in a system prompt plus a default worth
keeping; `db3d6f9` fixes the TUI throwing away the prompt it built and telling
the model it was a helpful assistant. **This is not Tier 10's prompt-template
leg** — no `PromptTemplate` type and no `$ARGUMENTS` handling exists.

### CLI parity — the shipped subset of Tier 6/7

`--append-system-prompt` (`cli.py:258`), `--exclude-tools`/`-xt`
(`cli.py:219`), `--no-builtin-tools`/`-nbt` (now genuinely
distinct from `--no-tools`: `-nbt` drops the built-in set and keeps
extension-registered tools, `-nt` withholds both while extensions keep
loading — hooks, commands and injections are untouched. The two argv
booleans collapse into one resolved `no_tools` at the argv boundary,
`headless.resolve_no_tools`, pi `main.ts:424-428`), `--session-dir`
(`cli.py:320-328`, threaded through TUI/print/rpc), `--no-session`
(`cli.py:234`, `headless.py:617-628`). `docs/CLI-PLAN.md`'s §3 status tables
still mark several of these ❌ against its own prose and the code — needs a
resync pass (see "Doc hygiene" below).

---

## Open work

Confirmed still-unbuilt by direct code inspection (not doc-trusting) on
2026-08-09, and re-checked 2026-08-21 and 2026-08-28.

The 2026-08-28 check enumerated every `"--flag"` literal in `cli.py`. The 32
flags that exist are: `--append-system-prompt`, `--bus`, `--continue`,
`--exclude-tools`, `--export-session`, `--ext-config`, `--extension`, `--fork`,
`--fun`, `--import-session`, `--max-turns`, `--mode`, `--model`, `--name`,
`--no-builtin-tools`, `--no-context-files`, `--no-extensions`, `--no-session`,
`--no-tools`, `--print`, `--provider`, `--resume`, `--session`, `--session-dir`,
`--store`, `--system-prompt`, `--theme`, `--thinking`, `--tools`,
`--ui-defaults`, `--verbose`, `--version`.

- **`--list-models [search]`** (Tier 6) — still open; absent from the flag list
  above as of 2026-08-28.
- **`--session-id`** (Tier 7) — still open; absent from the flag list above.
- ~~**Context-file discovery** (Tier 8)~~ — **built 2026-08-21** (`db98524`),
  and the diagnosis in this entry was wrong in a way worth keeping. The loader
  was not orphaned: `sdk.py` reached it, and the shipped
  `tau_default_config.json` shadowed it with a non-empty `system_prompt`
  string, which is truthy. The fix was precedence, not plumbing. τ now ports
  pi's `loadProjectContextFiles` — agent dir first, then every ancestor of cwd,
  one file per directory, deduped, with the nested-worktree shadowing rule —
  supports `CLAUDE.md`, keeps `.tau/SYSTEM.md`, composes with the system prompt
  rather than being displaced by it, and has `--no-context-files`/`-nc`
  (`cli.py:298`). The default config no longer sets `system_prompt` at all
  (`133e74d`). See `docs/PLAN-0.9.3.md` §1.
- **Trust gate** (Tier 8) — still open; no `trust.json`, no `TrustGate` symbol,
  no `--approve`/`--no-approve` flag, re-grepped 2026-08-28.
- **`--export` HTML** (Tier 9) — still open. `tau-agent-core/.../export.py`
  defines the format *types* (markdown and HTML, per PHASE-6-SUBPHASE-2), and
  `cli.py`'s only export flag is `--export-session`, a JMFTS→`.jsonl` copy
  (`cli.py:421-426`, `_run_export_session` at `cli.py:636`). No HTML exporter is
  reachable from the CLI.
- **pi-faithful `--mode json`** (Tier 9) — `--mode json` works but emits τ's
  own `AgentEvent` vocabulary, not pi's schema (doc already flags this as
  unvalidated/divergent).
- **Tier 10 — templates and skills legs** (the themes leg shipped 2026-08-24,
  see above). Still absent on 2026-08-28: no `PromptTemplate` type, no
  `$ARGUMENTS` handling, no `SKILL.md` loader, no shared resource-loader
  abstraction. The `{{fields}}` expansion added in `bf8d2fc` is system-prompt
  field substitution and is a different mechanism.
- **Tier 11 M4/M5** — `registerProvider`, package manager. Deliberately
  deferred (see "Shipped" above), not stalled.

---

## Debts carried out of the 0.9.4 cycle

`docs/PLAN-0.9.4.md` §8 is the authoritative list and says explicitly that
nothing in it is scheduled. Summarized here so this file is not the last place
to learn they exist. The mypy entry in that list is closed — the 52 findings
were a measurement artifact of running mypy without the project's dependencies
visible, not a real debt.

- **Three synchronous call sites block the UI event loop.** The agent loop runs
  as an async Textual worker on the app's own event loop, so anything
  synchronous freezes painting and input. `grep` and `find` do a synchronous
  `os.walk` with no `to_thread`; `read`/`write`/`edit` do synchronous file I/O;
  worst, `_persist_loop_messages` is a plain sync method at turn end, so with
  the JMFTS store a ten-tool turn issues about 21 blocking HTTP round-trips on
  the UI thread. The last one gets worse as turns get longer.
- **No repeat-tool-call detection in `agent_loop.py`** (0.9.3 §4.2). This got
  worse in 0.9.4, not better: `max_turns` now defaults to `None`, so the
  50-turn ceiling that used to bound a model calling one tool forever is gone.
- **No retry or backoff anywhere in `tau-llm`** (0.9.3 §4.3). The seven-backend
  probe found UnoRouter 429s that name their own retry interval, so the
  information is on the wire and unused.
- **A stated `--max-turns` ceiling is still reached silently** — nothing in the
  event stream distinguishes a truncated run from a finished one.
- **Two docs describe a pipeline that no longer exists.**
  `docs/TOOL-CALL-PIPELINE.md` and `docs/tau-coding-agent.md` still describe
  `TauBackend.stream_chat` with a `callback(delta)` and a 30 Hz render throttle.
  The TUI has used `subscribe_render`/`RenderRouter` since B3-a and the throttle
  was deliberately removed. **`CLAUDE.md`'s architecture section inherits the
  same wrong description** — step 5 and step 6 of its pipeline walkthrough.
- **58 ruff findings outside the gate's scope** (tests, `run_agent_loop.py`,
  `experiments/m2`), three rules, none a defect. The `src` trees are clean.
  Recorded because "ruff is clean" is said often enough here to be worth
  qualifying.
- **`docs/TECTUM-NO-TOOLS-MIGRATION.md`** — six sites, still the Tectum owner's
  call.
- **Tree browser steps not started** — `docs/TREE-BROWSER-AS-EDITOR.md` §10
  item 4d (the compaction fold header, parked because it rewrites row order so
  vertical position stops meaning time), step 7 (the plan buffer and commit
  algorithm), and the archive gesture.

---

## Doc hygiene found during this audit

**All six entries are now closed (2026-08-21).** Kept as the record of what the
audit found, with what actually fixed each one. This was separate from the
broader docs-overhaul plan already agreed (see memory
`docs-overhaul-plan-for-ffwfrobotics-site`), which is still to start.

- ~~`docs/REMOTE-CONTROL.md:3`~~ — **fixed.** Header now reads "shipped
  2026-08-01 → 08-07" and cites `cli.py:557`, the 20 implemented verbs and the
  463 RPC-scoped tests. It had said "Status: design. No code written." while
  its own last-touch commit postdated the code by a day.
- ~~`docs/SUBMISSION-LIFECYCLE.md:3`~~ — **fixed.** Header now reads "shipped
  2026-07-31 → 08-05" and cites `agent_session.py:1754` as the one door.
- ~~`docs/NODE-ADDRESSABLE-AGENTS.md:3`~~ — **fixed.** Header now reads
  "shipped 2026-07-30 → 08-01" and cites `agent_session.py:558` plus the
  reload-invariance contract test.
- ~~`docs/EXTENSIONS-DEMO-ROADMAP.md`~~ — **fixed.** Header now reads
  "SHIPPED", with M4/M5 named as open by written decision rather than as
  stalled work.
- ~~`docs/CLI-PLAN.md` §3~~ — **resynced 2026-08-21**, and it needed more than
  the four flags this entry named. The 2026-08-10 pass corrected those four but
  asserted the remaining ❌ rows were "still accurate", which was itself wrong:
  every flag was re-checked against `add_argument` calls in `cli.py`, and only
  seven remain unbuilt. §2's "plumbing reality check" is now marked as a
  pre-build snapshot, since every constraint it lists has been lifted.
- ~~`origin/feat/extensions-e6`~~ — **gone**; the ref no longer exists on
  `origin`, which now carries only `master` and `rpc/tier-b`. `rpc/tier-b` is
  fully merged into `master` (0 commits ahead) and is the remaining deletable
  remote ref.

---

## Cross-cutting: the 4 Phase-A seams (approved 2026-06-22)

`docs/SESSION-UX-REDESIGN.md` Phase A baked in four forward-compat seams:

1. **Session API parameter slots** (`base_dir`, `id`, `create_in_memory`) →
   Tier 7. **Partially realized:** `--session-dir`/`--no-session` shipped;
   `--session-id` still open.
2. **Raw `entries()`/`header` accessor** on `Session` → Tier 9. **Still
   open** — neither `--export` (HTML) nor pi-faithful `--mode json` has used
   it yet.
3. **Session-lifecycle event emission** (`session_start`/`before_fork`/
   `before_compact`/`shutdown`) → Tier 11. **Realized** —
   `registry.py:200` `LIFECYCLE_EVENTS`, consumed by the E6–E11 extension
   arc.
4. **Generic/dynamic command registry** → Tier 10, Tier 8, Tier 12. **The
   Tier 12 leg is realized** — RPC's `get_commands`/`COMMAND_TABLE` dispatch
   sits on this seam. The Tier 10 themes leg is realized by a different
   mechanism (`textual.theme.Theme`, not this registry). Tier 8 (trust
   commands) and Tier 10's templates leg are still open.

---

## Suggested order

Highest-value remaining items, roughly by dependency. Revised 2026-08-28: the
old items 1 and 2 are both built.

1. **Repeat-tool-call detection** (`docs/PLAN-0.9.4.md` §8) — promoted from an
   unscheduled debt because 0.9.4 removed the ceiling that was covering for it.
   `max_turns` now defaults to `None`, so a model calling one tool forever has
   nothing to stop it and nothing to report it.
2. **The three blocking call sites** (`docs/PLAN-0.9.4.md` §8) — `grep`/`find`
   need `to_thread`, and `_persist_loop_messages` blocks the UI thread for
   about 21 HTTP round-trips at the end of a ten-tool turn.
3. **Trust gate** (Tier 8) — security-ordered; Tier 10's skills leg and
   project-local extensions are gated behind it per the original plan.
4. **Tier 9** — `--export` HTML, pi-faithful `--mode json`. Both seams
   (`entries()`/`header`) are already in place, and `export.py` already defines
   the HTML format types.
5. **Tier 10's remaining legs** — shared resource loader, templates, skills.
   Themes shipped 2026-08-24 and did not need the shared loader, so that
   abstraction is still unwritten and still unproven.
6. **Docstring coverage** — 442 of 758 marked objects are incomplete. The gate
   cannot enter the pre-commit hook until this moves.
7. **Tier 11 M4/M5** — `registerProvider`, package manager. Lowest priority;
   deliberately deferred, no user demand signal yet.
8. ~~A doc for tau-006/tau-007~~ — **done 2026-08-10**, `docs/NATS-BUS-EXTENSION.md`.
9. ~~Wire or remove the AGENTS.md loader~~ — **done 2026-08-21** (`db98524`);
   the diagnosis was wrong, the fix was precedence, not plumbing.
10. ~~Session UX Phase B + C~~ — **done**; see "Shipped" above.

G7 (jump-forward branch hints) stays blocked on the llama.cpp fork server
work (`turboquant_experiments`), tracked in `docs/WORKSTREAM-CROSSWALK.md`,
not here.
