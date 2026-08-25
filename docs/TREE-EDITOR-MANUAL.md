# The tree editor — draft manual

**Status: draft, and describes 2026-08-24.** This says what the browser does
today, including what it does not finish doing. It is not a design document —
`docs/TREE-BROWSER-AS-EDITOR.md` is that, and it says *why*. This one says
*how*.

Written to be read next to a running τ, so that hands-on feedback can name
things. Where a gesture exists but leads nowhere, this manual says so rather
than omitting it. A manual that skips the unfinished parts teaches the reader
that a gesture is broken when it is only unbuilt.

Everything here was read out of `tau-coding-agent/src/tau_coding_agent/app.py`
(`SessionTreeModal`, `ZoneTree`, `TreeDetailPane`, `Parley.action_browse_tree`)
and `tau-agent-core/src/tau_agent_core/conversation_tree.py`. It has not been
checked against a running terminal.

---

## 1. What this is for

A τ conversation is a **tree**, not a list. Branching back to an earlier point
does not delete what came after — it leaves it in place as a sibling and starts a
new line. Compaction and elide do not delete either; they insert an anchor that
tells the reader where a span was folded out of the model's input.

The transcript can only show you one line through that tree. The tree editor
shows you the whole thing, and lets you move to a different point in it or fold
part of it away.

---

## 2. Opening and leaving

1. Press `Ctrl+G`, or press `Esc` twice with nothing generating.
2. Read the tree. Move with the arrow keys.
3. Press `Enter` on the node you want, then choose what to do with it.

Press `Esc` at any point to leave without changing anything.

The `Esc` route asks first. One press writes `press Esc again to view the tree`
into the header and does nothing else; the second press, within three seconds,
opens the browser. `Esc` still means "cancel this response" while a turn is
running, and that is checked first.

The browser refuses to open, with a warning, if there is no conversation, if the
tree is empty, or if the backend has no `navigate_tree` method.

**Nothing is written while the browser is open.** It holds no session log. Every
gesture builds up state in memory, and `Enter` hands one intent back to the app,
which is what performs the change. `Esc` discards all of it.

---

## 3. Reading a row

### A row is one log entry, not one message

The kinds that get a row:

| Kind | What it is |
|---|---|
| `message` | A user, assistant, or tool-result message. **A tool result is its own row.** |
| `customMessage` | A message an extension injected. Tagged `custom`, not `user`. |
| `compaction` | A fold anchor that replaced a span with an LLM summary. |
| `elide` | A fold anchor that dropped a span with no summary. |
| `branch_summary` | Written when a branch was abandoned with a summary. |
| `navigate` | A record that the cursor moved. Carries no message, and **is not drawn** — see §3. |
| `agent_spec` | The system prompt in force from this point. Stored as a digest. |

The label reads `role-or-kind: preview`. The preview is the first line of the
entry's text, cut to fit the width. The row you are currently at carries
`◀ current` at the end.

### Order

Top to bottom is a depth-first walk. Children are sorted oldest first, so within
one line of conversation, down is forward in time. Across a fork, down is into
the next branch.

### Indentation counts turns and forks, not messages

This is the part most likely to surprise you. Two things open a level, and
nothing else does.

**A user message opens a level, and the next user message closes it.** Your
message owns the turn it started: the assistant's reply, every tool call and
every tool result hang off it as children. The next thing you asked is that
group's *sibling*, at the same depth, not its child. So a session of a hundred
turns is a hundred rows at one depth, each holding its own traffic.

**A fork opens a level.** A node with two or more children indents each of them
one level in. A node with exactly one child does not indent it — the child is a
sibling on the next line.

Put together: a run of thirty turns with no branching is thirty rows at depth 0,
and a conversation that forked three times is a few levels deep, whatever its
length.

**A turn starts folded.** The exception is the turn you are currently in, which
opens so that `◀ current` is on screen. Press `→` on a folded turn to see what
happened inside it, `←` to fold it again.

**An arrow means there is something under that row.** Only a turn group or a
fork carries one. An assistant or tool row with nothing hanging off it has no
arrow and nothing to open.

**A `navigate` row is not drawn.** It records that the cursor moved and carries
no message, so the turn that was forked after it hangs off the answer it was
forked *from* — which is what actually happened. Two exceptions: a `navigate`
that is the current node keeps its row, and so does one with more than one child,
because that is a real branch point. The entry itself is still in the log and
still on the ancestry either way; only the row is gone.

