# Research: a VS Code / VSCodium head for τ

**Status:** research record, written 2026-08-31. **Nothing is built.** This
document fills the gap `docs/HEADS-AND-MULTIPLEXER.md` §4.1 left open, where the
recommendation to build a VS Code head carried the line "Unverified: I did not
survey the VS Code webview API". Every platform claim below was read from a
vendor page or a repository this session; every claim I could not check is
marked in §9.

**Provenance.** Platform sources are web pages fetched 2026-08-31, listed at the
end. I did not run VS Code, did not build an extension, and did not read the
Cline or Continue source trees directly — the architecture claims about those two
come from their published architecture documentation, not from their code.

τ claims are different in kind: every file:line in §6, §6.1 and §6.2 was read in
this checkout, and the verb counts come from importing `COMMAND_TABLE`, not from
reading a doc.

**Relationship to existing docs.** `docs/HEADS-AND-MULTIPLEXER.md` owns the
head/core rule (§2 there) and prices the multiplexer. This document prices one
head. `docs/REMOTE-CONTROL.md` owns the RPC surface a head would drive.
`docs/TREE-BROWSER-AS-EDITOR.md` owns the tree editing gestures §6 here asks how
to render.

---

## 1. What the platform offers, and which parts are shippable

VS Code has five extension surfaces that touch chat and agents. They differ in
stability, and stability is the deciding fact, not capability.

| Surface | What it is | Status |
|---|---|---|
| `contributes.chatParticipants` | An `@name` expert inside the Chat view. Handles a prompt, streams a response. | **Stable.** Documented as a guide, no deprecation note. |
| `contributes.languageModelTools` | A tool the *host's* agent calls. Your code runs, someone else's loop drives it. | **Stable.** |
| `contributes.languageModelChatProviders` | Supply a model to the host. Your code answers, someone else's loop drives it. | **Stable.** |
| Webview (`WebviewPanel`, `WebviewViewProvider`) | An iframe you fill with your own HTML. No chat semantics at all. | **Stable**, and the oldest of the five. |
| `contributes.chatSessions` + chat session providers | Register a *session list* with the Chat view. The host renders the transcript and the session picker. | **Proposed.** Not shippable. |

### 1.1 The chat session API is the attractive one and it is closed

This is the surface that would give τ the most for the least: VS Code renders
the transcript, the session list and the picker, and the extension supplies
sessions. GitHub's coding agent and Claude Code both use it.

It is a proposed API. VS Code's rule on proposed APIs is explicit: an extension
that declares `enabledApiProposals` cannot be published to the Marketplace, runs
only in Insiders, and needs `--enable-proposed-api=<id>` on the command line. The
distribution path is a hand-passed `.vsix`.

It is also still moving. As of this session `registerChatSessionItemProvider` is
marked deprecated in favour of `createChatSessionItemController`, and
`chatSessions` does not appear on the stable contribution-points page. The
proposal issue was opened 2025-09 and is still labelled `api-proposal`.

**Conclusion: τ cannot ship on it.** Microsoft's own extensions use it because
Microsoft allowlists them. That is not a path a third party has.

### 1.2 The chat participant API is stable but lands in the wrong product

A chat participant renders markdown, code blocks, command links, buttons, file
trees, progress messages, references and anchors. That is a richer response
vocabulary than the terminal has. It is registered declaratively and the host
owns the input box, the history and the layout.

The problem is where it lives. A participant appears in the Chat view, and on
VSCodium the Chat view is not reliably there. VSCodium users report the Copilot
Chat extension refusing to load with "this extension is using the API proposal
'chatParticipantPrivate' that is not compatible with the current version of
VSCodium", and the working combination is a specific pinned pair of versions. A
head whose availability depends on which build of a proprietary extension the
user pinned is not a head this project can support.

**Conclusion: a chat participant is a VS Code-only convenience, not the head.**

---

## 2. What the comparable extensions actually did

Three data points, all of them agent front ends, all of them chose the same
thing.

### 2.1 Claude Code — webview panel, bundled binary

The Claude Code extension bundles the `claude` binary and opens a webview. It is
an editor tab by default; `preferredLocation: sidebar` moves it. It renders
diffs inline and reads the editor selection for `@`-mentions. It did **not** use
the native chat view for its own UI, despite Anthropic having access to the
chat session API that the search results say it also adopted.

