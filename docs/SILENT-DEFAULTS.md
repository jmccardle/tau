# Constants presented as measurements

Built 2026-10-17 for four of the five values below; `context_window`'s 128000
(§5) is judged the same fault and is **not changed** — it needs a decision first.

Forgejo #8 and #9. Each value here was a constant that a reader could not tell
from a fact: a model id nobody chose, or a field nothing writes.

## 1. `build_model_from_config` sent `gpt-4` for an entry with no `model`

`config.get("model", "gpt-4")`, in `build_model_from_config` and again in
`TauBackend.__init__`. A config entry that forgot `model` went to the server as
`gpt-4`. A local llama.cpp server ignores the field, so the mistake never shows
there; any other server serves a different model or returns a 404 naming a model
the user never typed.

**Loud error.** No id is the obvious default for every endpoint, so this is a
missing fact rather than an ambivalent user. A missing, empty or non-string
`model` raises `ValueError` naming the entry (`models.<config_name> has no
'model'`), the same shape as the `api` and `base_url` refusals beside it.
`TauBackend` now takes `model_name` from the built `Model` instead of reading
the key a second time.

## 2. `ExtensionContext.is_idle` was always `True`

The constructor took `is_idle=True`, and the one construction in `src`
(`AgentSession._extension_api`) passed only `cwd`. A hook running mid-turn read
`True`.

**Wired up.** With a session bound, `is_idle` is `not any(cursor.is_streaming
for cursor in session.cursors)`: false inside a turn on any cursor, including a
sub-agent's. "Idle" means the whole session, not the acting cursor, because the
question an extension asks with it is "may I start something without racing a
turn". Unbound, it raises `RuntimeError` like `session_id` does. The constructor
argument is gone, so nothing can set it to a constant again.

## 3. `ExtensionContext.session_manager` was always `None`

Same constructor, same one call site: in a session it was `None`. It was typed
`Any` and pointed at the port-era `SessionManager`; a session holds a
`SessionLog` and a set of cursors instead, and `ctx.entries()`, `ctx.session_id`
and `ctx.cursor` already cover what an extension reads from them.

**Removed**, with the `shutdown()` pass-through that only fired when a test
supplied a manager. `shutdown()` now only sets `shutdown_requested`, which is
what `AgentSession.shutdown_requested` and every head already read.

## 4. `SessionInfo.tool_count` was always `0`

`tau_agent_core.session.SessionInfo.tool_count` had zero writers in `src`. The
picker and the RPC `list_sessions` row are built from
`tau_agent_core.session_catalog.SessionInfo`, a different class without the
field.

**Removed.** Counting tool calls means scanning every entry of every listed
session; nothing displays the number, so it is not worth paying for. The model
keeps `extra="allow"`, so an old caller passing `tool_count=` is stored as an
extra rather than refused.

## 5. `context_window` defaults to 128000 — same fault, not fixed

`build_model_from_config` does `config.get("context_window", 128000)`, and
`sdk.resolve_model` gives every non-built-in id the same number. It is the same
shape as §1: a number presented as the model's window. Auto-compaction is on by
default and both of its thresholds come from it (`compaction_limits`), the
summarizer-window check in `CompactionPolicy.bind_to` compares two invented
128000s, `ctx.get_context_usage()` reports a percentage of it, and the RPC
`get_models` notes already say a host must not trust it. `tau_llm.catalog`'s
docstring calls it "the exact class of invented number this module exists to
replace". Measured on one real config: 9 of 13 `models` entries set no
`context_window`.

It is not changed here because each fix has a cost someone should choose:

- **Required key, loud error.** Breaks those 9 entries at startup, and an
  ad-hoc `--model provider/id` has nowhere to get a window from at all, so it
  stops working unless the CLI gains a flag or a catalog lookup.
- **`Model.context_window: int | None`, unknown stays unknown.** No silent
  number, nothing breaks, but compaction must then refuse or stay off without a
  window and every reader of the field (97 lines across `src`) handles `None`.
- **Keep 128000 as a documented default.** Rejected by the reasoning above:
  compaction acting on it is not a user default, it is a guess the user never
  sees.

## 6. What is absent

- `context_window` (§5).
- `sdk.resolve_model`'s other defaults: it is the SDK's quick-start path and
  only its window has the §5 problem.
- `tau_agent_core.session.SessionInfo` has no producer in `src` at all; it is
  exported and documented, so retiring it is a separate change.
- `Backend.model` reads `config.get("model", "")`; `TauBackend` always reaches
  §1's refusal first, and `RemoteBackend` reads the model from the server.
