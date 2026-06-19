# TUI follow-ups (deferred, for work soon)

Issues surfaced while fixing the chat-rendering bugs (commit `a0e6f3c`, the
unified `MessageBox` + arrival-order streaming). They were **deliberately not
patched** in that change — per the repo's Fail-Early rule, papering over them
with fallbacks would hide the real defect. Captured here so they aren't lost.

Status: open. Owner decision (2026-06-19): document now, fix soon.

---

## 1. Saved-chat reload renders list-content as a string — BUG

**Where:** `tau-coding-agent/src/tau_coding_agent/app.py`
- `on_chat_selected` (`app.py:845`) reloads a persisted chat and renders each
  message via `display.add_message(msg["role"], msg["content"])` (`app.py:865-867`).
- `ChatDisplay.add_message(self, role, content: str, ...)` (`app.py:321`) and the
  underlying `MessageBox` (`app.py:125`) expect `content` to be a **`str`**.
- The system-prompt/history rebuild at `app.py:816-818` (`content = msg["content"]`)
  makes the same assumption.

**Root cause:** message persistence is asymmetric. The *user* message is stored
with a string body (`{"role": "user", "content": message}`, `app.py:626`), but
*assistant* and *toolResult* messages are appended straight from the agent loop's
result (`app.py:687`), where `content` is a **list of block dicts**
(`[{"type": "text", "text": ...}, {"type": "toolCall", ...}]`) — the τ message
shape from `SUBPHASE-0.0.md`. On reload, the list is handed to a `Static`/`Markdown`
that wants a string, so saved assistant turns render wrong (or raise).

**Why not patched yet:** the correct fix is to render persisted list-content
through the *same* block-formatting the live streaming path uses (extract text
blocks; render toolCall/toolResult as their own `MessageBox`es, exactly like the
live `tool_call`/`tool_result` events do), not to `str()` the list at the call
site. That is a real rendering path, not a one-liner, and it belongs with a pass
over the persistence format.

**Fix sketch:**
- Give `add_message` (or a new `add_persisted_message`) a content-normalizer that
  accepts `str | list[dict]` and, for a list, emits one `MessageBox` per block
  kind (text → assistant box; toolCall/toolResult → their boxes) — reusing the
  `_on_text_delta` / `_on_tool_call` / `_on_tool_result` rendering rules.
- Alternatively, normalize at persistence time so saved `content` is always the
  same shape the renderer expects. Decide which side owns the contract.
- mypy already flags the related `Chat | None` unions here (`app.py:626`, `:652`).

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
