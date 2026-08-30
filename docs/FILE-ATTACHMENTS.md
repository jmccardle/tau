# `@file` attachments

**Built 2026-08-30.** Typing `@notes.txt` in the chat editor now attaches the file.
The content goes in front of the prompt as an `<attachment>` block; an image goes
as an `ImageContent` block on the same message; anything too large, binary, or
unreadable goes as a `<reference>` block that names the path and the size instead.
Tab completes the path. A bar above the editor lists what the draft will attach,
and clicking a row removes it.

Code: `tau-agent-core/src/tau_agent_core/attachments.py` (the decisions),
`tau-coding-agent/src/tau_coding_agent/app.py` (`AttachmentBar`, `AttachmentRow`,
`ChatInput._complete_attachment`, `Parley._expand_attachments`).

## 1. Why the decision is in the core

The split is the one `commands.py` already encodes: **the core decides, the
frontend performs.** `scan_attachments`, `render_attachments`,
`complete_attachment` and `remove_attachment` are pure functions of the text plus
a working directory. They touch no session, no bus and no model, so the TUI, a
test, and a future frontend get the same answer from the same code.

What is deliberately NOT in the core: the `@` is expanded by the **frontend**, at
the moment it builds the `Submission`, and the expanded text is what
`Submission.text` carries. The core never sees a `@word` it has to interpret.
That keeps the tree-as-truth invariant intact — what is persisted, what is
replayed on resume, and what the model was sent are the same string, and no
hidden channel re-reads a file at replay time.

## 2. The three block shapes

| Situation | Block | Content sent |
|---|---|---|
| UTF-8 text ≤ the inline limit | `<attachment filename="notes.txt">` … `</attachment>` | the whole file |
| supported image | `<attachment filename="shot.png" type="image/png" />` | an `ImageContent` block on the same message |
| over the limit, not UTF-8, or unreadable | `<reference filename="big.log" path="/abs/big.log" size="19.5 KB" reason="…" />` | nothing — the path and the size |

Two words, and the difference between them is whether the content is present. A
model that gets a `<reference>` has the absolute path and can `read` the file
itself, which is the point: attaching a 4 MB log should cost one line of prompt
and a decision the agent makes, not 4 MB of context the human spent by accident.

The blocks go **in front of** the human's text, and the `@word` stays where they
typed it. The model reads the material first and the instruction last, and the
instruction still names the file the way the human named it.

The block body is never escaped. A Python file full of `<` and `&` reaches the
model as itself — these are a framing convention for a language model, not a
document an XML parser will ever see. Only the attribute values are escaped, so a
filename containing a quote cannot break the header.

**The inline limit** is `attachment_inline_limit` in `config.json`, 10240 bytes by
default — roughly 2500 tokens. Fail-Early: a value that is not a positive integer
raises at read time rather than falling back to the default, because the setting
decides whether a file's content reaches the model at all.

**Images** are bounded by `resize_image` (the same `max_image_dimension` the
`read` tool is configured with, so a file the human attaches and a file the agent
reads are capped by one number). Pillow missing is not a reason to send an image
unresized: the attachment degrades to a `<reference>` naming the extra to install,
and the human is told with a toast.

## 3. Tab, and why the cursor decides

Tab already belonged to slash-command completion (docs/SLASH-COMMANDS.md §3).
It now serves two vocabularies, and the rule for which one is the **cursor**:
inside a `@…` it completes a path, otherwise it completes a command. The two
cannot both apply — a command is the first word of a line, and a `@…` under the
cursor is not it — so `/fork @no` completes the path, which is what the cursor was
in.

`CommandPopup` shows whichever list applies, and it is asked in the same order the
Tab handler asks, so what the popup offers and what Tab inserts cannot disagree.
The popup is also redrawn on `TextArea.SelectionChanged`, not only on `Changed`:
an arrow key into an existing `@…` changes which reference is being completed
without changing a character.

Rules the completion follows, all borrowed from a shell because that is what the
hands already know:

1. Matching is a case-sensitive prefix test on the last path segment.
2. Hidden entries appear only once the prefix itself starts with a dot.
3. A directory candidate ends in `/` and is inserted **without** a trailing
   space; a file gets one.
4. With more than one candidate, repeated Tab cycles them. With exactly one, the
   insert ends the cycle, so the next Tab re-scans — which is the only way to get
   *inside* a directory that was the sole match.
5. The whole token is replaced, even with the cursor in the middle of it. One
   rule, so what Tab does is predictable.

