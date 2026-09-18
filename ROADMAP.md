# τ Roadmap

Living schedule of open work. Each item cites the evidence (file:line, doc, or
test) it came from, so a reader can check it against the code rather than against
this file. Older entries cite pi as "the source of truth"; that stopped being the
arrangement on 2026-09-03 (`CLAUDE.md`, "Parity with pi is not an objective") and
those citations are provenance now.

**State (2026-09-13):** re-audited against code after **seven releases this file
never mentioned** — 0.9.5 through 0.10.3, 2026-08-30 to 09-12. Its own top two
priorities are both in the first of them. Repeat-tool-call detection is
`AgentLoopConfig.repeat_tool_call_limit`, default 3 (`agent_loop_types.py:83`,
enforced at `agent_loop.py:322`), and five tools moved their file I/O off the
paint loop (`to_thread` in `read`/`write`/`edit`/`grep`/`find`). A third carried
debt went with them: `max_turns` no longer ends a run silently, because
`"max_turns"` and `"repeat_tool_calls"` are `end_reason` values on the wire
(`events.py:43`, `rpc_event_schema.py:110-111`). **Still open, re-verified
today:** the trust gate, Tier 9, Tier 10's templates and skills legs, Tier 11
M4/M5, `--list-models`, and `--session-id`. The last of the three blocking call
sites is **closed**: the `SessionLog` appenders are coroutines as of 2026-09-14
(`docs/ASYNC-SESSION-LOG.md`). Docs coverage **517/958 marked objects
(54.0%), 0 drift**, up from 316/758 (41.7%); the denominator grew by 200, so the
percentage rose while 441 objects remain incomplete.

**This file can no longer measure its drift in commits.** Every state header
above opens with "drifted N commits behind master". `master` is **17 commits**
long (`git rev-list --count master`), because 0.10.3's pivot re-pointed it onto
the public squash chain instead of merging onto it — `git merge-base
--is-ancestor` says the two chains are disjoint. The 611 private commits hang off
`oldmaster-0.10.2` and are ancestors of nothing on `master`. Releases are the
unit here from now on.

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
HTML; the `--mode json` half is struck), Tier 10's templates and skills
legs, Tier 11 M4/M5, two flags (`--list-models`, `--session-id`), and the `docs/PLAN-0.9.4.md`
§8 debt list. Suite: 4951 passed, 144 skipped, 0 failed. Docs coverage:
316/758 (41.7%), 0 drift.

**Release state (2026-08-28, corrected 2026-09-12).** `v0.9.4` is public: the tag
resolves to `c5fff1f` on the `github` remote, matching the local tag. `origin`
(the private development host) carried **no tags at all** — `git ls-remote --tags
origin` returned empty and exited 0, so the release tags lived on `github` and in
the local clone only. Worth stating, because an empty tag listing reads like an
unreachable remote and is not one. `origin/master` is at `947918b`, which is
behind local master. I did not check PyPI.

Both of those claims are now wrong, and the 2026-09-12 correction that replaced
them was wrong too — it said `origin` carried exactly one tag. Measured
2026-09-13 with `git ls-remote --tags`:

- `origin`: 21 tags — `oldmaster-0.10.2`, `v0.9.0`–`v0.10.1`, and ten
  `-fullhistory` tags.
- `github`: 12 tags — `v0.9.0`–`v0.10.3`, no `-fullhistory`.
- local: 22 — everything on `origin` plus `v0.10.2`, and **not** `v0.10.3`.

Where a version tag exists on both remotes it resolves to the same commit
(checked for `v0.9.7`, `v0.10.0`, `v0.10.1` — `379230c`, `f77a0e6`, `0a6700a`,
all on the public squash chain). The `-fullhistory` tags are what still name
commits in the private chain; they are on `origin` and in the local clone, never
on `github`. `v0.10.3` exists on `github` alone because publishing the release
draft created it and the release procedure forbids pushing a local tag —
`docs/RELEASING.md` §"The two repositories" and `.claude/skills/release/SKILL.md`
step 7.