`model_change` and `agent_spec` do keep their rows. Neither is a useful place to
branch from, but each records a real change to what the model is and what it was
told.

One consequence worth knowing before you complain about the layout:

* **A sub-agent's branch and a fork you made look identical.** That is
  deliberate at the data layer and is stated in `spawn_branch`'s own docstring.
  The browser has nothing to tell them apart with.

---

## 4. Keys

| Key | What it does |
|---|---|
| `↑` `↓` | Move the cursor. Redraws the detail pane and recolours the tree. |
| click | Moves the cursor. **Does not commit** — the browser stays open. |
| `Enter` | Commit. Closes the browser and opens the action chooser. |
| `Esc` | Cancel. Closes the browser and changes nothing. |
| `←` | Fold the turn or fork the cursor is on. If it is neither, move out to the row that contains it. |
| `→` | Unfold the turn or fork the cursor is on. Does not move the cursor into it. |
| `Space` | Mark or unmark the row. See §6. |
| `Ctrl+E` | Elide the span between the cursor and the other end. See §9. |
| `Ctrl+D` | Fold the detail pane away, or bring it back. See §5. |
| `Tab` | Move focus to the detail pane, so you can scroll it and open collapsibles. |
| double-click the pane's border | The same as `Ctrl+D`. |

`Enter`, `Space`, `Ctrl+E` and `Ctrl+D` are priority bindings, which is how they
take those keys from Textual's own tree widget and from the app underneath.
`Space` used to expand and collapse; that moved to `←` and `→`.

`Ctrl+D` and not the `Ctrl+M` that would have been the obvious choice: a terminal
sends the same byte for `Enter` and `Ctrl+M`, and Textual says so — its
`KEY_ALIASES` maps `enter` to `ctrl+m`. A `Ctrl+M` binding here would fire on
every `Enter`, which is the commit key.

### What `←` does and does not do

`←` folds a **turn** or a **branch point** — the two things §3 says open a level.
It cannot fold anything else, because nothing else has a parent row to fold into.
Pressing `←` on an assistant or tool row inside a turn moves the cursor out to
the user message that started that turn; pressing it again folds the turn.

---

## 5. The detail pane

The pane sits below the tree and takes half the body. It shows three nodes:

1. The last few rows of the **previous** node, so the selection reads as
   following something.
2. The **selected** node, in full.
3. The **first child**, which is the message that actually came next in time. A
   later sibling is a branch, and is counted rather than shown.

Rows it is not drawing are stated as `⋯ N earlier` and `⋯ N later`, so a
three-box pane never implies a three-message conversation.

It uses the same renderer as the transcript, so a node reads here exactly as it
read in the chat — a tool box or a reasoning region inside it can be opened with
`Tab` and then the usual keys.

**Press `Ctrl+D` to fold it away**, or double-click its border. The tree gets
its half of the body, less one row: what stands in its place is a single line
reading `▸ detail pane hidden — ctrl+D, or click here, to show it`, and clicking
that line brings the pane back. Fold it when you are reading the SHAPE of a
conversation rather than a message in it.

The fold is your choice and the app does not overrule it. A terminal shorter
than 20 rows also hides the pane, because below that it can no longer show
enough to be worth the space — but growing the terminal back does not un-fold a
pane you folded, and a terminal too short for the pane does not draw the marker
either.

---

## 6. Marks

`Space` adds the row under the cursor to the marked set, or removes it.

The line under the tree is the readout. It states the count, how many of the
marked rows are currently folded out of sight, their lowest common ancestor, and
an estimated size:

```
3 nodes marked (1 folded away) · common ancestor msg_014 · ~1820 tokens (estimate)
```

The word "estimate" is always there and the number is never shown bare. That is
a rule, not a hedge: the only measured token figure anywhere in a session is
`usage.input_tokens` on a finished assistant message, which measures one
request. A total over an arbitrary set of entries can only ever be a
4-characters-per-token guess.

**One mark is the elide's other end** (§9). Mark a row, move the cursor to the
row you want to pair it with, and press `Ctrl+E`. While exactly one row is
marked, every row that cannot be that pair is greyed out.

