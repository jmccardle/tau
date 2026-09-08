# The τ interface style guide

**Built 2026-09-04.** How τ's interfaces are put together, and what a person adding
one has to supply. Five audiences, in the order the constraints stack:

| § | Audience | The rule in one line |
|---|---|---|
| §1 | Someone changing the Textual TUI | Reuse the shell; state only what differs. |
| §2 | Someone writing a second head (web, VS Code) | Render the registry, never a branch per name. |
| §3 | Someone writing an extension with no head code | Declare a spec; every head draws it. |
| §4 | Someone writing an extension *and* head code | Put the capability below the seam and the flair above it. |
| §5 | Someone reading `tau.tcss` | Why each rule is what it is. |

§1 is Textual-specific. §2 and §3 are not: they describe what any head must do, and
they are the reason a web client does not need this document rewritten for it.

---

## 1. The TUI: one shell, six role classes

### 1.1 What the shell is

Every modal in τ is a frame, a title, an optional body, and a row of buttons, with
`escape` meaning cancel. Eleven `ModalScreen` subclasses arrived at that shape
independently, and the duplication was measured before it was removed:

| Copied thing | Before | After |
|---|---:|---:|
| `Binding("escape", "cancel", …)` lines | 9 | 2 |
| `action_cancel` bodies | 9 | 3 |
| `on_button_pressed` id-dispatch bodies | 8 | 4 |
| `Container(id="…-dialog")` openers | 12 | 2 |
| Rules in `tau.tcss` | 136 | 115 |
| Selectors sharing the dialog-title rule *byte for byte* | 7 | 1 |

`tau_coding_agent/dialogs.py` holds three classes:

- **`TauDialog`** — the shell. Owns the frame, the centred title, the `escape`
  binding and `action_cancel`. A subclass supplies `compose_body()` and two facts:
  `DIALOG_ID` (so the stylesheet can size it) and its title.
- **`ChoiceDialog`** — a question with a fixed set of answers. `CHOICES` pairs each
  button with the value it dismisses with, stated once rather than rebuilt inside an
  event handler.
- **`TextDialog`** — write something and hand it back. Three screens had each copied
  this shell; they are now three subclasses that set a label and an id.

The three remaining `action_cancel` bodies are not oversights.
`SessionPickerModal`'s escape is two-stage (out of the filter first, out of the
picker second), and the tree browser's discards a pending mark.

### 1.2 The two rules for adding a dialog

1. **Subclass, do not compose.** `test_dialog_shell.py` fails a subclass that
   overrides `compose()`. If the shell cannot express what you need, change the
   shell — the next dialog will want it too.
2. **Declare `DIALOG_ID`, and give it a sizing rule.** The id exists to carry
   `width` and `height`; a dialog inheriting the placeholder id has no rule to match.
   Both halves are tested, because the first time this shell was applied
   `#tree-browser-dialog` vanished from the DOM and the failure surfaced four files
   away in an appearance test.

### 1.3 The role classes

Style by role, not by dialog. The six are:

| Class | Role | Why it is separate |
|---|---|---|
| `.tau-dialog` | the frame | one background, border and padding for every modal |
| `.tau-dialog-title` | the title row | replaced seven byte-identical id rules |
| `.tau-dialog-help` | a paragraph naming a consequence | left-aligned; a centred paragraph is unreadable |
| `.tau-dialog-hint` | one line of key hints | centred, because it *is* one line |
| `.tau-dialog-buttons` | the button row | `Vertical.tau-dialog-buttons Button` takes the full width |
| `.tau-dialog-input` | anything typed or picked into | one focus ring, declared once |

`help` and `hint` stay separate deliberately. The screens this replaced had four
different treatments between them for what turned out to be these two roles.

A per-dialog id rule may state `width`, `height`, `padding` and `overflow` — the
things that genuinely differ — and nothing else. Colour, border and text style come
from the class or they are a bug.

### 1.4 Colour

**No hex literal ever appears in `tau.tcss`.** Every colour is a `$tau-*`
variable supplied by the active theme (`themes.py`), and
`test_themes.py::test_the_structural_stylesheet_names_no_colour` fails on one. The
variables are **roles, not hues** — `$tau-role-user`, never "cyan" — and the six
`$tau-text-*` steps are a ramp from brightest to faintest. Where two rules
deliberately share a hue they share the *variable*, so the pairing survives a theme
swap by construction rather than by two literals happening to agree.

### 1.5 The stylesheet's shape

`tau.tcss` (named `parley.tcss` until the same day) went from 1206 lines to 422 (65.0%), with every selector and every
declaration set unchanged — verified by comparing the parsed rule sets, and by the
snapshot suite passing untouched. Three things account for it:

- **410 lines of prose moved here**, to §5. A run of comment lines in this repo is
  prose that outgrew its place and drifts silently because nothing checks it; the
  same rule deleted 13 066 such lines from the Python tree in `cf3e30f`. A
  *single-line* comment stating a dependence stays in the stylesheet.
- **A rule with three or fewer declarations that fits in 98 columns is one line.**
  Sixteen `ZoneTree > .tree--zone-*` rules are a colour lookup table, and a table
  reads better as sixteen rows than as sixty-four. Consecutive one-liners get no
  blank line between them, so the table looks like one.
- **Duplicated bodies became one rule.** 135 rules carried 105 distinct bodies
  before; the dialog scaffolding was most of the gap.

---