**Released:** 0.10.3 is on PyPI — all five distributions (`ffwf-tau`,
`ffwf-tau-llm`, `ffwf-tau-agent-core`, `ffwf-tau-coding-agent`,
`ffwf-tau-jmfts`), published by `publish.yml` off the `v0.10.3` tag at `bcb73c0`.

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
Tier 8's trust gate, Tier 9 (`--export` HTML, and a `--mode json` half since
struck), Tier 10 (themes/templates/skills — untouched), Tier 11's M4/M5 (deliberately
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
line typed during a turn lands in `TauApp._pending_steer`, shown in a new
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
(`tau.tcss:42-43`, `display: none`). `--resume` is now rejected only with
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
in `tau.tcss`, colour moves to `textual.theme.Theme`, so a theme is a palette
and not a thousand-line stylesheet fork. `tests/test_themes.py` fails if a
colour literal appears in `tau.tcss`, which is what keeps the split real.
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

### 0.9.5 through 0.10.3 — seven releases, added 2026-09-13

One line per release, pointing at its own note. Each `docs/RELEASE-NOTES-*.md`
holds the measurements; this is the index, not a summary of them.

- **0.9.5** (2026-08-30) — *two vendors, a frozen screen, and a loop that could
  not stop.* Closes three entries in "Debts carried out of the 0.9.4 cycle":
  repeat-tool-call detection, the `grep`/`find` blocking sites (five tools, not
  two), and the silent `max_turns` ceiling. Also the two post-0.9.4 provider
  fixes this file listed as having no release note, the Textual-themes crash,
  and the agent-facing reference.
- **0.9.6** — *the image arrives, and the TUI stops locking.* Image path,
  editor, two fixes.
- **0.9.7** — *the tree becomes an editor, and the transcript stops growing.*
  `docs/TREE-BROWSER-AS-EDITOR.md` steps 1–7 and `docs/TRANSCRIPT-WINDOW.md`.
  This is what closed most of the "Tree browser steps not started" debt below.
- **0.10.0** — *one declaration of what τ can do, and a cache that was never
  asked for.* The capability registry and `docs/PROMPT-CACHING.md`. Has a
  breaking-changes section.
- **0.10.1** — *the tree a second head can draw, and the lock it can release.*
  Puts tree structure on the RPC wire — the largest blocker
  `docs/VSCODE-HEAD.md` §6 named. Adds `tau --mode repl`, a fourth head.
- **0.10.2** — *shadowing stops being destructive, and three silent no-ops start
  working.* `docs/EXTENSION-NAMESPACE.md`, `docs/EXTENSION-MESSAGES.md`. §"A
  fourth of the same shape, found by the release gate" is the one the gate
  caught rather than the audit.
- **0.10.3** (2026-09-12) — *an extension can import the module beside it, and
  this history is public.* 9 code lines in `sdk.py`, and the repository pivot
  recorded above.

---

## Open work

Confirmed still-unbuilt by direct code inspection (not doc-trusting) on
2026-08-09, and re-checked 2026-08-21, 2026-08-28 and **2026-09-13**. The
2026-09-13 pass re-grepped each item below and found **no change to this list
across seven releases** — none of them targeted these.

