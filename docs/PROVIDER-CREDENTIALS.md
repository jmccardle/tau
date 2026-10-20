# A request with no key is the endpoint's to refuse

Built 2026-10-19 for the anthropic-messages wire (#69). The other two wires
still refuse before sending (#77), and which environment variable fills a
missing key is not yet scoped to its endpoint (#75).

## 1. What was wrong

tau-router ran `tau -p -m anthropic/claude-haiku-5-5 "…"` with the key only
inside a config entry and `ANTHROPIC_API_KEY` unset. τ got as far as the first
request, then died with an uncaught
`ValueError: No API key for provider 'anthropic'. Set one of ANTHROPIC_API_KEY, or pass api_key in the call options.`
and a traceback, exit 1.

The issue proposed a startup refusal. John decided against it on 2026-10-19,
for two reasons:

- τ cannot know that an endpoint needs a key. An anthropic-format endpoint
  without authentication is legitimate.
- The real failure is the provider's refusal, so that refusal is what τ shows.
  Inventing a refusal earlier would be the fabricated error.

He also ruled out borrowing another entry's `api_key` for an ad-hoc model,
because that risks sending a token to the wrong API.

## 2. The anthropic wire sends without a key

`register_api(api, factory, sends_without_key=True)` declares that a wire's
provider sends a request when no key resolves. `tau_llm.client._resolve_request`
refuses a missing key only for a wire without the flag, and only
`anthropic-messages` has it. `api_sends_without_key(api)` reads the flag.

With no key, `AnthropicMessagesProvider` adds
`extra_headers={"X-Api-Key": Omit(), "Authorization": Omit()}` to the request.
Both headers are omitted for two reasons:

- With `api_key=""`, SDK 1.0.0 raises
  `TypeError: "Could not resolve authentication method…"` before anything is
  sent (ran). That would be a client-side refusal in a different form.
- With `api_key=None`, the SDK reads `ANTHROPIC_AUTH_TOKEN` from the
  environment itself. Omitting `Authorization` keeps that token from being
  sent. The test sets the variable and asserts the fake server saw neither
  header.

**Divergence from the request as relayed.** John asked for the ad-hoc entry's
`api_key` to default to `""`. The default is applied at the request boundary
instead, because the core drops a falsy key at several sites before it reaches
the client (`agent_loop.py`'s `if settings.api_key:`, `compaction.py`, the
extension `complete` path). A `""` stored on the entry would arrive as no key
anyway. The effect is the same, and it also covers a configured anthropic entry
that has no key.

## 3. A 401 says what to set

`ProviderHttpError` takes `provider_id`. On a 401 its `hint` is
`tau_llm.providers.credential_hint(provider_id)`, appended to `str()`:

```
HTTP 401 from model 'claude-x' at 'https://api.anthropic.com': x-api-key header is required. To send a key, set ANTHROPIC_API_KEY or "api_key" on the model's "models" entry in τ's config.json.
```

That is the real api.anthropic.com answer to the keyless request (ran).

Only 401 gets the hint. A 403 means a key was sent and lacks permission, so
"set a key" would mislead. Google's missing-key status is not 401, which #77
records.

`tau -p` now prints any `ProviderHttpError`, `ProviderConnectionError` or
`ProviderStreamError` as one `tau: error:` line, exit 1. Exit 2 stays reserved
for usage and config errors. This also removes the traceback half of #73.

## 4. The indication

`get_models`, the serve hello and attach list each model as a `ModelRecord`.
That record gains `has_api_key`:

- `true`: the entry's `api_key`, or the vendor's `api_key_env`, would give a key.
- `false`: neither would.
- `null`: the entry doesn't build (it has an `error`).

The reading is `tau_llm.providers.resolves_api_key(provider, key)`, the same
resolution the client applies. The TUI picker labels such a model
`name (no API key)` and still offers it (`tau_agent_core.flows.models_without_key`).

The field sits on `ModelRecord`, not on `ModelSpec`. `ModelSpec` is what
`AgentSession.get_model()` returns, and `_record_agent_spec` persists that into
the session tree. A key's presence is a fact about the environment at read
time, and writing it into history would make history wrong as soon as the
environment changes.

## 5. Not built

- **Keyless sending on the openai and google wires** (#77). They keep the
  client refusal, and `OpenAICompletionsProvider` keeps its own.
- **Scoping the environment fallback to the vendor's own endpoint** (#75).
  Until then, an entry with its own `base_url` and no key is sent the vendor's
  variable. Separately, `openai.py` reads `OPENAI_API_KEY` for every provider
  on its wire.
- **Keys outside config.json** (#76).
- **An indicator on `get_state` or serve `CursorState`.** A host reads the
  selected model's `has_api_key` from `get_models` by name. The TUI shows it in
  the picker, and the 401 hint covers the moment it matters.