## 2. Capabilities, reads, flows and views: what every head renders

This section is head-agnostic. A web or VS Code client implements exactly what
follows and gets every built-in gesture and every extension-declared one, without a
branch per command name.

### 2.1 The four records

`tau_agent_core/capabilities.py` is the one table the five vocabularies (slash
commands, the palette, key bindings, RPC verbs, CLI flags) project from. Today it
holds 30 capabilities (13 reads, 17 mutations), 10 domains, 9 flows and 2 views.

- **Capability** — one read or one mutation, declared here, performed by the core,
  addressable by name from anywhere. It states what it takes (`arguments`) and what
  it gives back (`returns`, one JSON Schema each).
- **Domain** — a named type, and how its values are found: `free`, a fixed `values`
  tuple, or an `enumerator` capability that computes them.
- **Flow** — an ordered argument list ending in one mutation. Declared here,
  *performed by a head*, because the head is what has a person in front of it.
- **View** — composes reads, holds selection state, starts flows from it. Views are
  head code and have no record beyond a name, because declaring them centrally would
  force every head to pretend it had the same ones.

### 2.2 The loop a head implements

```
next_step(flow, bound, cursor)  →  FlowStep   (an argument is missing)
                                →  Ready      (perform the mutation)

flow_form_spec(flow, bound, options=…)  →  a ui.form spec, or {} when nothing is needed
enumerate_domain(domain, …)             →  the legal values, right now, with labels
```

A head that implements `next_step` → render → `next_step` → perform can drive every
flow τ has and every flow an extension adds. **Nothing in that loop reads a flow's
name.** `_render_flow_step` in `app.py` is 45 lines and branches on exactly one
name — see §2.4.

### 2.3 How a domain becomes a widget

`Domain.field_kind` is the join between the two vocabularies. `DOMAINS` says how a
value is *found*; `FORM_FIELD_KINDS` says how it is *asked for*, and they are not
the same question. `model_name` and `session_id` are both computed; a head can put
every model on screen at once and cannot put every session there.

| Domain | Found by | `field_kind` | Because |
|---|---|---|---|
| `text` | free | `text` | |
| `number`, `integer` | free | `number` | |
| `boolean` | fixed values | `confirm` | two values is a checkbox |
| `model_name` | `get_models` | `select` | a config has a handful |
| `extension_name` | `list_managed_extensions` | `select` | likewise |
| `message_id_scope` | fixed values | `select` | three, and they never change |
| `session_id` | `list_sessions` | `text` | a person may have hundreds |
| `path` | `complete_path` | `text` | any path is legal, even a new one |
| `message_id` | `complete_message_id` | `text` | a long conversation has thousands |

An argument at cardinality `many` whose domain is a `select` becomes a
`multiselect`; that is why `multiselect` is not sayable by a domain, and
`Domain.__post_init__` refuses it. The other consistency rules are enforced too: a
`free` domain must be `text` or `number`, a fixed-value one `select` or `confirm`.

### 2.4 The one substitution a head may make

> **A head may substitute a control RICHER than the `field_kind` names. It may never
> substitute a poorer one.**

`session_id` says `text`. The TUI answers it with a filtered, sortable session
picker, which is strictly more than a text box. That is the single name
`_render_flow_step` branches on, and the branch is legitimate under this rule.

The rule read the other way is what it is for: a head must not render `model_name`
as a free text box because writing a `select` was inconvenient. Doing so accepts
values the domain does not admit, which is the silent failure the registry exists to
prevent. `flow_form_spec` enforces the same thing from below — a `select` argument
with no options **raises** rather than degrading.

### 2.5 Views

`VIEW_COMMANDS` declares a name and nothing else. A head that has the view opens it;
a head that does not says so out loud (`COMMAND_NOT_SUPPORTED` on the wire,
`UnsupportedCommandError` in process). Neither may send the word to the model as
prose — that is the silent fallback `commands.py` was built to prevent.

τ projects no view *state* yet, so today every `View` carries
`unavailable_because` instead. See docs/VSCODE-HEAD.md §6: the session tree is not
on the wire at all, and that is the blocking item for a second head, not any part of
this guide.

---

## 3. Extension UI with no head code

An extension declares a **spec**, and every head renders it. It writes no widget
code, imports nothing from `tau_coding_agent`, and works on a head it has never
heard of — including the headless one.

### 3.1 The three declarative surfaces

| Surface | Validator | Shape | Who declares it |
|---|---|---|---|
| `api.ui.form` | `validate_form_spec` | `{title?, fields: [{name, kind, label?, default?, options?}]}` | an extension |
| `api.ui.panel` | `validate_panel_spec` | `{title, text \| list \| table, actions?}` | an extension |
| a **flow** | `flow_form_spec` | the same form spec, built from the registry | `capabilities.py` only |

The third row is the newest and the one with a gap in it. `flow_form_spec` builds a
form spec from the registry, so every declared flow is rendered by the same screen
an extension's `ui.form` gets. Before 2026-09-04 it was not: `_render_flow_step`
computed a domain's legal values and printed them into a notification, so a built-in
got a toast where an extension got a dialog.

**An extension can declare a flow** since 2026-09-05 — `api.register_flow`,
docs/EXTENSION-FLOWS.md. `FLOWS` is still a frozen tuple; what an extension declares
arrives as a `Vocabulary` layer over it, per session, and every pure function here
takes one as a defaulted parameter. Such a command gets `next_step`'s partial
binding, the slash vocabulary's completion and the palette entry, on every head, with
no head code naming it.

