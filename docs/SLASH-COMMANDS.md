# Slash commands in the editor

**Built 2026-08-29; argument values added 2026-09-05.** τ's chat editor says
whether a `/…` you are typing is a command it knows, and Tab completes it — the
command word first, then the values that command's argument accepts. This document
records what the popup shows and when, why Tab is the only key it responds to, and
what a command does with text it was not expecting.

---

## 1. The problem this solves

An unrecognised slash command is sent to the model as ordinary text.
`resolve_command` returns `None` for it and the line becomes a prompt
(`commands.py:161-163`).

That is the right behaviour and it is not going to change. Refusing every
unrecognised slash would break pasting a file path, and `/usr/bin/env is on my
PATH` is a sentence a user is entitled to write.

It is also completely silent. A user who types `/exntesions` gets whatever the
model guesses that meant, and nothing anywhere says the command did not run. The
two outcomes — τ opens the extensions panel, or the model reads a typo and
improvises — look identical up to the moment the answer arrives.

`CommandPopup` says which one is coming, before the Enter key.

## 2. What it shows

The popup sits under the editor and hides itself unless the line starts with `/`,
so an ordinary turn costs no rows. `complete_command`
(`tau_agent_core/commands.py`) decides what it holds, from four rules:

| The line | The popup |
|---|---|
| does not start with `/` | hidden |
| `/` alone | every command τ knows |
| a prefix of one or more names | those names and their descriptions |
| a name τ does not know, no space yet | `/… is not a command — this line goes to the model as text` |
| a name τ does not know, followed by a space | hidden |

The last row is the pasted-path case. Once a space follows a word that names no
command, the line is committed to being prose and a warning about it is noise.

Matching is a **case-sensitive prefix** test on the first word, because that is
what `resolve_command` does with the finished line. `/TREE` offers nothing,
because `/TREE` resolves to nothing. pi fuzzy-matches here
(`tui/src/fuzzy.ts:99`), which would let `/xtn` offer `/extensions`; τ does not,
and §6 says why not yet.

Built-ins come first, and an extension that registered a built-in's name is
dropped from the list rather than shown after it. `resolve_command` gives the
built-in to such a collision, so the extension's command is unreachable by name;
offering it would advertise a command the user cannot run.

### Two vocabularies, one of which arrives late

`FRONTEND_COMMANDS` is τ's own and needs nothing loaded. Extension commands come
from `AgentSession.get_extension_commands`, through the backend — and
`TauApp.current_backend` is built by `action_new_chat`, which the app runs lazily
at the first submit.

So before any chat has started there are no extension commands, and the popup
says `/todo is not a command`. That is accurate rather than a gap:
`on_input_submitted`'s own peek reads the same empty vocabulary, so that line
really would go to the model. The popup is the first visible sign of a blind spot
that was always there.

### The second vocabulary: argument values

Built 2026-09-05, from a report that `/model ` gave no models. Once a space
follows a word that names a command, the command word is settled and the popup
switches to the values that command's argument accepts:

| The line | The popup |
|---|---|
| `/mo` | `/model — switch the active model…` |
| `/model ` | every configured model |
| `/model loc` | `local-llm`, `logan` |
| `/model zzz` | `no model_name matches 'zzz' — /model will refuse it` |
| `/name my session` | `/name — give this session a display name…` |

The split is the same one `resolve_command` / `complete_command` already use.
**`complete_command_argument` is pure and says only which argument is being typed
and over what span**; listing the values is `enumerate_domain`, which needs a live
session and therefore cannot be answered in the core's pure half. The head calls
both — `TauApp._argument_completions` — and it is the same pair
`TauApp._select_options` uses to fill a flow form, so a value offered in the
editor and a value offered in the modal cannot differ.

Nothing about the vocabulary is written in the head. The argument, its domain and
its description come from `FLOWS` and `DOMAINS`; a flow added to the registry gets
completion with no edit here. Five cases return `None` and offer nothing, and they
are different cases rather than one: the name is still being typed; the word is an
extension command that did not declare what it takes (`api.register_flow`,
docs/EXTENSION-FLOWS.md — one that DID completes exactly like a built-in); the
command is a view; the flow takes no argument; or the domain is `free`, where any
text is legal and a candidate list would misstate what is accepted. `/name` and
`/compact` are the `free` case, which is why the table's last row still shows the
command's own description.

`/extensions disable <name>` completes too, by the rewrite `dispatch_builtin`
already performs: the verb names a flow (`EXTENSION_VIEW_VERBS`), so the argument
being completed belongs to `disable_extension` rather than to the view that was
typed. The **verb itself** is not completed — it is not a `Domain`, and inventing
one to hold three words would put a vocabulary in the registry that no flow
declares.

