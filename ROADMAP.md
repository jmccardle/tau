# τ Roadmap

Living schedule of open work. Each item cites the evidence (file:line, doc, or
test) it came from so it can be audited against the source of truth (pi) and the
"Fail Early" rule.

**State (2026-08-09):** this file was last edited 2026-07-18 and had drifted
152 commits behind master. A full re-audit (5 parallel evidence passes: RPC,
submission-lifecycle, CLI flags, extensions, session UX) found five shipped
arcs this file never mentioned or still called unbuilt — folded into "Shipped"
below with commit-hash evidence. Genuinely still open, confirmed against code:
Tier 8 (context files/trust gate), Tier 9 (`--export` HTML, pi-faithful
`--mode json`), Tier 10 (themes/templates/skills — untouched), Tier 11's M4/M5
(deliberately deferred), Session UX Phases B/C (not started), plus two flags
(`--list-models`, `--session-id`). See "Doc hygiene found during this audit"
below — several spec docs' own status headers are now wrong in the same way
this file was.

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
2026-08-09.

- **`--list-models [search]`** (Tier 6) — no occurrence anywhere in
  `cli.py`/`headless.py`/`backends.py`.
- **`--session-id`** (Tier 7) — no such flag in `build_parser()`.
- **Context-file discovery** (Tier 8) — **partial, and orphaned**: real
  AGENTS.md/`.tau/SYSTEM.md` loading exists in
  `sdk.py:672-735` `_build_system_prompt()`, but nothing in
  `tau-coding-agent` calls it — `TauBackend` builds `AgentSession` straight
  from `config.get("system_prompt", "")` (`backends.py:966,1026`). No
  CLAUDE.md support at all. The τ CLI/TUI does not load AGENTS.md today
  despite the code existing. Wire it in, or remove it — leaving dead code on
  a documented-but-unreachable path is itself a Fail-Early violation.
- **Trust gate** (Tier 8) — no `trust.json`, no `TrustGate` symbol, no
  `--approve`/`--no-approve` flags anywhere.
- **`--export` HTML** (Tier 9) — only `--export-session` (JMFTS→`.jsonl`
  copy) exists; no HTML exporter.
- **pi-faithful `--mode json`** (Tier 9) — `--mode json` works but emits τ's
  own `AgentEvent` vocabulary, not pi's schema (doc already flags this as
  unvalidated/divergent).
- **Tier 10 (themes/templates/skills)** — entirely untouched: no
  `themes/` dir, no `PromptTemplate`/`$ARGUMENTS` handling, no `SKILL.md`
  loader, no shared resource-loader abstraction. Zero real commits reference
  any of it.
- **Tier 11 M4/M5** — `registerProvider`, package manager. Deliberately
  deferred (see "Shipped" above), not stalled.
- **Session UX sprint Phase B** (picker modal) — no session-resume picker
  exists. `SessionTreeModal` is a different thing (branch-tree browser, not a
  saved-session picker). `cli.py:607-611` explicitly rejects `--resume` at
  runtime with "isn't available headlessly."
- **Session UX sprint Phase C** (command unification + sidebar-closed
  default) — `--resume`/`--continue`/`--session`/`--fork` remain
  headless-only or rejected in the TUI; no palette "Resume session…" entry
  (`app.py:3637-3743`); sidebar still defaults open
  (`parley.tcss:9-13`, no startup override). Both decisions are already
  recorded in `docs/SESSION-UX-REDESIGN.md` §2 Decision 4 — the design is
  settled, only the build is missing.

---

## Doc hygiene found during this audit

Several spec docs' own status headers are now flatly wrong — written when the
work was proposed, never updated once it shipped. Each is a one-line fix, not
a rewrite, and is separate from the broader docs-overhaul plan already agreed
(see memory `docs-overhaul-plan-for-ffwfrobotics-site`) — flagging here so it
isn't lost before that pass starts:

- `docs/REMOTE-CONTROL.md:3` — still says "Status: design. No code written."
  RPC is built and tested (463 passing tests). Sharpest case: this doc's own
  last-touch commit (2026-08-06) postdates the code it's declaring nonexistent
  by a day.
- `docs/SUBMISSION-LIFECYCLE.md:3` — still says "Status: proposal." Phases
  1–5 of its own phasing table are shipped.
- `docs/NODE-ADDRESSABLE-AGENTS.md:3` — still says "Status: design. No code
  written." `agent_spec` + rollback are shipped; most of its cited "prior
  art" already existed when it was written.
- `docs/EXTENSIONS-DEMO-ROADMAP.md` — still says "Status: PLANNED (no
  implementation yet)." The entire E6–E11 arc (S38–S75) is merged.
- `docs/CLI-PLAN.md` §3's ❌/➖/✅ tables contradict both its own prose
  sections and the actual code for `--append-system-prompt`,
  `--exclude-tools`, `--no-builtin-tools`, and `--no-session` — needs a
  resync pass before anyone trusts it as a status source again.
- `origin/feat/extensions-e6` — stale remote ref, fully merged, safe to
  delete.

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
   sits on this seam. Tier 8 (trust commands) and Tier 10
   (templates/themes) legs are still open.

---

## Suggested order

Highest-value remaining items, roughly by dependency:

1. **Wire or remove** the orphaned AGENTS.md loader in `sdk.py` (Tier 8) —
   cheapest, and currently a live Fail-Early violation (dead code claiming to
   be a feature).
2. **Session UX Phase B + C** — the design is already settled
   (`SESSION-UX-REDESIGN.md` §2 Decision 4); only the picker modal and
   command unification remain to build.
3. **Trust gate** (Tier 8) — security-ordered; some Tier 10 work (skills,
   project-local extensions) is gated behind it per the original plan.
4. **Tier 9** — `--export` HTML, pi-faithful `--mode json`. Both seams
   (`entries()`/`header`) are already in place.
5. **Tier 10** — shared resource loader, themes, templates, skills. Entirely
   greenfield; no code exists yet.
6. **Tier 11 M4/M5** — `registerProvider`, package manager. Lowest priority;
   deliberately deferred, no user demand signal yet.
7. ~~A doc for tau-006/tau-007~~ — **done 2026-08-10**, `docs/NATS-BUS-EXTENSION.md`.

G7 (jump-forward branch hints) stays blocked on the llama.cpp fork server
work (`turboquant_experiments`), tracked in `docs/WORKSTREAM-CROSSWALK.md`,
not here.
