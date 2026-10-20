# Cache writes priced, and compaction configured in the CLI heads

Built 2026-10-15. Cache-write tokens are now priced, by one function in
`tau_agent_core.usage` that all three former copies of the formula call. And
auto-compaction is now on in every CLI head, tuned by a `compaction` block in
`~/.tau/config.json`. The compaction design was corrected twice in review the
same day: §4.1 and §6 say what changed and why, and they supersede the earlier
sections where the two disagree.

## 1. What was wrong

### 1.1 Cache writes were billed at nothing

Three copies of one formula priced a completion: `compute_cost_usd` in
`backends.py`, `Pricing.cost_of` in `examples/ext_kit/ledger.py` (through
`_PRICED_BUCKETS`), and `price_increment` in `examples/ext_kit/spawn.py`. All
three priced `input`, `output` and `cache_read`. None priced `cache_write`, but
every docstring listed it as a price key. Two tests pinned the omission:
`test_cache_write_is_inert_not_priced` and `test_pricing_cost_of_ignores_cache_write`.

The omission dates from `EXTENSIONS-IMPLEMENTATION.md` §E4.cost, which said
`cache_write_tokens` "is inert against today's provider (never populated) — real
0, comment it". That stopped being true when `docs/PROMPT-CACHING.md` turned
caching on. Both providers partition the prompt. Anthropic reports
`cache_creation_input_tokens` outside `input_tokens`, and `_usage_from_openai`
subtracts `cache_write_tokens` out of `prompt_tokens`. So the written span reached
no priced bucket at all.

Take the cold write measured in `PROMPT-CACHING.md` §3: `prompt_tokens` 14116,
with `cache_write_tokens` 14113. τ's `input_tokens` for that request is 3. The old
formula priced 3 of the 14116 prompt tokens. At $3/M input and $3.75/M cache
write (Anthropic bills a write at 1.25× input), it reported $0.000009 for a
prompt that cost $0.0529.

### 1.2 Compaction was off in every CLI head

`TauBackend.__init__` passed `CompactionSettings(enabled=False)` to its
`AgentSession`. That line arrived with the initial commit, `cc3c210`. No config
key read it, although `set_auto_compaction`'s docstring said "the config file is
where a durable answer lives". The TUI, `tau -p`, `tau --mode rpc`,
`tau --mode repl` and `tau serve` all build a `TauBackend`, so none of them ever
compacted unless a user typed `/compact` or an RPC host sent `set_auto_compaction`.

The SDK's `create_agent_session` compacts by default
(`CompactionSettings.enabled = True`). So the library and its own CLI disagreed.
A small model reached 131k tokens in a CLI session and failed with the provider's
prompt-too-long 400. The context was still on disk, but the turn was lost.

## 2. `price_usage`: one function, in the core

```python
tau_agent_core.usage.price_usage(cost: Mapping | None, usage: Mapping) -> float | None
```

This is `sum(cost[p] / 1e6 * usage[b])` over the four buckets in
`usage.PRICED_BUCKETS`: `input_tokens`/`input`, `output_tokens`/`output`,
`cache_read_tokens`/`cache_read` and `cache_write_tokens`/`cache_write`.
`backends.py`, `ledger.Pricing.cost_of` and `spawn.price_increment` all call it.
`compute_cost_usd` is deleted rather than kept as a wrapper, because it had one
caller.

**The semantics did not change.** An absent `cost` block returns `None`, so a
head emits tokens only and never a fabricated `$0`. A present all-zero block
returns `0.0`. A missing price key prices its bucket at 0, and a missing bucket
counts as 0 tokens.

