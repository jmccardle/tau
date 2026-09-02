# The tree browser is an editor, not a picker

**Status: built except for the fold header (2026-08-30).** Build-order steps 1, 2, 3,
5, 6, 7 and most of 4 have landed. One piece of step 4 — the fold header, §4.1 — is
deliberately still undecided. §10 carries the per-step status, what is left, and the
verification the built steps were measured against; it is the one place to read all of
that. Each section that has been built says
so in its own heading block, with any divergence from what the section decided.

**Reading the citations.** Sections 1 through 9 were written before any of this was
built, and their `file:line` citations are to that state. They are kept as written,
because the argument for each change is only checkable against the code it argued
about — §1 in particular is the problem statement and describes the *previous*
behaviour throughout, not current behaviour. Where a built change moved or renamed
something, the built-marker block names the new symbol. Citations into
`tau-coding-agent/.../app.py` are given as symbol names rather than line numbers: that
file is under active edit and its line numbers move daily.

**Relationship to existing docs.** `SESSION-TREE-IMPLEMENTATION.md` records how the
tree was built. `NODE-ADDRESSABLE-AGENTS.md` states what the built tree guarantees —
I1 in particular, which §6 of this document depends on and must not break.
`JMFTS-INTEGRATION-PLAN.md` §7 owns the conformance suite §7 here adds an obligation
to. `LANE-REMOVAL.md` is the precedent for the shape of this document: measurements
first, rejected designs kept on the record.

---

## 1. What the browser is today

