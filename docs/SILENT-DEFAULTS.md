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
  only its window has the §5 problem. Its `provider="openai"` default was
  removed in §7.
- `tau_agent_core.session.SessionInfo` has no producer in `src` at all; it is
  exported and documented, so retiring it is a separate change.
- `Backend.model` reads `config.get("model", "")`; `TauBackend` always reaches
  §1's refusal first, and `RemoteBackend` reads the model from the server.

## 7. No default provider (2026-10-19)

**What was wrong.** A bare `--model NAME` that was not a key in `config.json`
became an ad-hoc model with provider `openai`. Startup, `get_capabilities` and
`new_session` all succeeded. The first prompt then failed with "No API key for
provider 'openai'", or, with `OPENAI_API_KEY` set, went to OpenAI. The same
guess sat in three more places: a config entry with no `backend` was built as
`openai`, `sdk.resolve_model` gave any non-built-in id `provider="openai"`, and
a configured name with a `:level` suffix (`local-llm:high`) missed the config
lookup and became an ad-hoc OpenAI model called `local-llm`.

**The rule.** τ never picks a provider.
- `headless.resolve_model_config`, which the TUI, `-p`, rpc, repl and the serve
  daemon all call, takes a configured key (with or without `:level`), or
  `provider/id`, or a bare id with `--provider`. Anything else raises before a
  session exists. The message names the model, lists the configured keys and
  gives both ad-hoc forms. Each head prints it as one `tau: error:` line, exit
  2; the serve daemon answers `INVALID_PARAMS`. The same check covers an
  unconfigured `default_model`. A resumed session's stored model is §10.
- `build_model_from_config` refuses an entry with no `backend`, naming the
  entry and the registered providers. The packaged template sets one in every
  entry; a hand-written entry without one now fails at startup and says so.
- `sdk.resolve_model` and `create_agent_session` take `provider=None`. A
  built-in id (`gpt-4o`, `gpt-4`, `gpt-4-turbo`) still needs none; any other
  id without a provider raises. A built-in id with a different provider now
  gets that provider instead of being silently returned as OpenAI's.
- The TUI no longer falls back to the name `local-llm` when `default_model` is
  unset. It says to set `default_model` or pass `--model`.

**SDK migration (for the 0.14.0 release notes).** A call that named a model
outside the built-ins and relied on the old `provider="openai"` now raises.
Name the provider:

```python
create_agent_session(model="mistral-small", provider="openai", base_url="http://localhost:8080/v1")
resolve_model("mistral-small", provider="openai", base_url="http://localhost:8080/v1")
```

The built-ins that still need no provider are `gpt-4o`, `gpt-4` and
`gpt-4-turbo`. CLI users get the same rule: `--model openai/mistral-small`, or
`--provider openai --model mistral-small`, or a `config.json` entry.

**What stays, and why.**
- `create_agent_session()` with no arguments still means `gpt-4o`. That is a
  documented quick-start default for a caller who named nothing, not a guess
  about something they did name.
- An unregistered provider with a `base_url` speaks `openai-completions`
  (`build_model_from_config`, `resolve_model`). The URL is stated, and that wire
  is what generic endpoints serve. It is documented in both docstrings.
