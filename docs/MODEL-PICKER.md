# Models without a session, and a choice field Enter can answer

Built 2026-10-17, and extended 2026-10-18 so the serve hello lists models
(protocol 0.10, §7). `model_name` lists without a session in every head, and a
flow form's single-choice field is a filterable list with its buttons pinned on
screen.

Both faults came from test-driving `/model` on the new-session screen. Neither
was a TUI quirk. The first was a core seam that bound a fact of config to a
session. The second was the shared field renderer every flow form and every
extension ask goes through.

## 1. What was wrong, measured

**The refusal reached the screen.** Typing `/model ` before the first chat showed
`enumerating domain 'model_name' needs a session, and none was passed. An empty
list here would report 'there are none' for a question that was never asked.` in
the completion popup. `enumerate_domain` reached `_model_values(session)` only
through `_require(session, "session")`, and the TUI has no backend until the first
submission runs `action_new_chat`. The same refusal showed under `tau --connect`
even after attaching, because `RemoteBackend` has no `agent_session`. The list of
models was in hand the whole time. `make_model_resolver(config)` builds it with no
session, and the attach answer carries it as `replica.models`.

**The buttons left the screen.** A `select` field was a `RadioSet`, one row per
option. With 22 models in a 100-column terminal, measured with Textual's pilot
against the real `tau.tcss` before any change:

| rows | dialog height | hint visible | Submit visible |
|---:|---:|:-:|:-:|
| 24 | 21 | no | no |
| 30 | 27 | no | no |
| 33 | 29 | no | no |
| 36 | 32 | **yes** | **no** |
| 37 | 33 | **yes** | **no** |
| 38 | 34 | yes | yes |

The dialog's `max-height: 90%` capped the frame, and `.tau-dialog` clips rather
than scrolls, so below 38 rows the button row was cut off. At 36 and 37 rows the
hint ("Submit: confirm") was visible and the button it named was not, which is
exactly what John described. The initial focus was the `#ext-form-fields`
`VerticalScroll`, which has no Enter binding. Enter on a `RadioButton` toggles it.
So the only working sequence was Tab to the button, then Enter, and that only
worked on a terminal tall enough to show the button.

## 2. `model_name` needs a catalog, not a session

`enumerate_domain` takes one object per reader, and now it takes a third:
`models`, a `ModelCatalog` (anything with `model_names()`). Which object answers
which domain:

| domain | object |
|---|---|
| `message_id`, `extension_name` | `session` |
| `session_id`, `path` | `runtime` |
| `model_name` | `session.model_resolver`; with no session, `models` |

**The session wins when there is one.** A session's resolver is what `set_model`
resolves through, so it is what is actually legal for that session. A catalog is
for the case with no session. A head passes the same catalog it binds to the
sessions it creates, so the session list and the no-session list come from one
source and cannot disagree. Locally that is `make_model_resolver(self.config)`,
which is the expression `_rebind_after_session_swap` binds. With neither a session
nor a catalog, the call still refuses rather than answer "none". The refusal is
now one clause (`listing 'model_name' needs a session or a model catalog, and
none was given`). The old wording was an argument, and it was only ever seen by
someone who had done nothing wrong.

Alternatives, and why each loses:

- **An enumeration context object carrying session, runtime and config.** It
  makes every caller build a record to pass one more optional object, and it
  changes every call site, the RPC handler's included, for a domain only heads
  reach without a session. A keyword beside the two that already exist states
  the same dependency for less.
- **Put the resolver on the runtime.** The TUI has no runtime before the first
  chat either, so this moves the problem without fixing it.
- **Have the core read `config["models"]` itself.** Config loading and
  `ConfigModelResolver` live in `tau_coding_agent.backends`. The core would need
  a second reader of the same map, and that is two sources.

Per head:

- **TUI.** `TauApp._model_catalog()` gives the config resolver locally. Under
  `--connect` it gives the `RemoteConnection`, which answers `model_names()` from
  the daemon's hello, attached or not (§7, protocol 0.10). The client's own
  config never applies to a daemon. The completion popup, the flow form's
  options and bare `/model` all pass it. Bare `/model` no longer needs a backend,
  and its `replica` special case is gone.
- **REPL.** It builds its session before the prompt opens, so it always had one.
  Nothing changed.
- **RPC and serve.** `RPCHandler` always holds a session with a bound resolver,
  so `enumerate_domain {"domain": "model_name"}` with no session cannot be
  reached on the wire, and no test claims otherwise.
- **Web (tau-code).** It speaks the serve protocol, so it gets what serve gives:
  models in the hello from protocol 0.10, at attach, and through
  `enumerate_domain` after that. It must follow the bump (§7).

**An entry that does not build is listed, never offered (2026-10-19).** Any
entry a command does not select may fail to build: a malformed one, which
startup warns about, or one whose `backend` no loaded extension has registered
yet (docs/SILENT-DEFAULTS.md §8). `projections.model_catalog` now builds per entry:
RPC `get_models` and the serve hello's `models` list such an entry as
`{name, model: null, error}` instead of failing the whole list, and every other
entry carries `error: null`. A catalog may declare `model_errors()`
(`ConfigModelResolver`, and `RemoteConnection` from the hello); the core's
`flows.unavailable_models` reads it, the `model_name` domain leaves those names
out, so no picker or completion can select one, and the TUI and REPL say which
entries are unavailable and why when the picker opens.

## 3. A dialog whose body scrolls between pinned header and footer

The shell gained `BODY_ID`. A `TauDialog` that sets it is composed as a docked
header (the title and `compose_header()`), a docked footer (`compose_footer()`:
the hint and the buttons), and a `VerticalScroll` with that id holding
`compose_body()`. `ExtensionFormScreen` (`ext-form-fields`) and
`ExtensionAskScreen` (`ext-ask-body`, with the extension's sentence in the
header) use it. Every other dialog keeps the plain stack. `test_dialog_shell`
still forbids a subclass overriding `compose()`, so the change went into the
shell, as TUI-STYLE-GUIDE §1.2 asks.

The layout depends on one Textual behaviour, measured on 8.2.7. A percentage
height resolves against the container's content box minus its **docked**
children. The body is `height: auto; max-height: 100%`, so it shrinks to its
content and stops exactly at the footer. A form with three options is 20 rows
tall on a 60-row screen. Three alternatives were tried and rejected:

- **Footer docked, title in the flow.** The body ran two rows under the footer,
  which is the height of the title and its margin.
- **Body `height: 1fr`.** It fits, but an auto-height frame then always grows to
  its 90% cap. A three-option dialog was 54 rows tall on a 60-row screen.
- **Title and sentence each docked top.** Two widgets docked to one edge
  overlap. They do not stack.

After the change the OK button, Cancel and the hint are inside the frame at 16,
24 and 30 rows (`test_model_picker.py`, parametrised).

## 4. The single-choice field

A `select` field is now a `ChoiceField`: a `ChoiceList` (an `OptionList`) and,
past `CHOICE_FILTER_THRESHOLD` options, a filter `Input` above it. The threshold
is 8, the number of rows a short list shows at a glance. Above it a list is
longer than a person reads top to bottom.

- **The highlighted option is the value.** There is no separate "pressed" state
  to drift from what is shown. A click moves the highlight and does not submit.
  `ChoiceList` rebinds Enter from `OptionList`'s `select` to its own `commit`,
  because `OptionList` handles a click by calling the same `action_select` that
  Enter does, and a click that submits would act on a mis-click.
- **The filter** narrows with `filter_choices`, a case-insensitive substring
  match that keeps the original order. It lives in
  `tau_agent_core.extension_types` beside `CHOICE_FILTER_THRESHOLD`, so a web head
  filters the same way. While the filter has focus, Up, Down, PageUp and PageDown
  move the list, and Enter commits the highlighted option. So "type `son`, press
  Enter" picks a model.
