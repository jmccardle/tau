# What a head shows for a node an extension wrote

Built 2026-09-11. Three faults, one shape: a field an extension sets, stored and
then read by nobody, so the extension's instruction succeeded silently and did
nothing.

## 1. `display` was written and read by nobody

`create_custom_message` has taken a `display` parameter since the port and
writes it onto every node (`messages.py`). `api.send_message` accepts it as part
of the documented `{customType, content, display?, details?}` dict, and
`AgentSession._append_custom_message` reads it back out of the dict and threads
it through.

Nothing downstream branched on it. Measured 2026-09-11 before the fix: `.display`
has no reader in `tau_agent_core` at all, and every match in `tau_coding_agent` is
a Textual widget's own `display` attribute. `convert_to_llm` does not read it —
that is `visibleToModel`'s job, a different question with a different key — and
neither transcript path read it either. So an extension asking for a node the
reader should not see got one the reader saw.

`docs/EXTENSION-LOCKS.md` §1 names this failure class for the coroutines it
removed: a head quietly behind the thing it is rendering. This is the same class
on the other side — an API quietly ahead of the head.

## 2. The scope: the transcript, and nothing else

`is_displayed` (`messages.py`) is the reader, and it is deliberately narrow.

A node with `display: False` is still on the tree, still on the active path,
still counted by every span and token estimate, still reaches the model when
`visibleToModel` says so, and is still drawn by the tree browser — both its row
and its detail pane. What changes is one thing: the running transcript a reader
follows does not mount a box for it.

That scope is why the guard sits at the transcript's call sites and **not**
inside `MessageList.add_persisted_message`. `TreeDetailPane._render_entry` calls
that same method to draw the body of whichever node the browser's cursor is on,
so a guard inside it would have hidden the node from the one surface that must
keep showing it. Two call sites in the TUI and one in the REPL
(`_on_custom_message`, which the replay path also reaches through
`replay_render_events`).

**The reload guard filters the span, not the message.** Placing it one line
lower — inside `_reload_exchange`'s fallthrough — renders nothing and still
mounts an `ExchangeBox`, because that method mounts the exchange before it looks
at any message. A note appended from a command handler is alone in its span, so
a hidden one left an empty exchange in the transcript. Measured: with the filter
moved into `_reload_exchange`, `test_a_hidden_note_is_on_the_tree_and_not_in_the_transcript`
fails on the `ExchangeBox` assertion and passes every other one. The span is
built with `is_displayed` applied, and the `if span:` guard that was already
there does the rest.

Print mode renders no custom messages at all and is unchanged.

**Older sessions change appearance.** A saved node that asked to be hidden and
was not now is. That is the reason §21 of the review declined this fix; it is
also the fix. Nothing rewrites a stored node, and a node with no `display` key
reads back as displayed, which is its historical behaviour.

## 3. A row that names the kind is not a row that says anything

`tree_browser.py` draws `node.preview or f"({node.kind})"`. So an entry whose
preview came back empty rendered as the literal word `(customMessage)`, and a
`customEntry` rendered as `customEntry: <customType>` — the kind, with a label on
it.

`ConversationTree._agent_spec_preview` already fixed exactly this for one kind,
and says so in its own docstring: the browser used to render an `agent_spec` as
`customEntry: agent_spec`, "which is the same loss with a label on it". The other
kinds kept the loss.

Three previews now compose a row from what the entry holds.

| Entry | Row |
|---|---|
| `customMessage` with text | `tectum_note: the build failed` |
| `customMessage`, hidden, details only | `tectum_note (hidden): path=/a/b.py, line=42, +2 more` |
| `customMessage` with nothing | `ni: no content` |
| `customEntry`, `extension_request` | `Extension tectum requires a response: Approve rm -rf build?` |
| `customEntry`, anything else | `tectum_state — turns=3, last=hello there how are…` |

Three rules carry it.

**`customType` leads.** It is the extension's own name for the node, it is stored
on every one, and before this it had no reader anywhere outside the write path —
the finding §7 of the review recorded as "solved once, absent seven times". One
label (`ROLE_LABELS["custom"] = "Extension"`) still covers every extension in the
transcript; the tree row now says which one.

**The bounded part comes first.** The same rule `_splice_anchor_preview` states:
the row is cut to the width left after the indent, so the half a reader cannot
reconstruct — the type, the hidden mark, τ's framing of a request — goes in front
of the half they can guess at.

**A payload is summarized, never dumped.** `_payload_summary` names the first
four fields and counts the rest; `_value_phrase` cuts a scalar at 40 characters
and renders a container as a count rather than its contents. An empty payload
reads `no data` rather than `{}`, which looks like something an extension wrote
on purpose.

A reserved `extension_request` is read back through `read_request` and rendered
as `ExtensionRequest.label` plus the extension's own sentence, so the four states
of `docs/EXTENSION-LOCKS.md` §3 are legible from the tree without opening
anything. `read_request` returns `None` for a malformed payload, and the row then
falls through to the generic summary rather than raising — a browser drawing a
hand-edited log reads it, it does not enforce against it.

## 4. Not in this pass

- **`customType` in the transcript.** The tree row names the extension; the
  transcript box still says `Extension` for all of them, and the REPL duplicates
  that constant (`repl_theme.EXTENSION_LABEL`). Naming it there is a second
  decision about how much of an extension's identity belongs in the running
  conversation.
- **`display` on the wire.** `custom_message` is not an RPC event at all, so an
  RPC host learns about neither the node nor the flag. That gap is the one the
  review records against §21 and it is unchanged here.
- **A renderer hook.** `display` is still one boolean. An extension that wants
  its node drawn differently has no way to say so, and giving it one is a
  rendering API rather than a fix to this one.