- `Settings.default_model = "gpt-4o"`: nothing in any package reads it
  (`sdk.create_agent_session`'s docstring says so).
- `AssistantMessageEventStream._make_empty_partial` labels an empty
  placeholder message `openai-completions`/`openai` when the model object
  lacks those attributes. It chooses no provider for any request.

## 8. A malformed model entry is fatal only when selected (2026-10-19, #65)

**What was wrong.** `build_model_from_config` raised a plain `ValueError` for
a value it rejects, such as the old `"prompt_cache": "anthropic"`. No head
caught that, so `--model <entry>` ended in a traceback. The message also said
`models.<name>` literally instead of naming the entry. And `tau serve -d`
never came up: every hello builds every entry for its model list, so each
hello failed and the parent never got an answer.

**Now.** The rejections raise `config.ModelConfigError`, which is a
`ConfigError` (so every head's handler prints it as one `tau: error:` line,
exit 2) and also a `ValueError` (so existing `except ValueError` callers keep
working). Messages name the entry: `models.litellm-haiku.prompt_cache is a
boolean; got "anthropic". The dialect moved to prompt_cache_dialect…`; the
offending value is shown as JSON, as written (`got null`, `got "x"`). A type
pydantic rejects becomes `models.<entry> is not valid: field: message`; an
entry that is not a JSON object is `models.<entry> must be a JSON object`.

**Only the entry a command will use is fatal** (John's decision on #65,
2026-10-19, option c). `headless.check_model_entries(config, selected)` runs at
startup. The selected entry is `--model`, else a resumed session's recorded
model, else `default_model`; `tau serve` selects `default_model`, which every
`new_session` without a model uses. If that entry does not build, the command
stops with one line, exit 2:

    tau: error: <config>: the selected model 'old' does not build: models.old.prompt_cache is a boolean; …. Fix models.old, or select a model that builds

Every other entry that does not build is reported, never dropped:
- One line per entry at startup, derived once by `check_model_entries`:
  `<config>: model 'old' is unavailable and can't be selected: <error>`. `-p`,
  rpc and repl print it on stderr as `tau: warning: …`. The serve daemon logs
  it as `warning: …` after its listening line (with `-d`, in `serve.log`). The
  TUI shows it as a warning notification titled "Config" on mount, because
  stderr is under the screen.
- `get_models`, the serve hello and attach list it with `model: null` and its
  `error`. The picker and completion leave it out, and the TUI and REPL name it
  when the picker opens (docs/MODEL-PICKER.md §2).
- Switching to it mid-run (`/model`, RPC `set_model`, `ctx.set_model`) is
  refused before any request with its error, as `ModelUnavailableError` (§9).

The reason for the split: a bad entry nobody selected harms nothing until
someone selects it, and then it is refused with its own error, so stopping
startup for it blocked a working model over an unrelated typo. The earlier
argument for fatal-on-any (a bad entry breaks `get_models`, the hello and the
picker) no longer holds, since those build per entry and carry the error. The
entry a command is about to use is different: running on it cannot work, so it
fails before a session exists.

A resumed session whose recorded model does not build is §10, not this rule:
the REPL, the TUI's resume and the serve daemon open it and refuse only a turn;
`tau -p -c` refuses in its own one line. Such an entry gets no startup warning
in those heads, so it is reported once.

An entry's own `compaction` block is part of the entry. A malformed one, or one
whose `summarizer` names no configured model, is that entry's error
(`'models.<entry>.compaction.reserve_tokens' must be an integer; got "lots"`): fatal
when the entry is selected, otherwise a
startup warning, a `get_models`/hello/picker error, and a refused switch.
`ConfigModelResolver` no longer raises at construction for it. The top-level
`compaction` block applies to every model, so no selection avoids it: a
malformed one stays fatal in one `tau: error: <config>: …` line, exit 2, for
every command that checks models.

Every problem in an entry is reported, not only the first: `models.a.max_tokens
must be a positive integer…; models.a.grammar must be…`. A type error from the
`Model` constructor itself (for example `temperature: "hot"`) is reported once
τ's own checks on that entry pass.

Only commands that use models check them: the TUI, `-p`, rpc, repl, and
`tau serve` in the foreground or with `-d`. `tau serve --stop`, `--tail` and
`--schema`, `tau ext …`, `tau --connect` (the daemon's config applies),
`--import-session`, `--export-session` and `--export-html` neither stop nor
warn, so a typo in an entry can't stop you stopping or tailing a running daemon.
A `models` value that is not a JSON object is still fatal for every command
that checks, since no entry can be read.

**For the 0.14.0 release notes.** In 0.13.0 a malformed model entry ended in a
traceback when selected, and one nobody selected made `get_models` and the
serve hello fail. In 0.14.0 a bad entry is fatal only when selected: the
command stops in one `tau: error:` line, exit 2. Any other bad entry is a
startup warning, is listed as unavailable, and can't be selected. A config
whose selected model built in 0.13.0 keeps starting; it may now print a warning
naming an entry that never built.

One exception: an entry whose `backend` is not registered yet (and that sets no
`base_url`) is left to the build that selects it, because an extension may
register that provider first (docs/EXTENSION-PROVIDERS.md §2). It is neither
fatal nor warned at startup, selected or not, and none of its other fields are
checked there either, since the build stops at the provider before reaching
them. `get_models` and the picker still list it as unavailable until the
provider registers. When that build does happen and the provider is still missing, it raises
`backends.ProviderNotRegisteredError`, a `ModelConfigError`, so it too is one
`tau: error:` line, exit 2, and the serve daemon answers `INVALID_PARAMS`. A
config entry is named (`models.ext: backend 'acme' is not a registered
provider …`); an ad-hoc `foo/bar` or `--provider foo` has no entry to name, so
it reads `provider 'foo' is not registered (registered: …); an extension that
provides it must load first, or give a base_url`.

## 9. A model that cannot be used has a core name (2026-10-19)

**What was wrong.** `AgentSession.set_model` and `ExtensionContext.set_model`
documented "whatever the resolver raises". For an entry that does not build,
the τ resolver raises `tau_coding_agent.config.ModelConfigError`, so an
extension that wanted to catch it had to import a head.

**Now.** `tau_agent_core.ModelUnavailableError` (a `ValueError`) is the
contract. `ModelConfigError` subclasses it, and so does
`ProviderNotRegisteredError`; both stay `ConfigError`s for the heads'
one-line handler. Both `set_model`s name it, and `KeyError` for an unknown
name, in their `Raises`. A resolver bound with `set_model_resolver` is expected
to raise those two.

## 10. A session whose recorded model is gone still opens (2026-10-19)

**What was wrong.** §7 made `resolve_model_config` refuse an unconfigured bare
name, and the serve daemon resolves a session's recorded model through it on
`attach`. So a session whose cursor last ran `local-llm-tun`, opened under a
config without that key, could no longer be attached, read, forked or repaired
(tau-code's report). Before §7 the attach worked only because τ built an
ad-hoc `openai/local-llm-tun`, which is the silent default §7 removed.

**Now.** A recorded model that no longer resolves or builds opens the session
(an entry that is `null`, not an object, or has a malformed own `compaction`
block included, since `recorded_model_problem` builds through the same
`backends.build_config_entry` as §8) on a `tau_agent_core.model_availability.UnavailableModel(name, reason)`, never a
substitute and never `default_model`:
- Reads work: attach, `get_messages`, the tree, `get_models`, fork, export.
  The recorded name still shows (`model_name`, the serve `CursorState.model`).
- `AgentSession.model_unavailable` holds one message naming the model, the
  reason and the fix (`/model`, `set_model`, `ctx.set_model`, or restore the
  config entry). It is the serve `CursorState.model_error`, RPC `get_state`'s
  `model_error` (with `model: null`), and `get_session_stats` reports
  `context_window`/`context_headroom` as null. `get_model()` raises
  `ModelUnavailableError` (§9) rather than invent a spec.
- A turn is refused at admission, before any request, with that message as
  the `rejection_reason`. A slash command such as `/model` still runs.
  `set_model` to a configured name repairs it. At admission the recorded name
  is re-resolved once, so a provider an extension registered after the session
  opened is picked up.
- Nothing is recorded for the unavailable model: the config entry keeps the
  recorded `model`, and no `model_spec` is written.

**Per head.**
- `tau serve` (attach, fork) and the REPL's `--continue/--session/--resume`
  pass `open_unavailable=True` to `resolve_model_config`. The REPL prints the
  message once as an error line after the replay.
- The TUI's resume (`/resume`, the picker, the sidebar) opens it and notifies
  the message as an error. It used to refuse with "Model X not found in config".
- `tau -p -c` / `--session` is about to run a turn, so it refuses in one line:
  `the resumed session's model 'X' is unavailable: <reason>. Pass --model NAME
  to continue it on a configured model`.
- `tau --mode rpc` resumes nothing at startup, and `switch_session` keeps the
  live model, so it is unaffected.
- An explicit `--model nope` (or `--provider`) is user input and keeps §7's
  one-line refusal everywhere.
