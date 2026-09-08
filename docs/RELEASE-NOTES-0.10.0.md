# v0.10.0 — one declaration of what τ can do, and a cache that was never asked for

Written from the commits between `v0.9.7-fullhistory` (`bb82a67`) and master, so
the release commit, the GitHub release body and the site all say the same thing.

**Sixty-two commits**, 485 files, +31707 / −24438 lines. The minor bump is not
inflation: this release removes three published extension methods, renames the
TUI's application class, and takes a field off the RPC wire.

Two halves. Thirty-seven commits are the **architecture overhaul**, which asks one
question — *where does τ write down what it can do?* — and answers it once instead
of four times. The twenty-five after it are what the answer made cheap, plus the two
defects that cost real money and real time: an Anthropic model was being billed
full input price on every request, and a saved session could not say when
anything in it happened.

---

## The registry

### What τ can do is declared once

Before this release the slash vocabulary was a literal, the command palette was a
second literal, the RPC table's parameter half was a third, and its result half a
fourth. Nothing compared them. A capability could be reachable from the TUI and
absent from the wire, and the only way to find out was to look.

`tau_agent_core.capabilities` now holds three tables and one checker:

* **`Capability`** — one read or one mutation, performed by the core, addressable
  by name. It declares what it **takes** (`Argument` records over a domain
  vocabulary) and what it **gives back** (one JSON Schema, for reads as well as
  mutations).
* **`Flow`** — an ordered argument list ending in exactly one mutation, declared
  by the core and performed by a head.
* **`Domain`** — a named type plus how its values are found: `free`, a fixed
  `values` list, or an `enumerator`. `field_kind` says how it is *asked for*,
  which is a separate question from how it is found — `model_name` and
  `session_id` are both computed and only one is offerable.

`_check_registry` runs at import and cross-checks all three: every enumerator is a
declared read, every flow ends in declared mutations, every argument names a
declared domain. A typo used to surface as a step offering no candidates, which is
indistinguishable from an empty scope.

`FRONTEND_COMMANDS` is now a projection of the flow table rather than a literal,
so the slash vocabulary cannot drift from it.

### The wire grew, and stopped describing itself in English

Measured by importing `COMMAND_TABLE` at both revisions: **28 verbs → 43**, of
which live went **21 → 36**. The seven declined rows are unchanged (`bash`,
`cycle_model`, `cycle_thinking_level`, `export_html`, `send_tool_result`,
`set_follow_up_mode`, `set_steering_mode`).

`docs/VSCODE-HEAD.md` §6 had measured the gap and named it as the blocker for a
second head: none of the live verbs read or wrote tree structure, so τ's
differentiating feature was reachable only from inside the Textual head. Eleven
declared capabilities had no verb. They have one now, and **every declared
capability is on the wire** — asserted at import, not observed:

```
navigate  summarize_and_navigate  elide_span  commit_branch  paste_subtree
enable_extension  disable_extension  reload_extension
complete_message_id  list_managed_extensions  get_extension_state
```

Both halves of the table now derive from the registry. `params_schema_for` came
first; `Capability.returns` and `rpc.schema.result_schema_for` are this release.
The rejected alternative was thirteen read-shaped exceptions kept by hand in the
RPC table, and it lost on measurement (`get_messages` is 189 bytes, the
second-smallest schema in the table) and on the point of the exercise: a rule with
thirteen exceptions is a rule a head author learns twice.

The two halves stay asymmetric on purpose. Parameters are `Argument` records over
the domain vocabulary because a head has to **render a form** for them. A result is
read, never rendered blind, so `returns` is the schema itself and there is no
second vocabulary to keep in step.

Three arrays that used to describe their elements in English now carry schemas, and
a view command comes back as a success instead of an error.

`performer` left the wire. After the four-arm result union landed it had **zero
readers** in any `src` tree — three writers and some prose — and the fact
underneath it was never about who runs a command. `CommandOrigin`
(`"builtin" | "extension"`) replaces it in the `get_commands` listing and says
where the *name* came from, which is the fact a head wants: built-ins resolve
first, so an extension cannot shadow `/compact`, and a palette groups by it. The
`submit` / `prompt` acceptance payload drops the field outright.

### One Fail-Early gap closed on the way

