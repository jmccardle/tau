# Spec: heads and the multiplexer — τ is headless, the TUI is one head

**Status:** position and cost record, written 2026-08-30. **Nothing in §4, §5 or
§6 is built.** §1 through §3 are measurements of the tree at `dfc77f0`; every
file:line below was read, not remembered. This document exists so that the work
already in flight (image paste, `docs/TECTUM-NO-TOOLS-MIGRATION.md`) is done in
a shape a second head does not have to undo, and so the multiplexer's price is
recorded before anyone pays part of it by accident.

**Relationship to existing docs.** `docs/REMOTE-CONTROL.md` is the design of
record for the RPC surface and is not restated here; this document depends on
its §7.1 (reverse channel), §7.2 (one writing process) and §7.3 (transports
beyond stdio), and says what a *second head* adds to each. `docs/NODE-
ADDRESSABLE-AGENTS.md` owns decision 6, which §5.1 argues the multiplexer does
not violate. `docs/SUBMISSION-LIFECYCLE.md` owns the one door every head submits
through. `docs/TUI-STEERING.md` owns the pending-input buffer that §6.1 extends.

**Provenance.** τ facts are from this checkout at `dfc77f0`. pi facts are read
from `~/Development/pi` at `5cd93f688`. Claims about third-party software that
this session did not run are marked as unverified where they appear.

---

## 1. The claim, and where the code already supports it

The claim: **τ is a headless agent. The TUI is one head, not the product.**

This is already true in the code, in five places.

1. **`Backend` is an ABC with one implementation.** `backends.py:1033` declares
   `chat` / `stream_chat` / `stream_submission`; `TauBackend` (`backends.py:1207`)
   is the only subclass. The seam for a second head exists and is unused.
2. **`Submission` is the one door.** Every input source — interactive, headless,
   RPC, extension, bus, timer — constructs the same record and stamps
   `source`/`submitter` (`submission.py`, `docs/SUBMISSION-LIFECYCLE.md`). A new
   head adds a `source`, not a code path.
3. **The agent runs with no TUI.** `tau --mode rpc` (`cli.py:726`) serves
   JSON-RPC 2.0 over stdio. `COMMAND_TABLE` holds **27 verbs** as of `dfc77f0`
   (counted by import, not by reading the doc), with 7 more formally declined and
   each declination carrying its reason (`rpc/commands.py:3642-3727`).
4. **Rendering is already fan-out shaped.** `RenderRouter` (`backends.py:424`)
   routes events into lanes keyed by `submission_id`, and badges a lane with the
   source and submitter that opened it. It was built so a TUI could show a bus
   submission it did not make. That is the same problem a second head has.
5. **The runtime is extracted from the TUI.** `AgentSessionRuntime`
   (`agent_session_runtime.py`) exists because `docs/REMOTE-CONTROL.md` decision
   4 refused to put it on `AgentSession`. A daemon drives that, not the app.

**The one gap.** τ has an RPC *server* and no RPC *client*. pi has one:
`packages/coding-agent/src/modes/rpc/rpc-client.ts`, 601 lines at `5cd93f688`.
Every head that is not written in Python needs its own client anyway (that is
goal G2 — "the host need not be Python"), so the missing piece matters only for
a head written in Python that wants to drive a τ in another process.

---

## 2. What a head owns, and what it may not own

The rule this document proposes:

> A head owns capture, display, and at most one submission source. A head owns
> nothing that the conversation tree owns.

| Concern | Owner | Why |
|---|---|---|
| Keyboard, mouse, clipboard | head | It is the hardware in front of the human. Under a multiplexer the human is not on the machine running the agent. |
| Layout, theme, what a tool call looks like | head | `export_html` was declined for exactly this reason (`commands.py`, Tier D: "a host can render"). |
| Draft text, pending steering buffer, attachments | head | None of it has reached the model. `Parley._pending_steer` (`app.py:5352`) is the app's buffer, not the core's, and `docs/TUI-STEERING.md` §2 records why. |
| `Submission` construction (source, submitter, strategy) | head | Phase 3 of the submission lifecycle. The head declares who it is. |
| Admission, hooks, command dispatch | core | `AgentSession.submit` is the door. A head that dispatched its own `/compact` would be a second door. |
| The tree, the cursor, persistence | core | Decision 6. A head never writes the log. |
| Tool execution | core | `send_tool_result` was declined because accepting a tool result over the wire is a second privileged path into the same executor. |