The lesson for τ: the shipped shape of a serious agent head is *a webview plus a
child process*, which is exactly §4.1's recommendation, and it is now the
majority pattern rather than a guess.

### 2.2 Cline — a core, a host bridge, and several heads

Cline splits into a platform-agnostic core and a `HostProvider` singleton. The
core never calls a VS Code API. It calls gRPC clients that the host registers:
`HostProvider.workspace` (file and project metadata), `HostProvider.window` (UI
dialogs and notifications), `HostProvider.env`, `HostProvider.diff`. Protobuf
defines both directions. The webview talks to the extension over VS Code's
`postMessage`, wrapped in a gRPC-over-postMessage transport so the calls are
typed, streamable and cancellable. A standalone mode serves the same protocol
over a real port, and a CLI head built on Ink drives the same core.

Two lessons, and the second is the important one:

1. **The head/core split is the same split τ already made.** Cline's
   `HostProvider` is τ's `Backend` seam, and Cline's standalone gRPC server is
   τ's `tau --mode rpc`. τ arrived at this from `docs/REMOTE-CONTROL.md`; Cline
   arrived at it from wanting a JetBrains plugin. Convergent, which is evidence
   the shape is right.
2. **Cline built the reverse channel first, not last.** `HostProvider.window`
   exists so the core can ask the host to show a dialog.
   `docs/HEADS-AND-MULTIPLEXER.md` §5.3 defers τ's reverse channel and says a
   detached session makes it urgent. Cline's design says a *graphical* head makes
   it urgent too, before any multiplexer exists: an extension that wants to show
   a diff, open a file, or ask a question has no verb for it in τ's 27.

### 2.3 Continue — core, gui, extension, and a strict message rule

Continue names three parts. `core` holds the business logic. `gui` renders and
holds UI-only state such as the current chat session. `extension` sets up the
other two, passes messages between them, and implements an `IDE` interface. The
protocol lives in `core/protocol`. Core and gui cannot talk to each other
directly; they each talk to the extension, with a pass-through route for
efficiency. All three are TypeScript. `VsCodeIde` and `IntelliJIde` implement
the same `IDE` interface, and a CLI reuses the core.

The lesson: Continue's rule that the gui holds "UI-related things like the
current chat session" is the same rule as
`docs/HEADS-AND-MULTIPLEXER.md` §2 — the head owns the draft, the core owns the
tree. Two projects, same line, drawn independently.

---

## 3. The constraint that decides this: VSCodium

The request named VSCodium alongside VS Code. That single word removes two of
the five surfaces in §1.

- VSCodium ships without the Copilot Chat extension, and the Chat view comes
  with it in practice.
- Copilot Chat depends on private proposed APIs (`chatParticipantPrivate`) whose
  version handshake with VSCodium breaks on upgrade.
- Marketplace publishing is a VS Code-only channel; VSCodium uses Open VSX, so a
  head must publish to both.

A webview has none of these problems. It has been stable for a decade, it
depends on nothing proprietary, and it renders identically on both builds.

---

## 4. Options for τ

Four, cheapest first. Costs are to τ unless stated.

### Option A — a webview head over `tau --mode rpc`

The extension spawns `tau --mode rpc` as a child process, speaks JSON-RPC 2.0
over its stdio, and renders into a `WebviewViewProvider` in the sidebar.