`tree_ops.summarize_and_navigate` did not check `target_id`, and `subtree_text`
answers `""` for an unknown id — so it would have spent a completion summarizing
nothing and appended the result. It refuses first now, as `navigate` already did.

---

## Extensions

### An extension can say what its command takes

`register_command` gave a command a name and a handler and said nothing else, so
every head showed the name and handed the handler whatever was typed — because
nothing anywhere said what should have been typed.

`api.register_flow` adds that statement in the vocabulary τ's own gestures already
use. The command then gets tab completion, a rendered form and a palette argument
**in every head, with no head code written for it**. `docs/TUI-STYLE-GUIDE.md` §6
predicted this would need a registration path and an answer to whose domains an
extension may name, and no new rendering. That held.

**The registry stays frozen; an extension arrives as a layer.** `FLOWS`,
`DOMAINS`, `CAPABILITIES` and `VIEW_COMMANDS` are still module constants;
`Vocabulary` is one frozen value over them, `BUILTIN` holds τ's own, and every pure
function that reads the registry takes one as a *defaulted* parameter — which is
what left roughly forty existing call sites untouched. A mutable global was the
alternative and is wrong here: a fork, a `switch_session` and a sub-agent are
separate sessions in one process, so one would have seen another's flows and
`next_step` would have stopped being pure.

`docs/EXTENSION-FLOWS.md` is the record, including the six deliberate absences —
the largest being **one argument per flow**, refused at registration.

### `ui.confirm` / `ui.select` / `ui.input` are gone

**Breaking.** They emitted nothing on the record stream, were answerable only
through a bound TUI delegate, and held the FIFO `_turn_lock` for as long as a human
took — and a parked coroutine dies with the process while the thing it was
protecting does not.

They are replaced by one reserved `customEntry` (`extension_request`) carrying two
independent concerns: a `lock` boolean and an `ask` form. Four states over two
keys, all four persisted, all four rendered by every head.

* `api.request_user_action` appends it.
* `AgentSession.pending_request` reads it **at the cursor**, with no ancestry walk.
* `submit` refuses on it, and the refusal carries the whole entry — so
  `tau --mode rpc` ships it in `SUBMISSION_REJECTED` data with no new verb.
* `answer_request` appends the `extension_response` **and then** dispatches the
  action, in that order, so the append releases the lock before the handler runs.

Three findings corrected the design as written, and each is in
`docs/EXTENSION-LOCKS.md`:

| Finding | How it was caught |
|---|---|
| The cursor is not the raw cursor — `AgentSession.__init__` appends an `agent_spec` node, so opening a saved session landed past the lock | opening a saved session |
| A request must be raised where nothing appends after it; a `tool_call` hook's request ends up behind the leaf | `examples/30_permission_gate.py` had to change |
| A release command must **move** the cursor — being exempt from the lock is what lets it run, not what releases anything | a live headless drive, not a test |

Commands are exempt from the lock **by placement**: resolution happens inside
`_apply_input_pipeline`, which `submit` calls *after* the multitask gate, so the
check sits after the early return. There is no exemption list to keep correct.

`--ui-defaults confirm=yes` now refuses and names its replacement.
`examples/44_release_gate.py` and `examples/45_holy_grail.py` are the demos.

### An extension declares its config keys

`api.config` handed back an untyped dict slice, and nothing anywhere said what keys
were in it or what type each one was. That is why no generic settings screen could
be written — not because a head lacked a form renderer, but because there was
nothing to render.

`CONFIG_SCHEMA` is that declaration, and it is a `ui.form` spec, so the renderer
and the headless answerer were already written. It is read at import and validated
at load, so an invalid declaration raises `ExtensionCapabilityError` and
`register()` never runs. Two capabilities sit on top, both on the wire —
`get_extension_config(path)` and `set_extension_config(path, values)` — and the
write checks every value against the declared schema before it applies.

### An appended message reaches the transcript when it is appended

Reported from a live session: a tool ran, its note was on the tree, and the
transcript did not show it. `api.send_message` announced nothing — a
`customMessage` belongs to no completion and no tool call, so the whole
`AgentEvent` stream was silent about it, and the TUI's live transcript is built
from that stream.

It now emits on a `custom_message` channel; `RenderRouter` forwards it and
`ChatDisplay` mounts it through the **same** `add_persisted_message` the reload
path calls, so the live box and the reloaded one are one widget kind.