The consequence that matters for work in flight: **the clipboard belongs to the
head**. It is the clipboard of the machine where the human sits. A clipboard
reader in `tau-agent-core` would be a core module that only ever works when the
core happens to be co-located with a human, which is exactly the assumption this
document exists to remove.

---

## 3. The TUI limits image display, not image capture

Two halves, and only one of them is a Textual concern.

**Capture is not a TUI concern.** Reading an image off the OS clipboard is
`subprocess` calls: `wl-paste --list-types` then `wl-paste --type` on Wayland,
`xclip -t TARGETS -o` on X11, `osascript`/`pbpaste` on macOS, `powershell.exe`
plus `wslpath` on WSL. This is pi's `utils/clipboard-image.ts` (300 lines at
`5cd93f688`) ported to Python. Textual appears in that path only as the source of
one key event. Measured on the development machine 2026-08-30: `/usr/bin/wl-paste`
and `/usr/bin/xclip` both present, `XDG_SESSION_TYPE=x11`.

**Display is the real limit.** A terminal cannot draw the pasted image inline
unless it speaks the kitty graphics protocol, iTerm2 inline images, or sixel. I
did not test any of those against Textual 8.2.7, and τ depends on no library that
provides them. So the TUI's attachment row states the facts it can state —
`image/png · 1.2 MB · 1920×1080 · [X]` — and the human confirms the image by
having just copied it. A graphical head shows a thumbnail. That is a real
advantage of a graphical head and the only one this document found.

**Not a limit:** `ctrl+v` reaching the app. Textual 8.2.7's `TextArea.BINDINGS`
already maps `ctrl+v` to its own `paste` action, which is direct evidence that the
key arrives. `ctrl+shift+v` is the terminal's paste and never arrives, so it
cannot serve as an in-app escape hatch.

---

## 4. Candidate heads

Ranked by cost to τ, cheapest first. None of these is proposed for the roadmap
here; this section exists so the choice is made against measured numbers.

### 4.1 A VS Code extension driving `tau --mode rpc` — the recommendation

The RPC surface was designed for this. Goal G2 is "the host need not be Python",
the dialect is JSON-RPC 2.0, and the protocol reference is machine-generated
(`scripts/generate_rpc_protocol_doc.py` → `docs/RPC-PROTOCOL.md`, drift-tested).

- **Cost to τ: none.** The extension spawns τ as a child process, which is
  precisely the shipped model — one process, one session, stdio.
