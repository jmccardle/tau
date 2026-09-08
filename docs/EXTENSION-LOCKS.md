# Extension locks and asks

**Built 2026-09-07.** `tau_agent_core/extension_locks.py` is the entry algebra;
`ExtensionAPI.request_user_action` writes one; `AgentSession.submit` refuses on
one; `AgentSession.answer_request` answers one; the TUI bounces, draws a row and
opens a modal. §8's standardisation landed with it, including the two breaks.
Each section below carries a built note where the build corrected the design;
three did.

An extension can stop a session from being extended, and can put a request in
front of whoever is attached, in a way that survives a restart and that every
head can render. Both ride on one reserved `customEntry`.

This doc is the design and the reasoning. It is not a status page: when a
section is built, its heading gets a built note with the commit, in the style
`docs/TRANSCRIPT-WINDOW.md` uses.

## 1. What this replaces

τ already has a blocking ask: `ExtensionUI.confirm` / `select` / `input` /
`form`. It has three defects, and they are the reason for this design rather
than an incremental fix.

1. **Three of the four describe themselves to nobody.** `form` emits a
   `{"type": "extension", "kind": "form", …}` record on the `--mode json` sink
   before it resolves; `confirm`, `select` and `input` go straight from
   `_human_delegate()` to `_headless_token()` and emit nothing at all
   (`extension_types.py:502-540`). A head that is not the Textual TUI cannot
   learn that a question was asked.

2. **The answer is a return value, so only a bound delegate can give one.**
   `_human_delegate()` requires `ExtensionUI._mode == "tui"` — a two-value
   `Literal` — and a delegate object. One caller binds one (`app.py:2168`).