The half-cause is worth knowing if you write extensions: a command never re-read
`session.context`. A turn rebuilds the app's working list when it ends; command
dispatch did not — so a message appended from a handler existed on the tree and in
no list the app reads. Mounted, then gone at the next window rebuild, back only
after a restart.

Off the loop the announcement cannot be scheduled, and the new
`EventBus.has_listeners` decides what that means: **nobody subscribed** is a
headless script losing nothing; **somebody subscribed** is a head that would
silently fall behind the tree, and that raises with the channel named.

### A tool's `details` reach a head instead of being discarded

All eight built-in tools computed `result_dict["details"]` and nothing read it, so
no head had ever been handed a path, a line range or a diff. It is on the wire now.

---

## Money and time

### Prompt caching, on both wires

τ sent **no cache breakpoint on either wire**, so an Anthropic model was billed
full input price on every request — reported from a real session as 0.0% cached
input and roughly 5× the average cost per message.

Measured across `api.anthropic.com`, a LiteLLM gateway, `api.openai.com`,
llama.cpp and unorouter (`docs/PROMPT-CACHING.md` §2): the bug reproduces on both
wires and only where the marker is absent; LiteLLM translates an OpenAI-format
`cache_control` into Anthropic's native caching with identical token counts; and
OpenAI needs no marker and is unharmed by one.

**Two config keys, because they are two questions.**

| Key | Asks | Default |
|---|---|---|
| `prompt_cache` | should this model's requests be cached? | `true`, every wire |
| `prompt_cache_dialect` | how must an OpenAI-compatible endpoint be *asked*? | unset; `"anthropic"` is the one value |

The first design put both in one key with values `"anthropic"` / `"off"`, which
made `null` mean "on" on one wire and "off" on the other.
`build_model_from_config` now refuses a string and names the replacement, because
`"off"` coerces to `False` and `"anthropic"` to a truthy nothing.

`anthropic-messages` reads only the boolean — there the parameter is native and
top-level. `openai-completions` needs the dialect declared, because the marker has
to travel inside `messages`, which is in `_RESERVED_BODY_KEYS`: an operator who
lands on a strict server could not take it back out. The dialect is **declared,
never detected from the model id**.

Two breakpoints, and the second is the one that matters for a tool loop. Measured
on one 21058-token request: the system marker alone read 10736 tokens; system plus
a marker on the tail `role: "tool"` message read 17721 and wrote only the
3331-token delta.

### And a notice when it should have been read and was not

`tau_agent_core/prompt_cache.py` reads each turn's completions and says so on
screen when two or more calls all read 0, or when a turn's first call reads 0
within 300 s of the previous turn's last completion — both requiring a prompt over
4096 tokens, the highest minimum cacheable prefix of any current model.

It is gated on the **result**, never on the declaration. Gating on
`prompt_cache_dialect` was the first design and it is backwards: a model that
declares the dialect is one somebody already configured correctly, while the case
worth reporting — a gateway that drops `cache_control` en route to an Anthropic
model — has nothing declared to gate on.

The third gate is what makes the sentence definite. **A 0 in `cache_read_tokens`
says two things**: a cache that missed, and a server with no cache. So every
completion must also carry `Usage.cache_reported`, set in each provider from
whether the server's cache fields were *present* rather than non-zero. llama.cpp
reports neither and τ's default model points at one — without this, the first long
local turn told its reader to configure a gateway they are not running. The default
is `False`, so absence of evidence never manufactures a finding.

All three heads get it: the TUI mounts a box, `tau -p` writes two lines on stderr,
and `tau --mode rpc` ships `cache_notice` on `agent_end`.

### A conversation records when its events happened

Deciding whether a cache entry had expired needs the gap between two LLM calls, and
that was not recoverable from a saved session.

Nothing failed to *measure* time — the user send, the tool result and every
`AgentEvent` were already accurate epoch ms. Two things destroyed the record at the
persistence boundary:

* `openai-completions` wrote a literal `0` for `AssistantMessage.timestamp` where
  `anthropic-messages` wrote ms and `google-generative-ai` wrote **seconds**.