**One argument per flow**, refused at registration rather than truncated: an
extension flow ends in a handler taking one typed line, and splitting one line
across two arguments has no rule. An extension wanting a multi-argument gesture
still drives `ui.form` itself and collects the answers in its own handler. See §6.

### 3.2 The field kinds

Five, closed: `text`, `number`, `confirm`, `select`, `multiselect`. A
`select`/`multiselect` **must** carry a non-empty `options` list of strings.
Fail-Early throughout: an unknown kind, a duplicate field name, a missing `name`, or
a select without options raises rather than dropping the field.

A cancelled form dismisses with `None`, never a fabricated answer set. The headless
path refuses outright unless the user opts in with `--ui-defaults form=defaults`.

### 3.3 The known limitation

The form hands back the string it **displayed**. Two of the three select domains
label a value with something other than the value — `extension_name` reads
`"/x/a.py (enabled)"` and `session_id` reads the session's name — so the head keeps
a `{label: value}` map and translates. `app.py`'s `_select_options` raises if two
values share a label, rather than picking one.

The fix is to let an option be `{value, label}` rather than a bare string. That
changes `validate_form_spec`, which extensions already depend on, so it is **not**
done here. A second head should copy the translation, not invent a second contract.

---

## 4. Extension and head together: where the seam is

τ is FOSS and the Textual head is meant to be modified. Three shapes of contribution,
and the guide's whole point is that the first two must keep working when someone
does only the third.

### 4.1 The rule

> **A capability belongs below the seam. A gesture belongs above it.**

Below: anything that changes the session, reads the tree, calls a model, or touches
disk. It goes in the extension, reachable by name from every head, and it is
testable with no terminal attached.

Above: how a person triggers it — a key chord, a panel button, a bespoke widget. It
may be as clever as you like, because nothing else depends on it.

The test of a correct split is: **remove the head code and the extension still
does its job, one gesture less conveniently.** If removing the widget removes the
capability, the capability was written in the wrong place.

### 4.2 The three shapes

**Agent-side only.** Register commands, tools and shortcuts. A vanilla head drives
all of them: commands appear in the slash vocabulary and the palette, and
`ui.form`/`ui.panel`/`ui.confirm`/`ui.select` get real screens. A gesture needing
several arguments asks for them with one `ui.form`. Most extensions should stop
here.

**Head-side only.** A fork adds a widget, a key binding, a status line. It calls
existing capabilities by name. Nothing about the core changes, so the extension
ecosystem is unaffected.

**Both.** New capabilities *plus* a bespoke UI that drives them. This is where the
seam earns its keep, and where it is easiest to get wrong. Declare the capability
and the flow first; make them work through the generic form; *then* add the widget
that makes it shine. In that order the extension degrades to shape one on a head
that lacks your widget, instead of failing.

### 4.3 What a head must not do

- **Never read `result_dict["details"]`** and assume it arrived. All eight built-in
  tools compute it and nothing reads it (docs/VSCODE-HEAD.md §6.1); it is not on the
  wire.
- **Never put a head-local resource in the core.** The clipboard is the standing
  example: an image-paste reader belongs in `tau-coding-agent`, never in
  `tau_agent_core` (docs/HEADS-AND-MULTIPLEXER.md §2).
- **Never resolve a slash command the core did not resolve.** If the core says it
  does not know the name, the head says so; it does not guess and it does not send
  the word to the model.

---

## 5. `tau.tcss`, rule by rule

The prose that used to sit in the stylesheet. Each heading is the selector it
belonged to.

#### `Screen`

τ's structural stylesheet — clean, minimal, fast.
STRUCTURE ONLY. Every colour in this file is a `$tau-*` variable supplied by the active theme (tau_coding_agent/themes.py, docs/PLAN-0.9.4.md §6): a hex literal here would be a colour no theme could reach, so tests/test_themes.py::test_the_structural_stylesheet_names_no_colour fails on one. `$tau-*` is τ's vocabulary; the unprefixed `$primary`/`$surface`/… that Textual's own widgets use come from the same Theme's design tokens.
The variables are ROLES, not hues — `$tau-role-user`, not "cyan" — and the six `$tau-text-*` steps are a ramp from brightest to faintest. Where two rules deliberately share a hue (the branch-summary pair, the hover divergence), they now share the VARIABLE, so the pairing survives a theme swap by construction rather than by two literals happening to agree.


#### `ModalScreen`

Every dialog sits in the middle of the terminal, not in its top-left corner. Textual gives a ModalScreen no alignment of its own, so a dialog narrower than the screen anchors at 0,0 and reads as a rendering accident rather than an overlay. One rule rather than one per screen: the type selector matches every ModalScreen subclass, so a new dialog is centred the day it is written.


### The dialog shell (tau_coding_agent/dialogs.py, docs/TUI-STYLE-GUIDE.md §1)

#### `.tau-dialog`

Every modal is a frame, a title, a body and a row of buttons. Those four were written seven times each as per-dialog id rules — the title rule alone was byte-identical across seven selectors — so a new dialog was unstyled until someone remembered to add its own copies. They are classes now, and an id rule below states only what genuinely differs: width, height, and the two dialogs that want their own padding.


#### `.tau-dialog-help`

A paragraph naming a consequence before it happens. Left-aligned: it runs to several sentences, and a centred paragraph is unreadable.