- **A filter that matches nothing has no value.** OK refuses with a notice naming
  the field, and Enter does nothing. Falling back to the default would submit an
  option the person had just filtered out of sight.

A long form opens with focus on the first field's filter, or on its list if the
list is short. Before this change it opened on the scroll container.

## 5. The Enter rule

Enter in a text box or on a choice list **commits that field**:

- In a form with **one** field it submits.
- In a form with **several** it moves focus to the next control after the field.
  After the last field that is OK, so the final Enter is a deliberate second
  press.
- Enter that no control takes, for example with focus on the frame or the
  scroll body, submits. This is the `_FieldForm` binding `enter → submit`.
  Buttons, checkboxes and the multiselect keep their own Enter.

What "submit" means is the subclass's `action_submit`. For `ExtensionFormScreen`
it is OK. For `ExtensionAskScreen` it presses the action when the ask declares
exactly one. With several actions it focuses the first action button instead,
because Enter cannot say which one the person meant.

## 6. The REPL: one typed line, the same choice

The REPL prints a numbered list and reads a line, so it has no live filter. Its
equivalent is `match_choice`, also in `extension_types`. These rules are tried in
order, and the first rule with any hit decides:

1. **A number from 1 to N.** Text that is all digits is only ever a position. A
   number out of range is refused, never read as part of a name, because in a
   numbered list `30` means "the thirtieth", not "the one containing 30".
2. **The exact name**, ignoring case.
3. **The one name it begins.**
4. **The one name it appears in.**

An ambiguous answer is re-asked with the candidates named
(`'claude' matches 2 options (claude-sonnet, claude-opus); type more`). This is
the line-mode form of narrowing a list. The prompt says `[1-N, or a name]`. The
ask's action picker uses the same rule. A multiselect still takes numbers only.

## 7. Absent

- **Serve listing models before attach: built 2026-10-18, protocol 0.10.** This
  was absent in the first build. It needed a wire change, and John approved the
  bump. `HelloResult` now carries a required `models`, a list of `ModelRecord`
  (`{name, model: {id, provider, context_window}}`), the same shape and the same
  list as `Attached.models`. It is on the hello rather than `SessionList` because
  the hello is the one answer every client reads before anything else. The
  daemon builds `make_model_resolver(config)` once, as `Daemon.model_resolver`.
  It binds that object onto every session it loads and lists it in every hello,
  so the two lists cannot disagree. `ServeClient.hello` keeps the answer.
  `RemoteConnection.model_names()` reads it, and `_model_catalog()` returns the
  connection whether the head is attached or not. So the refusal under
  `--connect` is now reachable only in the moment before the hello is answered.
  The hello refuses a version mismatch, so a 0.9 client (tau-code until it
  follows) cannot talk to a 0.10 daemon. RPC stays 2.2: its version is
  independent of serve's, and nothing on the RPC wire changed
  (docs/TAU-SERVE.md §5, the 0.10 built note).
- **Other session-bound domains under `--connect` keystroke completion.**
  `message_id` and `extension_name` still refuse in the popup, because
  `RemoteBackend` has no in-process session. The flow form under `--connect`
  enumerates them on the daemon (`_render_remote_flow_step`) and works. Only the
  per-keystroke popup is affected, and that was true before this change.
- **Names in a REPL multiselect.** `1, 3` only. A comma is a legal character in
  an option name, so splitting typed names on commas needs a rule of its own.
- **Very short terminals.** At 16 rows the body is 2 rows tall. The buttons are
  visible and the filter works, but only a couple of options show at once.
- **A `{value, label}` option shape** is still absent (TUI-STYLE-GUIDE §6). A
  form hands back the label it displayed, and the head maps it back to a value.
