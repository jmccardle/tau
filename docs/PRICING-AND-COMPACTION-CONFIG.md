# Cache writes priced, and compaction configured in the CLI heads

Built 2026-10-15. Cache-write tokens are now priced, by one function in
`tau_agent_core.usage` that all three former copies of the formula call. And
auto-compaction is now on in every CLI head, tuned by a `compaction` block in
`~/.tau/config.json`.

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
onto the session is a wire change and not a small one. It is not built (§6).

## 3. Compaction is on, and configured by one block

```json
"compaction": {
  "enabled": true,
  "reserve_tokens": 16384,
  "keep_recent_tokens": 20000,
  "summarizer": "local-llm"
}
```

Every key is optional. An absent block means compaction is on with
`CompactionSettings`' defaults (`reserve_tokens=16384`, `keep_recent_tokens=20000`).
`tau_coding_agent.config.parse_compaction_config` validates the block and raises
`ConfigError` naming the key:

| Input | Message |
|---|---|
| a non-object block (`null` included) | `'compaction' must be an object; got NoneType` |
| a key outside the four | `unknown key 'compaction.<k>'; known keys: enabled, reserve_tokens, keep_recent_tokens, summarizer` |
| a wrong type (`true` is not an int) | `'compaction.<k>' must be int; got True` |
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
`model_config["compaction"]`, and copies the summarizer's `models` entry onto
`model_config["compaction_summarizer"]`. It returns a new dict and never mutates
the shared entry.

`TauBackend` validates the block again, so a direct construction is checked too.
With no block it builds a fresh `CompactionSettings()`. It never passes `None`:
`AgentSession` would then fall back to the module-level
`DEFAULT_COMPACTION_SETTINGS`, which `set_auto_compaction` mutates in place.

**One test assumed compaction was off.** `test_rpc_conformance.py` asserted that
`get_session_stats` started at `enabled: false` and then turned it on. It now
asserts `true` and turns it off, so the verb is still seen changing the state.
No other test noticed the change, because test models have
`context_window=128000` and test transcripts stay small.

## 4. The summarizer goes through `CompactionPolicy.local_summarizer`

`compaction.summarizer` names an entry in `models`. `TauBackend` resolves it with
`ConfigModelResolver`, the same class `--model`, `set_model` and RPC
`get_models` use. It then declares `CompactionPolicy(mode="local_summarizer")`.
That is the existing seam `AgentSession.summarizer()` reads for compaction and
for branch summaries. Nothing new was added to `AgentSession`.

The policy brings two checks from its measured-run design, and both are kept:

- **The summarizer must declare its own `api_key`.** `local_summarizer` refuses
  to inherit the session's key, because a separate summarizer is a different
  endpoint. A summarizer entry with no `api_key` raises `ConfigError` at
  construction. The alternative was to let `None` mean "the provider's
  environment variable", as it does for `--model`. That loses here, because
  `AgentSession.summarizer()` turns a `None` key into the *session's* key, so a
  local session key would be sent to the summarizer's vendor. Supporting the env
  var properly means changing how `summarizer_for` signals "no key". That is a
  change to the policy's contract, so it is not made here.
- **The summarizer's `context_window` must be at least the session model's.**
  `bind_to` raises `CompactionPolicyError` at construction and again on
  `set_model`. A compaction summarises a full window, so a smaller summarizer
  would fail mid-session as a `CompactionError`.

`enabled: false` with a summarizer still builds the policy, and then turns auto
compaction off with `set_auto_compaction(False)`. So a manual `/compact` still
uses the named summarizer.

The policy's other machinery does nothing in this mode. `admit_turn` and
`observe_context` are no-ops under `local_summarizer`, and no manifest is
written. The RPC `set_auto_compaction` notes used to argue "no RPC session ever
carries a policy". That argument now rests on the mode bounding no turns.

## 5. Placement: top-level, with a per-model replacement

The config has precedent for both. `default_model`, `theme` and `session_store`
are top-level. `cost`, `context_window` and `prompt_cache` are per-model.
`reasoning_replay`, `max_turns` and `system_prompt` are top-level with a
per-model override that wins.

Compaction follows the third shape. Its main settings are about the session, not
the model, and `summarizer` names a model, so it reads oddly inside another
model's entry. But a model entry may carry its own `compaction`, which **replaces**
the top-level block whole. It does not merge with it. Merging would make an
entry's `{"enabled": false}` inherit a top-level summarizer that the entry never
mentions.

## 6. What is absent

- **Cost on the RPC wire.** §2: the price block would have to reach the session
  or the `Model`, and `get_session_stats` would need a `cost_usd` field and a
  protocol bump.
- **Validation of the `cost` block's keys.** A typo such as `cache_writes` still
  prices its bucket at 0, as before. Rejecting unknown price keys would break
  every config that carries an extra key in that block today, so it needs its
  own decision.
- **An env-var key for the summarizer.** §4. The change belongs in
  `CompactionPolicy`, not in the config reader.
- **`soft_limit_ratio`, `hard_limit_tokens`, `soft_limit_tokens` in the block.**
  §3.
- **A submission id on an auto-compaction's `agent_start`/`agent_end`.** The RPC
  `set_auto_compaction` notes record this as a known gap: the pair goes through
  `_events.emit` directly. An RPC host used to meet it only after opting in. With
  compaction on by default, a host now meets it whenever a session nears its
  window.
