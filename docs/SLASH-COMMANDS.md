# Slash commands in the editor

**Built 2026-08-29.** τ's chat editor now says whether a `/…` you are typing is a
command it knows, and Tab completes it. This document records what the popup
shows and when, why Tab is the only key it responds to, and what a command does
with text it was not expecting.

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
`Parley.current_backend` is built by `action_new_chat`, which the app runs lazily
at the first submit.

So before any chat has started there are no extension commands, and the popup
says `/todo is not a command`. That is accurate rather than a gap:
`on_input_submitted`'s own peek reads the same empty vocabulary, so that line
really would go to the model. The popup is the first visible sign of a blind spot
that was always there.

## 3. Tab, and only Tab

Tab inserts the selected command with a trailing space. Pressing it again
replaces that with the next candidate, and wraps at the end of the list.

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

- **No argument completion.** `/resume <ref>` completing session refs is the
  genuinely useful one and `/extensions enable <name>` the second. Both need a
  per-command completer callback — pi's `getArgumentCompletions`
  (`tui/src/autocomplete.ts:339-357`) — which is the piece that grows this to
  pi's size.

- **No `@file` completion.** `@file` is expanded only in the headless argv path
  (`headless.py:assemble_prompt`). In the TUI it is literal prose, and a
  completion for it would need a filesystem walk on every keystroke. pi merges
  the two into one provider; τ has not, because only one of them exists.

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
| the Tab cycle | `tau_coding_agent/app.py` → `ChatInput._complete`, `ChatInput.on_key` |
| the widget | `tau_coding_agent/app.py` → `CommandPopup` |
| the redraw | `Parley._refresh_command_popup`, on `TextArea.Changed` |
| the merged vocabulary | `Parley._extension_command_table` + `FRONTEND_COMMANDS` |
| styling | `parley.tcss` → `#command-popup` |
| tests | `tau-agent-core/tests/test_command_completion.py`, `tau-coding-agent/tests/test_slash_command_popup.py` |

`complete_command` lives in the core beside `resolve_command` for the same reason
that one does: the command vocabulary is τ's, not the TUI's, and a second
frontend asking "what could this become" must get its answer from the same place
that answers "what is this".