The flag enumeration is re-run each pass over every `"--flag"` literal in
`cli.py`; it is still the same 32 flags on 2026-09-13, with no additions and no
removals since 08-28: `--append-system-prompt`, `--bus`, `--continue`,
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
- ~~**pi-faithful `--mode json`** (Tier 9)~~ — **struck 2026-09-17.** The
  objective was to make `--mode json` emit pi's `AgentSessionEvent` schema. It
  is retired rather than done, for two reasons measured that day. Nothing speaks
  pi's schema: pi's only consumer of it is pi's own in-process `rpc-client.ts`,
  τ's only consumers are `examples/ext_kit/`, which read three type names off
  τ-snake fields, and Claude Code's `--output-format stream-json` is a third
  schema that matching pi would not get you. And adopting it would COST — pi's
  events carry no `timestamp` and none of the four provenance fields τ stamps on
  every event, which is what `examples/51_delegate_fleet.py` needs to tell one
  child's output from another's. `CLAUDE.md` retired pi parity on 2026-09-03;
  this entry outlived that and was still stated in pi's terms.
  **The real defect underneath it is fixed** (`docs/JSON-MODE-DELTAS.md`):
  `--mode json` re-sent the whole accumulated answer on every fragment, so a
  10,000-character answer wrote 10.6 MB to stdout. It now emits prefix-diffs
  through the same `MessageDeltaProjector` the RPC wire has used since unit 2B —
  486 KB for the same answer, with a linearity gate. That was τ's own bug, not a
  divergence from pi, and pi having hit it too (its regression #7290) is
  evidence it is real rather than a reason to copy anything else.
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

- ~~**One synchronous call site still blocks the UI event loop**~~ — **fixed
  2026-09-14** (`docs/ASYNC-SESSION-LOG.md`), by `BLOCKING-PERSISTENCE.md`'s
  Option B rather than the `to_thread` this entry used to propose. The eight
  `SessionLog` appenders are `async def`; `id`/`cursor`/`entries()` are not.
  `_persist_loop_messages` and `_persist_turn_inputs` are coroutines, and only
  the JMFTS store hops to a thread — a decision that is now local to the one
  store that does network I/O per append, which is what §2's objection to
  `to_thread` at the call sites was about. ~~`grep`/`find`~~ and
  ~~`read`/`write`/`edit`~~ — **fixed in 0.9.5**, `to_thread` in all five.
  **It was not the six-line item this list claimed**: it cost two breaking
  contract changes (`SessionLog`, and three `ExtensionAPI` methods the record
  never named) and turned the construction-time `agent_spec` into a queued
  write. The freeze itself was never benchmarked, then or now.
- ~~**The RPC cursor-ordering invariant is documented but not gated**~~ —
  **closed 2026-09-14**, by the change that would otherwise have broken it.
  `docs/ASYNC-SESSION-LOG.md` §3.3: making the appenders coroutines does not add
  an `await` to the window `rpc/handler.py` names, but it lets persistence
  suspend part-way through, and the wire then carried `cursor: null` for any
  store whose appends really suspend — measured, not argued.
  `test_agent_end_cursor_is_still_post_persistence_when_appends_suspend` is the
  slow-append double this entry asked for (20 ms per append; `asyncio.sleep(0)`
  is measurably too weak to reproduce anything). The ordering is now stated
  rather than inherited: `AgentSession.persistence_settled`, awaited by
  `RPCHandler.await_outbound_prerequisites` before an `agent_end` is framed. The
  new test fails when that wait is removed; the old one still passes, which is
  why the old one was never the gate.
- ~~**No repeat-tool-call detection in `agent_loop.py`**~~ (0.9.3 §4.2) —
  **fixed in 0.9.5.** `AgentLoopConfig.repeat_tool_call_limit`, default 3, `ge=2`
  (`agent_loop_types.py:83`), enforced at `agent_loop.py:322`.
- **No retry or backoff anywhere in `tau-llm`** (0.9.3 §4.3). The seven-backend
  probe found UnoRouter 429s that name their own retry interval, so the
  information is on the wire and unused. **Re-checked 2026-09-13: still open**,
  and now the oldest item on this list.
- ~~**A stated `--max-turns` ceiling is still reached silently**~~ — **fixed in
  0.9.5.** `"max_turns"` and `"repeat_tool_calls"` are `end_reason` values
  (`events.py:43`), set at `agent_loop.py:331` and `:483`, and documented on the
  RPC wire at `rpc_event_schema.py:110-111`.
- ~~**Two docs describe a pipeline that no longer exists.**~~ — **fully closed
  2026-09-13** (`3cae472`); the history below is kept because the correction
  found three further wrong claims, one of them in its own first draft.
  `docs/TOOL-CALL-PIPELINE.md:37,43` drew `message_update → text delta →
  callback(delta)` and "stream text into a ChatMessage at 30 Hz";
  `docs/tau-coding-agent.md:14` calls the 30 Hz throttle "carried forward as-is;
  still the right" choice. The TUI has used `subscribe_render`/`RenderRouter`
  since B3-a and the throttle was deliberately removed. **The `CLAUDE.md` half
  is closed** (2026-09-13) — the whole pipeline walkthrough was deleted in the
  overhaul rather than corrected, because it was implementation detail in a file
  that should carry layout, and `docs/TOOL-CALL-PIPELINE.md` is where it belongs
  once that file is right.
- **Shrink the sent prompt when the cache has expired** (added 2026-09-16). Not
  compaction: compaction cuts because the conversation is outgrowing the window,
  and this cuts because a particular *request* is about to be expensive. τ has
  the signal already — `PromptCacheObserver` decides whether this turn's prefix
  was read from the server's cache (`docs/PROMPT-CACHING.md` §7's three gates) —
  and a turn that gets a cache miss pays full price for every input token, which
  makes it the cheapest moment to send fewer of them. The mechanism exists too:
  the count-based cut that `AgentSession.compact_messages` used to perform (keep
  the last user turn, summarise the rest) was removed in
  `docs/STREAMING-SIDE-WORK.md` §2 because it was wired to `/compact`, where it
  wrote nothing durable and undid itself on the next turn. Rebuilt here it would
  write a real entry like every other cut. **Open questions before it is
  schedulable:** whether the trigger is per-request or per-session; whether a
  user-visible prompt is wanted, since this spends a completion to save tokens
  and could cost more than it saves on a short conversation; and what it does
  when the cache expires mid-conversation on a model with no cache at all, where
  the observer's answer is "nothing observed" rather than "missed".