An `@…` that matches no file gets the popup's warning line — `@zzz matches no file
— this word goes to the model as text` — which is the same service the popup
performs for an unknown `/…`, for the same reason: the fallback is correct and
until it is shown, silent.

## 4. The bar, and what "remove" means

`AttachmentBar` sits between `PendingInput` and the editor and holds **no state
the editor does not**. It is redrawn from `scan_attachments(editor.text)` on every
`Changed`, so it is a view of the draft rather than a second buffer that could
drift from it.

That is what makes removal simple: clicking a row deletes the `@word` from the
editor, and the resulting text change redraws the bar. The word *is* the
attachment, so there is nowhere for the two to disagree, and there is no separate
"attached images" list to keep in step with the text. A span that has gone stale —
the human typed between the redraw and the click — raises rather than cutting the
wrong characters, and the app reports it instead of crashing.

The whole row is the click target rather than the `✕` glyph alone: a one-line row
inside a bordered bar leaves a two-cell target that is easy to miss, and the cost
of a mis-click is retyping one path. The row highlights on hover so it does not
read as a label.

**Unresolved references get no row.** A `@word` that names no file is prose on its
way to the model — the same answer an unrecognised `/…` gets — and the popup is
where that is said, while the cursor is still in the word. A bar row for every
`@handle` in a sentence would be noise on the one line the human is trying to
read.

## 5. Steering

A steering message attaches files exactly as a typed prompt does. The expansion
happens at the **delivery point**, not when the line was queued, so:

- the file is read at the moment it is sent, and
- several queued lines that each name a file arrive as ONE message carrying all
  of them, which is the shape `AgentSession._queued_content_to_user` already
  builds for a `multitask_strategy="steer"` submission.

The pending buffer holds the **raw** lines. Reclaiming (Up on an empty editor,
alt+up, Esc, a session swap) hands back what the human typed, `@word` and all, and
the bar redraws from it. The one path that puts a delivered message back on the
buffer — the backend disappearing between the flush and the worker — re-queues the
raw text for the same reason: re-queueing the expanded form would attach every
file a second time at the next delivery point.

## 6. The transcript fold

A user bubble shows the submission verbatim, which is the rule B3-b established:
the bubble is the record of what was actually sent. One display fold is now
applied on top of it — an inlined attachment body is replaced by
`… 84 lines, 3.2 KB not shown …`.

This is a rendering transform and nothing else. The wire, the session log and the
model all have the whole file; `elide_attachment_bodies` decides what the terminal
draws, and it says so in the marker rather than leaving a shortened body that
reads as the whole file. Without it, attaching a 10 KB file pushes the
conversation off the screen — the feature would be unusable in the frontend it was
built for.

The same fold is applied on reload, so a resumed chat and the chat it was resumed
from look alike. A persisted user turn's `image` block, which the reload path used
to skip silently (leaving an empty box for an image-only turn), now renders as one
line naming the mime type and the size.

## 7. Divergences from pi

pi's `@file` (`cli/file-processor.ts`) is a **CLI argument** feature: it processes
`@file` arguments before the run starts, with a different vocabulary
(`<file name="…">`, always absolute paths, and an unreadable file exits the
process). τ's is an **editor** feature, and:

- the block names are `attachment`/`reference` rather than `file`, because τ has
  the second case pi does not: a file whose content deliberately is not sent;
- `filename` is the path as typed, so the model reads the name the human used, and
  the absolute path appears on a `<reference>` where it is the actionable value;
- a missing file is prose, not a fatal error. In a CLI argument list, `@nope.txt`
  can only be a mistake; in a sentence typed at an editor, `@alice` is a word.

`tau -p "summarise @README.md"` still uses the CLI's own older rule
(`headless.assemble_prompt`): a whole positional argument beginning with `@` is
inlined raw, with no block wrapper. **The two have not been unified.** Doing it
means changing what `-p` sends for an existing flag, which is a separate decision.

## 8. What is absent

- **No clipboard paste.** Ctrl+V for an image is a second pass; this pass is
  `@file` only. The core is ready for it — `Submission.images` and the steer merge
  already carry images — and the missing half is the platform matrix
  (`wl-paste`/`xclip`/`osascript`/`powershell.exe`) plus a buffer for attachments
  that have no `@word` in the text to hang on.
- **No keyboard removal.** The bar's rows are mouse targets. The keyboard gesture
  for "remove this attachment" is deleting the word, which is already what the
  editor does.
- **No threading.** `render_attachments` runs on the event loop, and bounding a
  large image decodes it. A very large photo will stall the frame that Enter was
  pressed in. Not measured; not yet worth a worker.
- **No `@` for a directory.** `@src` is prose. Attaching a tree means deciding
  what to include, which is a policy question, and the agent's own `list`/`read`
  tools already answer it.
- **No glob.** `@*.py` names no file and is sent as text.
- **No de-duplication.** Naming the same file twice attaches it twice, because
  that is what was typed.
- **Not in a rollback prompt.** `action_rollback_turn` already states that its
  text goes to the model as-is and that slash commands are not expanded there;
  `@file` is not either, for the same reason — that text does not pass through
  `on_input_submitted`, and the two expansion sites are the two `Submission`
  constructions in `Parley`.