> **Partly unfinished.** A set of two or more marks is not consumed by anything.
> You can mark several rows, see the count, see where they converge and see what
> they are estimated to cost — and then the only thing you can do is close the
> browser, which forgets them. `Enter` commits the **cursor**, not the marks.
> The operation a whole marked set is for is step 7 of
> `docs/TREE-BROWSER-AS-EDITOR.md`, which is not built.

---

## 7. Colours

### The type tag

Every row starts with what it is — `user:`, `assistant:`, `toolResult:`,
`system:`, or the name of a bookkeeping entry — and that word is coloured on its
own, up to and including the colon. The rest of the row stays neutral, so the
left edge of the tree is scannable without reading it.

| Tag | Colour |
|---|---|
| `user:` | the same hue your own messages wear in the transcript |
| `assistant:` | the assistant hue |
| `toolResult:` | the tool hue |
| `system:` | the system hue |
| `compaction:`, `branch_summary:`, `elide:`, `navigate:`, `model_change:`, `agent_spec:` | quiet grey, italic — bookkeeping, not a turn |

The four conversation hues are the transcript's own, so a role cannot be one
colour in the chat and another in the tree.

The row under the cursor is left alone, tag included. Its own highlight is the
strongest thing on the screen, and a colour layered on top would lose the
contrast that makes it readable.

### The zones

Each row's text then takes **one** of these over the whole label, first match
wins, in this order. A zone paints over the type tag, which is deliberate: what
you have done to a row outranks what the row is.

| Order | Colour | Meaning |
|---|---|---|
| 1 | green, bold | Marked. You put it there, so it outranks everything. |
| 2 | faint grey | Cannot be the other end of the elide you have started. Only ever painted while exactly one row is marked. See §9. |
| 3 | dark grey, italic | Hidden — archived and excluded from counts. |
| 4 | mauve, italic | Copied. |
| 5 | peach, struck through | Covered: the cursor is **on** a fold anchor, and these are the rows it hides. |
| 6 | grey, struck through | Folded: on your path, but some anchor dropped it from the model's input. |
| 7 | cyan | A branch summary. |
| 8 | cyan, dim | The head of the branch that summary is about. |
| 9 | blue | On the cursor's ancestor chain — what the model saw at the cursor. |

`path` is last because it is true of a whole chain and would otherwise swallow
everything else.

Separately, and **layered on top** rather than competing:

| Style | Meaning |
|---|---|
| underlined, no colour change | Under the mouse: history this row and the cursor share. |
| underlined, amber | Under the mouse: history the hovered row has and the cursor does not. |

The underline is one continuous thread from the root to the row you are pointing
at. Where it turns amber is where that row's history stops being your history.
That is the answer to "what would I be picking up if I went there?".

Point at a row, do not click. Hovering costs almost nothing and recomputes only
when the pointer crosses into a different row.

> **Unfinished.** The `copied` row of that table can never appear today —
> nothing mints a copied entry. The rule exists so that building the producer is
> one change instead of three.

---

## 8. What `Enter` leads to

`Enter` closes the browser and opens a chooser with three buttons.

### First: which side of the row you picked

`Enter` means one of two things, and the row's kind decides which.

**On an assistant or tool row it means "continue from below this".** The cursor
moves onto that row and the next thing you send becomes its child. This is the
plain case.

**On one of your own messages it means "ask this differently".** Continuing from
below a user message would put two user turns in a row, which a conversation
cannot have — so the cursor moves to that message's **parent** instead, and the
message you are replacing comes back in the input box for you to edit. Send it
and you have a second branch beside the first, with the original still in the
tree.

The very first entry in a session has no parent, so there is nothing to fork
from; the browser says so and does nothing rather than falling back to landing
on it.

Eliding does not go through `Enter` at all any more — it is `Ctrl+E`, and §9 is
the whole of it.

### Branch: no summary

Moves the cursor as above. What came after it stays in the tree as a sibling
line. Refused with "Already at that node" if you picked the node you are already
on.

### Branch: summarize abandoned branch

The same move, and additionally asks the model to summarize the line you are
leaving, so the summary is in the tree where the branch was.

### Branch: summarize with custom instructions…

The same, with a third dialog for the instructions to give the summarizer.

---

## 9. Eliding a span

An elide folds a run of entries out of the model's input without deleting them.
It is one key, inside the browser.