- **Rollback conflates "pre-root" with "no target"** (added 2026-09-16, from the
  change that made it reachable). `agent_session.py:2547` refuses a rollback when
  `rollback_target is None`, and the rejection text says the target is *stale* —
  a different condition, and the one the second half of that same `or` actually
  tests. `None` is a real cursor value: it means the log's root. The branch was
  unreachable while `__init__` always wrote the `agent_spec` entry, so
  `_pre_turn_leaf` was never `None` at a real turn's start; queuing that write
  (`docs/ASYNC-SESSION-LOG.md` §3.2) removed the guarantee, and a
  `store_history=False` first turn now reaches `submit` on an empty log.
  Deliberately not fixed alongside the change that exposed it: distinguishing the
  two states is a change to the rollback contract, not to the persistence one.
  No test reaches it today.
- **58 ruff findings outside the gate's scope** (tests, `run_agent_loop.py`,
  `experiments/m2`), three rules, none a defect. The `src` trees are clean.
  Recorded because "ruff is clean" is said often enough here to be worth
  qualifying.
- **`docs/TECTUM-NO-TOOLS-MIGRATION.md`** — six sites, still the Tectum owner's
  call.
- **Tree browser: the fold header and the archive gesture.** Step 7 (the plan
  buffer and commit algorithm) **shipped in 0.9.7**; what remains of
  `docs/TREE-BROWSER-AS-EDITOR.md` §10 is item 4d (the compaction fold header,
  parked because it rewrites row order so vertical position stops meaning time)
  and the archive gesture.