> **"Today" here means 2026-08-23, before any of this was built.** §1.1 and the
> `_elide` overflow it describes are fixed (§2's built marker); §1.2 is fixed for
> `compaction` and `elide` and open for `branch_summary` (§4.2, §4.3); §1.3 is in
> progress. The section is kept as the problem statement — the argument for each change
> is only checkable against the code it argued about.

`SessionTreeModal` (`app.py:523`) is `ModalScreen[Optional[str]]`. It shows a
`textual.widgets.Tree` built from `ConversationTree.tree()`, highlights the current
leaf, and exits by `dismiss(entry_id)`. One cursor, one exit, one answer.

Three problems, in increasing order of how much they cost.

### 1.1 Indentation is a function of conversation length

`on_mount._add` (`app.py:686-694`) maps one `parentId` level onto one widget level.
`Tree.guide_depth` defaults to 4 (verified, textual 8.2.7), so every message costs
4 cells of indentation.

`_relabel` then computes the label's share of the line:

```python
# app.py:736
available = width - (depth + 1) * tree.guide_depth
```

At width 100, depth 24 drives `available` to zero. `_elide` returns its input
unchanged when `width < 2` (`app.py:518`), so the row is not shortened. It overflows
and the tree grows a horizontal scrollbar. A 25-message conversation is already past
that point.

pi does not have this problem. Its indent counts **forks, not messages**
(`tree-selector.ts:287-298`): a parent with more than one child pushes its children
in by one, the first generation after a branch pushes in by one more, and a
single-child chain stays flat.

That silent overflow is also a Fail-Early inversion in its own right: `_elide` is
asked for an impossible width and answers by producing the exact defect it exists to
prevent.

### 1.2 Transformations are invisible

`_preview_of` (`conversation_tree.py:468-485`) renders a `compaction` as the first
line of its `summary` and nothing else. Which entries it dropped is not shown, and
the dropped entries render as ordinary rows with no marking.

`elide` is the one kind that tried. `_elide_preview` (`conversation_tree.py:487-518`)
computes the span from structure and returns `"hides 42 entries, resumes at <id>"`.
So the node kind with *no* payload to render has a better row than the one with a
summary.

pi is no better here — `tree-selector.ts` renders `[compaction: Nk tokens]` and has
no marker for the covered span. This is a place to diverge.

`branch_summary` needs no structural change: `append_branch_summary`
(`session_store.py:621-639`) sets `_leaf_id = from_id` before appending, so
`parentId == from_id` and the summary is *already* a sibling of the abandoned
branch's first message. Only the rendering is missing.

### 1.3 One cursor and one exit is the wrong shape

Every operation worth adding needs more than a node id:

- summarize the subtree below a node — one node plus an action name
- summarize a traversal — a *set* of marked nodes and their lowest common ancestor
- hide or unhide a branch — state the modal mutates and keeps, with no exit at all
- set the active leaf — an action that runs and returns to the browser
- copy, cut, paste — a clipboard and a staged result

Building §2 through §5 against `dismiss(id)` forces a rewrite when any of these
lands. Changing the return type first makes them additive.

---

## 2. Layout: indentation counts forks

> **Built (2026-08-23), as decided.** `SessionTreeModal.on_mount` now adds a
> single-child entry under the *same* widget parent at the same depth, so only a node
> with more than one child opens a level. `guide_depth` is 2 — which is also
> `Tree.validate_guide_depth`'s clamp floor in textual 8.2.7, so the value is the
> minimum the widget will accept, not a taste. `data=node.id` is unchanged and
> `_depth_of` still holds the *data* depth for the detail pane; the depth stored in
> `_rows` is now the *widget* depth, which is what `_relabel` needs.
>
> `_elide` gained `_ELIDE_MIN_WIDTH` and `_ELIDE_TOO_NARROW`: below the floor it
> returns the marker instead of the untouched text.
>
> One thing the section did not predict. At `guide_depth == 2` the toggle prefix is
> exactly one more level wide, so `_relabel`'s `(depth + 1) * guide_depth` stops being
> a conservative over-estimate and becomes *exact*. Verified against
> `_TreeLine._get_guide_width` and `Tree.render_label`. The comment there records it,
> because the arithmetic is now load-bearing rather than slack.
>
> `testing/scenes.py` needed a fix: `_open_tree_modal_at_branch` looked up its target
> at a fixed nesting level, which no longer exists after flattening. It walks the
> widget tree by `data` now.

**Decision.** Keep `textual.widgets.Tree`. Change what is handed to it.

A run of single-child entries becomes *siblings* under one widget parent. Only a fork
creates a new widget level. The widget tree encodes branch grouping; `parentId` stays
the property of the data, where it belongs.

`guide_depth` drops from 4 to 2.

Indent depth is then bounded by the number of forks on a path, which is small and does
not grow with the conversation. `data=node.id` is unchanged, so `_view_of`, the detail
pane, and every test that reads `node.data` keep working.

`Tree`'s collapse then means "collapse this fork's subtree" rather than "collapse this
message", which is the operation a reader wants and the one §5 binds a key to.

`_elide` gains a floor: below the minimum width it returns a marker rather than the
full label. A row that cannot be shortened is a bug to report, not a scrollbar to grow.

**What this costs.** Textual highlights the indentation guides of the hovered row's
ancestry through the `tree--guides-hover` component style (verified, textual 8.2.7).
Today those rails trace the whole ancestor chain, because widget depth equals message
depth. After flattening, a 30-message run is 30 siblings at one level with no rails
between them, and the hover highlight conveys almost nothing.

§3 is the replacement, not an embellishment.

**Rejected: replace `Tree` with a custom row widget.** This is what pi does — a flat
row list, a hand-drawn gutter, and a horizontal viewport that pans the row bodies while
pinning the gutter (`tree-selector.ts:62-92`). It gives full control and costs a
reimplementation of selection, keyboard navigation and scrolling, across four test
files that touch `SessionTreeModal`. Revisit only if §3 proves it cannot say what it
needs to say inside a `Text`.

---

## 3. Zones: arbitrary row highlighting

> **Built (2026-08-23), as decided.** `ZoneTree(Tree[str])` declares all six
> `COMPONENT_CLASSES` and overrides `render_label`, layering zone styles over Textual's
> own as Rich spans on two character ranges — the toggle prefix and the label — which is
> the per-row partial styling §3 said a `Text` could carry. All colour lives in
> `parley.tcss`; none is in `app.py`. `set_zones()` is the write path.
>
> Four findings a later change should not have to rediscover:
>
> - **The cursor row is never zone-styled.** `tree--cursor` resolves with
>   `partial=False` and sets a foreground when focused, so any zone colour laid on top
>   wins it and destroys the contrast. The cost is real: marking the row *under* the
>   cursor shows nothing on that row. The `#tree-browser-marks` readout exists because of
>   this, not as decoration.
> - **`covered` is derived from `context_entries()[0]`, not from a kind test.**
>   `_active_path_entries` emits `[anchor] + kept + after`, so element 0 of a folded
>   result *is* the anchor. This keeps `_SPLICE_ANCHOR_KINDS` — private to
>   `conversation_tree` — from being copied into the TUI.
> - **Invalidation is `_line_cache.clear()` + `refresh()`, not `Tree._invalidate()`.**
>   The strip cache key (`_tree.py:1325-1332`) contains no zone state, and on a cursor
>   move only the two nodes whose `_selected` flips get a new key — every other path row
>   would serve a stale strip. `_invalidate()` would additionally rebuild every row's
>   width on each arrow key, and zone styling changes no widths.
> - **Rows are added with `add(..., expand=True)`, not `.expand()`.** `expand()` posts
>   `NodeExpanded`, whose handler recomputes all four selection sets — one message per
>   row at mount is quadratic.
>
> **The hover divergence highlight is built (2026-08-23), completing step 5.** §2 removed
> the guide rails that traced a hovered row's ancestry; `tree--zone-path` replaced that for
> the cursor, and this replaces it for hover.
>
> `Tree.hover_line` is a `var(-1)` written by `_on_mouse_move` from the style meta
> (`_tree.py:655`, `:1081`). Because it is a reactive, **`watch_hover_line` fires only when
> the row changes, not on every mouse move** — sliding along one row costs one assignment.
> The override calls `super()` and posts a `ZoneTree.HoverChanged` carrying the *entry* id,
> so the modal keeps ownership of `parentId` ancestry and the widget stays a pure renderer.
>
> The split is the longest common prefix of `_ancestry(hovered)` and `_ancestry(cursor)`.
> **When the divergent tail is empty, both sets are empty** — the hovered node is on the
> cursor's path, and painting the shared half alone would show a highlight meaning "you are
> already here", which reads as a divergence that is not there. A *descendant* of the
> cursor is not on its path and does diverge, correctly.
>
> `tree--zone-hover-common` sets `text-style: underline` and **no colour at all**;
> `tree--zone-hover-divergent` adds `#f9e2af`, the existing amber. Both are applied last,
> over the whole row, as a second span that *composes* rather than competes: a hovered
> chain crosses rows that are already path-blue or marked-green and should keep saying so.
>
> **Cost, measured on a 201-entry conversation.** The hover path is 14.1 µs; `_derived`
> alone, part of `_refresh_zones`, is 72 µs; `_branch_summary_pairs` is 24.7 µs and runs
> once at `__init__`. `set_hover_zones` uses `dataclasses.replace` on the two hover fields
> only and **returns early when the sets are unchanged**, which is the common case —
> hovering along the cursor's own path yields empty/empty, so the reader pays no repaint
> for it at all. `_refresh_zones` re-derives the divergence from the stored hover, because
> a cursor move stales an already-painted one.
>
> A rendering caveat for anyone judging this from a screenshot: **cairosvg does not
> rasterize `text-decoration`**, so the underline is absent from a PNG render while present
> in the SVG and in a real terminal.

> **`tree--zone-copied` and `tree--zone-hidden` have no visible effect today.** `copied`
> has no producer until §7; it is declared and commented, and nothing was invented to
> feed it. `hidden` is populated — rows folded away inside a collapsed fork — but such
> rows are not drawn, so the class paints nothing and its one observable consumer is the
> readout's "(N folded away)". See §11.2's built note for the archive gap.
>
> **No branch-colour palette.** §3 permits one; nothing needs it yet, so nothing cycles
> one.

Textual `Tree` rows are not DOM nodes. They cannot carry per-row CSS classes, so a
scheme like `branch-12` / `branchpoint-12` has nothing to attach to.

Two hooks exist, and together they are enough.

1. `COMPONENT_CLASSES` — a class-level frozenset resolved through
   `get_component_styles`. Declared once in a `Tree` subclass; the values live in
   `parley.tcss`.
2. `render_label(node, base_style, style) -> Text` (verified, textual 8.2.7) — called
   per row, returns a Rich `Text`.

**Decision.** Declare component classes for zone *roles*, not for branches. Resolve
each to a Rich style once, and apply them per row in `render_label`.

The vocabulary:

| Class | Meaning |
|---|---|
| `tree--zone-path` | on the cursor's ancestor chain |
| `tree--zone-folded` | on the path, dropped by a splice anchor |
| `tree--zone-covered` | covered by the anchor currently selected |
| `tree--zone-marked` | in the multi-select set |
| `tree--zone-hidden` | archived, excluded from counts |
| `tree--zone-copied` | minted by a copy (§7) |

Branch-distinguishing colour, if wanted, cycles a small fixed palette modulo N rather
than minting a class per branch.

Rich `Text` styles character ranges, so a row can carry one style on its gutter portion
and another on its text. That covers highlighting part of a branch — an elide span, or
the divergence between the cursor's path and a hovered node — without a custom widget.

`tree--zone-path` is what replaces the guide-hover highlight §2 removes. It is drawn
per row from the ancestor set, not from guide rails, so flattening does not affect it.

---

## 4. Making transformations visible

### 4.1 Compaction becomes a fold header

**Decision.** Draw a splice anchor immediately *before* the first entry it covers, with
the covered run as its widget children. The kept run continues after it, at the
header's own level.

```
⊟ compaction — folds 3 entries, resumes at m4
   m1
   m2
   m3
 • m4
 • m5
 • m6
 • m7
```

One structure answers four separate requests. The covered nodes are exactly the
header's subtree, so highlighting them on hover or select is free. Collapse is the
same gesture as every other fork. Archiving a span is a collapsed header. Iterative
compaction nests correctly, because a later anchor's covered span contains the earlier
anchor and everything it covered — which is precisely what "last anchor in the path
wins" (`conversation_tree.py:353-356`) means.

**What this costs, stated plainly.** §2 rewrites nesting. This rewrites *order*. Under
the file store, `append_compaction` (`session_store.py:572-577`) is a plain tip append:
the entry's real `parentId` is the leaf at the time, and we are drawing it above the
first entry it covers. A reader who assumes vertical position means time is misled.

Mitigation: render it as chrome — a labelled rule, not a message row — and let
`TreeDetailPane` state where the entry actually sits.

Note the two stores disagree on this shape. `SessionStore.append_compaction` appends at
the tip. `SessionManager.apply_compaction` re-parents `first_kept` onto a spliced entry
(I have not re-verified this in current code; it is recorded from prior work).
`_active_path_entries` folds both (`conversation_tree.py:365-371`), and the fold header
renders both identically, because it is positioned from `firstKeptId` rather than from
`parentId`.

**Rejected: the bracket.** Leave the anchor where the log put it and draw a rail down
the left of the covered rows, highlighted when the anchor is selected. No structural
lie. But no collapse gesture, no archive, and the rail must be hand-drawn — which is
the part of the custom-widget option §2 declined.

### 4.2 The compaction row gets elide's label

> **Built (2026-08-23), with one correction to this section.** `_elide_preview` is gone,
> replaced by `_splice_anchor_preview` (`conversation_tree.py:502`), `_splice_span_phrase`
> (`:552`) and `_boundary_trails_anchor` (`:578`). `_preview_of` (`:479`) dispatches
> `_SPLICE_ANCHOR_KINDS` *before* `_SUMMARY_KINDS`, since `compaction` is in both and the
> anchor rendering is the more specific one. `_SPLICE_VERBS` (`:75`) maps the kind to its
> verb — `compaction` "folds", `elide` "hides", following this document's own usage in
> §1.2 and §4.1. A kind added to `_SPLICE_ANCHOR_KINDS` without a verb raises rather than
> rendering a spanless row.
>
> **Composition: span first, summary after an em dash** — `folds 3 entries, resumes at
> e05 — SUMMARY-1`. `_preview_of` keeps only the first line and the row is then elided to
> the remaining width, so summary-first would let truncation eat exactly the fact this
> change adds, and a multi-line summary would delete it outright at the `split("\n")`. An
> elide has no summary and degrades to precisely the old string, so elide rows are
> byte-identical.
>
> **The correction.** "Give `compaction` the same arithmetic" is not literally
> implementable, and taking it literally would have made τ's own normal compaction shape
> report itself as corrupt. §4.1 already notes the two stores disagree: `SessionStore`
> appends at the tip, while `SessionManager.apply_compaction` re-parents `first_kept`
> onto the anchor. In the re-parented shape — which the frozen System-A oracle and the
> fold-parity fixtures pin — `firstKeptId` is a **descendant** of the anchor and is
> therefore *not* on the parent's path, which is the exact condition `_elide_preview`
> treats as a broken log. The case splits three ways, not two:
>
> 1. the boundary is on the parent's raw path → the kept suffix, counted as before;
> 2. the boundary is a descendant of the anchor → the kept region is empty and the count
>    is the whole folded parent context, which is a *real* count and renders normally;
> 3. anything else — missing, unknown, or on a sibling branch → the honest warning,
>    `compaction → e02: resume point is not on this path (folds everything) — SUMMARY-1`.
>
> A root-level anchor short-circuits to zero rather than calling `context_entries(None)`,
> whose `leaf=None` means "use the cursor" and would have answered a different question
> plausibly.

`_elide_preview` already computes span size and resume point from structure
(`conversation_tree.py:487-518`). Give `compaction` the same arithmetic. Both kinds are
in `_SPLICE_ANCHOR_KINDS` (`conversation_tree.py:64`) and the computation does not care
whether the anchor renders a summary.

### 4.3 Branch summary gets its sibling styling

> **Built (2026-08-23), as zone work.** Two component classes, `tree--zone-summary` and
> `tree--zone-abandoned` — roles, not branches, per §3. Two rather than one because a
> single "part of a summary pair" class cannot say *which* half a row is, and the pair
> only reads if the ends are distinguishable. Both sets are computed once in `__init__`
> from the `TreeNode` graph, so no payload is read and `fromId` is never consulted:
> `parentId` is what the graph is already built from, and `append_branch_summary`
> guarantees the two agree.
>
> **The pair is the summary and its *immediately preceding* sibling**, in the order the
> browser already draws — not every non-summary sibling. A branch point can be abandoned
> twice, and `b1, S1, c1, S2` then pairs correctly where a set difference would blame
> `S2` for `b1`. It is also this section's own phrasing: "*the* abandoned branch's first
> message", singular. A `branch_summary` with no earlier sibling lands in neither set
> rather than being half-painted.
>
> **One hue for both, differing weight**, and **no new colour**: `#89dceb`, already the
> palette's calm cyan (`.box-user`, `#chat-input:focus`) and the one Catppuccin accent no
> zone had taken. The shared hue says "pair"; `text-style: dim` on the abandoned half says
> which end. The pairing is carried by the two rules agreeing rather than by the hue
> itself, so a theme swap moves it intact. Precedence sits behind `folded`/`covered` — "not
> in your context" outranks what a row is — and ahead of `path`, since a whole-chain zone
> would swallow a two-row relation.
>
> **One thing this section did not predict.** Because §2 flattens single-child runs, the
> summary and its abandoned head usually render *several rows apart* rather than adjacent.
> Proximity therefore cannot carry the relation, which is exactly why the shared hue has
> to.

> **Superseded history (kept per this document's convention).** It was attempted alongside §4.2 and
> cannot be done there. `_preview_of` renders one line for one node, and what this section
> asks for is a *two-row relation* — that the summary and the abandoned branch's first
> message are siblings. That is `render_label` and zone-class work (§3). The only
> payload-local thing available is the raw `fromId`, which the detail pane already shows
> and which a reader cannot act on from a row. `conversation_tree.py` now carries
> `test_branch_summary_preview_is_untouched` pinning the current behaviour with that
> reasoning, so a later change here is deliberate rather than incidental.
>
> **It was reclassified into step 3 and step 3 then shipped without it.** The machinery it
> needs now exists — `ZoneTree.render_label` and `TreeZones` — but no zone marks a
> `branch_summary` and its sibling, so this is an open item with no step of its own. It is
> the smallest remaining piece of §4 and does not depend on §4.1.

No structural change (§1.2). The summary and the abandoned branch's first message are
already siblings. Render them so a reader can see that.

---

## 5. Selection, collapse and commit

### 5.1 Click selects; Enter commits

> **Built (2026-08-23), as decided.** `SessionTreeModal.BINDINGS` carries
> `Binding("enter", "commit", priority=True)`; `action_commit` dismisses with the cursor
> node, and `on_tree_node_selected` now calls `move_cursor` instead of dismissing. No
> Textual private method is overridden.
>
> The priority path was verified in the installed textual 8.2.7 rather than assumed:
> `App._process_messages` calls `_check_bindings(key, priority=True)` over
> `reversed(screen._binding_chain)` *before* forwarding the key to the focused widget, so
> `Tree`'s own `enter` → `action_select_cursor` never fires.

Today `Tree._on_click` sets `cursor_line` and then runs `select_cursor`, which posts
`NodeSelected` — the same path Enter takes. `on_tree_node_selected` (`app.py:746-749`)
dismisses on it. That is why a click jumps.

**Decision.** Add a screen-level `Binding("enter", "commit", priority=True)` on the
modal. Priority bindings run before focused-widget bindings, so the `Tree`'s own
`enter` never fires. `NodeSelected` then arrives only from clicks, and that handler
sets selection instead of dismissing.

This needs no override of Textual private methods. Rejected alternative: override
`_on_click` to suppress `select_cursor`. It works and it couples the modal to a private
method across Textual versions.

### 5.2 Left collapses

> **Built (2026-08-23), as decided.** `action_collapse` collapses the cursor node when it
> has widget children and is expanded, and otherwise moves to the widget parent. One case
> the section did not mention: the move skips the widget's hidden root, because
> `show_root` is False and moving onto it clears the cursor.

`left` is unbound in `Tree.BINDINGS` (verified: only `shift+left` is `cursor_parent`).
Bind `left` to collapse the current node when it has widget children, and to move to
the enclosing fork when it does not. That is the standard file-tree idiom, and after
§2 "has widget children" means "is a fork", which is the unit worth collapsing.

### 5.3 The modal returns an intent

> **Built (2026-08-23).** §11.1 settles what an intent is and who performs the durable
> operation; §11.2 settles that "hidden" is view state. Read both before this section.
>
> `TreeIntent(action, ids)` is frozen, with `action` typed
> `TreeAction = Literal["navigate"]` — one member today, widened as each operation lands,
> so an unhandled action name is a type error rather than a silently ignored string. A
> `sole_id` property raises unless the intent names exactly one node, which is what makes
> the degenerate `navigate` case safe to unpack at a call site.
>
> `TreeZones` is a frozen dataclass carrying `cursor`, `marked`, `path`, `folded`,
> `covered`, `hidden` and `copied`, **with no defaults** — a defaulted `copied` is how a
> real producer gets forgotten.
>
> **`roots` and `resolve_entry` collapsed into the `ConversationTree`.** This strengthens
> the contract this section's class docstring defends rather than weakening it:
> `ConversationTree.entry` answers for every id `tree()` produced, so the rows and the
> bodies are provably the same log. Before, a caller could pass a mismatched pair.
>
> **`right` now expands.** Taking `space` for marks removed Textual's only expand
> gesture, leaving §5.2's `left`-collapse with no inverse. That was an unlisted
> regression introduced by this step, not a feature.
>
> **The readout costs the tree no rows.** `#tree-browser-marks` took the blank row that
> `#tree-browser-help`'s `margin-top: 1` was spending. Adding a real row instead dropped
> the tree from 10 rows to 9 at 120×16 and failed
> `test_the_pane_gives_the_rows_back_to_the_tree_when_short`, so `DETAIL_MIN_HEIGHT` is
> unchanged at 20 and its measurement test still passes untouched.
>
> **No per-row measured `input_tokens`.** This section says a row *may* state one. It
> does not. The binding requirement is that totals be labelled, and a number on every row
> widens every label and every snapshot for no step-3 need. The selection total prints
> `~N tokens (estimate)`, and a test asserts that line carries exactly one token figure.

Change `ModalScreen[Optional[str]]` to return an action name plus the node ids it
applies to. `dismiss(id)` becomes the degenerate case (`navigate`, one id).

Four selection sets drive the rendering, not one cursor:

1. **cursor** — one node; drives `TreeDetailPane`
2. **marked** — the multi-select set; drives counts and the lowest common ancestor
3. **derived** — what the cursor node covers or connects; drives §3's zone classes
4. **hidden** — collapsed or archived

Sets 1 and 3 are required by §3 and §4. Sets 2 and 4 are the operations in §1.3.
Building the renderer against four sets costs little more than one and is the part that
is expensive to retrofit.

The modal also needs the `ConversationTree` itself, not just `roots` and
`resolve_entry` (`app.py:4901`). `context_entries` gives the folded-out span,
`subtree_text` already exists, and `_parent_of` — which the modal builds at
`app.py:581-586` — gives the lowest common ancestor for free.

**Counts are labelled estimates.** `compaction.estimate_tokens` is character-based.
Real numbers exist only on assistant messages, where `usage` is persisted into the
message dict (`agent_loop.py:819`). A row may state a measured `input_tokens`; a
selection total may only state an estimate, and must say so.

---

## 6. The general model: plan, then commit

> **Built (2026-08-30), with the plan DERIVED rather than edited.** The algebra is
> `tau_agent_core/tree_surgery.py` (pure, holds no `SessionLog`); the durable half is
> `TauBackend.commit_branch`, a sibling of `elide_span`; the gesture is `ctrl+B` in the
> browser and a two-button `BranchModeModal` after it. §6.3's four steps are performed
> in that order, and `append_at`'s "does not move the leaf" is what makes the mint
> atomic from the cursor's point of view, exactly as the section argues.
>
> **The one divergence: nothing edits the plan.** §6.2 describes an ordered list of
> `keep(id)` / `copy(id)` items, which implies a buffer the reader adds to, reorders and
> commits. What is built derives that list from the marked set — the longest run of
> marks that is already a real ancestor chain is kept, everything after the first gap is
> copied — and commits it in one gesture. The reason is that no gesture vocabulary for
> editing the list survived contact with the keyboard this browser has left (§6.4): the
> reader would need keys for "add here", "reorder", "make this one a copy", and the
> screen has `Enter`, `Space`, `←→`, `^E`, `^D` and `Esc` already spent. Deriving the
> split loses the ability to force a copy of a message that could have been kept — which
> nothing has asked for, since a keep is strictly better when it is available (same id,
> same recorded usage, no duplicate search document).
>
> **Case A is detected and preferred, as §6.3 says it must be.** A contiguous selection
> mints nothing: `commit_branch` finds `copies == ()` and the commit becomes a navigate
> plus an elide. `test_commit_branch_mints_nothing_for_a_contiguous_selection` pins it,
> because the day that stops being true is the day every branch starts littering.
>
> **What the two modes are.** §6 does not say what happens to the context ABOVE the
> plan, and both answers are wanted: "keep the context above them" appends no elide, and
> "keep only the system prompt" appends one resuming at the root-most mark. The second is
> only worth having because the fold now carries system messages across a splice
> (`docs/SYSTEM-PROMPT-IN-THE-FOLD.md`) — before that, "keep only the system prompt"
> would have produced a context with no system prompt in it.
>
> **A third mode is missing, and the gap is worth naming.** "Select which parts of a
> region go into a summary, then summarize exactly those" does not compose out of what is
> built: summarizing a branch summarizes its SUBTREE (`subtree_text`), which is the copied
> messages only — the reused ones are the branch's ancestors — and a contiguous selection
> copies nothing at all. The pieces for the mode exist (`planned_messages` is the exact
> message list, `summarize_branch` is the summarizer, `append_branch_summary` is the
> node); what it needs is a decision about where the resulting summary hangs, which is the
> same question §4.1 is still parked on for the fold header.

### 6.1 Why an intermediate step is load-bearing

Take a chain `m1 → m2 → m3 → m4 → m5` and a desired path `m1, m4, m5`.

This is not expressible with the current elide. `_active_path_entries`
(`conversation_tree.py:372-379`) emits `[anchor] + ancestors from firstKeptId onward`,
so the hidden set is always a **prefix** of the path. Keeping `m1` and skipping
`m2..m3` requires keeping a prefix and a suffix with a gap between them. Setting
`firstKeptId = m4` also drops `m1`.

The general form requires minting entries. Now consider composing two gestures — an
elide, then pasting a copy of `m4` above the result. There are two ways to implement
the paste:

1. Re-parent the existing node onto the new one. This mutates `parentId`.
2. Mint the new node *and* a fresh copy of everything below it, orphaning the previous
   attempt.

Option 1 breaks I1 (`NODE-ADDRESSABLE-AGENTS.md`). I1 holds *only* because `parentId`
is written once at append: an entry's ancestor chain is fixed the moment it exists, so
`context_for(L)` is fixed forever. Allow re-parenting and "what did this message see"
stops being answerable — which is the one property the whole browser is being built to
expose.

Option 2 is append-only and litters debris for every intermediate gesture.

**A staging buffer removes the choice.** Gestures mutate a plan. The commit mints the
divergent tail once, in order, with correct parents on the first attempt. No
re-parenting, no debris.

The second reason is validation. `fork_admission_reason`
(`conversation_tree.py:206-277`) rejects a point whose most recent assistant message has
`toolCall` blocks with no matching `toolResult` on the path — a prefix most providers
reject outright. Copy and paste can produce that state from individually valid
gestures: copy the assistant message, omit the tool result. **The staging buffer is the
only place the composed path can be checked before it is durable.**

So the intermediate step is not an efficiency measure. It is what keeps the log
append-only and the result well-formed.

### 6.2 The plan

An ordered list of items, root-most first. Each item is either:

- `keep(id)` — an existing entry, used in place
- `copy(id)` — an existing entry's content, to be minted here as a new entry

### 6.3 Commit

Let the plan be `[p0 … pn]`.

1. **Validate.** Walk the planned message sequence with
   `fork_admission_reason`'s outstanding-`toolCall` logic. Refuse the whole plan on
   failure. Nothing is appended.
2. **Find the reusable prefix.** Take the largest `k` such that `p0…pk` are all `keep`
   items forming a contiguous ancestor chain in the real tree.
3. **Case A — pure suffix.** If `k == n` and `p0…pn` is a contiguous suffix of `pn`'s
   real ancestor chain: `append_navigate(pn)`, then `append_elide(p0)` if `p0` is not
   already the root of that chain. **Zero copies. Every id keeps its identity.**
4. **Case B — everything else.** `append_navigate(pk)`; `append_elide(p0)` if the plan
   drops any of `pk`'s ancestors; then mint `p(k+1)…pn` as copies, each parented at the
   previous, using `append_at` (`session_log.py:116`, implemented in all three stores).
   Finish with `append_navigate` onto the last minted id.

`append_at` does not move the leaf. So the mint is invisible to any reader until the
final `append_navigate` lands. A mint that fails partway leaves orphan entries off the
path and the cursor exactly where it was — the commit is atomic from the cursor's point
of view.

**This is why elide survives.** It is not replaced by copies; it is the case-A
optimization the commit step detects and prefers, because it is the only form that
mints nothing and preserves identity. Compaction does not reduce to copies at all — it
replaces a span with new content, so its summary is a new entry under any model.

### 6.4 The gesture vocabulary

> **Decided and built (2026-08-30).** §10 required this to be designed before step 7
> started; it is recorded here rather than in §5 because it is what the plan is made OF.

Three keys, and the constraint that shaped all three is that the browser's keyboard was
nearly full before this step: `Enter`, `Space`, `←`, `→`, `^E`, `^D`, `Tab` and `Esc`
were spent, and the help line — one row, 76 columns of dialog interior — had already
wrapped once and been shortened.

| Key | Gesture | Why this key |
|---|---|---|
| `ctrl+B` | Branch from the marked messages | Beside `ctrl+E`. The two are one family: an elide keeps a contiguous run, a branch keeps a selection with gaps. A reader who has found one should find the other next to it. |
| `c` | Copy the subtree under the cursor | Bare letters are free on this screen — there is no text input — and `c`/`v` are what the gesture is called everywhere else. |
| `v` | Paste the copied subtree under the cursor | As above. |

All three are `priority`, like `Enter` and `Space` before them: `Tree` binds no letters
in textual 8.2.7, and relying on that staying true across an upgrade is how a key
silently changes meaning.

**Where the plan is displayed: nowhere, and that is the point.** The marks ARE the plan
(§6's built note), so the display of the plan is the zone colouring that already exists
— marked rows are green, the clipboard's subtree is mauve. What the readout adds is one
OFFER line, on the same rule §5's elide offer follows: the offer appears exactly when the
key would do something. Three gestures can now apply to one row and two of their offers
do not fit on a row together, so they are ordered by how specific the state that produced
them is — a pending paste beats a legal elide beats a branch — and only one is shown.

**Marks pair with their tool group at SELECTION time.** Marking an assistant message that
made tool calls also marks the results answering it, and marking a result marks the call.
This was chosen over refusing the half-selection at the commit: the commit's refusal
teaches the rule one attempt at a time, while the expansion shows the group on the rows
at the moment the reader presses `Space`. The commit still checks
(`tree_surgery.admission_reason`), because a caller that assembles ids itself — the RPC
surface, a test, a second head — does not go through the browser at all.

**A paste does not move the cursor.** It edits the tree; the context changes only when
the reader presses `Enter` on something. The consequence is that returning to the
conversation after a paste would show an unchanged transcript, so `action_browse_tree`
re-opens the browser instead — carrying the clipboard, which is what lets one copy reach
several places. That loop is also the shape §1.3's "set the active leaf — an action that
runs and returns to the browser" asked for, arrived at from the other direction.

---

## 7. Copy entries

### 7.1 Shape

> **Built (2026-08-30), with one correction.** The shape is exactly as written, minted by
> `tree_surgery.copy_of` and appended by `TauBackend.paste_subtree`. Three notes.
>
> **`copiedFrom` did NOT join `_CROSS_REF_FIELDS`, and must not.** That tuple is the set
> of fields a fork *must* be able to resolve, and `_remap_cross_refs` RAISES on one it
> cannot — correctly, because a dangling `firstKeptId` silently drops a region of context.
> A copy's source is normally OUTSIDE the subtree being copied; that is what makes it a
> copy rather than a move. Putting `copiedFrom` in that tuple would have made
> `JmftsSessionLog.fork` refuse to fork any tree anyone had ever pasted into. It went into
> a second tuple, `_PROVENANCE_REF_FIELDS`, whose rule is "remap when resolvable, leave
> alone when not" — the same reasoning §8's built note gives for `agentSpecId` not being
> validated: nothing folds on it, so a stale one costs a reader one hop of history rather
> than a region of context. The importer follows the same rule.
>
> **The copyable kinds are `message`, `customMessage` and `branch_summary`.** A copied
> `branch_summary` drops its `fromId`: that field names the branch point the summary was
> written at, which the copy is not at, and carrying it over would state a relation to a
> node the copy has no edge to. The two splice anchors and `navigate` are NOT copyable at
> all, for the reason that makes the whole §7 shape work in reverse — they carry an id
> naming a position the copy's new path does not have, and `_active_path_entries` reads an
> unreachable `firstKeptId` as "keep nothing".
>
> **A subtree cannot be pasted into itself.** The copy and the original would then share a
> root→leaf path, and a duplicated `tool_call_id` on one path stops naming one call. This
> is the only structural refusal `plan_paste` makes.

A copy is `{"type": "message", "message": {…}, "copiedFrom": "<source id>"}`.

`type: "message"` deliberately, not a new kind. A copy *is* an ordinary message on an
ordinary path; every existing walker should treat it as one without being taught
anything. The provenance is one extra field. This follows the same reasoning as
Decision 2 in `NODE-ADDRESSABLE-AGENTS.md`: put the meaning in the tree shape and the
payload, not in a flag every consumer must learn.

`copiedFrom` holds an entry id, so it joins `_CROSS_REF_FIELDS`
(`tau-jmfts/.../store.py:120-127`). Under the JMFTS store an entry id *is* a document
id, so anything that mints new documents for existing entries — `fork` today — must
rewrite it or leave it pointing at the wrong tree.

### 7.2 Duplicate documents in JMFTS

Every entry is a document and `_content_for` (`store.py:83-94`) projects message text
into the searchable content. Copying two messages mints two more documents with
identical text. Search returns the same content at two ids.

**Decision: store the content, accept the duplicate, record `copiedFrom` so a query can
dedupe.** A copy is a real message on a real path and must be findable there. The
alternative — an empty content projection on copies — makes the message invisible on
the live branch and visible only on the abandoned one, which is backwards.

### 7.3 Eliding never affects searchability

> **Built (2026-08-23), with a bounded projection.** `_elide_content`
> (`tau-jmfts/.../store.py:83`) is dispatched from `_content_for` (`:120`) and projects
> `elide: history folded here, context resumes at entry {firstKeptId}`, or, with no
> `firstKeptId`, `elide: history folded here, no resume point recorded`. An elide entry is
> now findable, which it was not.
>
> **The span count is deliberately absent, and this is not a shortcut.** This section says
> to fix it "using the same computed span text" as §4.2. That text is not reachable here.
> `_content_for(kind, payload)` receives one entry's payload, while the span needs the
> tree — a `parentId` walk plus a `context_entries` diff — and at both call sites the tree
> is unavailable *in principle*, not merely inconvenient: in `JmftsSessionLog._append` the
> document does not exist yet and has no doc id, and in `importer.py` the ancestors are
> mid-remap. A fabricated N in a search index reads as a recorded measurement forever, so
> the projection carries only what the payload holds.
> `test_elide_projection_invents_no_count` pins that the only digits in the content are
> the entry id. If the count is wanted in the index later, the seam is a post-write
> enrichment pass that has the tree, not this function.
>
> A missing `firstKeptId` is *stated*, not raised. Both appenders reject an anchor naming
> no entry, so such a payload is a corrupt or hand-written log — and the importer is the
> tool one reaches for to get such a log somewhere inspectable. Refusing to project would
> block that. It is the same policy `ConversationTree` applies to the row.
>
> The stale `_CROSS_REF_FIELDS` docstring is fixed: it now lists `navigate`
> (`targetId`), `compaction` **and `elide`** (both `firstKeptId`), and `branch_summary`
> (`fromId`).

Stated explicitly because it is load-bearing and easy to erode.

`elide` is not a deletion. It is engineering the conversation to proceed in a new
direction. Hiding a branch marks it done; it does not remove it. History is never
destroyed — the operations here are for moving around it and bringing chosen parts into
a new context.

Today an elide document has empty searchable content, because `_SUMMARY_KINDS` in the
JMFTS store is `("compaction", "branch_summary")` and elide falls through to `return ""`
(`store.py:83-94`). So a JMFTS query can never surface "where did someone fold
history". That is worth fixing alongside §4.2, using the same computed span text.

Also stale: the docstring above `_CROSS_REF_FIELDS` lists the appenders as "navigate,
compaction, branch_summary" and omits `elide`. The behaviour is correct — `elide` uses
`firstKeptId`, which is in the tuple — but the comment is wrong.

---

## 8. Provenance on anchors

> **Built (2026-08-23), per §11.3.** This section's claim that the change is "additive"
> is wrong about the *signature*; §11.3 corrects it, and the five implementations it names
> were confirmed exact. Read §11.3 before this section. 8.4 is untouched, as this section
> already says, and 8.3's `load_extensions` lag is documented at the Protocol rather than
> fixed. Release note: `docs/RELEASE-NOTES-0.9.4.md`.
>
> ```python
> def append_compaction(self, summary: str, first_kept_id: str, tokens_before: int, *,
>                       summarizer_model_id: str, summary_usage: dict[str, int],
>                       covered_entries: int, covered_tokens: int,
>                       agent_spec_id: str | None) -> str: ...
>
> def append_elide(self, first_kept_id: str, *,
>                  covered_entries: int, covered_tokens: int,
>                  agent_spec_id: str | None) -> str: ...
> ```
>
> Payload keys are camelCase beside `firstKeptId`/`tokensBefore`: `summarizerModelId`,
> `summaryUsage`, `coveredEntries`, `coveredTokens`, `agentSpecId`.
>
> **Elide takes three of the five, not five.** It generates no summary, so
> `summarizer_model_id` and `summary_usage` would be parameters whose only admissible
> value is a placeholder — the same swallowed gap §11.3 rejects, wearing symmetry as a
> disguise.
>
> **The covered span is passed in, not computed inside the appender.** Three reasons, kept
> in the `append_elide` docstring. `covered_tokens` is not recoverable from structure —
> §8.2 says exactly that — so it must cross the boundary anyway, and computing the *count*
> store-side while the *token* figure comes from the caller would let the two describe
> different spans with nothing to catch it. The arithmetic already exists once in
> `ConversationTree._splice_span_phrase`, so pushing it down would mean five copies or a
> store that depends on `ConversationTree`. And both call sites already compute the span
> and throw it away — `elide_span` literally builds `hidden` for its no-op refusal check —
> which is §8.1's pattern verbatim. The shared arithmetic went into
> `compaction.estimate_span_tokens`, the module that already owns token estimation, so
> `tokensBefore` and `coveredTokens` are produced on the same basis.
>
> **`agent_spec_id` is ancestry, not recency — the obvious answer was wrong.** It is *not*
> "the last spec this session wrote". `agent_spec_in_force(entries, leaf_id)`
> (`session_log.py`, beside `resolve_cursor`, for the same reason: entry algebra, not
> durability) walks `parentId` for the nearest `agent_spec` `customEntry`. The two answers
> diverge exactly where it matters — the browser aims an elide at a historical anchor that
> may be two `set_model` swaps behind, and a spec on a sibling branch governed nothing on
> this path.
>
> **`agent_spec_id: str | None`, required, no default.** `None` is a reachable real answer
> — a pi-imported log, a store driven without an `AgentSession`, a contract-suite store.
> §11.3 rejects a *default* of `None`; with no default, "the caller did not say" is
> impossible, which is the distinction that mattered.
>
> **`agent_spec_id` is deliberately not validated against the entry set**, unlike
> `first_kept_id`, with the asymmetry written at the Protocol: a dangling `first_kept_id`
> silently drops the whole kept region from model input, while a dangling `agentSpecId`
> makes one browser row unhelpful, and `agent_spec` is a record never read back.
> `_covered_span` **raises** rather than returning `[]` when the boundary is off-path — a
> fabricated zero beside a compaction that folded everything is worse than a traceback.

The browser's labels are only as good as what the log records. Four gaps, each
verified.

**8.1 Summary provenance is discarded at the write.** `CompactionResult` carries a
`usage` field documented as what generating the summary cost (`compaction.py:112-119`).
`append_compaction` takes `summary`, `first_kept_id`, `tokens_before` and drops the
rest. `AgentSession._summarizer()` (`agent_session.py:866`) can route a summary through
a *different* model than the conversation. So for any compaction node, "which model
wrote this and what did it cost" is unanswerable — and both numbers existed at write
time.

**8.2 Elide records no size.** `append_elide` (`session_store.py:579-599`) stores only
`firstKeptId`. Compaction at least stores `tokensBefore`. The span count is exactly
recomputable from structure; the token figure is not.

**8.3 The frame is a digest.** `_record_agent_spec` (`agent_session.py:585-640`) stores
`system_prompt_digest`, a sha256, deliberately never the prompt text. A change is
detectable; the prompt is not reconstructible. The record is also re-written only at
construction and `set_model`, not on `load_extensions`, so the recorded extension list
can lag what was bound.

**8.4 The TUI does not send the fold.** `app.py:3813` passes `self.messages` to
`submit_turn`, which forwards it as `context=` to `AgentSession.submit`
(`backends.py:1647-1661`). The model receives the TUI's working list, not
`ConversationTree.context_for`. They agree because every transformation reassigns
`self.messages` from the fold. Nothing checks that.

**Decision.** Record transformation provenance on the anchor entry: the summarizer
model id, the covered count and token estimate as computed at write time, and the id of
the `agent_spec` in force. Additive — all three stores pass unknown payload fields
through unchanged. It converts §4's labels from recomputed guesses into recorded facts.

8.4 is a separate defect and is not fixed here. `usage.input_tokens` on assistant
messages is an independently recorded measurement of what the model actually received,
so comparing it against an estimate over `context_entries(parent)` is a cheap detector
for the divergence. That belongs in its own change.

---

## 9. What holds, and what does not

**The audit property.** `_active_path_entries(X)` walks `X` to the root by `parentId`
and consults nothing else. An anchor appended after `X` is a descendant of `X`, so it is
never on that walk. Both stores are append-only. Therefore the fold evaluated at `X` is
stable against every future append, and `context_entries(X)` today reproduces the entry
set that was in context when `X`'s child was generated.

A conversation continued from an elided section is therefore structurally
distinguishable from the same conversation continued with full context: the elide node
is an ancestor of the later messages in one and absent in the other, and the hidden
entries remain in `entries()` either way.

This is the property §6.1 refuses to trade away, and §6.3 is shaped by it.

**What still qualifies it.** 8.3 and 8.4 above, and `Model.reasoning_replay`
(default `"turn"`), which drops cross-turn thinking at wire time — a deliberate
divergence from pi, not a defect, and one that makes an entry-level reconstruction
non-identical to the request that was actually sent.

---

## 10. Build order

Status as of 2026-08-30. This is the one place to read it.

| # | Step | Sections | Status |
|---|---|---|---|
| 1 | `guide_depth = 2`; regroup the `Tree` input so single-child runs are siblings; give `_elide` a floor instead of a silent overflow | §2 | **built** |
| 2 | Split click from Enter; bind `left` to collapse | §5.1, §5.2 | **built** |
| 3 | Pass the `ConversationTree` in; change the return to an intent; build `render_label` against the four selection sets and the zone classes | §3, §5.3, §11.1, §11.2 | **built** |
| 4a | Compaction span label | §4.2 | **built** |
| 4b | Elide searchable text | §7.3 | **built**, count deliberately absent |
| 4c | Branch-summary sibling styling | §4.3 | **built** |
| 4d | Compaction fold header | §4.1 | **not started — undecided**, see below |
| 5 | The hover divergence highlight | §3 | **built** |
| 6 | Anchor provenance fields | §8, §11.3 | **built** |
| 7 | The plan, copy entries and the commit algorithm | §6, §6.4, §7 | **built**, plan derived rather than edited |

Verified with steps 1–6 landed: whole suite **4647 passed, 140 skipped, 6
deselected, 7 snapshots passed**; `ruff check`, `ruff format --check` and `mypy` over all
four `src` trees in one call all clean.

Verified with step 7 landed (2026-08-30): whole suite **5231 passed, 145 skipped, 6
deselected, 7 snapshots passed**, of which 43 tests are new — 25 in
`tau-agent-core/tests/test_tree_surgery.py` (the pure algebra), 18 in
`tau-coding-agent/tests/test_tree_branch_and_paste.py` (the gestures, the two commits and
the two flows) — plus one case added to the `SessionLog` contract suite. `mypy` over the
four `src` trees in one call, `ruff check` and `ruff format --check` all clean. Two
snapshots were re-recorded, and the only pixels that moved are the help line's.

1 and 2 were independent and small, as predicted. 3 is the step that decides whether 6
and 7 are cheap or expensive, and 6 was built in parallel with it only because §11.3
settled 6's shape without waiting for 3 — which held: no failure in either step traced
to the other.

**What is left.** Two items, neither of which belongs to a numbered step:

- **The archive gesture** (§11.2's built note). `hidden` covers collapse only; no key
  marks a branch done and nothing is excluded from counts.
- **The plan cannot be inspected or edited before it is committed** (§6's built note).
  `ctrl+B` derives the keep/copy split and commits in one gesture. The reader sees the
  result in the notification, not the plan in advance.

`tree--zone-copied` has a producer as of step 7: the clipboard's subtree.

Everything else outstanding is step 4d (undecided).

**`tree--zone-path` moved from step 5 to step 3.** §2 removes the guide-hover ancestry
highlight and §3 calls itself "the replacement, not an embellishment", so shipping step 3
without `tree--zone-path` populated would leave that regression in the tree behind a
step boundary. Only the *hover divergence* highlight — the styling of where a hovered
node's path diverges from the cursor's — remains in step 5.

**4d is the one step still undecided.** §4.1 decides for the fold header and records the
bracket as rejected, but its mitigation ("render it as chrome — a labelled rule, not a
message row") is not specified, and the reason it gives for rejecting the bracket — that
the rail must be hand-drawn — is equally true of a labelled rule. The header also
rewrites row *order*, so vertical position stops meaning time. This is easier to judge
once `render_label` exists, so it is deliberately sequenced after step 3 rather than
inside step 4.

**A known consequence of §11.1, for step 7.** `TreeIntent(action, ids)` cannot express a
plan: §6.2's plan is an ordered list of `keep(id)` / `copy(id)` items, not a set of ids.
Step 7 will widen the return type. That was the stated cost of the option §11.1 chose,
and it is small — but the gesture vocabulary (which keys produce plan items, where the
plan is displayed) is not designed in this document and must be before step 7 starts.

> **Resolved (2026-08-30), and the return type did NOT have to widen.** The gesture
> vocabulary is §6.4. `TreeIntent` grew two action names and nothing else: `branch` carries
> the marked ids and `paste` carries `(copied, target)`, both of which the existing
> `(action, ids)` shape says perfectly well. The plan is derived from the ids by
> `tree_surgery.plan_branch`, so the ordered keep/copy list never has to cross the screen
> boundary — which is the same reasoning §11.1 used to keep the log out of the modal,
> applied one layer further out.

**Test obligations.** A commit-algorithm case per branch of §6.3. A refusal test for a
plan that fails turn-completeness, asserting the log is byte-identical afterwards. A
`copiedFrom` remapping case in the `SessionLog` contract suite
(`testing/session_log_contract.py`), because §7.1 adds a cross-reference field and the
suite is what makes a new store implementor aware of it.

> **All three are met (2026-08-30), the third in a different form.** Case A and case B are
> `test_commit_branch_mints_nothing_for_a_contiguous_selection` and
> `test_commit_branch_keeps_the_prefix_and_mints_the_rest`; the refusal test is
> `test_commit_branch_refuses_half_a_tool_call_and_appends_nothing`, which compares
> `log.entries()` against a copy taken before the call. The contract case is
> `test_a_copied_message_keeps_its_provenance_and_folds_like_any_other` — a
> *pass-through* case, not a *remapping* one, because §7.1's built note establishes that
> `copiedFrom` is provenance rather than structure and must NOT be remapped-or-raise. What
> the suite makes a store implementor aware of is that the field has to survive the round
> trip at all.

---

## 11. Decisions taken at build time (2026-08-23)

Three questions this document left open had to be answered before steps 3 and 6 could
start. Recorded here with what was rejected, following `LANE-REMOVAL.md`'s precedent.

### 11.1 The modal accumulates and returns one intent; it never writes

§5.3 said "change the return to an intent" without saying what an intent is, and §1.3
listed two operations a return value appears unable to express — hide/unhide, which has
"no exit at all", and set-active-leaf, which "runs and returns to the browser".

**Decision.** `SessionTreeModal` becomes `ModalScreen[Optional[TreeIntent]]`, where

```python
@dataclass(frozen=True)
class TreeIntent:
    action: str            # "navigate" | "elide" | "commit" | …
    ids: tuple[str, ...]
```

`dismiss(id)` becomes `TreeIntent("navigate", (id,))`, exactly the degenerate case §5.3
describes. The modal owns all four selection sets as in-memory state and holds no
reference to a `SessionLog`. Set-active-leaf is an intent the caller applies before
re-pushing the browser.

The apparent conflict with §1.3 dissolves once 11.2 is settled: hide/unhide is view
state, so it needs no exit because it needs no write.

**Rejected: inject an editor object the modal calls live.** A `TreeEditor` protocol
passed into the modal, which calls it and stays open. It expresses §1.3's cases
directly. It also puts log mutation inside a `ModalScreen`, so every test across the
four files that touch `SessionTreeModal` needs a real or fake log where today it needs
a `ConversationTree`. Revisit only if §7's plan buffer turns out to need incremental
durability, which §6.1 argues it must not.

### 11.2 Hidden and archived are view state

§3's `tree--zone-hidden` covers "archived, excluded from counts" and §5.3's set 4 covers
"collapsed or archived" — two different lifetimes under one name.

**Decision.** Both are per-session rendering choices. Nothing is appended, and
`tree--zone-hidden` is computed from modal state rather than read from the log. §7.3's
"history is never destroyed" then holds without an argument. Archiving does not survive
a reload; that is the accepted cost.

**Rejected: a durable archive entry kind.** It survives reload and is auditable, at the
price of a fifth kind that every walker, the JMFTS content projection and the contract
suite must learn — for a marker that says only "I am done reading this".

**Rejected: archive by writing a `branch_summary`.** No new kind, and §4.3 already
renders it. But it forces a summary to exist, so "mark this done without summarizing
it" stops being expressible.

**Built note (2026-08-23): only the *collapse* half exists.** Step 3 populates `hidden`
from rows folded away inside a collapsed fork, and the readout counts them. **Archive
has no gesture at all** — no key marks a branch done, and nothing is excluded from
counts. That was left uninvented on purpose rather than guessed at. Whoever adds the
gesture also has to decide what `tree--zone-hidden` should paint: a collapsed fork's
rows are not drawn, so the class currently paints nothing, and an archived-but-visible
row is the first case that would give it something to do.

### 11.3 §8 widens the appender signatures, with required keyword arguments

§8 called the provenance fields "additive — all three stores pass unknown payload fields
through unchanged". That is true of the payload and false of the signature.
`append_compaction` and `append_elide` are declared on the `SessionLog` Protocol
(`session_log.py:97-99`) and implemented **five** times, not three: `InMemorySessionLog`
(`session_log.py:199`), the branch wrapper (`session_log.py:387`), `SessionStore`
(`session_store.py`), `JmftsSessionLog` (`tau-jmfts/.../store.py:176`) and the
delegating `_EphemeralConversationSession` (`tau-jmfts/.../catalog.py:192-197`). Three
non-test call sites pass the arguments: `agent_session.py:3263`, `backends.py:1593` and
the catalog delegation above.

**Decision.** Add keyword-only parameters with **no defaults**, and update all four
implementations plus `testing/session_log_contract.py` together. A caller that cannot
name the summarizer model fails at the call site — which §8.1 establishes is exactly
where the value already exists and is currently being thrown away.

**Rejected: the same parameters, defaulted to `None`.** Existing callers and any
out-of-tree implementation keep working, and a missing summarizer model silently records
`None`. That is the swallowed-gap pattern the repo's Fail-Early rule exists to prevent,
and it would leave §4's labels indistinguishable between "no provenance recorded" and
"provenance recorded as unknown".

**Rejected: one `provenance: dict` argument.** One signature change no matter how many
fields §8 grows later, at the price of no type checking on field names — a typo becomes
a key nothing can query and nothing reports.

§8.4 stays out of scope, as §8 already states.