3. **A waiting question holds the session.** `AgentSession._turn_lock` is a
   FIFO `asyncio.Lock` (`agent_session.py:443`, and the FIFO note in that
   method's own docstring at 2111). A turn awaiting `ui.confirm` holds it for
   as long as the human takes, so every other submission queues behind a
   dialog nobody may be looking at.

The third defect is the one that matters, because the fix for it is not a
better dialog. What "do not continue until this is answered" wants is durable
state on the conversation, not a coroutine parked on a lock — a parked
coroutine dies with the process, and the thing it was protecting does not.

τ also already has the surface this design generalises. `ui.panel(key, spec)`
takes `{title?, body, actions}` where an action is `{label, command, args?}`,
is validated in the core by `validate_panel_spec`, emits a record on the
headless stream, and dispatches a named command when pressed. Its docstring
states the property outright: "a panel is never TUI-ONLY". The panel family is
right and the dialog family is wrong; this design deletes the difference.

## 2. Two readings of `customEntry`, and why they differ

A `customEntry` is a durable, non-model-visible tree node
(`agent_session.py:3482`). `ConversationTree` never folds one into the loop
context and `convert_to_llm` never sees it, so it is state the conversation
carries without the model reading it.

After this design τ has two reserved `customType` values with two different
reading rules, and the difference is not an inconsistency:

| `customType` | Read by | Because |
|---|---|---|
| `agent_spec` | walking `parentId` leaf→root (`agent_spec_in_force`, `session_log.py:266`) | it is **state at a point in the past** — which spec governed the entry you are looking at |
| the lock (§4) | the cursor only, no walk | it is **permission to extend the session**, which is a property of where the session is now |

A lock buried mid-path is therefore inert. That is deliberate and is not a
hole to be closed later; see §7.

**Built, with one correction the design got wrong.** "The cursor" cannot mean
the raw cursor id, because `AgentSession.__init__` ends with `_record_agent_spec`,
which appends a provenance `customEntry` — so opening a saved session lands the
cursor on THAT node and a lock that is in the log is not in the read. §5's
"restart costs nothing" was false as written; the first test of it failed.
`request_at_cursor` therefore steps over `PROVENANCE_ENTRY_TYPES`, a frozenset
whose one member is `agent_spec`, and stops at the first entry that is anything
else. That is not the ancestry walk §7 refuses: it passes τ's own writes about
itself and nothing a user or a model put there, so a lock under a user message
is still released and a lock spliced mid-path is still inert.

Both discriminate on `customType` and read their payload out of `data`, which
is the existing precedent (`session_log.py:302`). Neither adds a field to the
entry schema — `customEntry` has no pydantic model, it is built by
`_append("customEntry", customType=…, data=…)` (`session_log.py:513`).

## 3. Four states from two keys

The entry carries two optional keys. Every combination means something, and
the fourth is what `append_entry` already does today.

| `lock` | `ask` | Meaning | What a user message does |
|---|---|---|---|
| — | present | A request. Ignoring it is allowed. | advances; the ask becomes history |
| set | — | Intervention required, with no form. The extension's own slash commands are the way out. | bounces |
| set | present | Fulfilling the request is the normal way out. | bounces |
| — | — | Durable extension bookkeeping, read by the extension's own tree walks. | advances |

**Built note (2026-09-07).** Two examples cover this table. `examples/44_release_gate.py`
reaches the first three rows from commands you type, which is the shortest way to
read the mechanism. `examples/45_holy_grail.py` reaches all four the way an
extension actually raises them — three from tools the model calls, one from a
command the user types — and it is where the fourth row is legible: an entry that
neither locks nor asks is refused by `build_request_data`, so its Ni note is an
ordinary `api.send_message` and never a request at all. That example is also the
worked case for §4.1: all three of its tools defer to `user_turn_end`, because a
request appended from inside `execute` is behind the tool result before the turn
ends.

"Fulfilling the request resolves the lock" is a description of the normal
path, not a guarantee. Mechanically, fulfilling appends, appending moves the
cursor, and the cursor moving is the release. Nothing checks that the form was
filled, and the escapes in §6 bypass it. Do not try to make this a guarantee
later: the value of the design is that the tree stays the only truth, and a
lock that could not be branched around would be state outside the tree.

## 4. The entry

```
{
  "type": "customEntry",
  "customType": "<reserved>",
  "id": …, "parentId": …, "timestamp": …,
  "data": {
    "extension": "<path of the extension that appended it>",
    "sentence": "<one line a head shows>",
    "release": "<command name that clears it>",   // optional
    "lock": true,                                  // optional
    "ask": { … spec … }                            // optional
  }
}
```

Four decisions in that shape.

**`extension` is stored, not looked up.** The api that appends is
per-extension and carries its identity (`ExtensionHandlers.path`,
`runner.py:52-62`), so the name is available at append time. It must be
written, because the case this design exists for is a reload where the owning
extension did not load, and a lookup then returns nothing. Note the contrast
with `ui.notify`, which *cannot* attribute — every bound extension shares one
`ExtensionUI`, so its record carries `"extension": null` rather than a
fabricated name. `append_entry` has an identity where `ui.notify` does not.

**`sentence` and `release` are strings on the entry**, so the core can report
a lock whose owner is absent instead of enforcing something it cannot explain.
Reporting "locked by X, which is not loaded" is the Fail-Early answer;
silently not enforcing it is not.

**`ask` lives in the entry rather than in RAM.** The alternative was for the
extension to re-raise the ask from `session_start` after reading its own lock
through `ctx.entries()`. Putting it in the entry is better for one reason:
rendering after a reload then falls out of the *same cursor rule* as the lock,
with no re-raise path, and it works when the owner never loaded. One rule
serves both.

**There is no release entry.** An earlier draft had lock and release as a
pair, with the live state being the latest of the two in ancestry. The cursor
rule makes that unnecessary: `resolve_cursor` is last-entry-wins, with a
trailing `navigate` pointing at its target (`session_log.py:233-263`), so
appending the lock makes it the cursor and therefore a leaf, and anything that
appends or navigates releases it. Leaf-only is a consequence, not an invariant
anyone enforces.

### 4.1 Where a request may be raised — built, and not free

Leaf-only being a consequence has a cost the design did not name: **a turn keeps
appending after a hook returns.** A `tool_call` hook that raises a request is
four entries behind the leaf by the time the turn ends — the tool result, the
next completion, the answer — and the request is then inert. The first version
of `examples/30_permission_gate.py` did exactly that and its lock never held.

So a request must be raised where nothing appends after it. The three places
are `user_turn_end` (the last hook of a prompt), `session_start`, and a command
handler. `30_permission_gate` now blocks in the veto, remembers the command, and
raises the request at `user_turn_end` — which is also the more honest sequence:
the model is told its call was refused and finishes its turn, and only then does
the session stop for a human.

This is not enforced. Enforcing it would mean either a walk (refused, §7) or
moving somebody's entry, and both are worse than the rule being written down.

## 5. Where the check goes, and the placement that deadlocks

The lock is one refusal at the one admission point, returning the shape that
already exists: `SubmissionResult(accepted=False, submission_id=…,
rejection_reason=…)` (`submission.py:170-180`), plus a structured field
carrying the entry's `sentence`, `extension` and `release` so a head renders
them instead of parsing prose.

**The obvious placement is wrong.** Command resolution happens inside
`_apply_input_pipeline`, which returns an early accepted result when the text
resolves to a command (`agent_session.py:2013-2025`). That helper is called at
`agent_session.py:2383` and `2452` — *after* the multitask gate at `2369`. A
lock checked at the gate therefore refuses the very command that would release
it, and the "lock with no form, resolved by the extension's own slash command"
state has no way out at all.

The check goes after the `if early is not None: return early` line, so the
command short-circuit runs first. Commands are then exempt from the lock by
placement, and nobody has to write an exemption that could drift.

Refusing there means the turn reservation has already been taken, so the
refusal releases it first. That shape exists and is exercised: the `rollback`
strategy calls `self._turn_lock.release()` and then returns a refusal
(`agent_session.py:2400-2412`).

**Restart costs nothing.** Loading resolves the cursor, the cursor is the lock
entry (modulo the provenance node §2's built note describes), `submit` refuses.
No extension has to load for the lock to hold — proved by
`test_a_reload_still_refuses_with_no_extension_loaded`, which reloads the log
into a session with `extensions=[]`.

## 6. The escapes

Three, in ascending cost, and all of them are ordinary tree operations:

1. **Run the release command.** Exempt from the lock by §5 — which is what lets
   it RUN, and is not what releases anything. **A release command must move the
   cursor itself**, by navigating or by appending; a handler that only returns a
   string leaves the lock exactly where it was. This was wrong in the first
   version of `examples/44_release_gate.py` and the live run caught it:
   `/gate-clear` was admitted, reported success, and the next prompt was still
   refused. Both example release commands now call `ctx.navigate` to the
   request's parent, which is the same node escape 2 lands on.
2. **Navigate to the parent and continue.** The lock node stays in the tree,
   off the active path. The extension may or may not append a new lock when
   the turn runs — that is its business, not τ's.
3. **Disable the owning extension.** When the cursor is that extension's lock
   node, `disable_extension` moves the cursor back one, and its `message` says
   so. It needed no new return shape and, on inspection, no new FIELD either:
   the design proposed adding `cursor` to `ExtensionActionResult`, and that is
   wrong — `AgentSession.performed` already writes the live cursor into the
   `Performed` and *refuses a caller that hands it one*, "because two writers of
   one field is the drift it removes". The suite caught it.

A fork inherits the lock, because a fork inherits the cursor. That is the
intended behaviour: a fork of a locked conversation is still at the point
where the decision is pending.

## 7. Two degradations, deliberately not defended against

Both follow from reading one node instead of a path, and both are the price of
keeping extension composition open. They belong in the user-facing docs as
advice, not in the code as guards.

- **Navigating back onto a lock node whose owner is disabled re-locks**, with
  no in-band way out. Branch, or re-enable. *Advice: if you enable or disable
  extensions mid-session, be aware of the state they leave behind.*
- **A lock spliced mid-path by tree editing is inert.** Nothing walks the path
  looking for locks. *Advice: if you or your extensions create synthetic
  session history, be aware of what is present and what is being submitted to
  the model.*

τ should not be made bulletproof here. A guard against either case would mean
walking the path on every submission and would make a lock something a user
could not straightforwardly get out from under, which is worse than both
degradations.

## 8. The ask surface

An ask is a spec with a body, optional `fields` from `FORM_FIELD_KINDS`, and
`actions` naming commands — the panel shape (§1) with fields added. `confirm`
becomes one field of kind `confirm`, `select` one of kind `select`, `input`
one of kind `text`. Nothing new is validated; `validate_form_spec` and
`validate_panel_spec` merge.

`FORM_FIELD_KINDS` is already the shared render vocabulary: `Domain.field_kind`
is documented as "which of `FORM_FIELD_KINDS` a head renders"
(`capabilities.py:133-134`), so a flow argument and a form field already name
the same five kinds. This design inherits that, including the constraint that
`multiselect` is illegal on a single-value domain (`capabilities.py:159-173`).

The answer arrives as a command dispatch, which is what a panel action already
does. `register_flow` takes **one argument at most**, refused rather than
truncated (`docs/EXTENSION-FLOWS.md` §6). That rule does not bend here: the
flow's one argument is the ask id, and the filled form rides as its payload.
Splitting one answer across several command arguments would create a second
argument vocabulary to keep in step with the first.

**Built, and "rides as its payload" is now a second entry.** `answer_request`
appends an `extension_response` `customEntry` carrying
`{requestId, extension, action, values}`, THEN dispatches the action's command
with the request id as its one argument. The order matters: the append is what
releases the lock, so the handler runs on a session that is already unlocked and
may submit a turn of its own. A handler reads the answer back off the tree — see
`_answers` in `examples/44_release_gate.py` — which means the payload is durable
and a reload can still see what was answered, rather than living in a dict that
dies with the process.

Two Fail-Early consequences of the one-argument rule, both enforced at
validation: an ask action may not declare `args` (its one argument is the
request id, so a declared one is a conflict, refused rather than overridden),
and an ask must declare at least one action (an ask with no action is a
notification, and `ui.notify` is how you send one).

The ask does not block. What stops the conversation is the lock.

### 8.1 Standardised

- One record family, `{"type": "extension", "kind": …}`, for every
  extension-to-head surface. Six of nine emit one today; `confirm`, `select`
  and `input` are the exceptions.
- Actions name a command, everywhere. The delegate return value stops being
  the contract.
- `FORM_FIELD_KINDS` as the one field vocabulary.
- `customEntry` as the one durable kind.
- `SubmissionResult` as the one refusal.

### 8.2 Removed

- `ExtensionUI._mode`, and `set_ui_delegate`'s flip of it. `interactive` and
  `_human_delegate` now read one thing — whether a delegate is bound — where
  they read two that could not disagree.
- `confirm` / `select` / `input` as methods on `ExtensionUI` and on the
  delegate protocol. The TUI delegate drops from seven methods to four.
- **Two** modal bodies in `modals.py`, not three: `ExtensionConfirmModal` and
  `ExtensionSelectModal` are gone, and `ExtensionInputModal` **stays**, because
  the palette opens it for a command that declares `"args"`
  (`TauApp._prompt_command_args`) and that is head-local, not an extension
  surface. Its docstring now says so.
- Three of the four `HEADLESS_DIALOG_ANSWERS` entries, and with them the
  `--ui-defaults` tokens `yes` / `no` / `true` / `false` / `first` /
  `default`. Only `form=defaults` survives.
- The `_turn_lock` hold during a human decision.

The field machinery the two surviving dialogs share came out as
`modals._FieldForm`: `ExtensionFormScreen` and `ExtensionAskScreen` both render
`FORM_FIELD_KINDS` through it, so the five widgets are written once.

### 8.3 Two breaks

**`--ui-defaults confirm=yes` stops parsing.** Follows the precedent
`build_model_from_config` set for the retired `prompt_cache` string: refuse the
old token and name the replacement, rather than silently mapping it
(`docs/PROMPT-CACHING.md`). `RETIRED_DIALOG_ANSWERS` holds the three names and
what replaced each, and `set_headless_defaults` reads it before it reports an
unknown dialog — so an operator with `confirm=yes` in a script is told what to
write instead, not that `confirm` was never a thing.

**`await ctx.ui.confirm(...)` stops returning a bool.** An extension branching
on the answer moves that branch into a flow handler. The one known downstream
owner is Tectum, which the repo owner also owns and has said can be migrated.

## 9. The TUI

- **A refused submission bounces.** The editor clears on *admission* rather
  than on send; a refusal leaves the text where it was and raises a toast
  carrying the entry's `sentence`. The editor already holds unsubmitted text
  for the steer-reclaim gesture (`docs/TUI-STEERING.md`), so this is the same
  ownership rule applied one case wider.
- **The entry draws a row in the chat.** It did not: the chat transcript is
  built over messages and a `customEntry` contributes none — and the message
  list the transcript renders IS `session.context`, the model's own input, so a
  synthetic message injected for display would be a message the model read.
  `MessageList.set_extension_request` therefore mounts an `ExtensionRequestBox`
  BESIDE the messages rather than among them, at the tail, and every reload site
  goes through one `TauApp._reload_transcript` so the row cannot be forgotten at
  one of them. **Only the request at the cursor is drawn**, which is every
  request a user can act on; one buried mid-path is inert (§7) and is read in
  the tree browser, whose detail pane already renders a `customEntry` from its
  `preview`. The row carries no widget id, because Textual defers `remove()` and
  a replacement row collided with the one being removed.
- **Clicking the row re-opens the ask.** The modal is dismissable, so it is
  never a trap; the row is how it comes back.
- **On resume the ask opens by itself**, when the cursor is the entry *and*
  its actions resolve to registered commands. Render always, auto-open only
  when the buttons would do something. The same rule fires at the turn edge, so
  a request an extension raises during a turn is on screen the moment the turn
  ends rather than the next time something reloads.

The label a head shows is a function of the two keys:

| `lock` | `ask` | Sentence |
|---|---|---|
| — | present | Extension X requests a response |
| set | — | Extension X requires intervention |
| set | present | Extension X requires a response |

Every other head substitutes a poorer control and is still correct: a CLI can
print the sentence and the choices and take a numbered answer. That is
`docs/TUI-STYLE-GUIDE.md` §2's rule read in the other direction — the TUI is
allowed to be richer, and is not allowed to be the only one that works.

### 9.1 The other half: a message an extension appends (built 2026-09-07)

The row above is for a `customEntry`, which contributes no message. Its sibling
is `api.send_message`, which contributes one — and reported from a live session,
that message did not show up. Three symptoms, one cause and one half-cause.

The cause: **`_append_custom_message` announced nothing.** A `customMessage`
belongs to no completion and no tool call, so the whole `AgentEvent` stream is
silent about it, and the TUI's live transcript is built from that stream. The
node was on the tree and no head had been told. It now emits on a
`custom_message` channel that `RenderRouter` forwards as
`{"kind": "custom_message", …}`, and `ChatDisplay._on_custom_message` mounts it
through the SAME `add_persisted_message` the reload path calls — one widget kind,
live and reloaded, which is the property whose absence produced symptom 3.

The half-cause: **a command never re-read `session.context`.** A turn rebuilds
`TauApp.messages` when it ends; command dispatch did not. So a message appended
from a command handler existed on the tree and in no list the app reads: mounted
by the channel, then gone at the next window rebuild, and back only after a
restart. `TauApp._resync_working_list` runs at both command doors.

The three symptoms this explains:

1. **From a tool, the note appeared only when scrolled to.** No event, so no
   widget; the turn edge did rebuild `messages`, so a later window rebuild found
   it.
2. **From a command it was labelled "system", then vanished, then came back as
   "Extension" after a restart.** The "system" box was never the note: it is the
   command's RETURN VALUE, which `_render_command_output` mounts as display-only
   chrome and deliberately keeps out of `messages`. `examples/45_holy_grail.py`
   returned the same string it appended, so the echo and the note read alike.
   `/ni` now returns nothing — the note is the feedback.
3. **The tree browser says "custom", the preview box says "Extension".** Both are
   right about different things: `custom` is the message's `role` on disk,
   `Extension` is `ROLE_LABELS["custom"]`, what a reader is shown.

The announcement is fire-and-forget on the running loop, like
`route_session_event`. Off the loop it is not always an error, so
`EventBus.has_listeners` decides: nobody subscribed is a headless script losing
nothing; somebody subscribed is a head that would silently fall behind the tree,
and that raises.

**A separate defect the same report surfaced: extension text was parsed as Rich
markup.** Textual's `Static` interprets console markup by default, so an ask body
reading `rm -rf [build]` rendered as `rm -rf` — content deleted, no error. Every
`Static` carrying an extension's own words now passes `markup=False`, and
`ExtensionRequestBox`, which needs markup for its own bold and dim, escapes the
sentence it interpolates. The answer to "is the body Markdown?" is therefore: it
is neither, and now it is plain text. Markdown in an ask body is absent for the
same reason §10 lists the rest — see there.

## 10. What is absent

- **No ancestry walk, ever.** §2 and §7.
- **No release entry.** §4.
- **No new RPC verb, and no entries on the wire.** The lock reaches a head on
  the `submit` refusal, which now carries the whole entry — `entry_id`,
  `extension`, `sentence`, `label`, `release`, `ask` — in the
  `SUBMISSION_REJECTED` error's `data`, beside the `submission_id` it always
  carried. `pending_request` and `answer_request` are triaged `NOT_EXPOSED` in
  the RPC audit with that as the reason.
- **The wire addition §10 predicted was not needed.** The design assumed the ask
  would travel as an `{"type": "extension", …}` record and therefore need an
  event type on `WireEvent`. It does not: the ask lives on the tree, so a head
  learns of it from the refusal it caused, and no record is emitted at all. The
  gap that remains is the pre-existing one — `notify`/`status`/`panel` records
  reach a `--mode json` sink and not an RPC client — and it is not this design's
  to close.
- **Nothing tells an idle RPC host that a request is pending.** A host learns at
  the moment it matters, which is when it tries to submit. Between submissions
  it cannot ask. That is deliberate: a second reader of the cursor could
  disagree with the one `submit` uses, and the refusal is the only reading that
  can be acted on.
- **No timeout on an ask.** An unanswered ask is exactly a lock that is still
  set, which is the point.
- **No enforcement that a form was filled.** §3.
- **No per-field answer validation beyond `validate_form_values`.** That much
  IS enforced — `answer_request` refuses an undeclared key, a missing declared
  field, a type mismatch, or a value outside its options, and persists nothing
  when it does. A handler that wants more checks its own argument.
- **No enforcement of §4.1.** A request raised mid-turn is silently inert.
- **No aggregation.** Two requests raised in one turn leave two entries, of
  which only the last is at the cursor; `30_permission_gate` collapses its own
  to one rather than τ doing it.
- **No Markdown, and no markup, in a `sentence` or an ask body.** §9.1 made it
  plain text on purpose. An extension's own words are shown, not parsed: a body
  is one of the three `validate_panel_spec` shapes (`text`, `list`, `table`),
  and the shape is what gives it structure. Rendering the text as Markdown is a
  head decision that could be taken later — `MessageBox` already has a
  `"markdown"` source — but it must be taken for the WHOLE vocabulary at once,
  or an extension author has to know which of four fields is parsed.
- **`send_message` has no channel of its own on the RPC wire.** §9.1's
  `custom_message` is an in-process bus channel that `RenderRouter` consumes;
  `tau --mode rpc` and `tau -p` still learn of the node only from
  `get_messages`. That is the same pre-existing gap as `notify`/`status`/`panel`
  above, now one item wider.
- **`display: False` is stored and read by nobody.** `create_custom_message`
  records it, `convert_to_llm` does not consult it, and neither transcript path
  does — so a node marked not-for-display renders anyway. Pre-existing, found
  while fixing §9.1, and left alone because changing it changes what already-saved
  sessions show.
