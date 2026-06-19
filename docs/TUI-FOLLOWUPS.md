# TUI follow-ups (deferred, for work soon)

Issues surfaced while fixing the chat-rendering bugs (commit `a0e6f3c`, the
unified `MessageBox` + arrival-order streaming). They were **deliberately not
patched** in that change — per the repo's Fail-Early rule, papering over them
with fallbacks would hide the real defect. Captured here so they aren't lost.

Status: item 1 FIXED (2026-06-19); items 2–3 open. Owner decision (2026-06-19):
document now, fix soon.

---

## 1. Saved-chat reload renders list-content as a string — FIXED ✅

**Was:** clicking a sidebar session busy-looped and froze the TUI.

**Root cause (confirmed):** message persistence is asymmetric. The *user* message
is stored with a string body, but *assistant* and *toolResult* messages are
appended straight from the agent loop, where `content` is a **list of block
dicts** (`[{"type": "text", ...}, {"type": "toolCall", ...}]`). The reload path
(`on_chat_selected`) handed that list to the **str-only** `MessageBox`, whose
`_format` does `content.replace(...)` → `AttributeError: 'list' object has no
attribute 'replace'` *inside* `compose()`. Fired for every message during the
mount/layout cycle, that manifested as the freeze (reproduced: a Pilot
`add_message("assistant", [block])` raises in compose).

**Fix (commit on master, 2026-06-19):**
- Persistence (`Chat` + `TAU_DIR`) extracted to a Textual-free
  `tau_coding_agent/session_store.py` (so `tau -p` can save sessions without
  importing the TUI). `app.py` re-exports both for back-compat.
- New `ChatDisplay.add_persisted_message(msg)` normalizes `str | list[dict]`
  content and emits one `MessageBox` per block kind (text → assistant box;
  `toolCall` → tool-call box; `toolResult` role → tool-result box), preserving
  arrival order — so a *reloaded* turn looks identical to a freshly *streamed*
  one. It raises `TypeError` on an unrenderable shape (Fail-Early), never
  `str()`s a list.
- Tool-call / tool-result Markdown formatting is now shared between the live and
  reload paths via `format_tool_call_body` / `format_tool_result_body`, so they
  can't drift.
- `on_chat_selected` and `action_export_chat` go through the normalizer.
- Verified against all 28 real saved chats: 27 render cleanly (incl. tool-heavy
  ones with the old corrupted `{"raw": ...}` args). New regression tests in
  `tests/test_chat_rendering.py` (reload section).

**Known remaining edge case (separate cause, not the systemic bug):** a single
*degenerate* chat — `1781803484.json`, an 827 KB runaway assistant **string**
(not a list) — still times out, because parsing that much Markdown at once is
genuinely slow. It is plain-string content, so the normalizer renders it fine in
principle; the cost is the Markdown size, not the shape. That file (and the rest
of the pre-fix test garbage) was deleted from `~/.tau/chats/` with the owner's
go-ahead (one-session backup at `/tmp/tau-chats-backup-pre-reset.tar.gz`). If
pathologically large assistant messages recur in normal use, consider a
display-only large-content strategy (lazy/plain-Static render) — but do **not**
silently truncate assistant prose (Fail-Early).

---

## 2. Dead stray package `tau_coding_agent/widgets/chat_display.py` — CLEANUP

**Where:** top-level `tau_coding_agent/widgets/chat_display.py` (46 lines,
git-tracked) — distinct from the real `src/`-layout package
`tau-coding-agent/src/tau_coding_agent/`.

It defines only an unused `ChatMessageData` dataclass and is imported by nothing
live (it predates the `MessageBox` refactor and is not the TUI's widget). It is
the same "stray duplicate package dir" called out as finding #9 in
`docs/CODE-QUALITY-NOTES.md`, and the two import roots sharing the name
`tau_coding_agent` invite confusion.

**Fix:** confirm no importer (`grep -rn "widgets.chat_display\|ChatMessageData"`),
then `git rm` the top-level `tau_coding_agent/` tree.

---

## 3. Message label placement — DESIGN NOTE (low priority)

The unified `MessageBox` (`app.py:125`) shows the role label via Textual's
`border_title` (the titled-border idiom), uniform across user/assistant/toolCall/
toolResult/error. This deliberately replaced the old "label overlapping the box
border" and "prepend a first line of text" treatments.

If a distinct first-line header *inside* each box is preferred over a
border title, it is a localized change in `MessageBox.compose` (`app.py:~145`)
plus the `box-<role>` rules in `parley.tcss`. No behavioral impact; purely visual.