* `append_at` stamped the **write** time, so a turn persisted in one pass after
  `loop.run` returns collapsed onto one millisecond — measured on a real 685-entry
  session as a user message and four completions all inside 2 ms.

Both are fixed at the origin. `timestamp` is now `int | None`, `None` means no
clock applies, and a stored `0` is legacy data interpreted in exactly one place —
`normalize_loaded_entries`, called from every store's load path. All three
`SessionLog` implementations call one `event_iso`.

The consequence: **every LLM call is now bracketed with no new field**, because
call 1 starts at the user's send and call K at tool result K−1.

The TUI's head-local clock is gone with it — `TurnStream.elapsed_seconds` live and
`transcript.span_seconds` on reload both read the loop's clock, so they agree by
construction.

`docs/MESSAGE-TIMESTAMPS.md` §6 records the two costs: the entry timestamp changed
meaning (it is the sibling sort key in `ConversationTree`; same format, so old and
new logs still sort together), and there is no backfill.

### A cut-off completion reads the same in all three heads

`docs/TRUNCATED-TOOL-CALLS.md` §3.1: the reading moved into the pure
`tau_agent_core/truncation.py`, the TUI keeps its box, `tau -p` writes two lines on
stderr, and `tau --mode rpc` ships `stop_reason` and `dropped_tool_calls` on
`message_end`.

The wire gets the **fact**, not the sentence — the one deliberate divergence from
`cache_notice` — because a five-value enum is something a host branches on rather
than string-matches. `WireEvent` redeclares the `Literal` with an anti-drift test
against `AssistantMessage.stop_reason`, and `dropped_tool_calls` is `null` rather
than `0` when none were dropped.

An `aborted` completion's drops read as zero, because this notice tells an operator
to raise a cap and an Esc is not a cap.

---

## The TUI

### `app.py` went from 9964 lines to 3462

Six modules hold what left it — `tree_browser.py`, `transcript.py`,
`chat_widgets.py`, `editor_widgets.py`, `modals.py`, `extension_ui.py` — with
measured import edges: everything points down except one `TYPE_CHECKING` edge back
to `TauApp`. Every move is rope's `MoveGlobal`, one symbol at a time, so no source
text was retyped. `docs/ARCHITECTURE.md` is the map.

**Eleven `ModalScreen` subclasses became one shell and four role classes.** They
had converged on one shape without sharing it — measured: nine identical `escape`
bindings, nine identical `action_cancel` bodies, twelve `Container(id="…-dialog")`
openers, and a dialog-title rule byte-identical across seven selectors.
`dialogs.py` now owns the frame, the title, the centering and the cancel binding; a
subclass states `DIALOG_ID` and its labels. `test_dialog_shell.py` exists because
the first application of the shell lost `#tree-browser-dialog` from the DOM and the
failure surfaced four files away.

**The stylesheet's prose moved to a style guide.** `parley.tcss` → `tau.tcss`,
1206 → 439 lines, with the parsed rule sets provably identical. A test now fails a
multi-line comment in it. `docs/TUI-STYLE-GUIDE.md` §5 is the 410 lines that moved,
keyed by selector.

**`Parley` is now `TauApp`.** Breaking for anything that imported the class by
name; there are zero occurrences of the old name left in any `src` tree.

### Tab completes a command's argument, not just its name

And a flow's missing arguments are *offered* rather than described. `/fork` forks
the session instead of aliasing `/tree`; `/model`, `/name` and `/autocompact` are
flows over the registry rather than bespoke handlers; `/compact` carries the
argument its flow declares.

### A 60-tool turn stops mounting widgets nobody asked to see

Reported as streaming lag plus a lock-up on regaining focus. The live window itself
was innocent of the first complaint — it does bound consecutive big turns (four
60-tool turns flat at 554 widgets). What it cannot do is cut *inside* one, and that
stands (`docs/TRANSCRIPT-WINDOW.md` §10.4).

Two real faults:

1. **The trim deferral had become unbounded.** `watch_scroll_y` was its only
   release, and that watcher returns early while a lane is open — so a reader who
   scrolled up once and kept prompting went 372 → 1860 widgets over five 40-tool
   turns. `begin_exchange` now claims the tail and trims, but only when a trim is
   being held, which leaves one turn of grace: the turn read *through* moves no
   rows, the turn started *next* trims.
