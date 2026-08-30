# v0.9.6 — the image arrives, and the TUI stops locking

Written from the commits between `v0.9.5-fullhistory` (`941dc20`) and master, so
the release commit, the GitHub release body and the site all say the same thing.

0.9.5 fixed two vendors that had never carried a tool-using conversation end to
end. This release is the same shape one layer up: two paths that looked like they
worked and did not.

**The `read` tool could not show a model an image.** It sent a 200-character
prefix of the base64 and an ellipsis. Asked to name the character drawn in each
of two pictures, a vision model answered "red circle A" and "blue square B" —
right about colour and shape, invented about the character, and it ignored an
explicit instruction to say NO IMAGE. A stub that looks like an image is worse
than no image.

**The TUI locked for the length of a turn.** The editor was disabled and every
renderer scrolled to the tail on each delta, so you could neither say anything to
the agent nor re-read what it had already said. The core has had
`multitask_strategy="steer"` since the submission lifecycle's phase 4 and nothing
in the TUI could reach it.

**Ten commits.** Two on the image path, four on the editor, two fixes, two
documentation.

---

## The image path

### A tool result's image never reached the model

`read` now returns a real image block beside the text, and all three clients
carry it: a `tool_result` content block for Anthropic, a `functionResponse` plus a
user turn for Google, a tool message plus a user turn for OpenAI.

The converter shape was decided by measurement, not by reading pi. pi puts every
image of one tool-result run into a single user turn. On the same server, same
model, same images, that shape answered `alpha.png: red circle K | beta.png: NO
IMAGE` 3 runs of 3 — one image seen, attributed to both files. **One image per
user turn** is what makes attribution work; adjacency to the tool message and
filename labels do not. So the images are buffered until the tool-result run ends
— which also keeps the run intact for OpenAI's documented ordering rule — and
then emitted one turn each. Correct 3 of 3 with two images, and with three.

Found while tracing the same path, and fixed here:

* `split_tool_result_content` in `providers/base.py` replaces two drifted copies
  of one splitter, and raises on a block it cannot read. The old code iterated a
  bare dict's **keys**: a content of `{"type": "text", "text": "hi"}` reached the
  model as `type text`.
* The `toolResult` branch of `_convert_message_dict` is unreachable once the loop
  handles tool results itself. Instrumented to confirm that only `user` and
  `assistant` arrive there, then removed.

### An image is capped at 2000px a side

An unbounded image is not merely expensive. Measured 2026-08-28 against llama.cpp
with a vision model, a 2000×2000 PNG — 31 KB on disk, 4307 prompt tokens — closed
the connection with no HTTP status and left the server process gone. Twice. The
same image succeeded once the server restarted with a smaller model context, so
the ceiling was the vision encoder's spare VRAM: a number the client cannot read
and the server does not advertise.

So the cap is a budget the operator picks, not a property discovered from the
model. 2000px a side, pi's default (`image-resize-core.ts`). An image already
inside it passes through byte-identical — re-encoding a screenshot that already
fits costs quality and gains nothing. The text block reports a resize, so a model
reading a downscaled screenshot can say it cannot read the fine print rather than
guess at it.

Verified end to end against the live server: a 3000×3000 PNG, 265 KB on disk,
reached the model as 768×768 and 31.9 KB (the cap lowered for the test,
deliberately, to avoid a third crash), and the model still read the character
drawn in it.

Pillow is an optional dependency, `[images]`. Without it, reading an image
**fails** and the error names the extra. It does not send the image unresized:
reporting a bound that is not enforced is the silent fallback the cap exists to
remove.

### `tool_options`, the seam a config key travels through

`_resolve_tools` constructed every built-in as `cls()`, so nothing an operator
wrote in `~/.tau/config.json` could change one — not even `cwd`. It now takes
`{tool name: constructor kwargs}`, keyed by name rather than being a parameter
named after one tool, and `TauBackend` passes `max_image_dimension` from the
config. A missing key means the tool's own default, not "no cap"; a literal
`null` is the explicit opt-out. An option for a tool the denylist removed is
ignored, and an option no constructor accepts raises `TypeError`, which is the
right answer to a typo in a config.