A domain that cannot be enumerated at all — `/model ` with no model resolver bound
— puts `enumerate_domain`'s own refusal in the popup rather than raising. This
redraws on every keystroke, so raising is not available; and an empty list would
say "there are no models" for a question that was never asked, which is the
distinction `enumerate_domain` exists to keep.

## 3. Tab, and only Tab

Tab inserts the selected command with a trailing space. Pressing it again
replaces that with the next candidate, and wraps at the end of the list.

Tab reads the same three vocabularies in the same order the popup does — the
`@…` the cursor is inside, then the argument value, then the command word — so
what is offered and what is inserted cannot disagree. An argument value replaces
**the span the core named**, not the whole line, which is what makes
`/mo` Tab `loc` Tab arrive at `/model local-llm `.

Completing the command word used to rewrite the whole editor, which meant a Tab
on `/nam my session` silently discarded `my session`. It now replaces the first
word only and keeps the rest verbatim.

The cycle needs no mode and no escape key. It is identified by the editor still
holding exactly what the last Tab wrote (`ChatInput._complete`), so typing any
character ends it and the next Tab starts a fresh cycle from the new prefix —
which is what the user meant by typing.

**Tab is claimed only when there is something to insert.** With prose in the box,
or a `/…` that names nothing, the key is left alone and keeps Textual's
`tab_behavior="focus"` — which is the one thing Tab did in this editor before,
and the reason completion could have it.

### Why not a dropdown with arrow keys

Opinion, stated as such. pi's editor opens a select list below the input and
binds Esc, Up, Down, Tab and Enter while it is open
(`tui/src/components/editor.ts:664-729`). τ has already spent four of those five:

| Key | What it means in τ | Cost of a popup claiming it |
|---|---|---|
| Esc | cancel the running turn | Esc becomes three-valued |
| Up | history, or reclaim pending steer on an empty editor (docs/TUI-STEERING.md §4) | a fourth meaning |
| Enter | newline or submit, per `enter_key` (docs/ENTER-KEY.md) | conditional on a setting |
| Tab | nothing | free |

A dropdown is a mode, and every key inside a mode needs an escape hatch. Here the
escape hatch would be the key that cancels your turn.

The one-key design also keeps a property a dropdown loses: **the editor always
holds what will be sent.** With a highlighted row that is not yet in the text,
Enter sends the un-completed line, which is exactly the surprise this feature
exists to remove.

### The trailing space

`/tree ` and `/tree` resolve identically, because `parse_command` strips. The
space is inserted so a command that takes arguments is ready for them, which is
pi's choice too (`tui/src/autocomplete.ts:393`).

## 4. What stray text does

Measured against `parse_command`:

```
'/tree'                        ('tree', '')            -> runs
'  /tree  '                    ('tree', '')            -> runs
'/tree extra words'            ('tree', 'extra words') -> runs, args discarded
'/tree\nmore'                  ('tree\nmore', '')      -> prose
'/usr/bin/env is on my PATH'   ('usr/bin/env', '…')    -> prose
'/'                            ('', '')                -> prose
'//tree'                       ('/tree', '')           -> prose
'/TREE'                        ('TREE', '')            -> prose
```

Two of these are worth knowing.

**Arguments to a command that takes none are silently discarded.** `/tree extra
words` opens the browser and `action_browse_tree` ignores `args`. Same for
`/compact` and `/fork`. This is a Fail-Early violation and it is **not fixed
here**: naming it needs per-command argument metadata, and `FRONTEND_COMMANDS` is
a `dict[str, str]` whose values are prose descriptions read by
`unsupported_command_message` and by the `get_commands` RPC verb. Giving it a
declared placeholder — the shape extension commands already have, through
`AgentSession.get_extension_command_args` — is the fix, and it changes a type
other repositories may read. §6 keeps it as a decision, not a plan.

**Revised 2026-09-03: the metadata now exists, for the flows.**
`tau_agent_core.capabilities.FLOWS` carries an `Argument` tuple per flow, keyed by
the same names `FRONTEND_COMMANDS` uses, so `/model`, `/resume`, `/name`,
`/autocompact` and the three extension flows can each say what they take without
changing `FRONTEND_COMMANDS`' type. `TauApp.action_run_session_flow` already reads
it, which is why `/autocompact` with no argument answers "needs enabled: true,
false" rather than guessing.