- **Cost to τ:** small but not zero. §6 below lists the RPC gaps.
- **Cost to the extension:** a client (pi's is 601 lines), the render layer, the
  build. TypeScript.
- **What it buys:** images render natively; the DOM `paste` event removes the
  platform matrix of `HEADS-AND-MULTIPLEXER.md` §3 (see §4.1 below — *not* the
  editor's clipboard API, which cannot do it); the tree browser can be drawn
  rather than spelled in box-drawing characters; and §4.2's remote split.
- **Portability:** VS Code, VSCodium, and any fork with the same webview API.
- **This is the recommendation.** It is what Claude Code, Cline and Continue all
  ship.

#### 4.1 Correction: the clipboard claim in `HEADS-AND-MULTIPLEXER.md` §4.1

That section says a VS Code head gets image paste because "the editor's own
clipboard API removes the whole platform matrix of §3". **That is wrong, and the
conclusion survives for a different reason.**

`vscode.env.clipboard` exposes `readText` and `writeText` and nothing else. There
is no image API. Extensions that need clipboard images work around it — one
documented workaround spawns an Electron process to call `clipboard.readImage`
and talks to it over node-ipc, which is a *worse* platform matrix than §3's, not
a better one.

The mechanism that actually works is the **DOM `paste` event**. A webview is a
browser context, so `event.clipboardData` yields `DataTransferItem`s and an
image arrives as a blob. Claude Code's extension does exactly this: it intercepts
the DOM paste event in the webview and forwards the bytes over VS Code's
protocol.

The consequence for τ: the advantage belongs to **the browser context, not to
VS Code**. Any web head gets it, including a standalone one. It is no longer an
argument for the extension specifically.

#### 4.2 What the extension has that a standalone web app does not

The webview itself is not special. It is an iframe, and it is more restricted
than a browser tab, not less: a Content Security Policy is mandatory,
`localResourceRoots` bounds the filesystem, it is destroyed when hidden unless
`retainContextWhenHidden` is set, and it reaches nothing except through
`postMessage`.

What is unique is the **process on the other end of that channel**. Four things,
in descending order of how hard they are to reproduce.

1. **The remote split, for free.** An extension declares `extensionKind`. A
   `workspace` extension's host process runs *where the workspace is* — over
   SSH, in WSL, in a devcontainer, in Codespaces. The Webview API, by contrast,
   "is always run on the user's local machine or in the browser, even when used
   from a Workspace Extension."

   Read that against `HEADS-AND-MULTIPLEXER.md` §2 and it is the head/core split,
   drawn by the platform, on the correct line. `tau --mode rpc` spawns beside the
   repository. The head renders next to the human. Neither end binds a port,
   forwards one, authenticates, or handles CORS. **This is §5.2's socket
   transport cost, paid by the host, for this one head.** A standalone web app
   must build every part of it.

2. **Editor context as a submission source.** The current selection, the active
   file, the workspace folders, opening a file at a line, the native diff view,
   decorations, the problems list. This is what makes `@`-mentions and a
   permission prompt that shows a real diff possible. It is also the whole of
   §6's reverse channel, already built, on the host side. A browser tab knows
   nothing about the editor.

3. **Placement, identity and lifecycle.** It docks in the sidebar beside the
   code and returns when the window reopens. It contributes to the command
   palette and to keybindings. There is no URL, no port, and no login. It
   installs and updates through Marketplace and Open VSX.

4. **Theme.** The webview inherits `--vscode-*` CSS variables, so the head
   matches the user's theme and font without τ deciding anything. Note this
   collides with `ffwf-theme-deferred-tcss-swap`: a VS Code head that themes
   itself looks wrong in VS Code.

**A first reading said:** items 2, 3 and 4 are all editor integration, so if τ's
head does not use the selection, the diff view or the file tree, a standalone web
app is the better deal.

**That reading is wrong, and §6.1 is why.** It treats editor integration as
optional, when opening an edited file at the changed lines and toggling a diff
for a turn are the reasons to put a head in an editor at all. The correct
statement is narrower: item 1 is what a standalone web app *cannot* reproduce,
and items 2–4 are what it would have to reimplement badly. A browser tab can show
a diff; it cannot show it in the reader's own editor, next to the file, with the
project's own syntax highlighting and their cursor left where they put it.

The consequence is a sequencing one, and it is the whole of §6.3: those
integrations need file-change records that τ does not persist. They therefore
constrain the shared render layer's contract, so they are designed **before** the
shared code, not after it.

#### 4.3 The two are not exclusive, and Cline proves it

A webview's content is HTML driven over a message channel. Written against a
small transport interface, the same bundle serves a browser tab over a WebSocket
and a webview over `postMessage`. Cline ships precisely this pair: a webview
using gRPC-over-`postMessage`, and a standalone mode serving the same protocol on
a port.

For τ that means the render layer is the reusable asset and the transport is the
cheap part — which is the answer to "can we reuse this in other τ projects". The
decision to make early is that **the render layer never imports `acquireVsCodeApi`
directly**; it takes a `send`/`onMessage` pair. Cline and Continue both enforce a
version of this rule, and Continue's is the stricter one (§2.3).

### Option B — Option A, plus a stable chat participant as a thin front door

Ship the webview as the head. Additionally register `@tau` as a chat
participant, whose only job is to hand the prompt to the same session and focus
the webview.

- **Cost:** small, additive, and deletable.
- **What it buys:** discoverability for VS Code users who live in the Chat view.
- **What it does not buy:** anything on VSCodium.
- Take this only after A works. It is a garnish, not a design.

### Option C — a native `TreeView` for the conversation tree, webview for the transcript

VS Code's `TreeView` API is stable and has more than a file list in it:
`canSelectMany` for multi-selection, `TreeItemCheckboxState` for per-item
checkboxes with `manageCheckboxStateManually` to stop parent/child
auto-propagation, and `TreeDragAndDropController` for drag and drop.

Read against `docs/TREE-BROWSER-AS-EDITOR.md`, that maps almost exactly onto
τ's existing gestures. Marks become checkboxes. `ctrl+B` and `c`/`v` become
context-menu commands. The manual checkbox mode is what lets a mark stay on one
message without dragging its children in.

- **Cost:** lower than drawing a tree in a webview, because the host renders it.
- **What it loses:** the zone colours and the anchor rows are a Textual layout
  that a `TreeItem` label cannot reproduce. A `TreeItem` has a label, a
  description, an icon and a tooltip. That is less than the TUI shows today.
- **Worth pricing seriously.** It is the only option where τ's differentiating
  feature costs less in the extension than it did in the TUI.

### Option D — the proposed chat session API

Rejected for now, per §1.1. Revisit if and when `chatSessions` finalizes. The
watch item is its appearance on the stable contribution-points page, not the
proposal issue closing.

---

## 5. Prior art for the conversation tree

τ's tree browsing and editing is the unique part, and it is not unprecedented.
Four kinds of prior art, useful for different reasons.

1. **Loom** (`socketteer/loom`) — the closest ancestor. A multiversal tree
   writing interface for language models: `Ctrl+Space` generates *n* children of
   the cursor position, and the UI has a read mode (the linear path) alongside a
   tree view (the branching structure), with hotkey navigation and JSON trees on
   disk. τ's split between the transcript and the tree browser is the same
   split. A reimplementation exists as an Obsidian plugin (`loomsidian`), which
   is direct evidence the pattern ports into an editor host.
2. **LibreChat forking** — a shipped, documented product feature. Fork from any
   message, and *choose how much history and which branches to carry over*. That
   choice is `docs/TREE-BROWSER-AS-EDITOR.md` §6's plan, exposed as a dialog
   instead of derived from marks. Worth reading before deciding whether τ's plan
   should stay derived.
3. **LangChain's branching chat** — every edit or regeneration submits a new run
   from the selected message's parent checkpoint. Same algebra as τ's re-parent,
   different vocabulary ("checkpoint" for what τ calls a node).
4. **Vocabulary** — the same structure is published under at least four names:
   tree-of-thought chat, non-linear chat, conversation forking, AI chat canvas.
   τ should keep saying "conversation tree", but a person searching for prior art
   needs all four.

### 5.1 How to draw it, if not a `TreeView`

VS Code extensions that draw graphs in a webview converge on two libraries:
Cytoscape.js (viewX, `vscode-dgmlviewer`) and D3 (`code-review-graph`). Both are
MIT. Neither is needed for Option C.

---

## 6. What a VS Code head would need from τ that τ does not have

This is the real cost column, and none of it is extension work.

1. **An RPC client is still missing, and no longer matters.**
   `HEADS-AND-MULTIPLEXER.md` §1 records this gap and correctly notes a
   non-Python head writes its own. A TypeScript client is extension work.
2. **The reverse channel is now the blocking item, earlier than §5.3 predicted.**
   Cline needed `HostProvider.window` for a head, not for a multiplexer. τ has 27
   verbs, all host→core. An extension that wants to open a file, show a diff, or
   ask a yes/no question has no verb. `docs/REMOTE-CONTROL.md` §7.1's v1 policy
   ("fail fast with the declared default, never hang") is survivable, but it
   means the head cannot host a permission prompt, which is the single most
   visible feature of every extension in §2.
3. **Lane verbs.** `HEADS-AND-MULTIPLEXER.md` §8 lists `open_lane`,
   `list_lanes`, `close_lane` as Tier C and absent. A webview showing several
   lanes side by side wants them. A single-pane head does not.
4. **The tree is not on the wire at all.** Measured in this checkout by
   importing `COMMAND_TABLE`: it holds 27 entries, of which **20 have a handler
   and 7 are formally declined** (`bash`, `cycle_model`, `cycle_thinking_level`,
   `export_html`, `send_tool_result`, `set_follow_up_mode`, `set_steering_mode`).
   None of the 20 reads or writes tree structure.
   - `get_messages` returns, in its own schema text, "the terminal, flat message
     array". A head cannot draw a tree from it.
   - `fork` is session-level: it copies the current session's *active path* into
     a new session. It is not `commit_branch`.
   - `commit_branch` and `paste_subtree` live on `TauBackend`
     (`backends.py:1724`, `backends.py:1815`) — in `tau-coding-agent`, the head.
     The pure algebra underneath them (`tau_agent_core/tree_surgery.py`) is in
     the core, but nothing exposes it over RPC.

   This is the largest single finding in this document. τ's differentiating
   feature is currently reachable only from inside the Textual head. A VS Code
   head needs a tree read verb and branch/paste write verbs before Option C is
   possible at all, and before Option A can show anything the terminal cannot.
5. **File-change records. τ computes them and throws them away.** This is §6.1
   below, and it is the second blocking item.
6. **A transcript window policy for the webview.** `docs/TRANSCRIPT-WINDOW.md`
   solved a widget-count problem specific to Textual's compositor. A webview has
   its own version of that problem and none of that solution transfers. Note
   also that a hidden webview is destroyed unless `retainContextWhenHidden` is
   set, which the vendor docs call expensive; the supported path is `getState` /
   `setState` plus a `WebviewPanelSerializer`. So the head must be able to
   rebuild a transcript from `get_messages`, which it can.

### 6.1 The editor integrations, and the data they need that τ discards

The integrations that justify the extension are: open the edited file in a tab,
jump to the lines of the edit, and toggle a diff for a turn or a tool call. Each
needs a fact about a file change. Here is what τ produces today, measured in this
checkout.

| Integration | Needs | τ today |
|---|---|---|
| Open the edited file | `path` | `edit`/`write`/`read` compute `details["path"]` — **discarded** |
| Jump to the edited lines | `path` + line range | **nothing computes a line number** |
| Diff for one tool call | before/after, or a patch with line numbers | `edit` computes a diff — **discarded**. `write` records no before-content at all |
| Diff for a whole turn | the above, grouped by turn, coalesced per file | turn grouping exists in the tree; the per-file data does not |

**The discard is real and it is silent.** All eight built-in tools set
`result_dict["details"]` (`edit.py:188`, `write.py:148`, `read.py:199`,
`bash.py:310`, `find.py:137`, `grep.py:136`, `ls.py:123`). **Nothing reads it.**
The chain, verified by reading each hop:

1. `AgentLoop._execute_single_tool` (`agent_loop.py:1611`) takes `content`,
   `is_error` and `terminate` off the returned dict and constructs an
   `AgentToolResult`. `details` is not read. It is dropped here.
2. `AgentToolResult` has no `details` field. `agent_loop.py:1659` states this
   deliberately: "a genuine model divergence from pi, not a swallowed value".
3. The `tool_result` hook event hardcodes `"details": None`
   (`agent_loop.py:1684`), so an extension cannot recover it either.
4. `tool_execution_end` carries `result=result.content` (`agent_loop.py:1323`).
5. `ToolResultMessage` (`agent_loop.py:1468`) is built from `content`,
   `is_error`, `tool_name`, `tool_call_id`. The session log therefore stores the
   string `"Replaced 1 occurrence(s) in <path>"` and nothing else.

The TUI does not compensate: grepping `tau-coding-agent/src` for `diff` finds
only the streaming suffix-diff and prose. **No head has ever rendered a diff,
because no head has ever been given one.**

So `EditTool` runs `_generate_diff` on every call and the result is immediately
unreachable. Under "Fail Early" that is work done and discarded without a word.

### 6.2 The diff τ computes would not drive a diff view anyway

`EditTool._generate_diff` (`edit.py:224`) has two defects, and they matter
because a fix must not just re-plumb it.

1. **It is not a unified diff**, despite the docstring saying so. It emits no
   hunk headers, no line numbers and no elision — every unchanged line of the
   file, prefixed with two spaces. A one-line edit to a 2000-line file yields
   2001 lines. There is nothing in the output to jump *to*.
2. **An insertion desynchronizes it.** On a mismatch the walk emits one `-` and
   one `+` and advances **both** indices (`edit.py:245-248`). It has no
   longest-common-subsequence step. Since `edit` performs
   `old_string → new_string`, any edit that changes the line count makes every
   subsequent line mismatch, and the rest of the file renders as `-`/`+` pairs.
   A correct-looking diff is produced only when the replacement preserves line
   count.

`write` is worse for this purpose and in a way plumbing cannot fix:
`details` is `{path, lines, bytes}` (`write.py:148`), with no before-content. A
`write` over an existing file is not reconstructable after the fact from
anything τ keeps.

### 6.3 What to decide before the shared render code exists

The user's point stands: these are not features to bolt on after a web app
works. Three decisions, in the order they constrain each other.

1. **Persist the change facts, not a rendered diff.** The core owes structured
   data — path, ranges, and enough content to reconstruct before/after. It does
   not owe a string with `+` and `-` in it. `export_html` was declined Tier D on
   exactly this ground ("a host can render"), and a persisted diff *string* would
   be that same mistake written to disk. The head renders; that is §2's rule.

2. **This is a session-log schema change, which makes it the expensive one and
   the one that must go first.** The feature the user described is clicking turn
   6 of *yesterday's* session, so live events are not sufficient — the record has
   to be in the log. That reaches `docs/library/reference/sessions.md`, the
   `SessionLog` contract suite in `tau_agent_core.testing`, and every store
   implementor. It also interacts with compaction, which already tracks
   `read_files` / `modified_files` in `CompactionDetails` (`compaction.py:930`)
   — that is a second, independent notion of "files this session touched", and
   the two should be reconciled rather than left to disagree.

3. **Design the host capabilities as intents, not as VS Code calls.** The
   reverse channel (§6 item 2) should carry `reveal(path, range)` and
   `show_diff(change_id)`, not `vscode.diff`. Then the VS Code head delegates to
   `showTextDocument` and the diff editor; the browser head renders inline; the
   TUI renders what it can. Per "Fail Early" this needs capability negotiation
   with a visible answer — a head that cannot honour an intent says so, rather
   than silently doing nothing.

Ordering against §8's recommendation: the tree verbs and the change records are
both core work, both on the wire, and both testable with no extension in
existence. They are one design pass, not two.

Facts from the vendor webview guide, recorded so they are not rediscovered.

- Message passing is `webview.postMessage()` outward and `acquireVsCodeApi()`
  then `vscode.postMessage()` inward. `acquireVsCodeApi()` may be called once.
- A Content Security Policy is required. Start from `default-src 'none'` and use
  `${webview.cspSource}`. Inline scripts and styles must move to files.
- `localResourceRoots` restricts filesystem access. Convert paths with
  `webview.asWebviewUri()`.
- The vendor guide warns that webviews are resource heavy and advises using
  native APIs where they suffice. That warning is an argument for Option C.
- `@vscode/webview-ui-toolkit` is **deprecated** — archived read-only 2025-01-06
  because its FAST dependency was discontinued. The community replacements are
  `vscode-elements/elements` and `estruyf/vscode-community-ui-toolkit` (Lit).
  Do not start on the Microsoft toolkit.

---

## 8. Recommendation

Build Option A with Option C's tree. That is: a webview head in the sidebar for
the transcript and the editor, a native `TreeView` for the conversation tree,
both driving one `tau --mode rpc` child process. Defer Option B until A ships and
skip Option D until `chatSessions` finalizes.

Three prerequisites are τ's work, not the extension's. The first two block; the
third does not.

1. **File-change records** (§6.1–6.3). τ computes `details` in all eight built-in
   tools and discards every one of them, so no head has ever been handed a path,
   a line range or a diff. This blocks the editor integrations that are the
   reason to be in an editor. It is a session-log schema change, so it is also
   the most expensive item here and the one whose shape constrains the others.
2. **Tree verbs on the wire** (§6 item 4). Measured: none of the 20 live verbs
   reads or writes tree structure. Without them the head cannot show τ's
   distinguishing feature.
3. **The reverse channel** (§6 item 2), designed as intents per §6.3 item 3.
   Needed before the head can host a permission prompt or honour a
   `reveal`/`show_diff`. `docs/REMOTE-CONTROL.md` §7.1's declared-default policy
   is survivable for a first version.

The order that follows: items 1 and 2 are one core design pass, testable with no
extension in existence, and both land on the wire. The intent vocabulary (item 3)
is designed alongside them because it decides what the records must contain. The
extension is last, and by then it is mostly rendering.

---

## 9. Not verified, and not investigated

- I did not build or run an extension. Every API claim is from documentation.
- I did not read Cline's or Continue's source. Their architecture claims come
  from their generated architecture docs, which may lag their code.
- I did not check whether a chat participant works in VSCodium when *no* Copilot
  Chat is installed. §3 assumes it does not, from the reported version-pinning
  problem, which is weaker evidence than a test.
- I did not price the extension's own build and publish pipeline, or the
  double-publish to Marketplace and Open VSX.
- I did not investigate JetBrains, which Cline and Continue both support and
  which would be a third head.

---

## Sources

- [AI extensibility in VS Code](https://code.visualstudio.com/api/extension-guides/ai/ai-extensibility-overview)
- [Chat Participant API](https://code.visualstudio.com/api/extension-guides/ai/chat)
- [Contribution Points](https://code.visualstudio.com/api/references/contribution-points)
- [Webview API guide](https://code.visualstudio.com/api/extension-guides/webview)
- [Using Proposed API](https://code.visualstudio.com/api/advanced-topics/using-proposed-api)
- [Chat Session API proposal (microsoft/vscode#268063)](https://github.com/microsoft/vscode/issues/268063)
- [vscode.proposed.chatSessionsProvider.d.ts](https://github.com/microsoft/vscode-copilot-chat/blob/main/src/extension/vscode.proposed.chatSessionsProvider.d.ts)
- [Sunsetting the Webview UI Toolkit (issue #561)](https://github.com/microsoft/vscode-webview-ui-toolkit/issues/561)
- [vscode-elements/elements](https://github.com/vscode-elements/elements)
- [Tree view drag-and-drop sample](https://github.com/microsoft/vscode-extension-samples/blob/main/tree-view-sample/src/testViewDragAndDrop.ts)
- [Allow TreeItems to have optional checkboxes (issue #116141)](https://github.com/microsoft/vscode/issues/116141)
- [Cline architecture overview](https://deepwiki.com/cline/cline/1.3-architecture-overview)
- [Cline host provider abstraction](https://deepwiki.com/cline/cline/2.2-controller)
- [Cline gRPC communication system](https://deepwiki.com/cline/cline/6.1-grpc-communication-system)
- [Continue system architecture](https://deepwiki.com/continuedev/continue/2.1-system-architecture)
- [Continue IDE integration patterns](https://deepwiki.com/continuedev/continue/2.4-communication-flow)
- [Use Claude Code in VS Code](https://code.claude.com/docs/en/vs-code)
- [Add Side Bar (Webview View) Support for Claude Code (issue #15037)](https://github.com/anthropics/claude-code/issues/15037)
- [socketteer/loom](https://github.com/socketteer/loom/blob/main/README.md)
- [socketteer/loomsidian](https://github.com/socketteer/loomsidian)
- [LibreChat: Forking Chats](https://www.librechat.ai/docs/features/fork)
- [LangChain: Branching chat](https://docs.langchain.com/oss/python/langchain/frontend/branching-chat)
- [VSCodium Copilot compatibility discussion #1487](https://github.com/VSCodium/vscodium/discussions/1487)
- [viewX-vscode (Cytoscape.js in a webview)](https://github.com/textX/viewX-vscode)
- [Supporting Remote Development and GitHub Codespaces](https://code.visualstudio.com/api/advanced-topics/remote-extensions)
- [Extension Host](https://code.visualstudio.com/api/advanced-topics/extension-host)
- [Clipboard API reference (readText/writeText only)](https://vscode-api.js.org/interfaces/vscode.Clipboard.html)
- [Clipboard access through Extensions API (issue #4972)](https://github.com/microsoft/vscode/issues/4972)
- [Claude Code: clipboard image paste in a devcontainer (issue #51244)](https://github.com/anthropics/claude-code/issues/51244)
- [How to paste files (web.dev clipboard pattern)](https://web.dev/patterns/clipboard/paste-files)