### `ffwf-tau` installs the whole client

The metapackage held one dependency, and its test forbade a second without a
deliberate decision. This is that decision. It now pulls `[tui]` as before, plus
`ffwf-tau-llm[anthropic,google]` and `ffwf-tau-agent-core[images]`.

τ speaks three wire protocols and ships a config naming models on all three, so a
one-SDK install fails on two thirds of its own defaults; and `read` on a
screenshot is the most obvious thing a person points it at. Each of those already
raises an error naming its extra, which is a good failure and still one the
guessable name should not hand anybody. `[jmfts]` stays out: it needs a running
server, and that is the line between "works on its own" and "answers a question
nobody asked".

## The editor

### Type and read while the model is generating

* `MessageList` follows the tail only while the reader is at the bottom
  (`scroll_to_tail` plus an extended `watch_scroll_y`). Scrolling up detaches,
  scrolling back re-attaches, and a reload re-attaches explicitly.
* The editor stays enabled during a turn. A line typed then goes into
  `Parley._pending_steer`, shows in a new `PendingInput` widget above the editor,
  and comes back with Up on an empty editor — `alt+up` with a draft in the box,
  which is pi's own binding for it.
* `steering_strategy` in `config.json` picks the delivery point: `steer`
  (default, the running turn's next tool call) or `enqueue` (the turn edge). An
  unrecognised value raises at startup rather than selecting the other one
  silently.
* A delivered steering message is rendered inside the open exchange. `TurnStream`
  consumed no `message_start`, so a steer used to reach the model and the session
  log without appearing on screen until a reload. That makes an exchange's steps
  no longer all the model's, which `_close_exchange` assumed twice: it now
  promotes only an assistant step as the final answer, and never unwraps an
  exchange holding a steer.
* Esc and a session swap hand the pending text back to the editor rather than
  delivering it into a turn that no longer exists.
* A command typed mid-turn is refused and left in the editor. `/compact` rewrites
  the context the running turn is being answered from, and steering goes through
  the door that dispatches it.

Costs one thing: `ctrl+z` is Rollback while a turn generates, and the editor's
undo is now reachable enough to contend with it. Rollback wins, because the
Footer says so for exactly that long. `docs/TUI-STEERING.md` has the design.

### `enter_key`, so Enter can send like every other agent

τ's editor uses Enter for a line break and `Ctrl+J` to send. pi, Claude Code, and
every other terminal coding agent use the opposite pair, and moving between them
in an afternoon is how you send half a prompt.

`enter_key` in `config.json` picks one. `"newline"` (default, unchanged) keeps
τ's pair; `"submit"` is pi's — Enter sends, `Shift+Enter` and `Ctrl+J` break the
line. An unrecognised value raises while τ is starting, and the Footer names
whichever key actually sends.

The swap lives in `ChatInput.on_key`, not in `BINDINGS`: `TextArea._on_key`
claims Enter and stops it, so a non-priority binding never fires, and
`priority=True` would take Enter away from every other widget on the screen.
Measured against Textual 8.2.7, along with everything else in `docs/ENTER-KEY.md`
— including why `Shift+Enter` is unreachable without the kitty keyboard protocol
(the legacy encoding has one byte for Enter, Shift+Enter and Ctrl+Enter alike)
and why `Alt+Enter`, which *is* distinct on the wire, still arrives as plain
`enter` (Textual applies the `alt+` prefix only to single-character key names).
`scripts/keyprobe.py` reports what your own terminal sends.

Not added, and recorded as such: Claude Code's backslash continuation, which
turns a missed keystroke into a message ending in a stray `\` rather than a
visible failure.

### The editor says whether a `/…` is a command

An unrecognised slash goes to the model as ordinary text. That is right —
refusing every one of them would break pasting a file path — and it was silent:
`/exntesions` got whatever the model guessed it meant, and nothing said the
command did not run.

`CommandPopup` sits under the editor and says which outcome is coming.
`complete_command` (core, beside `resolve_command`) is the pure candidate list:
case-sensitive prefix on the first word, built-ins first, an extension that
shadowed a built-in dropped because `resolve_command` makes it unreachable. Its
four rules cover the pasted-path case — a space after a word that names nothing
hides the warning, because the line is then committed to being prose.

**Tab is the only key**, and only when there is something to insert; otherwise it
keeps Textual's `tab_behavior="focus"`. Repeated Tab cycles and wraps, identified
by the editor still holding what the last press wrote, so any keystroke ends the
cycle with no mode to clear. Esc, Up and Enter were already spent on cancel,
history/steer-reclaim and `enter_key`, and a dropdown claiming them would also
let Enter send a line the highlighted row is not in.

`docs/SLASH-COMMANDS.md` §4 records the measured stray-text table and the one
Fail-Early violation this does **not** fix: `/tree extra words` runs and discards
the arguments.

### `@notes.txt` attaches the file

The decisions are pure functions in `tau_agent_core.attachments` — the core
decides, the frontend performs — and the frontend expands at `Submission` build
time, so what is persisted and what the model saw are the same string.

Two block vocabularies: `<attachment>` when the content is present, and
`<reference filename= path= size= reason=>` when it deliberately is not (over
`attachment_inline_limit`, default 10240 bytes; not UTF-8; unreadable). Images
ride as `ImageContent` behind an empty `<attachment>` marker that names the file,
so the same `@` reaches a multimodal model.

The cursor picks between the two Tab vocabularies — command completion and path
completion — and the path completion follows five shell rules. The attachment bar
is a view of the editor text, so removing an attachment is a text edit. A line
typed mid-turn keeps its `@` raw in the buffer and expands at the delivery point.
`docs/FILE-ATTACHMENTS.md` §7 records the deliberate divergence from pi's `<file
name=>` and from τ's own older `-p @file` rule, which was **not** unified.

## Two fixes

### A tool call cut off at `max_tokens` is dropped, not raised on

A repeating `JSONDecodeError: Unterminated string starting at: line 1 column 12`
was killing the turn against a local llama.cpp server. Column 12 is the opening
quote of a 7-character first key, so the buffer ended inside `bash`'s
`{"command":"…` — a **prefix**, not a malformed payload. τ has sent
`Model.max_tokens` on the wire since `00601cc`, and a config stating none
resolves to 4096, which reasoning can spend before the tool call even starts.

`_build_final_message` now treats `stop_reason == "length"` the way it already
treated `"aborted"`: the call is dropped and counted, never repaired and never
given `{}`. A `"stop"` or `"toolUse"` finish with a buffer that will not decode
still raises — and the raise now names the tool call, the stop reason and the
buffer, which the bare `JSONDecodeError` did not.

The drop is visible rather than silent. A durable notice mounts in the TUI,
quoting the `max_tokens` actually in force (read through the same config path the
backend resolves, or `unknown` when it cannot be resolved); `stop_reason` rides
`completion_end` out of `TurnStream`; and `format_telemetry` prints `dropped=N`
when a completion dropped any.

`docs/TRUNCATED-TOOL-CALLS.md` records why grammar-constrained decoding does not
help — a grammar binds which token comes next, not how many remain — and what is
deliberately absent: no retry, no repair, no change to the 4096 default.

Also here: `MarkdownFence` gets its horizontal scrollbar back. Textual 8.2.7 sets
`scrollbar-size-horizontal: 0`, which clipped the long single lines a traceback
and a provider error are made of.

### A sub-agent's tools are the session's, extensions included

`ctx.spawn_branch` checked its allowlist against `session._tools` — the
constructor's list, which omits every `api.register_tool`. So on the supported
shape for a host that owns its whole vocabulary,
`AgentSession(tools=[], no_tools="builtin")`, `_tools` is empty while the model is
being offered several tools, and **every non-empty allowlist was refused**, naming
a tool the model had just successfully called. `tools=[]` — a sub-agent that can
think and do nothing — was the only value that did not raise.

`_spawn_fork` read the same attribute to mean "this session's own tools". A fork
is a second full agent continuing the same job, so on that shape it arrived with
no tools at all and nothing anywhere said so.

Both now read `_build_turn_tools()`, which is what `_run_one_turn` builds every
loop from and is therefore by definition the list the model is offered. Reusing
that method rather than computing a union here keeps the two as one decision:
`no_tools="all"` still yields a toolless branch, and a duplicate name in `_tools`
still Fails-Early. Scoping is unchanged — the branch gets exactly the named tools,
and an extension tool's adapter stays bound to the **spawning** session's `ctx`.

## Documentation

`docs/HEADS-AND-MULTIPLEXER.md` is new, and is a position and cost record rather
than a plan: τ is headless and the TUI is one head, §1 is where the code already
says so, and §5 prices the live multiplexer. Its live consequence in this release
is the ownership rule that keeps an image-paste reader in `tau-coding-agent` and
out of the core — the clipboard is head-local.

`docs/RELEASING.md` had its last step wrong since drafts were introduced.
Publishing the draft already creates `refs/tags/vX.Y.Z` on the remote through the
API, as a lightweight tag; the local tag is a tag object, and the push is
rejected. Measured at 0.9.5. The same commit records that the TestPyPI rehearsal
in step 6 — which the document had claimed for two releases had never run — ran
at 0.9.5 and uploaded.

---

## Upgrade notes

**If you install `ffwf-tau`.** It now pulls the Anthropic and Google SDKs and
Pillow as well as the TUI. The install is larger. `ffwf-tau-coding-agent[tui]` is
unchanged if you want only what you had.

**If you `read` an image.** Two changes. It now reaches the model as an actual
image rather than a base64 prefix, so a run that "worked" may now cost real vision
tokens. And an image larger than 2000px a side is downscaled before it is sent;
set `max_image_dimension` in `~/.tau/config.json` to change the bound, or `null`
to opt out. Without Pillow installed, reading an image now raises an
error naming the `[images]` extra instead of sending the file unresized.

**If you call `_resolve_tools` or `create_agent_session` with tool options.**
`tool_options` is a new `{tool name: kwargs}` mapping. Additive; an existing call
that passes none behaves as before.

**If a tool call of yours was being cut off at `max_tokens`.** The turn no longer
dies with a `JSONDecodeError`. The truncated call is dropped, counted, and shown.
The default `max_tokens` is still 4096 — this release makes the failure legible,
it does not raise the ceiling.

**If you drive the TUI.** The editor is live during a turn. Enter still makes a
newline by default (`enter_key: "newline"`), and `ctrl+z` is Rollback rather than
undo while a turn generates.

## Known open

**`repeat_tool_call_limit` is still reachable from `AgentLoop` only.** 0.9.5's
notes called this the first thing on the 0.9.6 list. It did not happen: the knob
is not plumbed through `AgentSession`, `create_agent_session`, the CLI, or
`~/.tau/config.json` the way `max_turns` is.

**Persistence still blocks the UI thread.** Unchanged from 0.9.5.
`AgentSession._persist_loop_messages` is a synchronous method at turn end, and
with the JMFTS store a ten-tool turn issues roughly 21 blocking HTTP round-trips
on the thread that draws the screen. The honest fix is an async `SessionLog`
protocol, which is a breaking change to a published contract and belongs in its
own release. `docs/BLOCKING-PERSISTENCE.md` records the three options.

**An image still has no byte budget.** The 2000px cap bounds dimensions, not
bytes, so a photographic 2000×2000 PNG can still exceed Anthropic's 5 MB inline
limit. pi re-encodes down a quality ladder to 4.5 MB; τ does not. There is also
no modality check — τ has no field saying whether a model accepts images, so it
sends one and lets the endpoint answer.

**`/tree extra words` runs and discards the arguments.** A Fail-Early violation
the slash-command work found and did not fix; naming it needs a declared
placeholder on `FRONTEND_COMMANDS`, whose value type the RPC verb and
`unsupported_command_message` read as prose. `docs/SLASH-COMMANDS.md` §4.

## Verification

Suite: 5184 passed, 144 skipped, 6 deselected, 0 failed. `mypy` clean across all
five `src` trees. `ruff check` and `ruff format --check` clean. Docs coverage
347/789 (44.0%), 0 drift. Python 3.11, 3.12, 3.13 and 3.14 measured in clean
`python:<v>-bookworm` containers before the tag, per `docs/RELEASING.md` §2.