2. **A collapsed `ToolBox` cost 9 widgets where 3 would do.** Both `Markdown`
   bodies were built in `__init__`, and Textual arranges hidden widgets too
   (`visible_only=False`, 8.2.7). They mount on first expand now.

Measured on one live turn of 100 tool calls: **786 → 312 widgets, 3.33 → 2.16 s per
200 deltas, 124.6 → 58.4 ms per full repaint.**

### Three punch-list defects

A sidebar fault, popup wording, and a picker that could exhaust memory.

---

## The tree the code is now in

**Every multi-line comment block is gone** — 13980 deletions across 369 files in
one mechanical commit, verified by re-parsing each file and refusing to write when
the AST changed. `scripts/strip_comment_blocks.py --check` is now a fourth
pre-commit gate, which it can be because the five `src` trees hold zero blocks: it
passes on a clean tree and only ever fails on something a commit just wrote.

`CLAUDE.md` states the code style that replaces them: docstrings carry the
explanation, a comment is one line stating a dependence or an assumption, and
design rationale lives in a `docs/` file cited by name.

**Agent-facing docs**: 493 of 933 marked objects complete (52.8%), 0 drift. The
generated reference is exported to the public site, byte-identical in the body.

**The published prose is scanned now.** Every release replaces the public tree
with `git archive master`, so `docs/` and `ROADMAP.md` ship verbatim — and the
host-address guard covered only the installable trees. Measured on the unpacked
archive: eight lines named a home directory or the private git host, across five
files, all of them present at 0.9.7. They are placeholders now, and
`test_no_host_addresses.py` has a second scope that fails on the next one. A
measured `192.168.*` address in `docs/probe-results/` or `experiments/` is
deliberately left alone: there the address is the measurement.

**The release matrix now installs what CI installs.** The four-version matrix
reported `0 failed` on this tree and the next CI run failed, on a test the matrix
had silently skipped: `tau-meta` is what pins `ffwf-tau-llm[anthropic,google]`,
the matrix never installed it, so neither optional SDK was present and
`pytest.importorskip("anthropic")` took the test out. It installs `-e ./tau-meta`
now. The test it skipped is also fixed rather than snapshot-bumped: it asserted
that `SDK_STREAM_PARAMS` **equals** the installed `messages.stream` signature,
which against an unbounded `anthropic>=1.0` can only hold for one SDK version at
a time — and it broke on `workspace_id`, a parameter τ does not send. It asserts
a **subset** now, which is the property the stub needs: a name the SDK dropped
still fails, a name the SDK added does not, because the provider reads the
installed signature at request time and filters against it.

**pi parity stopped being an objective** (`4e91ee3`). pi remains provenance — the
fastest way to understand how a ported behaviour was derived — and is no longer an
authority on what τ should do. A divergence needs no justification beyond its own
reasons.

Six new design records: `ARCHITECTURE.md`, `EXTENSION-FLOWS.md`,
`EXTENSION-LOCKS.md`, `MESSAGE-TIMESTAMPS.md`, `PROMPT-CACHING.md`,
`TUI-STYLE-GUIDE.md`.

---

## Breaking changes

| What | Who it breaks | What to do |
|---|---|---|
| `ui.confirm` / `ui.select` / `ui.input` removed from `ExtensionUI` | any extension that asked the user a question | use `api.request_user_action` (`lock` and/or `ask`); see `examples/44_release_gate.py` |
| `--ui-defaults confirm=yes` | headless runs that pre-answered a dialog | it refuses and names its replacement |
| `Parley` renamed to `TauApp` | anything importing the TUI application class | import `TauApp` |
| `performer` removed from RPC results | an RPC client reading that field | read `origin` |
| `AssistantMessage.timestamp` is `int \| None` | a third-party `SessionLog` implementation | `None` means no clock applies; a stored `0` is normalized on load |
| `parley.tcss` renamed to `tau.tcss` | a fork carrying a patched stylesheet | re-apply against `tau.tcss` (1206 → 439 lines) |

## Upgrading

```bash
pip install --upgrade "ffwf-tau-coding-agent[tui]==0.10.0"
```

Or the metapackage:

```bash
pip install --upgrade ffwf-tau
```

Nothing in the on-disk session format changed shape. Sessions written by 0.9.x
load, and their entry timestamps are normalized on the way in.