#### `Vertical.tau-dialog-buttons Button`

Stacked buttons are full-sentence choices, so they take the width and drop the side margin; each already carries a border, so a blank row between them would buy separation that is already there and cost one row per choice.


### Sidebar

#### `#sidebar`

A share of the width, not a fixed 30 columns. 30 is 37.5% of an 80-column terminal — more than a third of the screen spent on a list that is often nothing but "No sessions yet". A quarter instead, floored at 24 (below that an elided session name is shorter than the words in it) and capped at 32 (one row per session; columns past a full title show nothing more). At 120 columns that is the same 30 it always was, so the wide-terminal look is unchanged.

CLOSED at startup (docs/SESSION-UX-REDESIGN.md §8, decision 4). The session picker (--resume / /resume / the palette's "Resume session…") is the canonical way to reach a saved session now, so the first frame does not spend a quarter of its width on a list nobody asked for. ctrl+b opens it, and from that point TauApp._apply_side_columns owns the display through an inline style — this rule only decides the frame before anyone has pressed anything.


#### `.sidebar-title`

One line, not three: the title says which program this is and nothing else, so it does not need two blank rows of its own.


#### `.chat-list-item`

One row per session, cut visibly, with a rule under it. A session name is a sentence and the column is ~20 columns wide, so the default wrap spent two or three rows on a single entry and — with no boundary between entries — the list stopped reading as a list at all. `text-wrap: nowrap` holds each name to its own row and `text-overflow: ellipsis` marks where it was cut (same contract as the tree browser's `_elide`: a reader can see that there is more). The bottom rule is the entry boundary, in the same quiet gray the panel host divides itself from the chat with.


#### `#chat-list`

Vertical padding only. The horizontal column is the scarce one — 24 columns of sidebar already spend 1 on the border, 2 on the scroll gutter, 2 on the item's own padding and 2 on its bullet — and an inset that only shortens the session names buys nothing the item padding does not already give.


### Chat Display

#### `#chat-placeholder`

The empty-state pane (handoff §4.4). One color, because the label/value hierarchy inside it is drawn with Rich's `dim` — a RELATIVE style that composes with whatever color lands here, so a future FFwF theme restyles this pane by changing this one line and nothing in app.py. `height: auto` keeps it out of the way: it is a child of the scrolling chat column, not an overlay, and ChatDisplay hides it the moment the first message box mounts.


#### `#chat-display .chat-fold`

"⋯ 750 earlier · scroll up to load, click for all" — the row that stands where the window declined to mount the older messages (docs/PLAN-0.9.4.md §1, docs/TRANSCRIPT-WINDOW.md). Its twin `.chat-fold-later` says the same thing pointing down, and is present only while the reader has slid the window back into history; it carries `.chat-fold` too, so this one rule dresses both. Same muted, centered, italic treatment as the tree pane's fold rows, because it is the same statement: this is a fold marker, not another message. `link`-less but clickable, so the underline of a real link does not promise a URL.


### Message Styling

#### `.chat-message`

ONE universal widget (MessageBox) renders every message. The role selects a border color + label on the BOX ITSELF, so an assistant's reasoning + answer + tool boxes all sit inside one titled border (one completion = one box).

No VERTICAL padding: the border already separates the box from its neighbours, and a padding row above and below every message cost two rows per message for nothing. One column left and right stays, so text does not sit against the border glyph.


#### `.message-reasoning, .message-tools`

The lazy reasoning / tools slots collapse to nothing when empty, so a plain user/system message looks exactly like a single text box.


#### `.message-content`

Textual's Markdown default is `padding: 0 2`, which sat inside the box's own padding and border — six columns of chrome per side before a character of text. The box supplies the inset now.


#### `.chat-message MarkdownH1, .chat-message MarkdownH2, .chat-message MarkdownH3, .chat-message MarkdownH4, .chat-message MarkdownH5, .chat-message MarkdownH6, .chat-message MarkdownParagraph, .chat-message MarkdownBlockQuote, .chat-message MarkdownBulletList, .chat-message MarkdownOrderedList, .chat-message MarkdownList, .chat-message MarkdownListItem, .chat-message MarkdownTable, .chat-message MarkdownFence`

=== Markdown block spacing === Textual's markdown block widgets each carry vertical spacing of their own: MarkdownHeader `margin: 2 0 1 0` (H3-H6 `margin: 1 0`), MarkdownParagraph `margin: 0 0 1 0`, MarkdownFence `margin: 1 0` plus `padding: 1 2` on its inner Label, MarkdownBlockQuote `margin: 1 0`, the bullet/ordered lists `margin: 0 0 1 0`, MarkdownTable `margin-bottom: 1`, MarkdownHorizontalRule `padding-top: 1; margin-bottom: 1`. After the message box's own padding was zeroed those are the ONLY remaining blank rows in a message — a blank row above and below every code block, one after every paragraph, three around a heading. On a 24-row terminal a four-block answer spent a quarter of the screen on them.
Every vertical value goes to zero; every horizontal value is left exactly as it was (the blockquote's `padding: 0 1`, the fence Label's 2 columns, the list items' `margin-right: 1`), because the horizontal inset is what keeps a fence or a quote legible as a block and costs no rows.
CONSEQUENCE, deliberately accepted: paragraph breaks in an assistant answer are now carried by nothing but the sentence ending short of the right margin — two consecutive paragraphs abut. Headings, fences, lists and quotes still separate themselves visually (they are styled), so the loss is confined to prose paragraph against prose paragraph.
Scoped under `.chat-message` — the MessageBox — so it covers all four Markdown widgets τ mounts (the message body, a reasoning region, a tool box's args and result, all of which live inside a MessageBox) and reaches no Markdown widget outside the transcript.


#### `.chat-message MarkdownFence > Label`

The fence's inner Label is where its `padding: 1 2` lives — a blank row above and below the code itself, on top of the fence's own margin. Two columns of left/right inset stay.


#### `.chat-message MarkdownFence`

Give the fence back its horizontal scrollbar. Textual 8.2.7's MarkdownFence is already scrollable — `overflow: scroll hidden`, `allow_horizontal_scroll` True — but its own CSS sets `scrollbar-size-horizontal: 0`, so a line wider than the box is clipped with nothing on screen saying so and nothing to drag. Code fences are where τ prints a traceback and a provider's error text, both of which are long single lines; a reader who cannot reach the end of one is reading a truncated error and cannot tell.
`overflow-x: auto` rather than the widget's own `scroll`, so the bar appears only for a fence that actually overflows — a short code block looks as it did, with no permanently reserved row under it.


#### `.chat-message MarkdownBlockQuote > BlockQuote`

A blockquote's inner BlockQuote carries `margin-top: 1` (its `margin-left: 2` is the horizontal inset that makes it read as a quote — kept).


#### `.chat-message MarkdownHorizontalRule`

A rule is a one-row border-bottom with a row of padding above it and a row of margin below; the line itself is the separator.


### Foreign lanes (B3-b)

#### `.chat-message.lane-foreign`

Every box belonging to a turn this frontend did not originate — a bus/timer submission's bubble, a forked sub-agent's steps, the answer promoted out of either. ONE rule for all of them (including a source this build has never heard of), so "not the main line" is a single glance; the border TITLE says which source, and the border SUBTITLE who. Dashed + mauve so it reads as adjacent to the conversation rather than part of it.


#### `.reasoning-region, .tool-box, .exchange-box`

=== Collapsible density (reasoning regions, tool boxes, exchanges) === Textual's Collapsible spends three rows on a COLLAPSED one: a `border-top: hkey` rule, the title, and a `padding-bottom: 1`. Stacked — an exchange holding a step holding two tool calls — that is most of a screen of chrome around a handful of title lines. Every collapsed collapsible is one row here, and an expanded one puts its content flush against the box.


#### `.reasoning-region > Contents, .tool-box > Contents, .exchange-box > Contents`

ONE column of left indent inside an expanded collapsible, and nothing on the other three sides. Nesting is otherwise conveyed only by the enclosing border, so a `▼ Thought` nested inside an answer and a `▼ ✓ read(…)` that is a SIBLING of that answer sat at the same left offset and read as siblings. The signal a reader actually uses is the vertical alignment of the `▼` markers; one column is enough to break it, and on an 80-column terminal it costs 1.25% of the line — cheap next to the two rows Textual's own Collapsible padding used to cost.


### Reasoning / thinking regions (collapsible, dim+italic like pi)

### Tool call + result boxes (one collapsible per call)

### Exchange grouping (one user→answer span; summary line title)

#### `.exchange-box.exchange-foreign > CollapsibleTitle`

A foreign lane's exchange (B3-a set the class; B3-b gives it the rule it never had). Same mauve as the boxes inside it, so the whole span reads as one not-the-main-line unit.


### Pending steering input (docs/TUI-STEERING.md §3)

#### `#pending-input`

Text typed during a turn, waiting for its delivery point. Sits between the transcript and the editor; hidden (display:none, set in code) with nothing pending, so an ordinary turn costs no rows. `max-height` rather than a fixed height because the buffer grows by a line per message, and a tall paste must not push the editor off the screen. Dim, and no border: it is a note about the editor below it, not a third pane.


### Attached files (docs/FILE-ATTACHMENTS.md §4)

#### `#attachment-bar`

Directly above the editor, describing the draft still in it. Hidden (display:none, set in code) when the draft attaches nothing, so an ordinary turn costs no rows. Bounded and scrollable for the same reason #pending-input is: five attached files must not push the editor off the screen.


#### `.attachment-row`

One attached file. The whole row is the click target that removes it, so it highlights on hover — a row that does something on click and looks like a label is the affordance failure this hover state exists to prevent.


### Input Styling

### Slash-command completion (docs/SLASH-COMMANDS.md)

#### `#command-popup`

Under the editor. Hidden (display:none, set in code) unless the line starts with a `/`, so an ordinary turn costs no rows. No border and no max-height: the widget caps its own row count (CommandPopup.MAX_ROWS) so that the SELECTED row is always inside the window — a scrollbar could not do that, because a Static has no selected line to scroll to.


#### `#command-popup.command-popup-unknown`

The one row that is not a suggestion: this `/…` names no command, so the line will be sent to the model as text. `role-blocked` rather than `role-error` — nothing has failed, and nothing will; the line is simply about to mean something other than what the slash suggests.


### System Prompt Editor Modal

#### `#prompt-editor-dialog`

80x25 flat, which is one row taller than an 80x24 terminal and one column wider than its usable width: the buttons rendered off the bottom edge, where they cannot be clicked. The preferred size is kept — an editor wants room — but it is now a ceiling rather than a demand, and the `1fr` textarea absorbs whatever the clamp takes away.


### Session Picker Modal (docs/SESSION-UX-REDESIGN.md §6)

#### `#session-picker-dialog`

The picker chooses among SAVED SESSIONS; the tree browser below it navigates inside one. They are different screens and this is the one that does not need the whole terminal: three narrow columns and a title, so it is a share of the screen the `ModalScreen { align: center middle }` rule above then centres. `max-width` because the title column stops earning columns somewhere around a full sentence, and a 200-column terminal would otherwise draw a 200-column row of mostly whitespace.


#### `#session-picker-filter`

Present but quiet until `/` puts the cursor in it: an always-visible box is how the reader learns the filter exists, and a bordered empty input at the top of the dialog would otherwise read as the thing to type into first.


#### `#session-picker-status`

Exactly one row, whatever the cwd is. A `height: auto` status wrapped a long absolute path over three lines on an 80-column terminal and took those three rows out of the table — the list is what the dialog is for, and the caption under it must not be able to grow at the list's expense. `text-overflow` puts the cut where the reader can see it.


### Session Tree Browser Modal

#### `#tree-browser-dialog`

Fills the screen. It was 90x30 regardless of terminal size, which left a dead band on a wide terminal and rendered off-screen (clipped top and bottom) on an 80x24 one. The tree is the widest thing in the app — every row is a preview sentence — so it gets the whole width there is.


#### `#tree-browser-body`

Rows on top, the highlighted node in full below — STACKED, not side by side. A column split gave both halves about half the terminal's width, and both of them are text that has to wrap: the tree rows elided down to a few characters and the pane's prose broke every five or six words. Stacking spends rows instead, which is the cheaper currency here — a tree row is one line whatever its width, so the tree loses only the count of nodes it can show at once, while the pane gets the full width its markdown wants. `1fr`/`1fr`: even halves. Below `SessionTreeModal.DETAIL_MIN_HEIGHT` there are not enough rows to halve, and the pane hides.


### Tree browser zones (TREE-BROWSER-AS-EDITOR.md §3)

#### `ZoneTree > .tree--zone-path`

Textual `Tree` rows are not DOM nodes and cannot carry per-row CSS classes, so the browser declares COMPONENT CLASSES on a `Tree` subclass (`ZoneTree` in app.py) and resolves each to a Rich style inside `render_label`. These are the values it resolves to. They live here, not in app.py, because every other colour in this application does — a hex literal in a widget is invisible to a theme swap. Seven of the ten borrow a `$tau-role-*` or `$tau-text-*` variable rather than naming a colour of their own; `$tau-zone-path` is the one zone with no role to borrow, because "on the cursor's ancestry" is about position rather than about a transformation.
Ten classes for ten zone ROLES. Deliberately NOT a class per branch: a branch vocabulary is unbounded and no stylesheet can enumerate it, so §3's answer for telling branches apart is a small fixed palette cycled modulo N, which is a renderer decision and not a set of classes. Nothing cycles a palette yet.
Each rule sets as few properties as it can. `render_label` layers these over Textual's own row styles as Rich spans, and Rich combines spans attribute-wise — so a rule that sets only `color` leaves the hover background and the row's weight intact. A rule that set `background` here would fight `tree--highlight-line`, which is the widget's, not ours.

On the cursor's ancestor chain: the entries whose text is what the model saw at the cursor. Blue, and the only zone that is about POSITION rather than about a transformation — it is what replaces the guide-rail ancestry highlight that fork-nesting (§2) took away.


#### `ZoneTree > .tree--zone-folded`

On the path, but dropped by a splice anchor: in the chain, not in the context. Muted and struck through, which is exactly what happened to it.


#### `ZoneTree > .tree--zone-covered`

The same span seen from the anchor doing the dropping — the cursor is ON the compaction/elide node and these are the rows it covers. Peach rather than grey: this is an answer to "what does this node hide", so it is foreground, not background.


#### `ZoneTree > .tree--zone-marked`

In the multi-select set. The one zone the reader put there on purpose, so it is the loudest.


#### `ZoneTree > .tree--zone-hidden`

Archived, excluded from counts (§3) — view state, never read from the log (§11.2). Today's only members are rows folded away inside a collapsed fork, which are not drawn at all, so this rule has no visible effect until an archive gesture produces a hidden row that IS drawn.


#### `ZoneTree > .tree--zone-copied`

On the clipboard (§7): the node `c` copied and everything under it — what `v` would re-create. The producer landed with the copy gesture (`SessionTreeModal._copied_zone`); the rule is unchanged from the one declared ahead of it, which is what the declaration was for.


### The branch summary and the branch it looks back on (§4.3)

#### `ZoneTree > .tree--zone-summary`

`append_branch_summary` parents the summary at the branch point, so it is ALREADY a sibling of the abandoned branch's first message — the structure is right and only the rendering was missing (§1.2). What a reader needs to see is that these two rows are one relation, and which end of it each row is.
So: ONE hue for both, which is what says "a pair", and a weight difference, which is what says which half. No new colour — both rules take `$tau-role-user`, the hue `.box-user` and `#chat-input:focus` already carry and the one role no other zone had borrowed. Naming the same VARIABLE is what makes the pairing survive a theme swap: a theme cannot break the relation without deciding that a user message and a branch summary are different colours, which is a decision it has to make on purpose. `dim` is relative to whatever the hue becomes.


#### `ZoneTree > .tree--zone-abandoned`

Its sibling — the head of the branch the summary is about. Dimmed: it is the abandoned side, and the summary above it is now the readable account of it.


#### `ZoneTree > .tree--zone-ineligible`

Cannot be the other end of the elide in progress: not on one line of the conversation with the marked node. Painted only while exactly one node IS marked, so this is never the resting state of the tree.
`$tau-text-faint` and no text-style: the row has to recede far enough that the eligible rows read as a shape, and it is still a row the cursor moves through — a strike or an italic would say something happened to the entry, when what is true is only that it cannot be picked right now. `tree--zone-hidden` above is the same colour family for the same reason, one step brighter, because a folded row is a fact about the log and this is a fact about the gesture.


### The hover divergence (§3, step 5)

#### `ZoneTree > .tree--zone-hover-common`

§2 flattened single-child runs into siblings, which took away Textual's `tree--guides-hover` ancestry highlight: 30 messages are 30 rows at one level with no rails between them. `tree--zone-path` replaced the CURSOR's ancestry; these two replace the HOVER's, and answer a question the rails never did — where does the row you are pointing at stop sharing history with where you are?
These are layered OVER whatever the row already carries (`render_label` stylizes them last, across the whole row) rather than replacing it, so a hovered chain crossing a marked or folded row keeps saying so. That is why the shared half sets no colour at all: `underline` composes with the path blue, the marked green and a plain row alike, where a hue would fight all three. The underline is the thread; the colour on the divergent half is what splits it in two.
`$tau-role-assistant` is the borrowed hue (`.box-assistant`, and whatever `$tau-role-blocked` resolves to beside it) — an existing role, and the one that already means "attention, not alarm" here.


#### `ZoneTree > .tree--zone-hover-divergent`

Below the divergence point: the history the hovered node has and the cursor does not. This is the part a reader would be picking up by going there.


### The row's type tag (`user:`, `toolResult:`, …)

#### `ZoneTree > .tree--kind-user`

Every row already says what it is; before these rules it said so in the same colour as the sentence after it, so telling a user turn from a tool result meant reading the word. These paint the tag ALONE — `render_label` stops at the colon — so the preview text stays the neutral colour a sentence should be, and the left edge of the tree becomes scannable.
The hues are the transcript's own `$tau-role-*` variables, deliberately: a user message is the same colour here as the bubble it is a preview OF, so the two views cannot disagree about what colour a role is. Colour only, no weight — the zone rules above use weight, and a tag that was also bold would compete with the mark it is supposed to sit underneath.
Five rules for eleven tags (see `_TREE_KIND_CLASS` in app.py): the reader is separating sides of a conversation, not enumerating entry kinds.


#### `ZoneTree > .tree--kind-structural`

Bookkeeping entries — a compaction, a branch summary, an elide, a model change. These are not turns and should not read as one, so they take the quiet text colour rather than a role hue: present, identifiable, and not competing with the conversation for attention.


#### `#tree-detail-folded`

What stands in for the detail pane while it is folded away (§4a). One row — the whole point of the fold is the rows it returns to the tree, so the marker that makes it reversible must not charge for more than one of them. `height: 1` and not `auto`: `auto` on a Static is one row for this string and two for a string that wraps, which would make the tree's height depend on the terminal's width.


#### `#tree-detail .detail-fold`

"⋯ 3 earlier" / "⋯ 2 branches from here, 5 later" — the rows that keep the pane from reading as a three-message conversation. Muted and centered so they read as a fold marker rather than as another message.


### Detail-pane context messages

#### `.chat-message.detail-context`

The neighbours of the selected node, which must recede without becoming unidentifiable — the reader still tells a user turn from an assistant one at a glance, so the role hue is KEPT and only its strength drops.
`opacity`, not a second table of hand-picked dim hex values, and not `color`/`text-opacity`. A box's body is not all plain text: a tool call's arguments and a tool result are syntax-highlighted by Rich, which writes per-token colors no CSS `color` can reach, and neither `color` nor `text-opacity` reaches a CHILD widget (the Markdown blocks, the tool boxes) because neither is an inherited property. `opacity` composites the whole subtree against the background, so border, title, prose and highlighted code all fade by the same amount and every `.box-*` rule above keeps working unchanged.


#### `#tree-browser-marks`

The selection readout: "2 nodes marked · common ancestor n2 · ~340 tokens (estimate)". It keeps its row on a terminal too short for the detail pane, because a mark placed on the row under the cursor is not drawn on that row (the cursor's own styling owns it) and this line is then the only feedback the gesture produces. Brighter than the help line below it and dimmer than the tree above: it is state, not instruction.
It costs the tree NO rows. `#tree-browser-help` used to hold the dialog's bottom edge off the body with `margin-top: 1`; that blank row is now this one, and the help line sits directly under it. Spending a real row on the readout instead would have taken one from the tree at every height — measured at 120x16, where the tree drops from 10 rows to 9 and `test_the_pane_gives_the_rows_back_to_the_tree_when_short` says so.


### Tree three-mode chooser Modal

#### `#tree-mode-dialog`

`height: auto` alone is not a fit: five 3-row buttons plus their margins, the title and the chrome come to 26 rows, two more than an 80x24 terminal has, and the last two modes rendered off the bottom. Auto still picks the height on a terminal that can hold the list — dropping the inter-button margin below brings that to 21, so 80x24 is now a terminal that can. `max-height` caps it on one that cannot (a sixth mode, or a 15-row window), and `overflow-y: auto` (Container defaults to `hidden`, i.e. clip and say nothing) turns what no longer fits into something the reader can scroll to rather than lose.


### Extension dialogs (confirm / select / input) — E7 §3 / S47

#### `#ext-confirm-dialog, #ext-select-dialog, #ext-input-dialog, #ext-chord-dialog, #ext-form-dialog`

Same ceiling the two tree dialogs learned to keep: a fixed 70 columns is wider than a 60-column terminal and the select list's fixed 30 rows are taller than an 80x24 one, and an off-screen dialog is not a dialog. These are not scenes, so test_tui_appearance's modal guard cannot see them — the bound is the guard.


### Foreign-lane strip (B3-b)

#### `#lane-strip`

One line naming every lane in flight that the user did not type, just above the extension status strip. Hidden (display:none, set in code) whenever the only thing running is this frontend's own turn — or nothing is.


### Extension status strip (E10 §6 / S67)

#### `#ext-status-bar`

One-line keyed status slots from ctx.ui.set_status(), just above the Footer. Hidden (display:none, set in code) until an extension writes a slot.


### Extension panel host (E10 §6 / S68)

#### `#ext-panel-host`

Persistent keyed panels from ctx.ui.panel(), docked to the right of the main area. Hidden (display:none, set in code) until an extension opens a panel.
A share of the width, not a fixed 40 columns. At 80 columns a fixed 40 left the chat 8 columns, 4 of them ChatDisplay padding — the primary content reduced to four usable columns by a secondary surface. 30% instead, floored at 26 (a panel's table wants four short columns inside a round border and a column of host padding per side) and capped at 44 (past that a keyed status panel is mostly whitespace, and the chat should have the rest).
Together with #sidebar this leaves the chat 45% of the width in the middle of the range, less 2 columns of scrollbar gutter and 4 of ChatDisplay padding. Where that stops clearing the 40-column floor asserted in tests/test_tui_appearance.py is TauApp.SIDE_COLUMNS_MIN_WIDTH. The sidebar no longer yields there — it starts closed, so a sidebar beside a panel is one the user opened with ctrl+b, and that is honored at any width.


#### `.ext-panel-action`

An equal share of the row each, rather than each sizing to its own label. A Button defaults to `width: auto`, which here is 16 columns; two of them plus the margin want 33, and since the panel became a share of the terminal (`#ext-panel-host: 30%`, floor 26) its content row is about 30 columns at 120x40 and 19 at 80x24. The Horizontal does not wrap — it clips — so the second button was cut in half on a wide terminal and off the screen entirely on a narrow one, silently, because a clipped widget still reports its full size. `1fr` makes the row divide by however many actions a panel declares, which is a number this stylesheet does not get to know.
`min-width: 0` is the load-bearing half. Textual's Button carries `width: auto; min-width: 16`, and the floor outranks the share — with `1fr` alone each button still measured 17 columns in a 19-column row, i.e. exactly the bug, because a min-width a container cannot honour is not reported as an error, it is just drawn past the edge.


### Code blocks in markdown

#### `Markdown CodeBlock`

INERT AGAINST textual 8.2.7 — kept, and labelled rather than deleted. `CodeBlock` is not a widget this Textual has: a fence mounts `MarkdownFence` holding a `Label`, and nothing named `CodeBlock` appears anywhere in the tree (verified by walking the `tools` scene). So this rule matches zero widgets and `$tau-code-fg` currently paints nothing.
What actually colours a fence is `textual.highlight.HighlightTheme`, whose token styles are written against Textual's DESIGN TOKENS (`$text-accent`, `$text-warning`, `$text-primary`, …) and never against `$tau-*`. That is the one surface a τ theme reaches only through its `textual.theme.Theme` half — see themes.py, and the report in docs/PLAN-0.9.4.md §6: a theme that tunes the τ palette and leaves the design tokens at some other palette's values gets code blocks from a third palette.
Deleting the rule is a structural change, and §10's non-goal on styling still holds; it stays as written so that if a later Textual reintroduces the widget the intent is already here.


#### `(end of file)`

Tool-call / tool-result blocks are rendered by the shared MessageBox widget (see the `.box-toolCall` / `.box-toolResult` rules above), not by a separate Static widget.

---

## 6. Deliberately absent

- **No config key for any of this.** The stylesheet is a file; a fork edits it.
- **No `{value, label}` option shape** (§3.3), so a head still translates.
- **No view state on the wire** (§2.5), so `/tree` cannot open in a second head.
- **No theme beyond Catppuccin.** The FFwF theme is deferred and will be a `.tcss`
  swap, which is why §1.4's no-literals rule is enforced by a test rather than by
  convention.
- **No `search` field kind.** A large closed set (`session_id`, `message_id`) renders
  as `text` and relies on §2.4's substitution, which means a head with no picker
  offers a bare box. Adding a sixth kind would oblige every head to implement it.
- ~~**No way for an extension to declare a flow**~~ (§3.1). **Built 2026-09-05** —
  `api.register_flow`, docs/EXTENSION-FLOWS.md. The prediction here held: it needed
  no new rendering anywhere, only a registration path and an answer to whose domains
  an extension may name. `FLOWS` is still a frozen tuple; a session's declarations
  arrive as a `Vocabulary` layer over it, which every pure function now takes as a
  defaulted parameter. The one limit is **one argument per flow**, so §4.2's
  multi-field case still uses `ui.form`.