- **Test trees are outside both leakage scans** (added 2026-09-13, from
  `docs/RELEASE-NOTES-0.10.3.md` and the worklog's §24). `tau-*/tests/` is in
  neither the SHIPPED roots nor the PROSE roots of
  `test_no_host_addresses.py`, and still carries hard-coded home-directory
  fixture strings and LAN addresses. That was defensible while a commit stayed
  private until a release squashed it; since 0.10.3 a push publishes it. The
  scan's own failure message names offending lines, so start by widening the
  roots and reading what it reports — this entry deliberately names no value,
  per `CLAUDE.md`'s rule. **Closed 2026-09-13** (`bbb9e90`).

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
2. **Raw `entries()`/`header` accessor** on `Session` → Tier 9. **Half
   realized** — `--mode json` writes `session.header` as its first line; no
   HTML exporter uses `entries()` yet.
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

Highest-value remaining items, roughly by dependency. Revised 2026-09-13: the
2026-08-28 items 1 and 2 both shipped in 0.9.5, so the list has been renumbered
and two items promoted out of the debt list.

1. ~~**`docs/TOOL-CALL-PIPELINE.md` and `docs/tau-coding-agent.md` describe a
   render path that was removed.**~~ — **done 2026-09-13** (`3cae472`).
   Re-derived from the code, not reworded. Three claims the first pass made were
   themselves wrong and were caught by an adversarial re-read: `message_end` does
   produce a render event (`completion_end`), `custom_message` is deliberately
   the one render dict with no lane, and "a head renders the third vocabulary and
   never the second" is true of the TUI only — `tau -p --mode json` and
   `tau --mode rpc` both read `AgentEvent`s directly.
2. ~~**`_persist_loop_messages` blocks the UI thread**~~ — **done 2026-09-14**,
   on the second attempt and by a different route. The first attempt
   (`to_thread` at the call sites) was reverted: it is the fix
   `docs/BLOCKING-PERSISTENCE.md` §3 had already priced and rejected in 2026-08,
   and it introduced a confirmed `tau --mode rpc` regression, because
   `RPCHandler._stamp_agent_end_cursor` depends on there being no `await`
   between `_forward_event`'s `put_nowait` and `_persist_loop_messages`. What
   shipped is §3's Option B — the appenders are coroutines — which moves no call
   site onto a thread and so leaves that invariant alone. See
   `docs/ASYNC-SESSION-LOG.md` for what it cost, including the second broken
   contract the record had not named. **The invariant is still not gated**; that
   debt stands on its own above.
3. ~~**Widen the leakage scan to `tau-*/tests/`**~~ — **done 2026-09-13**
   (`bbb9e90`). Third scope, `TESTS`, held against the strict pattern; ten lines
   in six files cleaned; `tau-jmfts/tests/conftest.py` now requires
   `JMFTS_TEST_URL` instead of defaulting to a live LAN instance, so 117 jmfts
   tests skip until it is set. `CLAUDE.md`'s "exactly one place" claim was false
   and is corrected — two test constants hold the value, and both exist to forbid
   it elsewhere.
4. **Trust gate** (Tier 8) — security-ordered; Tier 10's skills leg and
   project-local extensions are gated behind it per the original plan.
5. **Tier 9** — `--export` HTML. The other half of this tier, a pi-faithful
   `--mode json`, was struck on 2026-09-17; see "Open work". Both seams
   (`entries()`/`header`) are already in place, and `export.py` already defines
   the HTML format types. Re-checked 2026-09-13: no HTML exporter is reachable
   from `cli.py`.
6. **Tier 10's remaining legs** — shared resource loader, templates, skills. No
   `PromptTemplate` type, no `$ARGUMENTS` handling, no `SKILL.md` loader exists
   in any `src` tree (re-grepped 2026-09-13). Themes shipped 2026-08-24 and did
   not need the shared loader, so that abstraction is still unwritten and still
   unproven.
7. **Docstring coverage** — 441 of 958 marked objects are incomplete (54.0%
   complete, up from 41.7% on 08-28). The gate cannot enter the pre-commit hook
   until this moves.
8. **Retry/backoff in `tau-llm`** (0.9.3 §4.3) — unscheduled since 2026-08-21
   and the oldest open item on this file.
9. **Tier 11 M4/M5** — `registerProvider`, package manager. Lowest priority;
   deliberately deferred, no user demand signal yet.
Closed items from earlier orderings, kept so a reader can tell a finished item
from one that was quietly dropped:

- ~~A doc for tau-006/tau-007~~ — **done 2026-08-10**, `docs/NATS-BUS-EXTENSION.md`.
- ~~Wire or remove the AGENTS.md loader~~ — **done 2026-08-21** (`db98524`);
  the diagnosis was wrong, the fix was precedence, not plumbing.
- ~~Session UX Phase B + C~~ — **done**; see "Shipped" above.
- ~~Repeat-tool-call detection~~ and ~~the `grep`/`find` blocking sites~~ —
  **done in 0.9.5**, 2026-08-30. These were items 1 and 2 of the 2026-08-28
  ordering and stood at the top of this list for a fortnight after they shipped.

G7 (jump-forward branch hints) stays blocked on the llama.cpp fork server
work (`turboquant_experiments`), tracked in `docs/WORKSTREAM-CROSSWALK.md`,
not here.