### Read this first: the two ends bracket what is KEPT

This is the thing about an elide that almost everybody guesses backwards. Given
a conversation `[1, 2, 3, 4, 5, 6]`, pairing `2` with `4` leaves you with
**`[2, 3, 4]`** — not `[1, 5, 6]`.

An elide is the summary-less form of the compaction anchor, and a compaction
keeps a *tail* and drops the *head*. The kept region is always one contiguous
run ending at the deeper of the two nodes you named. **Cutting a span out of the
middle is not something this operation can express at all.**

There is a second effect when the deeper of your two nodes is not the current
`◀ current` row: the conversation moves back to it, so everything newer than it
leaves the context as well. In the `[1..6]` example with the cursor at `6`,
pairing `2` with `4` drops `1` to the fold *and* `5` and `6` to the move.

The line under the tree states both, before you press anything:

```
ctrl+E: keep this span, drop the other 3 entries, and move back to it
```

Nothing is deleted either way. Every entry is still in `entries()`, and the
branch you left is still in the tree.

**The ordinary case — drop the old history and carry on:**

1. Press `Ctrl+G`.
2. Put the cursor on the oldest entry you want to KEEP.
3. Read the line under the tree. `ctrl+E: keep this span, drop the other 37
   entries` is what the key will do.
4. Press `Ctrl+E`.

Everything older than the cursor leaves the model's input; the conversation
continues where it already was, so the line says nothing about moving.

**Choosing both ends:**

1. Put the cursor on one end and press `Space` to mark it.
2. Move the cursor to the other end.
3. Press `Ctrl+E`.

You do not have to remember which end you picked first. The two ends of an elide
are always an ancestor and a descendant of each other, and the deeper one is
always the one the conversation continues from — so the tree decides, not the
order of your keystrokes. Both of them are kept.

### What can and cannot pair

The two ends must be on **one line** of the conversation: one has to be an
ancestor of the other. Cousins on two different branches cannot form a span,
because the fold only ever walks a node's ancestors.

While exactly one row is marked, every row that cannot pair with it is greyed
out. That is the ancestry rule only. One more thing can be illegal — a legal
pair whose span happens to be empty, which is what you get by resuming at a row
that is already the first one kept — and `Ctrl+E` refuses that by name:

```
That would hide nothing — the span between those two nodes is already empty.
```

**Every refusal happens with the browser still open**, on the row you were
looking at, so you can move and try again. Nothing is written by a refused
elide: the validation runs before any append, twice — once here, and once in the
backend against the live session.

On success the browser closes and the status line reports `Elided 128 → 41
messages`.

---

## 10. What the tree editor cannot do yet

Collected so that feedback can distinguish "this is broken" from "this was never
built".

* **A marked SET leads nowhere.** One mark is the elide's other end; two or
  more are counted and nothing more. §6.
* **There is no copy, and no plan buffer.** You cannot assemble an ordered
  keep/copy plan and commit it. `Enter` answers with one action and one node id,
  which is the only shape the return type can currently express.
* **There is no archive gesture.** Archiving was decided to be view state rather
  than something written to the log, and the collapse half exists — but no key
  marks a branch as done, and nothing is excluded from any count.
* **A compaction's fold has no header row.** The rows a fold covers are struck
  through, but there is no single row you can collapse to hide the whole span.
  This is parked, not forgotten: adding it rewrites row order, which would stop
  vertical position meaning time, and no one has decided what to do about that.
* **Nothing distinguishes a sub-agent's branch from yours.** §3.
* **A turn's rows cannot be filtered.** You can fold a turn whole (§3) or open
  it whole. pi has a filter that hides tool rows, or shows user messages only;
  τ has one fixed rule — the `navigate` row — and no key to change it.

---

## 11. Where to look next

* `docs/TREE-BROWSER-AS-EDITOR.md` — the design document. §2 is the
  fork-counting rule, §3 the colour zones, §5 the keys and the four selection
  sets, §6 and §7 the unbuilt plan buffer, §10 the status table, §11 the
  rejected alternatives.
* `docs/PLAN-0.9.4.md` §4 — the four things the owner reported after working
  this browser against a real forked session, what each turned out to be, and
  the three-step build that answered them. The candidate diagnoses that predated
  that feedback are kept below it, marked as such.