- **Cost to the extension:** its own client (pi's is 601 lines), a webview, and
  the render logic. TypeScript, not Python.
- **What it buys:** images render natively; the editor's own clipboard API
  removes the whole platform matrix of §3; and it is the only option that gets a
  graphical head without τ growing a GUI dependency.
- **Unverified:** I did not survey the VS Code webview API for image paste
  specifics. The claim here is only that a webview can display an image.

### 4.2 `textual-serve` / `textual-web`

Textualize ships tooling to serve a Textual app over the browser. Cost to τ is
close to zero — it is the same app.

- **What it does not solve:** the browser renders a character grid, so §3's
  display limit is unchanged, and a browser image paste would have to be
  forwarded through the web driver as something other than a key event.
- **Unverified:** I did not run either package against τ, and I did not check
  whether either forwards a clipboard image. Treat this whole entry as
  unmeasured. What it is good for is remote *access*, not images.

### 4.3 A native GUI as a second `Backend` consumer

PySide6 or GTK4, in-process, no Electron and no web stack. Real image display,
real file dialogs.

- **Cost:** an entire second front end. `app.py` alone is 8,342 lines at
  `dfc77f0`. A GUI would not need all of that, but the honest estimate is
  thousands of lines, plus a GUI toolkit in the dependency tree of a package
  whose current optional extras are Pillow, a bus client and a store.
- **What it buys over 4.1:** it can be the *same process*, so there is no wire
  and no serialization. That advantage disappears the moment the multiplexer
  exists, which is an argument for deciding §5 first.

### 4.4 Tauri, or any webview app over RPC

Same class as 4.1 — a non-Python host on the shipped wire — with the added work
of shipping, signing and updating an application. Chosen only if the head must
stand alone rather than live inside an editor.

---

## 5. The multiplexer

The shape asked for: **a daemon holds one or more live sessions; heads attach and
detach; a session with no head attached keeps running.** Today the agent's
lifetime is the client's lifetime, because stdin EOF is what ends it
(`rpc/handler.py`, "the peer is gone").

### 5.1 Decision 6 survives, and this is the load-bearing argument

`docs/NODE-ADDRESSABLE-AGENTS.md` decision 6: *a conversation has exactly one
writing process.* The hazard it names is not id collision but `_leaf_id` being
process-local memory that nothing re-reads, so two writers parent off the same
node and a line silently becomes a fork.

A multiplexer daemon does not create a second writer. **The daemon is the single
writing process.** Attached heads submit through `AgentSession.submit` — the same
door the TUI uses — and never touch the log. N heads on one session is N
submission *sources*, which the core already models (`RenderRouter` badges them)
and `docs/REMOTE-CONTROL.md` F1 already recommends as the answer to "several
agents on one tree".

So the multiplexer is a transport and fan-out change. It is not a concurrency
change to the tree, and it must not become one.

### 5.2 The four costs

1. **A socket transport.** `docs/REMOTE-CONTROL.md` §7.3 X1 says block [1] is the
   only code that knows about stdio, and that is honored — `rpc/transport.py` is
   the only file naming `sys.stdin`/`sys.stdout`. But the stream construction
   inside it is stdio-specific (`connect_read_pipe` on `sys.stdin.buffer` at
   `transport.py:277`, and a matching write-pipe path), so this is a real refactor
   of one 819-line file, not a parameter. X2 — "the peer is gone" as one named
   event with per-transport detection — is the second half.
2. **Per-client output.** `RPCHandler` owns exactly one `_output_queue`
   (`handler.py:249`) with one credit counter. N heads means N queues, each with
   its own backpressure. **This carries a policy question that must be answered,
   not defaulted:** goal G4 promises the loop stalls when the host cannot keep up.
   With several heads attached, does one stalled reader stall the agent for
   everyone, or does that head get dropped? Both are defensible. Silently
   choosing one is not.
3. **Session identity on every message.** F2 already reserved the
   `{store, session_id, lane, cursor}` tuple for this, so it is additive rather
   than a protocol break. This is the cheap item, and it is cheap because it was
   paid for in advance.
4. **Replay on attach.** A head that connects mid-turn needs the transcript.
   `get_messages` and `get_state` already exist and already return the cursor on
   every read (F3: no host may cache the tip). This is a client-side sequence
   — pull state, then subscribe — not a new core capability.

### 5.3 The reverse channel stops being a footnote

`docs/REMOTE-CONTROL.md` §7.1 defers the reverse channel (extension → host UI)
with three reservations, and v1 policy is "fail fast with the declared default,
never hang". That is comfortable while a human is always attached.

The moment a session can run **detached**, an extension calling `confirm("proceed?")`
has nobody to ask, and the v1 default becomes a behavior users notice rather than
a footnote. `Submission.allow_user_input` is already the per-submission
assertion that a human is reachable, and the TUI's own call site is the only
place that sets it honestly. A multiplexer must decide what that flag means for a
submission whose head has since detached. I recommend: `allow_user_input` is
re-evaluated at the moment of the ask, not at admission, and a detached session
takes the declared default. That is a change to the core, and it is the one item
in §5 that is not purely transport.

### 5.4 What does not need to change: images on the wire

`prompt.images` is already a declared RPC parameter (`rpc/commands.py:441`), and
`MAX_REQUEST_LINE_BYTES` is 8 MiB (`transport.py:84`). An image bounded to
2000×2000 by `resize_image` (`tools/image_resize.py`, pi's default and τ's)
base64-encodes to well under that. A head captures an image locally and sends it
as `images` on a `prompt` or `submit`. No protocol change, no new verb.

---

## 6. Consequences for work in flight

### 6.1 Image paste (not yet built)

The design that follows from §2:

- The clipboard reader lives in **`tau-coding-agent`**, head-local. Not in
  `tau-agent-core`. A VS Code head does its own capture through the editor's API
  and never imports it.
- The attachment buffer lives on `Parley`, beside `_pending_steer`
  (`app.py:5352`), for the same reason that buffer does: nothing that has not
  reached the model belongs to the core, and the removal gesture has to be able
  to take it back.
- Attachments reach the model only as `Submission.images` (`submission.py:214`),
  which the core already merges into the user turn for an ordinary submission and
  for a steered one (`agent_session.py:3513`, `_queued_content_to_user`).

Built that way, the paste feature is already multiplexer-shaped: the same
attachment buffer, pointed at an RPC client instead of an in-process backend,
sends the same `images` array over the wire described in §5.4.

Two rendering gaps this uncovered, both small and both real:

- `lane_start` carries only `text` (`backends.py:517`), so a user bubble cannot
  say what was attached without a new field.
- `ChatDisplay.add_persisted_message` (`app.py:3915`) walks content blocks
  handling `text` and `toolCall` only. An `image` block is **skipped silently**,
  so a resumed image-only user turn renders as an empty box. That is a Fail-Early
  violation the paste work would be introducing, and it is fixed in the same
  change or not introduced at all.

### 6.2 Tectum is the first real second head

`docs/TECTUM-NO-TOOLS-MIGRATION.md` describes a consumer that passes `--no-tools`
and registers its entire capability through extensions. Read through §2, that is
not an awkward configuration — it is a head that owns capture (voice) and display
(speech) and owns none of the tree. `docs/REMOTE-CONTROL.md` §7.1 already names
it as the reverse channel's natural first consumer. If the multiplexer is built,
Tectum is the design target, not a later port.

---

## 7. Decisions taken

1. **A head owns capture, display, and one submission source. Nothing else.** §2.
2. **The clipboard is head-local.** The reader goes in `tau-coding-agent`; the
   core never grows one. §2, §6.1.
3. **The TUI ships image paste anyway** — Windows, X11, Wayland, macOS — because
   capture is not a display problem and the attachment path is the same one a
   second head would use. §3, §6.1.
4. **A graphical head, if built, is an RPC host and not a Python import.** The
   wire is the product (G1); a second in-process front end would be a second
   `Backend` with none of the guarantees the process boundary provides. §4.
5. **The multiplexer is a transport and fan-out change, not a tree-concurrency
   change.** The daemon is the single writing process; decision 6 stands
   unmodified. §5.1.
6. **The multi-head backpressure policy is an open decision, not a default.** §5.2.
7. **Detached sessions force the reverse-channel question.** `allow_user_input`
   must be evaluated at the ask, not at admission. §5.3.

---

## 8. Deliberately absent

- **A recommendation to build the multiplexer.** This document prices it. The
  four costs in §5.2 are real and none of them is forced by the image work.
- **Optimistic concurrency / multi-writer support.** F4 says do not build it on
  speculation, and §5.1 removes the reason anyone would want to.
- **Lane verbs over RPC** (`open_lane`, `list_lanes`, `close_lane`). Named as
  Tier C in `docs/REMOTE-CONTROL.md` F1, absent from `COMMAND_TABLE` today,
  verified by import at `dfc77f0`. A multi-head UI would want them; a single head
  does not.
- **Terminal graphics protocols** (kitty, iTerm2, sixel). Unmeasured, and an
  attachment row that states mime type, size and dimensions is honest without
  them.
- **A τ-native Python RPC client.** Needed only by a Python head in another
  process, which nothing today is.

---

## 9. Open questions for the owner

1. Is a graphical head wanted at all, or is the answer "the TUI plus whatever a
   third party builds on the wire"? §4 is written for the second answer.
2. Multi-head backpressure: stall the agent for everyone, or drop the slow head?
   (§5.2 item 2.)
3. Does a detached session run at all, or does detaching pause it? "Keeps
   running" is what makes §5.3 urgent; "pauses" makes the reverse channel stay a
   footnote.

---

## 10. Test obligations, when any of §5 is built

Following the repo idiom that a contract is executable, and mirroring
`docs/REMOTE-CONTROL.md` §9:

- **H-T1** The socket transport passes the existing conformance suite unchanged
  (`test_rpc_conformance.py` drives real pipes). A transport that needs its own
  fork of the suite has changed the protocol.
- **H-T2** Two attached clients see the same event stream for one submission,
  and a submission from either is attributed to its own `submitter`.
- **H-T3** A client that stops reading is resolved by the §5.2 policy, and the
  test asserts which one — the observable difference between "agent stalls" and
  "head dropped" is the whole decision.
- **H-T4** Attach mid-turn: `get_state` + subscribe reconstructs a transcript
  equal to the one a client attached from the start would hold.
- **H-T5** An extension UI request under a detached session takes the declared
  default and is recorded, and never hangs (RC3).