One case is still unfixed, and one was. `/tree` and `/extensions` are VIEWS, and
`VIEW_COMMANDS` is a `dict[str, str]` with nowhere to say a view takes nothing —
so `/tree extra words` still discards them silently, which is what §6 records.

`/compact` was the worse of the two and is fixed. The `compact` flow DECLARES an
optional `custom_instructions` argument; `_perform_command_outcome` called
`self.action_compact()`, which took no parameters, so `/compact focus on the auth
bug` ran a compaction that never saw the focus — the registry said what the
command took and the head did not read it.

**Fixed 2026-09-04**, by threading the argument down the path the core had
already built for it: `action_compact(custom_instructions="")` →
`TauBackend.compact_messages(messages, custom_instructions)` →
`AgentSession.compact_messages(messages, custom_instructions)`, which has taken
the parameter since the compaction port and hands it to the summarizer's system
prompt (`compaction.py:497`). Nothing in the core changed. The keybinding and the
palette pass nothing and get the default; empty becomes `None` at the backend
call rather than `""`, because the summarizer appends an "Additional focus"
paragraph for any truthy value and an empty one would be a paragraph saying
nothing.

The two cases were never one bug, which is why only one of them closed here:
`/compact`'s argument was declared and dropped, so the fix is wiring. `/tree`'s
is not declared at all, so the fix is a type change in another package's
vocabulary.

**A second line makes the first word unknown.** `parse_command` splits on the
first SPACE, so `/tree` followed by a newline yields the name `tree\nmore`, which
matches nothing and goes to the model. The popup is the only place this is
visible: the description disappears and the warning takes its place as the
newline is typed.

## 5. How machine-parseable it has to be

Less than it looks. At keystroke time the editor answers one question: **is the
first word of the line a registered command name?** That is a set-membership
test, and `complete_command` runs it on the same `strip()`-then-`find(" ")` that
`parse_command` uses, so the two cannot disagree about where the first word ends.

Nothing else about the line is parsed. Everything after the first space is the
command's business, and no part of τ inspects it until a handler does.

## 6. What this deliberately does not do

- **No fuzzy matching.** Prefix only. Fuzzy would let `/xtn` offer
  `/extensions`, and it also lets a stray keystroke offer something the user
  never meant, one Tab away from replacing what they typed. Worth revisiting on
  its own, once the prefix version has been lived with.

- ~~**No argument completion.**~~ Built 2026-09-05; see §2's second vocabulary.
  It cost no per-command completer callback — pi's `getArgumentCompletions`
  (`tui/src/autocomplete.ts:339-357`) — because `FLOWS` and `DOMAINS` already say
  what each command takes and `enumerate_domain` already lists it.

- **No completion of an `/extensions` verb.** The three words are not a `Domain`,
  and the target after a recognised verb completes without them being one.

- ~~**No `@file` completion.**~~ Built; see docs/FILE-ATTACHMENTS.md §3. The three
  vocabularies are asked in one order by one function rather than merged into pi's
  single provider.

- **No highlighting inside the editor.** Textual's `TextArea` colours text
  through a tree-sitter highlight map, and injecting a synthetic span means
  overriding a private method. How stable that seam is was not checked. The
  popup carries more than a validity colour could — the description — and touches
  no private API.

- **No argument metadata on the built-ins**, and therefore no warning that
  `/tree extra` will discard `extra`. See §4.

## 7. Where the pieces are

| Piece | Where |
|---|---|
| the candidate list, pure | `tau_agent_core/commands.py` → `complete_command` |
| which argument is being typed, pure | `tau_agent_core/commands.py` → `complete_command_argument` |
| the values it accepts, live | `tau_agent_core/flows.py` → `enumerate_domain`, called by `TauApp._argument_completions` |
| the Tab cycle | `tau_coding_agent/chat_widgets.py` → `ChatInput._complete`, `ChatInput.on_key` |
| the widget | `tau_coding_agent/editor_widgets.py` → `CommandPopup` |
| the redraw | `TauApp._refresh_command_popup`, on `TextArea.Changed` |
| the merged vocabulary | `TauApp._extension_command_table` + `FRONTEND_COMMANDS` |
| styling | `tau.tcss` → `#command-popup` |
| tests | `tau-agent-core/tests/test_command_completion.py`, `tau-coding-agent/tests/test_slash_command_popup.py` |

`complete_command` lives in the core beside `resolve_command` for the same reason
that one does: the command vocabulary is τ's, not the TUI's, and a second
frontend asking "what could this become" must get its answer from the same place
that answers "what is this".