**It takes τ's usage names only.** The codebase has two spellings:
`input_tokens`/`output_tokens` (`tau_llm.types.Usage`, `usage_totals`,
`message_end` usage, `get_usage()`) and `prompt_tokens`/`completion_tokens`
(the OpenAI wire, and `TauBackend`'s outbound `usage_out`). Every pricing caller
already holds the first. Accepting both would have meant choosing a winner when a
dict carried both. And a dict carrying only the wire spelling would price as
zero, which is the silent fault §1.1 is about. So that case raises `ValueError`.

**It lives in `tau_agent_core.usage`.** That module was already "token-usage
arithmetic" (`zero_usage`, `add_usage`), and the examples can import it without
pulling in `tau_coding_agent`. It is `@agent_facing(topic="messages")`, next to
`Usage` in the generated reference.

**RPC still emits no cost.** `cost_usd` is computed at the emit boundary of
`TauBackend.stream_submission`, which `tau -p` and the TUI read. `tau --mode rpc`
reports usage through `get_usage()` and `get_session_stats`. It has no price,
because the `cost` block lives in the coding agent's config and never reaches
`AgentSession` or `Model`. The function is head-neutral now, but carrying a price
onto the session is a wire change and not a small one. It is not built (§7).

## 3. Compaction is on, and configured by one block

```json
"compaction": {
  "auto": true,
  "reserve_tokens": 16384,
  "keep_recent_tokens": 20000,
  "summarizer": "local-llm"
}
```

Every key is optional. An absent block means compaction is on with
`CompactionSettings`' defaults (`reserve_tokens=16384`, `keep_recent_tokens=20000`).
`tau_coding_agent.config.parse_compaction_config` validates the block and raises
`ConfigError` naming the key. The table shows the top-level block, whose
messages start with the config path; a model's own block reads
`models.<name>.compaction` where these read `compaction`
(`'models.small.compaction.reserve_tokens' must be int; got "lots"`). An
offending value is shown as JSON, as it was written (`null`, `"x"`, `true`):

| Input | Message |
|---|---|
| a non-object block (`null` included) | `'compaction' must be an object; got null` |
| a key outside the four | `unknown key 'compaction.<k>'; known keys: auto, reserve_tokens, keep_recent_tokens, summarizer` |
| a wrong type (`true` is not an int) | `'compaction.<k>' must be int; got true` |
| a token count ≤ 0 | `'compaction.<k>' must be positive; got 0` |
| `"summarizer": ""` | `'compaction.summarizer' must name a model; got an empty string` |
| a summarizer not in `models` | `'compaction.summarizer' names unknown model 'x'; configured models: …` |

`soft_limit_ratio` and the two pinned limits are not exposed. They exist for
tests and for SDK callers, and nobody asked to tune them from the config.

**The block is applied in two places, one per config path.**
`apply_compaction_config(model_config, config)` runs at the end of
`headless.resolve_model_config`, which serves `tau -p`, `--mode rpc`,
`--mode repl`, `tau serve`'s two session constructors, and the TUI launcher's
`--model` override. It also runs at the top of `TauApp._apply_run_config`, which
every TUI `create_backend` goes through: new chat, `/model` and resume.
`TauBackend` sees only one model's config. So the helper copies the block onto
`model_config["compaction"]`, which is the block the session starts under. It
returns a new dict and never mutates the shared entry. (§4.1 had it also copy
every model's entry onto `model_config["models"]`; §6 removed that.)

`TauBackend` validates the block again, so a direct construction is checked too.
With no block it builds a fresh `CompactionSettings()`. It never passes `None`:
`AgentSession` would then fall back to the module-level
`DEFAULT_COMPACTION_SETTINGS`, which `set_auto_compaction` mutates in place.

**One test assumed compaction was off.** `test_rpc_conformance.py` asserted that
`get_session_stats` started at `enabled: false` and then turned it on. It now
asserts `true` and turns it off, so the verb is still seen changing the state.
No other test noticed the change, because test models have
`context_window=128000` and test transcripts stay small.

## 4. The summarizer is a model NAME, resolved like any other

`compaction.summarizer` names an entry in `models`. `TauBackend` declares
`CompactionPolicy.local_summarizer(summarizer=<name>)`. The policy holds only the
name. `AgentSession.summarizer()` resolves it through `AgentSession.resolve_model`,
which calls `ConfigModelResolver`: the same path `--model`, `ctx.set_model` and the
RPC `set_model` verb use. The key is therefore the summarizer entry's `api_key`,
or `None`, which makes the provider read its own environment variable.
`summarizer()` is what compaction and branch summaries read.

### 4.1 Correction (2026-10-15, same day)

As first built, the policy carried a ready-built `Model` and an API-key string.
`TauBackend` resolved the entry itself and refused a summarizer entry with no
`api_key`. John rejected that: "can't we use the model by name out of the Tau
model configuration? Why would we have a separate path to making LLM calls? It
makes sense for it to be outside of a conversation, but not for two paths for API
keys to get into the harness."

The hazard was real, but it was in the core, not the config. When a policy named
no key, `summarizer()` substituted the *session's* key. That fallback is deleted.
The policy's `summarizer_model`/`summarizer_api_key` fields and `summarizer_for`
are gone, and `local_summarizer` takes `summarizer: str`. So a credential now has
one way into the harness, `resolve_model`, and the session's key cannot reach
another model's endpoint by default.

Consequences:

- **The session must have a resolver before the policy is bound.**
  `AgentSession.__init__` now assigns `model_resolver` before binding. A
  `local_summarizer` policy with no resolver raises `CompactionPolicyError` at
  construction. (As built here, `TauBackend` built that resolver from a copy of
  `config["models"]`; §6 replaced this with binding at `set_model_resolver`.) The heads' later `set_model_resolver` calls bind an
  equivalent resolver. `set_model_resolver` re-checks the policy and keeps the
  old resolver if the new one fails. `create_agent_session` gained a
  `model_resolver` argument so an SDK caller can declare the policy at all.
- **A plain `name -> Model` resolver has no `api_key`.** `resolve_model` then
  uses the session's key, for the summarizer exactly as for `set_model`. That is
  the existing contract of the one path, not a second path.
- **The run manifest records the summarizer by name only.** It used to record id,
  provider, `base_url` and `context_window`. The policy no longer holds a model,
  and `to_manifest()` takes no session.

The window check is kept: **the summarizer's `context_window` must be at least the
session model's.** `bind_to(model, summarizer_model)` raises
`CompactionPolicyError` at construction, on `set_model` and on
`set_model_resolver`, always against the resolved model. A compaction summarises
a full window, so a smaller summarizer would fail mid-session as a
`CompactionError`.

`auto: false` with a summarizer still puts the summarizer in force and only
turns automatic compaction off. So a manual `/compact` still uses the named
summarizer.

The policy's other machinery does nothing in this mode: `admit_turn` and
`observe_context` are no-ops under `local_summarizer`. The RPC
`set_auto_compaction` notes used to argue "no RPC session ever carries a policy".
That argument now rests on the mode bounding no turns.

## 5. Placement: top-level, with a per-model replacement

The config has precedent for both. `default_model`, `theme` and `session_store`
are top-level. `cost`, `context_window` and `prompt_cache` are per-model.
`reasoning_replay`, `max_turns` and `system_prompt` are top-level with a
per-model override that wins.

Compaction follows the third shape. Its main settings are about the session, not
the model, and `summarizer` names a model, so it reads oddly inside another
model's entry. But a model entry may carry its own `compaction`, which **replaces**
the top-level block whole. It does not merge with it. Merging would make an
entry's `{"auto": false}` inherit a top-level summarizer that the entry never
mentions.

## 6. Compaction follows the model: second correction (2026-10-15)

John's second review found three faults in §4.1's build.

**Every model's key rode along with one model's config.** §4.1's
`apply_compaction_config` copied all of `config["models"]` onto the config of
the one model `TauBackend` was built for, so that the session could resolve a
summarizer during construction. That copy is gone, along with the `cli.py`
strip that tidied it up. `TauBackend`'s config now carries only its own model's
`api_key` (a test checks this).

**A per-model block reached only one head.** The TUI's `/model` rebuilds the
backend through `_apply_run_config`, so the new model's own block applied there.
`ctx.set_model` and the RPC `set_model` verb (and so `tau -p` extensions and
`tau serve`) switch in place, and `AgentSession.set_model` only re-checked the
window. Switch to an entry carrying `"compaction": {…}` and the two paths
compacted under different settings: §1's "a fix that reached one head" fault,
inside one feature.

The fix hands out compaction per model by name, the way the resolver already
hands out a model and its key:

- `ConfigModelResolver(config)` takes the whole loaded config, not just
  `models`, because the top-level block is part of what a name resolves to.
  `make_model_resolver(config)` does the same, and all five call sites pass the
  config: the TUI, `tau -p`, `--mode rpc`, `--mode repl` and `tau serve`'s
  `_host_for`. The resolver validates the top-level block when it is
  constructed. A model's own block is checked when that name resolves, and a
  bad one is that entry's build error (docs/SILENT-DEFAULTS.md §8, 2026-10-19).
- `ConfigModelResolver.compaction(name)` returns a
  `tau_agent_core.compaction.CompactionConfig(settings, summarizer)`. It uses
  the model's own block, else the top-level block, else the defaults. A name
  that is not a configured model gets the top-level block.
- `AgentSession` re-reads it whenever its model changes. It does so in
  `set_model_resolver` for the current model, and in `set_model` at the head
  cursor. A summarizer becomes a `local_summarizer` policy, which is checked
  against the new model before anything is committed. A refused switch changes
  nothing. A plain `name -> Model` resolver (an SDK caller's) has no
  `compaction` method, so the session keeps whatever it was constructed with.
- The TUI now passes `config_name` when it builds a backend, as `-p` already did,
  so the session's `Model.name` is the config key that `compaction(name)` looks up.

**Binding happens when the resolver is bound, not during construction.**
`TauBackend` starts its session under the block `apply_compaction_config`
copied, so `auto`, `reserve_tokens` and `keep_recent_tokens` hold from the first
moment. If that block names a summarizer, `TauBackend` calls
`AgentSession.require_compaction_resolver(name)`. Until a resolver with a
`compaction` method is bound, every turn and every summary raises
`CompactionPolicyError`, so nothing ever compacts through the wrong model.
Every head calls `set_model_resolver` right after `create_backend`, so that call
is where compaction is configured. `tau serve`'s `_new_session` builds a
throwaway backend only to read its system prompt, and runs no turn on it. An
unknown summarizer, or one whose window is too small, raises when the resolver is
constructed or bound (`ConfigError`, or `CompactionPolicyError`); in a model's
own block, an unknown summarizer is that entry's build error instead.

**`enabled` said the wrong thing.** `CompactionSettings.enabled` was read only by
the two automatic paths: the mid-turn compactor and the end-of-turn check.
`AgentSession.compact()`, which `/compact` calls, never read it. John asked:
"`compaction: false` doesn't make sense. Does it refuse /compact? Do we mean
autocompaction?" The config key is now `auto`, and so is the core field
`CompactionSettings.auto` ("whether automatic compaction runs; /compact and
AgentSession.compact() ignore it"). There is no alias: τ is pre-1.0, and a
misleading name is a bug. The rename touched 24 Python files: 7 in `src`, 17 in
tests. The wire followed in the same release: `get_session_stats`'
`compaction_settings` reports `auto`, riding the RPC 2.2 / serve 0.9 bump
(`TAU-SERVE.md`), and breaking a 2.1 client that read `enabled` there.
`set_auto_compaction` keeps its `enabled` argument, because the verb's name
already says which compaction it sets.

**A model switch resets `auto`.** A switch re-reads the new model's config, so
an earlier `set_auto_compaction` does not survive it. That toggle was always
in-memory and per-run.

## 7. What is absent

- **Cost on the RPC wire.** §2: the price block would have to reach the session
  or the `Model`, and `get_session_stats` would need a `cost_usd` field and a
  protocol bump.
- **Validation of the `cost` block's keys.** A typo such as `cache_writes` still
  prices its bucket at 0, as before. Rejecting unknown price keys would break
  every config that carries an extra key in that block today, so it needs its
  own decision.
- **`soft_limit_ratio`, `hard_limit_tokens`, `soft_limit_tokens` in the block.**
  §3.
- **Compaction settings in the tree, decided.** A resumed conversation compacts
  under the config as it is now. These settings decide when a summary happens;
  they are not input the model sees, and the summary itself is recorded.
- **KV-cache-aware compaction.** Filed as forgejo john/tau#21.
- **A submission id on an auto-compaction's `agent_start`/`agent_end`.** The RPC
  `set_auto_compaction` notes record this as a known gap: the pair goes through
  `_events.emit` directly. An RPC host used to meet it only after opting in. With
  compaction on by default, a host now meets it whenever a session nears its
  window.
