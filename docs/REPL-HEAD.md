# The REPL head: `tau --mode repl`

**Status: built (2026-09-08); designed the same day, revised the same day after
critique (§11).** Step 1 is the skeleton: the `--mode repl` dispatch and its refusals (§2), the
startup/shutdown order (§3), one prompt loop that submits a turn or a command through the one
door, and the §4 render vocabulary end to end. Step 2 is §5: the `FIRST_COMPLETED` loop, the
Ctrl+C state machine, the head-owned steering buffer and both delivery points. Step 3 is §6: the
four dispatched arms, the flow loop with one control table, Tab over the three vocabularies and
`@file` expansion at submission-build time. Step 4 is §7: the delegate's four methods, the
extension request in both of its states, `/panel key n` through the one door, the shortcut
listing, and the two notices through the readers print mode reads them with. Step 5 is the
compact forms of §4 and the gates: one line per tool call, a folded result, a reasoning block
collapsed to its estimated size, the badge on a foreign lane, a right-aligned footer, and a test
that fails on a colour named anywhere but `repl_theme.py`. A fourth
head beside the TUI, print mode and RPC: the interactivity of the TUI in the paradigm of a CLI — a
prompt line, streamed answers in the scrollback, no screen takeover, `rich` as the only renderer.
This head is an experiment in whether the docs suffice: it was specified from `docs/` plus four
source readings, and §9 records what the build found the docs did not say. §11 records the
fifteen findings of the design critique and what each one changed.

Abbreviations: A = `tau-coding-agent/src/tau_coding_agent/app.py`, B = `…/backends.py`,
C = `…/cli.py`, H = `…/headless.py`, R = `…/rpc_mode.py`, XU = `…/extension_ui.py`,
CW = `…/chat_widgets.py`, T = `…/transcript.py`,
S = `tau-agent-core/src/tau_agent_core/agent_session.py`, X = `…/extension_types.py`,
L = `…/extension_locks.py`, F = `…/flows.py`, K = `…/commands.py`,
RT = `…/agent_session_runtime.py`, RPC = `…/rpc/commands.py`.

## 1. Requirements, in priority order

1. A head: session catalog, extensions, `session_start`/`shutdown`, every line through
   `Submission` → `submit_turn`/`submit_command`, ONE persistent `subscribe_render`.
2. Streaming Markdown, a spinner, compact tool/reasoning blocks, one palette.
3. Ctrl+C: abort mid-turn, exit at idle, never hang on a second press.
4. Steering per `docs/TUI-STEERING.md` §2 (lines 52–111).
5. Extension surfaces: delegate, `extension_request`, commands, shortcuts, notices.
6. Slash commands and flows per `docs/TUI-STYLE-GUIDE.md` §2.2.
7. Tab completion, `@file`, input history.

## 2. Module layout and the changes outside it

New files, all under `tau-coding-agent/src/tau_coding_agent/`; none imports `textual`:

| File | Holds |
|---|---|
| `repl.py` | `run_repl(args, config)`, startup/shutdown (§3), the render handler (§4), the main loop (§5), the flow loop (§6), `ReplDelegate` (§7), `REPL_COMMANDS` (§6). |
| `repl_input.py` | `LineReader` protocol + `PromptToolkitReader` + `MemoryReader` (§5, §8). The only module that imports `prompt_toolkit`. |
| `repl_theme.py` | `ROLE_STYLE`, `GLYPH`, `lane_label`, `lane_role` — the ONE palette (§4). |
| `render_text.py` | Two pure text formatters MOVED out of Textual-importing modules: `render_panel_body` from XU:100–138 (XU imports Textual at XU:4–8) and `format_telemetry` from CW:1042 (CW imports Textual at CW:26–30). Both source modules re-import from here, so `examples/70_telemetry.py`, which imports `format_telemetry` from CW, is untouched. **Not built in step 1** (2026-09-08): the move is only correct if CW and XU re-import, and both files are frozen for this work — a second copy of either function would be the duplication the move exists to prevent. Until the step that may edit them, the `lane_end` footer carries `ctx · out · seconds` and no telemetry segment. **Step 4 needed the body renderer and PORTED it** into `repl.py` rather than waiting: a panel, an ask's body and a request body are §7's whole visual vocabulary, so the alternative was a step that cannot draw what it collects. The copy names XU as its origin, adds the Fail-Early raise XU has no need of (`validate_panel_spec` refuses an unknown kind upstream, so XU falls through to `table`), and `test_repl_extension_ui.py` renders all three body kinds through BOTH functions and compares — the duplication is now checked rather than merely regretted. `format_telemetry` is still out of reach. |
| `steering.py` | `STEERING_CONFIG_KEY`, `STEERING_STRATEGIES`, `DEFAULT_STEERING_STRATEGY`, `configured_steering_strategy(config)` — the values at A:108–112 and the validator at A:318–343 — plus `STEER_JOIN`, `steering_note(strategy)` (A:996–1006's sentence) and `SteeringBuffer`, the two-list buffer §5 describes. A:app.py is frozen for this work, so it keeps its copies; `test_repl_steering.py` asserts the two tuples are equal (anti-drift, the `WireEvent` pattern). |

**`headless.py`** (run_print stays byte-identical in behaviour): `_select_session` (H:331–358)
becomes public `select_session`; run_print's call site (H:696) and
`test_rpc_session_dir_isolation.py:16`, which names it, are updated. `_apply_resume_metadata`
(H:361–372) stays private and stays print-mode's: it is a head appending to the log, which
print mode may do because it never binds, and this head may not (§3, "bind, do not append").

**`backends.py`**, four changes, each named because the mypy gate and the one-door rule depend on
them:

1. `Backend.submit_turn` (B:959) and `TauBackend.submit_turn` (B:1594) take
   `context: list[dict] | None`; `None` means "the bound log's `context_for`", which is what
   `AgentSession.submit` already accepts (S:2069–2075). Without the widening,
   `submit_turn(sub, None)` is a mypy error under the all-trees gate.
2. `TauBackend.set_model` (B:1359–1377) appends a `model_change` entry through the bound log
   after `AgentSession.set_model` succeeds, gated the way `set_session_name` is (S:862–863: no
   `append_model_change` on the log → `RuntimeError`, never a silent switch — and checked BEFORE
   the switch, so a refusal leaves the live model matching the record). **Built 2026-09-08**,
   three steps after `record_model_change` itself, which until then was the only caller of it:
   `set_model` appended nothing, so `/model NAME` reached the generic mutation branch and switched
   at RUNTIME only. The RPC verb appends by hand after calling the SESSION (RPC:2723–2730), so a
   `/model` under a bound log switched the live model and left the record naming the old one,
   and the next `--continue` (`fallback_model=prior.model`, H:705–706) resumed on the wrong one.
   The RPC verb keeps its own append because it calls the session, not the backend; the two
   appends never meet.
3. `TauBackend.record_model_change(name)` (new): the append from (2), callable on its own.
   `set_model` calls it; §3 step 9 calls it on a resume whose `--model` differs from the prior.
   It takes the config NAME because the backend holds only the model id (`model_name = model_id`,
   B:1077) and the log records names; the PROVIDER is read off the backend's own
   `config["backend"]`, which is the entry the backend was built from. It returns `None`, not a
   `Performed` (built 2026-09-08): a `Performed` is what a dispatched command produces, its `data`
   is checked against the capability's declared `returns` by `test_performed_records.py`, and this
   is startup bookkeeping no command dispatched.
4. `replay_render_events(messages, *, lane) -> Iterator[dict]` (new, beside `TurnStream`;
   B imports no Textual): projects persisted messages onto the §4 vocabulary so a resumed
   transcript is rendered by the SAME handler as a live one. Details in §4, "Replay".

**cli.py**: `--mode` gains `"repl"` (C:154). A `repl` branch sits beside the rpc block (C:564–602),
BEFORE the `--continue/--session/--fork require --print` gate at C:611–616, which would otherwise
refuse the flags this head accepts. Flags and exact refusals:

| Flag | REPL | Text |
|---|---|---|
| `--model/--provider/--thinking` | accepted | via `resolve_model_config` (H:92–185) |
| `--system-prompt` | accepted for a fresh session | with a prior: the H:698–702 refusal, verbatim |
| `--continue/--session/--fork` | accepted | run_print's semantics (H:696–730) |
| `--name`, `--store`, `--session-dir`, `--no-session` | accepted | consumed by H:675–681, 721–723 |
| `-e/--no-extensions/--ext-config/--bus/--tools/--exclude-tools/--no-tools/-nbt/--max-turns/--append-system-prompt/--no-context-files` | accepted | as `_launch_tui`'s `run_config` (C:446–459) |
| `--resume` | accepted | a numbered picker over `catalog.list(cwd)` (session_catalog.py:203, `SessionInfo` rows :112) read at §3 step 2, BEFORE a backend exists; the pick becomes `prior`. Not `enumerate_domain("session_id")`: that needs a runtime (F:680–687), the runtime needs the session, and the session's model is what step 3 resolves from — a cycle. |
| `-p/--print` | refused | `--mode repl is an interactive prompt loop; -p/--print runs one headless turn and exits. Drop -p, or use --mode text/json for a headless run.` |
| positional messages | refused | `--mode repl reads prompts from its own prompt line, not positional arguments; drop the trailing message text.` |
| `--ui-defaults` | refused | `--ui-defaults auto-answers extension dialogs when no human is present; --mode repl has one at the prompt, so a form is asked there. Drop --ui-defaults (config.json "ui_defaults" is ignored by this mode too).` |
| `--theme` | refused | `--theme selects a TUI stylesheet; --mode repl renders with rich in the terminal's own colours.` |

The TUI does NOT refuse `--ui-defaults`; `_launch_tui` drops it silently with `--no-session`,
`--name` and `--mode` (C:446–459). There is no TUI message to copy, so the text above is new and
the silent drop is recorded in §10. The `--ui-defaults` help text says "Headless (--print) only"
(C:266–269) while rpc consumes it (R:262–268); the repl change corrects that string.

## 3. Startup and shutdown

Mirrors rpc_mode's order (R:221–288), which binds, with run_print's flag handling (H:675–730).

1. `catalog = build_session_catalog(config, args.store, args.session_dir, persist=not args.no_session)` — H:675–681.
2. `prior = select_session(args, catalog)` — H:696; `--no-session` with a continuation flag refused as H:689–693, `--resume` included (it resumes a persisted session too, which print mode never had to say). `--resume`: print `catalog.list(cwd)` numbered (`display_title`, id prefix, last-modified), read one number through the reader (§5), `prior = catalog.load(row.ref)` (`SessionInfo.ref` is what `load` takes, :112–125); an empty list or an empty answer exits with a one-line message, not a fresh session.
3. `model_name, model_config = resolve_model_config(config, args, fallback_model=prior.model if prior else None)` — H:705–706; `--system-prompt` with a prior refused as H:698–702, else injected as H:710–713.
4. `backend = create_backend(model_config)` — H:717 / R:233. `backend.agent_session is None` raises as R:291.
5. Session: fresh → `catalog.create`/`create_ephemeral(cwd, model_name, backend_name, system_prompt=backend.system_prompt, name=args.name)`; `--fork` → `catalog.fork(prior, cwd)`; else `prior` — H:719–730. NOT `_apply_resume_metadata`: see step 9.
6. **`backend.bind_session_log(session)`** — B:1126 / R:255–257. See the paragraph below.
7. `runtime = AgentSessionRuntime(agent_session, catalog, cwd, model_name, backend_name, store_name)` — R:258–260; `runtime.set_rebind_session(rebind)` (RT:310). Needed before `/fork`: `enumerate_domain("path"|"session_id")` raises without it (F:680–687). **`rebind` does three things and re-subscribes nothing**: return the steering buffers to the prompt (§5, `pending` and `delivered`), re-read `pending_request` (§7), and for a `switch_session` replay the new transcript (step 15). A swap keeps the SAME `AgentSession` and replaces only its log (RT:464 `session.session_log = new_log`, after RT:481–495's transient reset), so the resolver of step 8 and the router of step 13 are still bound. **Step 3 built the first of the three** — the callback is installed here and returns the steering buffers to the prompt, since `/fork` and `/resume` became performable with §6; the `pending_request` re-read arrives with §7 and the replay with `replay_render_events`. The TUI's rebind re-subscribes because its no-runtime path recreates the backend (A:2137–2139, A:2142–2179), and it detaches the old router first (A:2101–2103); a rebind that re-ran step 13 without detaching would render every event twice from the first `/fork` on.
8. `agent_session.set_model_resolver(make_model_resolver(config.get("models", {})))` — H:733–734.
9. Resume metadata through backend doors, after 6 and 8: `--name` → `backend.set_session_name(args.name)` (B:1379–1394 → S:850, `session_info` appended by the core); `model_name != prior.model or backend_name != prior.backend` → `backend.record_model_change(model_name)` (§2). Not `backend.set_model(model_name)`: `--thinking` lands on `model_config["thinking"]` (H:140–142) and the backend was built from that, while `ConfigModelResolver.__call__` (B:866–879) rebuilds from the raw config entry, so a `set_model` here would silently drop the flag.
10. `subscribe_session_events(agent_session.route_session_event)` — A:2172; keep the unsubscribe.
11. `backend.set_ui_delegate(ReplDelegate(...))` BEFORE extensions load — A:2286–2288 precede A:2290.
12. `loop.add_signal_handler(SIGINT|SIGTERM, on_interrupt)` — H:754–770, except that `NotImplementedError` is NOT swallowed (H:769–770): the head prints one line saying external SIGINT will not abort a turn, and continues (the key binding in §5 still works).
13. `ext_result = await backend.load_extensions(explicit, discover=not args.no_extensions, extensions_config=resolve_extensions_config(config, parse_ext_config_overrides(args.ext_config)), collect_explicit_errors=True)` — H:778–787 for the arguments, A:2299 for the flag (a bad `-e` is a warning at a prompt, an exit code in print mode). Errors printed as `error` role.
14. `router = backend.subscribe_render(on_render, on_orphan=on_orphan)` — B:1610–1646; once, never per turn and never per swap (B:1598–1604, step 7). `stream_submission` is never called: it subscribes per call (B:1869–1871) and would double-render. Being step 14 and not step 12 has one measured consequence: an `api.send_message` made from an extension's module body, before this line runs, reaches no renderer — the price of loading extensions under a delegate that is already installed, which is the ordering that matters more.
15. `await backend.emit_session_start("startup")` — H:789–790, after 11 and 13.
16. On a resumed session: `for event in replay_render_events(session.context, lane=REPLAY_LANE): on_render(event)` with `elide_attachment_bodies` (attachments.py:575) applied to user text, then §7's `pending_request` check.
17. Loop (§5). Exit on EOF, idle Ctrl+C, `/quit`, or `agent_session.shutdown_requested` (S:644) checked after every submission.
18. Shutdown, in a `finally` (H:854–862 / R:298–307): remove signal handlers; `router.detach()`, `await router.close_all()` (B:403–417); `await runtime.dispose()` (fires `session_shutdown`, RT:416–428) — NOT also `emit_session_shutdown`, which would fire it twice; `await aclose_providers()` on the same loop.

**Bind, do not append.** The TUI binds (RT:461–464 or A:2137–2139) and contains no `append_message`
call: the core persists the user message at S:2899–2908 and the loop's messages at S:3442–3447
once a log is bound. Print mode appends by hand (H:799–805, 849–851, and `_apply_resume_metadata`
H:361–372) precisely because it never binds (H:622–626), and that is why `/compact` is refused
there (H:620–631). A REPL is long-lived and wants `/compact`, `/fork` and `/model` (a `Performed`
from `backend.set_model`, which the TUI cannot use because it recreates the backend, A:2330), so
it binds, passes `context=None` to `submit_turn` (§2 change 1), and appends NOTHING itself — a
rename and a model change on resume go through `set_session_name` and `record_model_change`
(step 9) so the core is the log's only writer. A head that binds AND copies H:799 writes the user
message twice; one that binds and copies H:361 puts a `session_info` entry on the cursor that no
core event announced.

## 4. The render vocabulary

The handler receives the ten kinds below (`RenderHandler`, B:58; `TurnStream` B:120–129, 195–337;
`RenderRouter` B:361–367, 431–457, 506–519, 581–593); the ABC docstring at B:995–997 listed five
and was stale — corrected to all ten in the same commit as step 1, since this head is the first
consumer that must handle every one. Every kind is printed; nothing is dropped, and a kind with no
case RAISES rather than being passed over. `repl_theme.py` is the one place a colour or
glyph is named: `ROLE_STYLE = {"user":…, "assistant":…, "reasoning":…, "tool":…, "tool_error":…,
"blocked":…, "system":…, "extension":…, "foreign":…, "error":…}` and `GLYPH` keyed the same way.
There is no head-agnostic palette to mirror: `tau.tcss` declares no `$tau-role-*` variables and
the TUI's colours stay in its stylesheet (`docs/TUI-STYLE-GUIDE.md` §5).

`agent_end` is NOT in the vocabulary: `TurnStream.feed` returns `[]` for it (B:195–222),
`lane_end` carries no end reason (B:581–593) and `SubmissionResult` has none (submission.py:171–188).
Whether a turn was aborted is known to the head's own state machine (§5) and to
`stop_reason == "aborted"` on the last message of `result.messages`; nothing on the render
stream says it, so the head says it (the `lane_end` row).

| Kind (payload) | REPL prints |
|---|---|
| `lane_start` `{lane, source, submitter, correlation, text}` (B:431–440) | Interactive/human lane: nothing live — the user's line is already in the scrollback; under replay (below) `text` in `user` style with a `›` prefix. Foreign lane: a rule `── {lane_label} ──` in `foreign` style, then `text` as a `user`-styled quote. `lane_label` = `None` for interactive/human, else the WHOLE badge — `"{lane_role(source)} · {submitter}"`, e.g. `Timer · cron:nightly` (A:1925–1936); a branch lane is `source="agent"`, `submitter="fork:{label}"`, key `branch:…` (B:506–519), so it badges as `Sub-agent · fork:review`. Step 5 corrected this row: it read `"source · submitter"` and the renderer composed a second badge around it, printing `Timer · timer · cron:nightly`. |
| `turn_start` `{turn_index}` (B:200–202) | Nothing on turn 0; `↻ turn N` in `system` style from turn 1 (a tool loop is visible without noise on a one-shot answer). |
| `steer_message` `{text}` (B:228–254) | `↳ steer` rule then `text` in `user` style — the only carrier of the question (B:254), so dropping it shows an answer with no question. Also the delivery confirmation: pops the oldest entry of the head's `delivered` list (§5). |
| `reasoning_delta` `{delta}` (B:267–268) | Accumulated per lane; while streaming, the toolbar label reads `thinking… (~N tokens)`; at the next `text_delta`/`tool_call`/`completion_end` the block is printed COLLAPSED and dim: one `reasoning`-styled line `▸ reasoning (~N tokens)`; `/reasoning` toggles expanded printing for later blocks. Head-local, off by default. The count is an ESTIMATE and says so with its `~`: no provider τ speaks reports reasoning tokens separately (§9), so `reasoning_tokens` is the core's own `compaction.estimate_tokens` over the block rather than a second heuristic invented here. |
| `text_delta` `{delta}` (B:264–266) | ONE rule: the unflushed text is shown ONLY in the reader's bottom toolbar (the last `TOOLBAR_TAIL_LINES = 3` lines of it, raw, beside the spinner), and a block is written to the scrollback exactly once, as `console.print(Markdown(block))`, at a fence-aware boundary or at `completion_end`. Nothing raw is ever printed to the scrollback, so nothing is printed twice and nothing has to be rewritten (`rich.live.Live` is not used, §5). A boundary is a `\n\n` OUTSIDE a fence (``` parity tracked over the unflushed text), followed by a line whose first characters are NOT a list marker (`- `, `* `, `N. `), a table pipe, or 4+ spaces of indent; the decision waits for that next line's first characters, so a flush trails the boundary by one line. |
| `tool_call` `{id, name, arguments}` (B:209–219) | Flushes the live block first. ONE line, cut at the terminal width rather than wrapped: `⚙ name(first=…, +N)` — the first argument as `k=v` capped at 80 chars, then how many more there are, since a call's identity is the argument the model wrote first and a wrapped one costs three rows for a record whose point is that it costs one. Expanded (`Syntax`-highlighted JSON) under `/tools verbose`. Also the steering flush trigger (A:2081–2082). |
| `tool_result` `{id, name, result, is_error, blocked, blocked_by}` (B:312–337) | `✓ name — first line (N lines)` in `tool`; `✗ name — first line` in `tool_error` when `is_error`; `⛔ name blocked by {blocked_by}` in `blocked` (the veto record reaches the sink only, X:775–799, so this event is the veto's one surface). Under the header, the body folded to `RESULT_FOLD_LINES = 4` lines, each cut at the width and dim, then `⋯ N more lines` — a fold that does not count what it hid cannot be told from a result that was that short. Full result under `/tools verbose`. |
| `completion_end` `{output, context, stop_reason, dropped_tool_calls}` (B:300–310) | Flushes the live block. NOT the truncation trigger: it fires on both `message_end`s of a completion and restates `stop_reason` on the second (B:289–310), so the TUI's A:2079–2080 notice appears to fire twice (unverified, §9). |
| `lane_end` `{lane, source, submitter, context, output, seconds, cache_notice, extra}` (B:581–593) | A `system`-styled footer, aligned RIGHT so a column of them reads as a margin rather than as more answer, `ctx 12 345 · out 678 · 4.2s · {format_telemetry(extra)}` (seconds omitted when `None`; the telemetry segment omitted when `format_telemetry` returns `None`, as A:2066 / CW:1042 do); when the head is in state `aborting` for this lane, a `⏹ aborted` line in `system` style precedes the footer, and the partial answer IS on the cursor (S:2536–2538 returns the aborted messages persisted) so the next prompt continues from it. A foreign lane closes with `── end {lane_label} ──`. `cache_notice`, when not `None`, is printed once per model per session (the TUI's `_cache_warned_models`, A:208, 1957–1989) followed by H:420–425's advice sentence. |
| `custom_message` `{entry_id, message}` — no `lane` key (B:448–457) | `ROLE_LABELS["custom"]`-style header `Extension` (CW:42–52 — the string is duplicated in `repl_theme.py`, not imported, since CW imports Textual) and the text as plain text, never markup (`docs/EXTENSION-LOCKS.md` §9.1). |
| `on_orphan(reason)` (B:461–469, 536–543, 555–561, 566–570) | `error`-styled `[τ] orphan render event: {reason}` — reported, never dropped. |

**Replay.** `replay_render_events(messages, *, lane)` (§2 change 4) is the projection step 16
runs; it exists because `session.context` is a list of persisted messages and the §4 handler
consumes lane-tagged render events, and the only module that turned one into the other (T,
`transcript.py`) imports Textual at T:6–11. It emits, per user message, `lane_start{text}`; per
assistant message `turn_start{turn_index=k}` (k from 0 within the user turn — the index is not
persisted), one `reasoning_delta` per `thinking` block, one `text_delta` per `text` block, one
`tool_call` per `toolCall` block (the `_harvest_message_end` shape, B:271–310) and
`completion_end` with `stop_reason` and `usage`-derived counts; per `toolResult` message a
`tool_result` (the `_feed_tool_execution_end` shape, B:312–337); per `custom` entry a
`custom_message`; and `lane_end` at the next user message or the end, with `seconds` from the
turn's first and last timestamps — `span_seconds` (T:76) moves next to the projection and T
re-imports it — and `extra` from the last assistant `usage`. The system message prints as one
`system`-styled line `▸ system prompt (N chars)`; `image` blocks print the one line
`docs/FILE-ATTACHMENTS.md` §6 specifies. **Built 2026-09-08**, and the handler has TWO
replay-specific cases rather than the one predicted here: the `lane_start` row's `›` echo, which
`lane_start` now carries a `replay: true` key to ask for, and `system_prompt` — a kind of its own
because no live event announces the prompt, so there was nothing for the size line to ride on.
An image block rides the `text_delta` it interrupts, which needs no case at all. `test_repl_render.py` drives one live turn through a `RenderRouter`,
replays the resulting `session.context`, and asserts the two outputs are equal modulo the `›`
echo, the `seconds` value and the `↻`-less turn 0 — the anti-drift check that makes a second
rendering path acceptable.

**Truncation.** After `await submit_turn` returns, `truncation_notice(truncation_from_messages(result.messages), max_tokens=model_config.get("max_tokens", DEFAULT_MAX_TOKENS))`
(truncation.py:76, 96; H:847 for the cap, safe because B:802–804 refused a bad one) prints once
per turn with H:455–459's advice line. This is the reader print mode uses; the cache reader is
not — `report_cache_miss` builds a fresh observer per call (H:416) with no latch, so a REPL uses
the router's shared observer through `lane_end.cache_notice` (B:400, 572–577).

## 5. Input

**Reader: `prompt_toolkit`**, behind `LineReader` in `repl_input.py`, declared as a new extra
`ffwf-tau-coding-agent[repl] = ["rich", "prompt_toolkit>=3.0.53"]`. It is present in the venv only
as tach's transitive dependency (`pip show prompt_toolkit` → Required-by: tach; no τ pyproject
names tach), so it is a new declared dependency — the one decision in this spec the owner has to
ratify. `rich` stays the only RENDERER; `prompt_toolkit` reads EVERY line, including a form field
and a request answer. The spikes (steer-interrupt analysis, scratchpad `spike_ptk_toolbar.py`)
settled it: stdlib `readline` in an executor hangs `asyncio.run` at idle Ctrl+C (the executor
join blocks in `input()`), interleaves the spinner with the prompt on one line, silently loses
completion under rich's `FileProxy`, and excludes Windows; `prompt_toolkit` with `c-c` bound and
`handle_sigint=False` reached `abort()` with the buffer intact, absorbed a second Ctrl+C, exited
cleanly at idle, and showed rich `Markdown` above the prompt with the spinner in `bottom_toolbar`
— `rich.live.Live` under `patch_stdout` is unusable (the prompt was re-emitted every frame).

```python
class LineReader(Protocol):
    async def read(self, *, default: str = "") -> str | None          # None on EOF; prompt text from the prefix callable
    async def ask(self, question: str, *, default: str = "", validate=None) -> str | None  # §6; None = cancelled
    def set_spinner(self, label: str | None) -> None                  # bottom toolbar, with the live tail (§4)
    def set_prefix(self, text: str) -> None                           # status slots; re-rendered in place, the draft survives
    def set_interrupt(self, handler: Callable[[], bool] | None) -> None  # step 2: the c-c binding; False ends the read
    def set_reclaim(self, supplier: Callable[[], str | None] | None) -> None  # step 2: Up on an EMPTY line, ahead of history
    def set_draft(self, text: str) -> None                            # step 2: text handed back, read outstanding or not
    def draft(self) -> str                                            # buffer.text of a cancelled read
```

Three members the state machine needed and the first draft did not name (step 2, 2026-09-08).
Ctrl+C and Up are the reader's KEYS, so the head installs what each MEANS rather than polling:
`set_interrupt`'s handler answers whether the press was handled — `False` (the idle row below)
ends the outstanding read the way EOF does, `True` (the streaming and aborting rows) leaves it
outstanding with its draft intact, and a press inside `ask` reaches no handler at all, which is
what makes the "inside an `ask`" row true rather than merely intended. `set_draft` is the write
half of `draft()`: reclaimed and refused text has to land in the buffer whether or not a read is
outstanding at that moment, and holding it for the next read is the same gesture as writing it
into the live one. `MemoryReader` scripts both keys as the bytes a terminal sends for them
(`"\x03"`, `"\x1b[A"`) in its `lines`, so a test drives the state table through the same door a
person does. Both handlers are uninstalled in the teardown, since one left behind would abort a
turn of a session that is gone.

Two corrections the build made (2026-09-08). **There is no `history_add`**: a
`PromptSession` appends every accepted line to its own history, so a head calling one
would double-add — history is the reader's, entirely, and the head never mentions it.
And `validate` is a plain `Callable[[str], str | None]` returning a complaint to re-ask
with, not a `prompt_toolkit` `Validator`, so `MemoryReader` implements the protocol
without importing the library the protocol exists to hide; `PromptToolkitReader` runs the
callable itself between reads rather than wrapping it. `set_completer` arrived
with the Tab work (§6) that needs it; a protocol member with no implementation behind it
is a promise, not a seam.

One defect step 3 found in step 1's reader (2026-09-08). `PromptSession.prompt(message=…)`
does not scope that message to the call — it ASSIGNS `self.message` (3.0.53, the
`if message is not None:` block) — so the first `ask` replaced the prompt marker with its
question for the rest of the process, and `--resume`'s picker made every later line read
`resume which session?`. `read()` now passes the marker callable back explicitly on every call.
The same fact is why `ask` cannot carry a per-call completer (§6).

`PromptToolkitReader`: ONE `PromptSession(message=<callable>, history=FileHistory(~/.tau/repl_history),
completer=…, bottom_toolbar=<callable>, refresh_interval=0.1, key_bindings=…)`. `ask` is a MODE of
that session, not a second reader: it is `prompt_async(message=question, default=, validator=,
completer=)` on the same session with the same key bindings, so Ctrl+C inside it is the `c-c`
binding (never a SIGINT into a blocked thread) and returns `None`. There is no `run_in_terminal`,
no executor and no rich `Prompt.ask`: rich's `Console.input` is a blocking builtin `input()` on a
worker thread, `abort()` (S:3328–3355) sets a flag the provider polls and cannot unblock that
thread, and `executor.shutdown(wait=True)` at teardown joins it — the hang §5 rejected `readline`
for, back through a different door. `message` and `bottom_toolbar` are callables, which is what
lets a status slot (§7) or the spinner change the prefix WITHOUT cancelling the outstanding read:
`app.invalidate()` repaints and the draft is untouched. `MemoryReader` (§8) answers from lists.
**The spinner is the reader's in both of its forms** (built 2026-09-08): only one thing may own
a terminal, so while no read is outstanding `set_spinner` drives a `rich` status through the
console the reader holds, and once one is it is the `bottom_toolbar` callable. A head driving a
`Live` of its own against an open prompt is the fight the spike measured; a head that instead
told the reader what to show has no second owner to reconcile. **The code did both** until step 4
(2026-09-08): `set_spinner` started a `rich` status whether or not a prompt was open, and a turn's
first spinner is set while the steering read IS open — so the two forms ran together in exactly
the case the rule exists for. It now asks whether a prompt is running (`_repaint`) and repaints
the toolbar when one is. That is the same finding as `patch_stdout` in §7, twice over: what
reaches the terminal during an open read is the READER's problem, not each caller's.
Input history is head-local (`docs/HEADS-AND-MULTIPLEXER.md` §2); the TUI keeps no file, so
nothing is shared.

**The main loop: one reader, one outstanding read.** At most one `prompt_async` is pending at any
moment, and the loop is `asyncio.wait({turn_task, read_task}, return_when=FIRST_COMPLETED)`:

- Idle: only `read_task` exists. A line → §6 (command) or a turn (`turn_task = create_task(submit_turn(...))`),
  then a new `read_task` is issued immediately so the prompt stays open for steering.
- `read_task` completes while `turn_task` runs: the line is a steer/enqueue candidate (below);
  a new `read_task` is issued.
- `turn_task` completes: post-turn work runs in a `finally` that the exception paragraph below
  describes. If any of it needs an answer (an `extension_request` ask, §7; a `FlowStep` from an
  enqueued command, §6), the outstanding `read_task` is cancelled FIRST, its `buffer.text` is
  kept as `draft`, the question is asked through `reader.ask` (the same session, now free), and
  the next `read(default=draft)` restores the draft. Two prompts never coexist on the tty.
- A `FlowStep` or a delegate `form` (§7) raised at IDLE from a command's own path cancels the
  read the same way; one raised MID-TURN from a hook (X:594–597, reached because the human
  submission carries `allow_user_input=True`, A:869) does the same to the steering read.

**Ctrl+C.** One function `on_interrupt()` is reached from the `c-c` key binding and from the loop
signal handler (§3 step 12). `abort()` is idempotent (S:3328–3355; B:1037–1042).

| State | Ctrl+C | Ctrl+D / EOF |
|---|---|---|
| idle (no lane open, prompt shown) | exit cleanly, exit code 0 — `event.app.exit()` from the binding | exit cleanly |
| streaming (a lane open; prompt open for steering) | `backend.abort()`; state → aborting; `pending` AND `delivered` returned to the editor (below; `docs/TUI-STEERING.md` §4, A:1610) | ignored until idle |
| aborting (abort sent, `lane_end` not yet seen) | no-op; the turn ends with the `⏹ aborted` line and the §4 footer (the head's own marker — nothing on the render stream says "aborted", §4), and `submit_turn` returns `accepted=True` with the aborted messages persisted (S:2536–2538) | ignored |
| inside an `ask` (a form field, a request answer, a flow field), idle or mid-turn | the ask returns `None` — the form/flow is cancelled (X:590 "when a TUI user cancels"), nothing performed; the turn, if one is open, is NOT aborted by this press — the row above applies to the next press, which lands on the re-issued read. Two presses, no hang, no blocked thread. | the ask returns `None` |

**Steering** (`docs/TUI-STEERING.md` §2, lines 52–111; A:837–850, 924–952, 996–1112): a line read
while a lane is open is first peeked with `resolve_command` (K:173) and `REPL_COMMANDS` (§6); a
command is refused with `/x runs between turns` (A:842) and left in the editor, never queued.
Prose goes to the head-owned `pending: list[str]` and is echoed in `user` style with a `⏳` marker.
`steer`: at the next `tool_call` render event from ANY lane (A:2081–2082) the buffer is joined
with `"\n\n"` (A:1028), `@file` expanded at that point (A:1031), and submitted as
`Submission(source="interactive", submitter="human", multitask_strategy="steer",
expand_commands=False, allow_user_input=True)` (A:1032–1041); `accepted=True` with non-empty
`messages` means the turn had already ended and it ran as its own turn (A:1086–1089). `enqueue`,
and any buffer left at `lane_end` under either strategy, is submitted as an ordinary turn at idle
(A:1097–1112). The strategy comes from `configured_steering_strategy(config)`, which raises
`ConfigError` on an unknown value at startup (A:338–342).

**`delivered`.** A `steer` submission accepted with `messages=[]` (S:2429–2430) is parked in the
core's `_pending_steer_messages` until the loop weaves it in, and `abort()` clears that list
(S:3353) without telling anyone. So the head keeps the raw joined text in `delivered: list[str]`
(one entry per accepted steer submission, FIFO) until the `steer_message` render event on that
lane (B:254) confirms the weave and pops it. Abort reclaims `pending + delivered`, in that order,
ahead of the draft (A:962–982's ordering); a session swap reclaims the same way. The TUI reclaims
only its own buffer (A:1610) and loses the delivered line — a TUI defect recorded in §10, not
copied. **Reclaim**: Up on an empty line — a key binding conditioned on `buffer.text == ""` —
puts the joined `pending` back (A:940–952).

**An exception out of a turn.** `submit_turn` can raise: a provider `ErrorEvent` becomes a
`RuntimeError` out of `AgentLoop` (B:482–483), and the TUI catches it at A:1576–1586 (a notice,
a verbatim system box, then `_settle_submission`). Here every `await submit_turn`/`submit_command`
sits in `try/except Exception` — never `BaseException`, so `CancelledError` and a real
`KeyboardInterrupt` still unwind — printing `[τ] turn failed: {type}: {exc}` in `error` style and
the traceback as a `Syntax("python")` block, then continues to the next prompt. The post-turn
settle runs in a `finally` regardless: state → idle, `pending_request` re-read (A:1111), the
enqueue flush (A:1112), the `shutdown_requested` check (§3 step 17). Only §3 step 17's four
causes exit the process; a 5xx on turn 30 is not one of them.

## 6. Commands and flows

**Two vocabularies, head-local first.** `REPL_COMMANDS = {"quit", "reasoning", "tools"}` — plus
`"panel"`, which arrives with §7 in step 4, since a `/panel key n` with no panel registry behind
it is a name with no handler —
are display and process toggles this head invents; none is in `FRONTEND_COMMANDS` (K:117 —
`autocompact compact disable_extension enable_extension extensions fork model name
reload_extension resume tree`), so `resolve_command` (K:191–199) returns `None` for them and by
the rule below `None` is a prompt. They are therefore checked BEFORE `resolve_command`, by name,
and they are fed to the completer merged into the `{name: desc}` mapping `complete_command`
(K:296) takes, so the Tab line never calls them unknown (`docs/SLASH-COMMANDS.md` §3's promise
holds for them). `test_repl_commands.py` asserts `REPL_COMMANDS.isdisjoint(FRONTEND_COMMANDS)`
so a later built-in cannot be shadowed silently; an extension registering one of these four
names loses to the head, the same order K:183–186 gives τ's built-ins over extensions.
`/quit` exits (§3 step 17); `/reasoning` and `/tools verbose|compact` flip §4's toggles;
`/panel key n` is §7's action press. None of the four touches the session, which is why a head
may own them (`docs/HEADS-AND-MULTIPLEXER.md` §2: display is the head's). Each refuses stray text
(`/reasoning please`, `/tools sideways`) rather than running and discarding it — the head owns
these words, so `docs/SLASH-COMMANDS.md` §4's unfixed defect for τ's own commands is not repeated
here. `/reasoning` needed one change in §4: the renderer accumulates the reasoning TEXT rather
than a character count, since the count cannot be expanded later.

Every other line at idle: `resolve_command(text, extension_names)` (K:173–199). `None` → §5's
`@file` expansion (`scan_attachments`/`render_attachments`, attachments.py:258, 354; failures
printed) and the A:861–870 submission via `submit_turn(sub, None)`. The expansion happens at
submission-build time in `_run_turn` and again at the steering delivery point in
`_deliver_steer`, whose buffer keeps the RAW line — reclaimed text must be expandable exactly
once (A:1048–1056's `raw_text`). The two size limits are read with `attachment_inline_limit(config)`
and `max_image_dimension(config)`, which refuse a non-positive value at STARTUP beside
`configured_steering_strategy`; they are a second copy of A:374–407, because app.py is frozen for
this work and a shared home for them would leave that copy unused — the `steering.py` treatment
is not available twice. A command → the same
`Submission` through `submit_command` (B:1648–1659) and the four outcomes of A:1272–1333 /
H:554–599: `ValueError`/`UnsupportedCommandError` → one `error` line; `accepted=False` →
`rejection_reason` (a lock refusal carries `result.lock`, submission.py:186–188); `command is None`
→ raise (an input hook ran a turn unrendered, A:1323–1330). Then `_perform_command_outcome`
(A:1335–1379):

- `Performed` → **`summary()` (F:169–189), or `data["output"]` as Markdown when the capability
  returned one**; there is no `output_text()` — the first draft named a method F has never had.
  Re-read `pending_request` afterwards (A:2529–2530, step 4).
- `FlowStep` → the loop below.
- `Ready` → `perform_ready` (A:1477–1551), with **three** head actions, not four:
  `runtime.fork()`, `runtime.switch_session(id)` and — the built correction —
  **`agent_session.compact(custom_instructions)` rather than `backend.compact_messages`**. That
  method returns a shortened LIST for a head that owns its working list; this head BINDS its log
  and passes `context=None`, so the list would be discarded milliseconds later and the session
  would be compacted in name only. `AgentSession.compact` appends the compaction entry the bound
  log reads through, which is what the RPC head (the other binding head) already calls.
  `set_model` needs no case at all: the generic branch below reaches `backend.set_model`, which
  returns a `Performed` and records the change (§2). `ready.flow in vocabulary.extension_flows` →
  `run_extension_command(flow, " ".join(str(v) for v in arguments.values()))` (A:1519–1522) —
  acceptable here and only here, because the submission that produced this `Ready` already
  passed the input hooks and the lock check; else
  `await getattr(backend, ready.mutation)(**ready.arguments)` must return `Performed` or raise
  (A:1524–1543).
- `View` → both views are absent here (tree browser is a non-goal; `/extensions` prints the
  listing from `summarize_extensions` instead of opening one), so `extensions`
  prints and `tree` raises `UnsupportedCommandError(unsupported_command_message(name, "the REPL
  (tau --mode repl)"))` (K:485–504, H:638). Never sent to the model (`docs/TUI-STYLE-GUIDE.md` §2.5).
  The listing is built from **`agent_session.get_extension_state()`** (S:1299), not from the
  `LoadExtensionsResult` step 13 returned: `reload_extension` REPLACES the loaded set, and the
  TUI's cached snapshot showing a pre-reload tool list is the defect that read exists for.

**One orphan pair per `/compact`, and it is correct.** `AgentSession.compact` emits
`agent_start`/`agent_end` with no `submission_id`, so `RenderRouter.on_agent_event` sends both to
`on_orphan` — its own docstring names this case (B:461–469). This head prints them, because §4's
rule is that nothing is dropped. It reads as noise beside a compaction the reader asked for, and
the alternative — muting orphans for the duration of a call — is a renderer that cannot be
distinguished from one that has quietly stopped working. Recorded here rather than worked around.

**Flow loop** (`docs/TUI-STYLE-GUIDE.md` §2.2; A:1428–1475): `vocabulary = agent_session.vocabulary`
read per use (S:3833; A:1144–1154). For a `FlowStep`: `select_options(step)` (A:1381–1425) calls
`enumerate_domain(domain, session=, runtime=, scope=, cursor=, vocabulary=)` (F:609) for each
`select` domain and builds `{argument: {label: value}}`, raising on a label collision; then
`flow_form_spec(flow, bound, options=, vocabulary=)` (F:357) → fields asked one by one through
`reader.ask` (§5: the one session, a cancelled steering read, the draft restored after); answers
mapped label → value (A:1465–1467); `next_step(flow, bound, cursor=, vocabulary=)` (F:279) →
`Ready` performed, a second `FlowStep` raises (A:1472–1475). Cancel is Ctrl+C (`ask` → `None`) →
nothing performed. **Corrected in the build (2026-09-08):** an empty answer was also a cancel, and
`validate_form_spec` has no `required` key — so a form with one optional-in-practice box could not
be submitted at all, and blanking it threw away the fields already typed. An empty answer is now
the field's VALUE, falling back exactly as the TUI's untouched widget does (`_FieldForm._collect`,
modals.py:206–238): `""` for a text box, the declared default for a number, the default or the
first option for a select, `[]` for a multiselect.

| `field_kind` | control — rich PRINTS the choices, `prompt_toolkit` READS the answer (`docs/TUI-STYLE-GUIDE.md` §2.3 baseline; a richer control is allowed, never a poorer one, §2.4) |
|---|---|
| `text` | `ask(label, default=)`. |
| `number` | `ask` with a validator accepting what parses as `int` else `float`; the answer is coerced to that type — the modals.py:207–238 types, which `validate_form_values` (X:172) checks without coercion. |
| `confirm` | `ask(label, default="y"|"n")` with a `y/n` validator → `bool`. |
| `select` | options printed as a numbered list; `ask` with a validator over the indices; the chosen option STRING is returned (X:594–597 hands back the displayed string). A declared default pre-fills as its 1-based NUMBER, not its label: `ask` re-prefills the same default after every complaint, so a label — which this field's own validator rejects — is an unbreakable re-ask loop. |
| `multiselect` | the numbered list; `ask` with a validator accepting comma-separated numbers in range, de-duplicated → `list[str]`. |

The table is one function, `ask_form(reader, renderer, spec)`, taking a spec
`validate_form_spec` accepts — so §7's `ui.form` reuses it rather than growing a second
renderer. There is no `WordCompleter` of the labels: `prompt_toolkit`'s `prompt(completer=)`
OVERWRITES the session's completer permanently rather than for one call (measured, 3.0.53), so a
per-ask completer would leave the next command line completing colour names; the numbered list is
what the answer is validated against instead.

**`session_id` is asked as a `select`, not as a text box that happens to print a list.** The
first draft had the field stay `text` with the enumeration printed above it, which offers numbers
and accepts something else — a reader typing `2` would have reached `resolve_ref` with the
literal string. `PICKER_DOMAINS` names the domains a head re-kinds, `_offer_pickers` rewrites the
field to `select` over the enumerated labels, and the label→value map (built by `_select_options`
for every select AND every picker domain, refusing a duplicate label) turns the pick back into
the id. That is §2.4's richer-not-poorer rule applied where the TUI applies its picker
(A:1445–1447), and it means one control table covers both.

`flow_form_spec` raises for a select with no options (F:420–428) and `_offer_pickers` raises the
same way for a picker domain that enumerated nothing; both are caught in the flow loop and
printed as one `/flow: reason` line, the way A:1449–1451 notifies — a domain that cannot be
listed is a refusal, not a traceback.

**Tab.** `ReplCompleter` reads three vocabularies in order (`docs/SLASH-COMMANDS.md`
§3): `complete_attachment(text, cursor, cwd=)` (attachments.py:494) when the cursor is inside
`@…`; else `complete_command_argument(text, vocabulary)` (K:386) → `ArgumentSlot`, values from
`enumerate_domain(slot.domain.name, …, query=slot.query)` replacing `[start:end]`, a `ValueError`
shown as the completion's meta text (A:1181–1192) rather than swallowed; else
`complete_command(text, {**extension_commands, **REPL_COMMAND_DESCRIPTIONS})`
(K:296) replacing the first word, with an empty match list shown as the one warning line "not a
command τ knows; it will be sent as text".

Two things the build fixed. **Every span ends at the cursor**, because the completer is asked
about `line[:cursor]`: `prompt_toolkit`'s `Completion` carries only a `start_position` and
`Buffer.apply_completion` deletes BEFORE the cursor, so a candidate claiming to replace text
after it would silently insert instead — completing the word to the left of the point is what a
shell does anyway. And the command WORD is offered only while the cursor is still inside it
(`" " not in the first word`): `complete_command` keeps matching a completed first word, and
offering to rewrite `/tre` while someone types its argument is a completion aimed at the wrong
span. The head-neutral `Candidate` (`text`, `start`, `display`, `meta`) is what crosses the
`LineReader` seam, so `MemoryReader` can be Tab-ed in a test with no library present.

## 7. Extension surfaces

**`ReplDelegate`** (four methods, XU:20–59; consumed at X:643–645, 596, 693–695, 756–758):

- `notify(message, level="info")` → one line in the level's own style, `info` being the
  `extension` role and `warning`/`error` being themselves (three of `repl_theme`'s roles ARE the
  three levels, so the level picks a style rather than a second colour table). A level the palette
  does not name is printed as a warning WITH its name: the level is information about the message.
  **`patch_stdout` is the READER's and not this method's** (built 2026-09-08): everything the head
  prints while a prompt is open — a streamed block, a tool line, this notify — has to land above
  it, and the render subscription that prints most of it knows nothing about whether a line is
  being typed, so `PromptToolkitReader.read`/`ask` wrap the outstanding `prompt_async` instead.
- `async form(spec)` → `validate_form_spec` already ran (X:559); prints `spec["title"]` as one
  `extension` line — a frame would enclose nothing, since the fields are asked one at a time
  BELOW it and the answers scroll past as they are typed — then asks each field with §6's table
  through `reader.ask` (§5: the steering
  read is cancelled first and its draft restored after); returns `{name: value}` or `None` on
  cancel. Reached only when the driving submission carries `allow_user_input=True` (X:491–505) —
  every human submission here does (A:869). A bus/timer submission under this head has no human
  policy and raises `HeadlessDialogError` (X:526–557); the message names the cause and is printed
  as `error`.
- `set_status(key, text|None)` → an ordered `dict` shown in the prompt prefix: `[key: text] › `;
  `None` removes the slot (XU:86–97 semantics). The prefix is a callable (§5), so the change
  repaints in place and the half-typed line survives.
- `panel(key, spec|None)` → the normalized `{title, body, actions}` (X:755) printed as a rich
  `Panel` with `render_panel_body` (ported, §2) whenever it is SET; `None` prints `panel key
  removed`. Actions are listed inside the frame as `n) label → /command args` above one line
  saying `press one with /panel <key> <number>`, and pressed with `/panel key n`,
  which submits `Submission(text=f"/{command} {args}", source="interactive", submitter="human",
  expand_commands=True, allow_user_input=True)` through `submit_command` — the one door — and
  NOT `run_extension_command(command, args)` (S:3960–3992 calls the handler and nothing else).
  The TUI does dispatch directly (A:723–734); this head does not, because the door is what runs
  the input-hook chain (S:2041–2052) and the lock refusal (S:2496–2504). **What the door decides
  is not what the first draft said it would decide** (measured 2026-09-08): a command resolves
  INSIDE `_apply_input_pipeline` and returns before the lock check (S:2490–2504, the comment
  there), so a panel press is ADMITTED under a lock where a typed prose line is bounced — the
  reason to go through the door is the hook chain and whatever the door itself refuses, not a
  refusal this press was expected to earn. The press therefore checks ONE thing itself, the same
  check the ask path makes (`_open_ask`): that a loaded extension registered the command the
  action names. `validate_panel_actions` never checks registration, and `resolve_command` answers
  `None` for a name nothing registered — so an unchecked press sends the literal `/gate-approve
  now` to the MODEL as prose, and the head then blames an `input` hook that did not run. §8's test row said "refused under a lock" and now
  asserts the honest thing: a refusal the door gives is printed rather than swallowed. The TUI
  has no listing gesture (its panels are mounted, A:623), so there is no `/panels` command —
  print on change.

**`extension_request`** (`docs/EXTENSION-LOCKS.md` §9, whose closing paragraph licenses exactly
this: a CLI prints the sentence and the choices and takes a numbered answer). Read
`backend.pending_request` (B:1260–1267 → L:203–228) at every cursor move: after `lane_end`, after
any command, on resume, on a swap (A:1110–1111, 1333). When present: print `request.label`
(L:66–80) and `sentence` in `extension` style; body via `render_panel_body`; a lock without an ask
prints `refusal_reason(request)` (L:244–258) and the three escapes (§6 of the doc). An ask:
fields via §6's table, then a numbered action list, each read through `reader.ask` with the
steering read cancelled and its draft restored (§5); `answer_request(request.entry_id,
action_label, values)` (B:1269–1273 → S:3607–3655); `ValueError` printed; `handled=False` printed
as a warning (the lock is released regardless, S:3626–3628); `output_text()` printed. Empty answer
or Ctrl+C = dismiss; the request is shown again at the next prompt. Auto-open only when every
action's command is registered (A:682–684). A prose line typed under a lock is bounced before
submission with `refusal_reason` and left in the editor (A:882–900) — the core would refuse it
anyway (S:2496–2504).

Three things the build settled (2026-09-08). **Drawing a request and opening its ask are ONE
step here**, where the TUI draws a row and opens the ask on a click (A:663): a transcript row is
clickable and a scrollback line is not, so a request printed without being asked would be one
nothing could answer. **A lock without an ask prints `refusal_reason` ALONE** rather than the
label, the sentence and then the reason, because that function is already all three plus the way
out — printing both says each of them twice. And **the field asker is split in two**: an ask
carries a validated field list that may be EMPTY (`validate_ask_spec` normalizes a missing
`fields` to `[]`) where `validate_form_spec` requires a non-empty list, so `ask_fields` takes the
list and `ask_form` takes the spec around it; no fields is an empty answer set, never a
cancellation.

**Commands and shortcuts.** `/extensions` prints `summarize_extensions` plus
`get_extension_shortcuts()` as `ctrl+e <key> → /command args — desc` (S:3911); a shortcut is an
accelerator over a registered command (X:2212–2213), so this head binds no chord — `ctrl+e` is
readline's end-of-line — and a shortcut is used by typing its `/command args`, which goes through
`submit_command` like any other line. Extension commands come from `get_extension_commands()`
(S:3898) and are dispatched through `submit_command` like built-ins. Nothing in this head calls
`run_extension_command` except the `Ready` branch of §6, whose submission already passed the door.

**Records.** No record sink is installed: a delegate wins over the sink (X:643, 693, 756), so
`veto`/`constraints` (X:775–829) would be invisible either way; vetoes render from
`tool_result.blocked` (§4). Extension load errors print as `warning` (S:3744–3766). Re-verified
in step 4 (X:598–609, 643–659, 696–705, 759–769): the sink is the `--mode json` record family and
nothing else, and each of the four surfaces checks the delegate FIRST, so installing one replaces
both the JSON record and the plain stderr line rather than adding to them. A record on stderr
under this head would therefore be a surface the delegate failed to render — which is why there
is no stderr path here at all.

## 8. Tests

All under `tau-coding-agent/tests/`, using `tau_home`/`make_app`'s isolation from `conftest.py`
(52–63: `TAU_DIR` under `tmp_path`, never `~/.tau/config.json`) and a fake backend the way
`test_headless_lifecycle.py:46–92` builds `_LifecycleBackend` and installs it over
`tau_coding_agent.backends.create_backend` (:105–108). No tty exists under pytest, so every test
runs `run_repl` with `MemoryReader(lines=[…], answers=[…])` injected through a `reader=` keyword
and asserts on a `rich.console.Console(record=True)`.

| File | Asserts |
|---|---|
| `test_repl_cli.py` | `--mode repl` dispatches to `run_repl`; each §2 refusal text verbatim; `--continue` no longer trips C:611–616; `--ui-defaults` help text changed; `--resume` lists `catalog.list(cwd)` and loads the pick before any backend is built. |
| `test_repl_lifecycle.py` | the §3 order (delegate before `load_extensions`, `session_start` after, `subscribe_render` once, `dispose` once, `aclose_providers` last); `bind_session_log` called; NO `append_*` call on the session from the head (a resume with `--name` and a different `--model` goes through `set_session_name` and `record_model_change`); a `/fork` and a `/resume` swap call `subscribe_render` zero more times and `detach` zero times; `NotImplementedError` from `add_signal_handler` prints its line. |
| `test_repl_render.py` | each of the ten kinds through a real `RenderRouter` (as `test_render_router.py:199–426` drives one) yields the §4 text; a foreign lane's rule; an orphan line; footer arithmetic including the `format_telemetry` segment and that the footer sits at the right margin; `cache_notice` once per model; the compact forms (a call showing its first argument and counting the rest, a line cut rather than wrapped, a result folded with its count, a reasoning block collapsed to `~N tokens` with the text itself absent); the streaming rule: a fenced block with a blank line and a two-item list across a blank line each reach the scrollback as ONE Markdown block, and no raw delta is ever in the recorded scrollback; the replay parity check (§4, "Replay"). |
| `test_repl_theme.py` | the palette is the ONE place a colour is named: an AST scan of `repl.py` and `repl_input.py` for a style word in any string but a docstring, asserted empty — the claim decays silently otherwise, since an inline `style="bold red"` renders perfectly and is found only when someone wants to change the theme. Plus the head's other silent-decay claim: importing `repl.py` in a FRESH interpreter leaves `textual` out of `sys.modules` (this one's has a TUI test's imports in it). And `ROLE_STYLE`/`GLYPH` keyed identically, and the three badge forms of `lane_label`. |
| `test_repl_truncation.py` | `stop_reason="length"` in `result.messages` prints the notice once with the configured cap; `aborted` prints none (truncation.py:63); a `cache_notice` on `lane_end` prints once per model with the dialect advice; and print mode's stderr carries the SAME advice sentence — the anti-drift check on the two strings step 4 lifted out of `report_truncation`/`report_cache_miss` (`TRUNCATION_ADVICE`, `cache_dialect_advice`), which is all that was extracted: both reporters keep their bodies, so `run_print` is byte-identical in behaviour. |
| `test_repl_interrupt.py` | the §5 state table: idle Ctrl+C → clean return 0; streaming → `abort` called once, `pending` and `delivered` reclaimed in order; two presses mid-abort → `abort` called once more, no hang (timeout-guarded); Ctrl+C inside an ask → the ask returns `None`, `abort` NOT called, the turn continues; `lane_end` while aborting prints `⏹ aborted`. |
| `test_repl_steering.py` | `steer`: a line mid-turn is delivered at `tool_call` as one `steer` submission joined with `\n\n`, `expand_commands=False`; the entry sits in `delivered` until `steer_message`, and an abort before it reclaims the text; `enqueue`: at idle; a command mid-turn refused, not queued; unknown `steering_strategy` raises at startup; the `steering.py`/A:108–112 anti-drift equality. |
| `test_repl_commands.py` | `Performed` printed (its `output`, else its `summary()`); `View("tree")` → `UnsupportedCommandError` message with nothing submitted to the model, `View("extensions")` → the listing; `Ready` performed through the backend method it names and refused when the backend has none; `/compact` reaching `AgentSession.compact`; `command is None` reported; a lock refusal shows `rejection_reason`; `REPL_COMMANDS.isdisjoint(FRONTEND_COMMANDS)`; the three head words never submitted anywhere and each refusing stray text; a `RuntimeError` out of `submit_turn` prints the `error` block and the next line is still read. |
| `test_repl_flows.py` | each `field_kind` asked with its own control and value type (`bool`, `int`, `float`, `list`) through `ask_form`; a select refusing an answer outside its options and re-asking; an extension-declared flow driven end to end with label→value translation into `run_extension_command`; a colliding label refused before anything is asked; cancel performs nothing; `session_id` asked as a numbered pick and performed through `runtime.switch_session`; a vetoed swap reported as one. A second `FlowStep` after an answered form is unreachable with today's registry (the form asks for exactly the set `next_step` blocks on) — the raise stays, untested. |
| `test_repl_completion.py` | the command word matching `complete_command`'s for three prefixes; this head's words offered beside the built-ins; the one warning line for a `/…` that names nothing; an argument value replacing the span the core named; an unlistable domain surfacing as meta text; `@…` beating the other two; nothing for prose; the completer installed at startup and removed at teardown. |
| `test_repl_attachments.py` | a `@file` expanded into the prompt with the `@word` left where it was typed; an unresolved `@word` left as prose; a steering line expanded at its DELIVERY point; a bad `attachment_inline_limit` refused before a backend exists. |
| `test_repl_extension_ui.py` | the four delegate methods: notify per level (by GLYPH, which survives a pipe where a colour does not) and an unknown level keeping its name, form kinds/cancel, status slots in the prefix in first-seen order and never a write to the draft, panel printed on set/removed on `None`; `/panel key n` produces a `submit_command` call with `text="/cmd args"` and NO `run_extension_command` call, and a refusal the door gives is printed (see §7: a command is exempt from a lock by placement, so the press is not the thing a lock refuses); a prose line under a lock bounced and left in the draft; an ask answered through `answer_request`; a dismissed ask answering nothing; `handled=False` warns; an ask whose actions name no loaded command explained rather than asked; the shortcut listing; and the `render_panel_body` port compared against XU's original over all three body kinds. |
| `test_headless_*.py` | unchanged and green after the `select_session` rename. |
| `test_backends_*.py` (existing files, new cases) | `submit_turn(sub, None)` reaches `AgentSession.submit` with `context=None`; `TauBackend.set_model` appends one `model_change` through the bound log and raises `RuntimeError` when the log has no `append_model_change`; `record_model_change` alone appends without switching. |

## 9. Findings

The experiment's result. Each claim says how it is known: **measured** (a command was run and read),
**read** (a file or page at the cited line), **inferred** (neither). The head is 3 350 lines in four
new modules and 159 tests, all green (measured, `wc -l`; `pytest tau-coding-agent/tests -q`).

### 9.1 The seven requirements of §1 (measured: `pytest …/test_repl_*.py -q` → 159 passed)

| # | Requirement | State | The test that shows it |
|---|---|---|---|
| 1 | A head: catalog, extensions, lifecycle, one door, ONE `subscribe_render` | met | `test_repl_lifecycle.py::test_startup_and_shutdown_run_in_the_documented_order`, `::test_the_render_subscription_is_taken_once`, `::test_the_head_binds_and_appends_nothing`, `::test_a_typed_line_becomes_the_documented_submission` |
| 2 | Streaming Markdown, spinner, compact blocks, one palette | met | `test_repl_render.py` (24 tests: `::test_a_whole_turn_renders_in_order`, `::test_no_raw_delta_reaches_the_scrollback_twice`, `::test_a_call_shows_its_first_argument_and_counts_the_rest`, `::test_a_replayed_turn_reads_as_the_live_one_did`) and `test_repl_theme.py::test_no_module_but_the_palette_names_a_colour` |
| 3 | Ctrl+C: abort, exit at idle, never hang | met | `test_repl_interrupt.py` (10 tests, each timeout-guarded: `::test_a_second_press_mid_abort_does_nothing_and_does_not_hang`, `::test_a_cancelled_ask_does_not_abort_the_turn`) |
| 4 | Steering per `docs/TUI-STEERING.md` §2 | met | `test_repl_steering.py` (14 tests, both delivery points, plus `::test_the_strategy_tables_have_not_drifted_from_the_tui`) |
| 5 | Extension surfaces | met | `test_repl_extension_ui.py` (22 tests: delegate ×4, `/panel key n` through the door, both request states, the shortcut listing) and `test_repl_truncation.py` (7) |
| 6 | Slash commands and flows | met | `test_repl_commands.py` (15, all four arms) and `test_repl_flows.py` (14, every `field_kind`) |
| 7 | Tab completion, `@file`, input history | **two of three** | `test_repl_completion.py` (8) and `test_repl_attachments.py` (4). **History has no test**: `PromptToolkitReader` persists it to `~/.tau/repl_history` (read, `repl_input.py:269–309`; `repl.py:1450`), `MemoryReader` has none, and no test drives a real reader — the one requirement resting on inspection alone. |

### 9.2 The docs-only reader's 45 gaps

**35 were real** — the build read source, and every step's commit cites `file:line` for what it
found (read). **4 were answered outright** by a page the reader did not use and **6 in part**
(measured: one `grep -rn` per gap over `docs/`, each hit then read):

| Gap | Answered at | What the reader still had to read |
|---|---|---|
| 3 (file-store catalog) | `CLI-PLAN.md:320` — `FileSessionCatalog(base_dir=DIR)` | nothing; `store_factory.build_session_catalog` is the better door and is one grep away |
| 21 (`subscribe_session_events`) | `library/reference/sessions.md:866–879` — the dict shape AND the exact call `subscribe_session_events(agent_session.route_session_event)` | nothing |
| 23 (`SubmissionSource`) | `SUBMISSION-LIFECYCLE.md:95–99` — the Literal, with `"interactive"` commented *a human at a frontend* | nothing; the head needs no new member |
| 45 (`shutdown_requested`) | `library/reference/extensions.md:1103–1110` — `ctx.shutdown()` sets it and fires the hook | nothing |
| 7 (delta shape) | `REMOTE-CONTROL.md:366` E1 — `message_update` carries a **delta**, never the cumulative message | the thinking-delta and `tool_execution_update` halves: real |
| 12 (`AgentSessionRuntime`) | `library/reference/rpc.md:83` (what it backs) + `sessions.md:4697` (why `enumerate_domain` raises without one) | the six-positional constructor: real (`repl.py:1510`) |
| 17 (abort) | `library/reference/rpc.md:101` — abort is A SIGNAL | the second-press and mid-unwind semantics: real, and answered by `S:3328–3355` |
| 24 (`append_model_change`) | `SESSION-UX-REDESIGN.md:236` (signature) + `RPC-TIER-B.md:133` (raise if the log has none) | *which head calls it*: real, and the answer was **none did** — §11 finding 10 |
| 29 (`end_reason`) | `RPC-PROTOCOL.md:2176` — all six values, each explained | how a head *words* them: real, and there is still no shared reader (§9.6) |
| 31 (`max_tokens` = 4096) | `TRUNCATED-TOOL-CALLS.md:56` | the rest of `Model`: real — `messages.md` is largely "(no description)" |

The 35 real ones cluster, and the cluster is the finding: **the backend seam** (5, 6, 11, 12, 26,
30 — the surface a head calls is documented nowhere, and `subscribe_render`'s own docstring said
*five* render kinds where the router emits **ten**: measured, corrected in `e00e1d5`); **the two
new pure readers** (15, 16 — `prompt_cache` and `truncation` appear **zero** times in
`docs/library/reference/`, measured, because `@agent_facing` was never applied to them); **the
head-local decisions the docs deliberately do not make** (19, 20, 41, 42); and **stale or absent
docstrings** (28, 31, 34 — 28 confirmed: `extensions.md` still names `confirm`/`select`/`input`,
removed in `EXTENSION-LOCKS.md` §8.2).

### 9.3 Nine sentences the docs should gain

Each would have removed a gap; the page names where it goes.

1. `HEADS-AND-MULTIPLEXER.md` §1 (gaps 5, 6): "A head calls five `TauBackend` methods and no
   others — `submit_turn(submission, context)`, `submit_command(submission)`, `abort()`,
   `bind_session_log(session)` and `subscribe_render(handler) -> unsubscribe`, whose docstring is
   the complete render vocabulary: TEN kinds and their payload keys."
2. `HEADS-AND-MULTIPLEXER.md` §2 (gaps 41, 42): "Input history, the palette and the transcript
   window are head-local: τ ships no shared history file, no role palette and no windowing
   helper, and a second head invents its own rather than looking for one."
3. `CLI-PLAN.md` §2 (gap 1): "`cli.main` dispatches on `args.mode` to one coroutine per mode,
   each `(args: CLIArgs, config: dict) -> int`; `headless.select_session(args, catalog)` is the
   shared translation of `--continue/--session/--fork` into a loaded `ConversationSession`."
4. `tau-coding-agent.md` (gaps 2, 18): "`config.load_config()` reads `~/.tau/config.json`,
   `headless.resolve_model_config(config, args, fallback_model=)` returns `(name, model_config)`
   and `backends.build_model_from_config(entry)` builds one `models.<name>` entry into a `Model`;
   the startup order is load-bearing — `set_ui_delegate` and `set_model_resolver` both precede
   `load_extensions` (a `register` handler may call `ui.notify` or `ctx.set_model`), and
   `emit_session_start` follows both."
5. `TRUNCATED-TOOL-CALLS.md` §3.1 / `PROMPT-CACHING.md` §7 (gaps 15, 16): "The readers are
   `truncation_from_messages(messages) -> Truncation(completions, dropped_tool_calls)`,
   `truncation_notice(truncation, max_tokens)` and `cache_miss_reason(completions, prefix)`;
   both modules are unmarked, so they have no reference page — read the module."
6. `EXTENSION-LOCKS.md` §5 (gaps 8, 9): "`SubmissionResult.command` carries the `Dispatched`
   union and `.rejection_reason` the lock's sentence; `AgentSession.pending_request` returns an
   `ExtensionRequest`, and `refusal_reason(request)` is the line to print when it declares no ask."
7. `TUI-STYLE-GUIDE.md` §2 (gaps 11, 14): "A `Ready` is performed by the method the head's own
   table names — a head lacking one refuses rather than guesses; the UI delegate is three
   synchronous methods (`notify`, `set_status`, `panel`) and one coroutine (`async form`), all
   called on the session loop."
8. `SUBMISSION-LIFECYCLE.md` (gap 33): "A malformed command argument raises `ValueError` out of
   `submit()` and an unperformable one `UnsupportedCommandError`; those two are the whole
   exception contract of the one door."
9. `EXTENSION-FLOWS.md` §7 (gap 13): "`Vocabulary`, `BUILTIN` and `FlowDeclaration` import from
    `tau_agent_core.capabilities`; a head that loads no extensions passes `BUILTIN`."

### 9.4 What had to be extracted, and what could not be

How much of the "head-agnostic" stack was head-agnostic in name (read, measured against the diff
`356072a..HEAD`):

- **`headless.py` — everything needed was reusable after a rename.** `_select_session` →
  `select_session`, `_extension_command_names` → `extension_command_names`,
  `refuse_unperformable` split out of `_perform_command_outcome`, the two advice sentences lifted
  into `TRUNCATION_ADVICE` / `cache_dialect_advice`. No logic moved; `resolve_model_config`,
  `parse_ext_config_overrides` and `resolve_extensions_config` were already public. `run_print` is
  byte-identical in behaviour (measured: `test_headless_*.py` green).
- **`transcript.py` — one move:** `span_seconds` to `backends.py`, where `replay_render_events`
  now sits beside it; `transcript.py` re-imports it.
- **`extension_ui.py` — a copy**, because the module imports Textual at line 4:
  `render_panel_body` is ported into `repl.py`, with a test rendering all three body kinds
  through both.
- **`app.py` — nothing could be taken.** `steering.py` re-derives the strategy table (A:108–112)
  and the validator (A:318–343) and pins them with an equality test: two copies of one fact.
- **`chat_widgets.py` — unreachable.** `format_telemetry` stayed, so the footer carries no
  telemetry segment (measured: `test_repl_render.py:295` asserts it ends `ctx 0 · out 0`).

So: all of what this head needed from `headless.py` was reusable and none of what lived in a
Textual-importing module was — one move, one copy, one re-derivation, one function stranded.

### 9.5 Three findings the build recorded as facts about τ

**`@agent_facing` cannot mark a head, and CLAUDE.md reads as if it can.** `docs_build.PACKAGES` is
`("tau_llm", "tau_agent_core")`, so a marker anywhere in `tau_coding_agent` is collected by neither
`scripts/build_agent_docs.py` nor `scripts/check_docs_coverage.py` — measured: the package holds
zero markers today, `extension_ui.ExtensionUI` included — the TUI's delegate, and exactly the object
an extension calls. `ReplDelegate` and `LineReader` therefore carry full docstrings and NO marker: a
marker there would claim a reference page nothing can generate. Marking a head means adding its
package to `PACKAGES`, which sweeps the whole Textual tree into the denominator — a decision about
the docs project, not about this head.

**No reasoning token count exists anywhere in τ.** `Usage` has no reasoning field and no provider
sets one, so a head can only estimate what a turn's thinking cost. This one reuses
`compaction.estimate_tokens` (~4 chars/token) rather than inventing a second heuristic and prints
the `~` to say which number it is; an exact count needs a provider field (§10).

**`lane_label` returned half a badge.** §4's `lane_start` row specified `"source · submitter"`,
leaving the source's display name to the caller, which composed a badge around it — printing
`Timer · timer · cron:nightly`. A label a caller must decorate has no one right decoration; it now
returns the whole badge, the row says so, and `test_repl_theme.py` pins all three forms.

### 9.6 What remains unbuilt

Distinct from §10, which is what this head refuses on purpose. Its own spec's open items:

- **`render_text.py` was never created** (measured: no such file), so `format_telemetry` is still
  stranded (§11 finding 12, half-addressed) and `render_panel_body` stays a second copy; the move
  needs a step allowed to edit a Textual-importing module.
- **Input history has no test** (§9.1, requirement 7), and the TUI's history and
  `~/.tau/repl_history` are two stores of one thing — which is gap 41, unanswered.
- **No run in a real terminal.** Every test drives `MemoryReader`; there is no tty under pytest.
  Unexercised: the toolbar under a real emulator, an external `kill -INT` while a prompt is open,
  and Windows at all (`add_signal_handler` raises there; no Windows run was made).
- **A second `FlowStep` after an answered form** raises, untested and unreachable today (§8).
- **`end_reason` has no shared reader.** `truncation.py` and `prompt_cache.py` are the pattern —
  one pure module all three heads read — and `agent_end.end_reason` has none, so this head prints
  `⏹ aborted` from its own state and `max_turns` / `repeat_tool_calls` reach no head as a
  sentence. That is the next instance of the shape those two modules closed.
- **Two TUI defects and one core gap are recorded, not fixed** (§10): abort losing an accepted
  but unwoven steer line, the TUI's `/model` recreating the backend and so never reaching the
  `model_change` append this head added, and `switch_session` keeping a live model the loaded
  session does not name.

## 10. Deliberately absent

- **The tree browser.** `View("tree")` refuses; the algebra in `tree_surgery.py` needs the
  two-zone layout of `docs/TREE-BROWSER-AS-EDITOR.md`, which a prompt line cannot show.
- **Images.** No terminal-agnostic way to draw one; a persisted `image` block prints the one
  line `docs/FILE-ATTACHMENTS.md` §6 specifies, and `@image.png` still attaches.
- **A multiplexer.** One process, one head, one session; `docs/HEADS-AND-MULTIPLEXER.md` §5
  prices the daemon and this head adds nothing to that bill.
- **Wire changes.** Nothing new on `WireEvent` or in `COMMAND_TABLE`; the REPL is in-process.
- **`rich.live.Live` for incremental Markdown.** Rewriting scrollback fights the line reader
  (spiked). The growing block is visible only in the toolbar tail (§4, `text_delta`), and the
  scrollback gets each block once, as Markdown, at a fence-aware boundary; a Markdown construct
  the one-line lookahead does not recognise (a table whose rows are separated by blank lines, a
  block quote continued after one) renders as two blocks — a known cost of a splitter that is
  not a parser, and the reason the boundary rule is stated rather than "a paragraph".
- **`--ui-defaults`.** A human is at the prompt; a bus/timer form raises instead of defaulting,
  which is the Fail-Early reading — and leaves `_launch_tui`'s silent drop (C:446–459) as a
  recorded TUI defect, not something this head copies.
- **A `/panels` listing and chord keys.** The TUI has neither gesture; panels print on change
  and a shortcut is typed as its `/command args`.
- **A shared palette with the TUI.** Colours stay in `tau.tcss` for the TUI
  (`docs/TUI-STYLE-GUIDE.md` §5) and in `repl_theme.py` here; unifying them is the deferred
  theme work, not this head's.
- **Windows.** `add_signal_handler` raises `NotImplementedError` there; the head says so at
  startup and the key binding still aborts, but no Windows run was made.
- **Auto-compaction.** `TauBackend` constructs the session with `CompactionSettings(enabled=False)`
  (B:1120); `/autocompact on` reaches `set_auto_compaction` (B:1396) like any other head.
- **Two TUI defects this head does not fix** (app.py is frozen for this work): abort reclaims
  only the TUI's own buffer (A:1610) and loses a steer line the core had accepted but not yet
  woven (S:2429–2430 → cleared at S:3353); and the TUI's `/model` recreates the backend (A:2330)
  so it never reaches the `TauBackend.set_model` append of §2. The RPC verb (RPC:2723–2730) and
  this head record the change; the TUI still does not.
- **A model change on `switch_session`.** `runtime.switch_session` swaps the log and keeps the
  live model (RT:398–414, 464), and no head records the mismatch when the loaded session's
  `model` differs from the one running. The `--resume` flag at startup avoids it by resolving
  the model FROM the pick (§3 steps 2–3); the `/resume` command mid-session does not. Recorded,
  not designed here.

## 11. Critique dispositions (2026-09-08)

Fifteen findings against the first draft. Each was verified against the cited source before
the section it names was changed; none was refuted outright, and one proposed remedy (11) was
replaced with evidence.

| # | Finding, in one line | Verified at | Disposition |
|---|---|---|---|
| 1 | `ask` via `run_in_terminal(in_executor=True)` around rich `Prompt.ask` is the readline hang again: `abort()` sets a flag (S:3353) and cannot unblock a thread in `input()`; teardown joins it. | S:3328–3355; X:594–597; A:869 | §5: `ask` is a mode of the ONE `PromptSession`; no executor, no rich prompt; the state table gains an "inside an ask" row. §6's control table reads with `prompt_toolkit`, rich only prints. |
| 2 | `--resume` over `enumerate_domain("session_id", runtime=)` is circular: the runtime needs the session whose model step 3 resolves from. | F:680–687; R:258–260; H:705–706; RT:398–414 | §2 flag table and §3 step 2: a picker over `catalog.list(cwd)` before any backend exists. §10 records the residual `/resume`-mid-session model gap. |
| 3 | `rebind` re-running step 13 attaches a second router per swap; the AgentSession is unchanged across a swap. | RT:464, 481–495; B:1610–1646; A:2101–2103 | §3 step 7: rebind reclaims, re-reads `pending_request`, replays; re-subscribes nothing. `test_repl_lifecycle.py` asserts zero extra `subscribe_render` across `/fork` and `/resume`. |
| 4 | §4 and §10 contradict each other on streaming, and a bare `\n\n` split breaks fences and lists. | spec-internal | §4 `text_delta`: one rule (toolbar tail; scrollback once, as Markdown, at a fence-aware boundary with a one-line lookahead); §10 restated; `test_repl_render.py` covers the fence and the list. |
| 5 | An exception out of `submit_turn` propagates to the `finally` and exits the process. | B:482–483; A:1576–1586 | §5 "An exception out of a turn": caught per submission, printed, the settle in a `finally`; only §3 step 17's four causes exit. |
| 6 | `agent_end.end_reason` is not on the render stream; an aborted lane closes with an ordinary footer. | B:195–222, 581–593; submission.py:171–188 | §4 preamble and `lane_end` row: the head prints `⏹ aborted` from its own state; the partial is persisted (S:2536–2538). |
| 7 | The loop's concurrency shape is unspecified; an ask after a turn opens a second reader over the steering read; a prefix change loses the draft. | spec-internal | §5 "The main loop": one outstanding read, `asyncio.wait(FIRST_COMPLETED)`, cancel-then-ask-then-restore-draft; `message` and `bottom_toolbar` are callables so a prefix change repaints in place. |
| 8 | `/panel key n` and shortcuts dispatching `run_extension_command` skip the input hooks and the lock refusal. | S:3960–3992, 2041–2052, 2496–2504; A:723–734 | §7: both go through `submit_command` as `/{command} {args}`; the only remaining `run_extension_command` call is §6's `Ready` branch, whose submission already passed the door. |
| 9 | `/reasoning`, `/tools`, `/panel` are not in `FRONTEND_COMMANDS`; `resolve_command` sends them to the model and the completer calls them unknown. | K:117, 191–199, 296 | §6: `REPL_COMMANDS` (with `/quit`, also absent from K:117), checked first and fed to the completer; disjointness asserted. |
| 10 | `/model` via `backend.set_model` persists no `model_change`; the RPC verb appends by hand. | B:1359–1377; RPC:2723–2730; H:705–706 | §2 backends change 2: `TauBackend.set_model` appends, gated on the log's `append_model_change`. |
| 11 | `apply_resume_metadata` is the head writing the log; use `backend.set_session_name` and `backend.set_model`. | H:361–372; B:1379–1394; S:850 | Finding adopted, remedy amended: `set_model(name)` would drop `--thinking` (H:140–142 puts it on `model_config`; `ConfigModelResolver.__call__` B:866–879 rebuilds from the raw entry), so §2 adds `record_model_change(name)` and §3 step 9 uses it; `_apply_resume_metadata` stays private to print mode. |
| 12 | `lane_end.extra` is dropped from the footer; `format_telemetry` lives in a Textual-importing module. | CW:26–30, 1042; A:2066 | §2: `render_text.py` holds `render_panel_body` and `format_telemetry`; §4 footer carries the segment. |
| 13 | Step 15 names a messages→render-events adapter that does not exist; `transcript.py` imports Textual. | T:6–11, 76 | §2 backends change 4 and §4 "Replay": `replay_render_events` beside `TurnStream`, `span_seconds` moved next to it, a live-vs-replay parity test. |
| 14 | `submit_turn(sub, None)` fails mypy: both signatures declare `list[dict]`. | B:959, 1594; S:2069–2075 | §2 backends change 1: both widened to `list[dict] \| None`; `None` documented as the bound log's `context_for`. |
| 15 | Abort reclaims only the head's buffer; a steer the core accepted (`messages=[]`) but not yet woven is cleared at S:3353 and reported nowhere. | S:2429–2430, 3353; B:254; A:1610 | §5 "`delivered`": kept until `steer_message` confirms the weave, reclaimed on abort and swap; the TUI's copy of the defect recorded in §10. |
