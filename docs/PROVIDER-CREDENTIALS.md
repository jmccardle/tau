# A request with no key is the endpoint's to refuse

Built 2026-10-19 (#69, #75, #77). With no key, the anthropic-messages and
openai-completions wires send the request anyway, and the endpoint's 401 is
the error. A vendor's environment variable reaches only that vendor's own
endpoint. The google wire still refuses before sending, because its SDK cannot
build a keyless client (§2). Keys stored outside config.json are #76.

## 1. What was wrong

**Keyless refusals.** tau-router ran `tau -p -m anthropic/claude-haiku-5-5 "…"`
with the key only inside a config entry and `ANTHROPIC_API_KEY` unset. τ got as
far as the first request, then died with an uncaught
`ValueError: No API key for provider 'anthropic'. Set one of ANTHROPIC_API_KEY, or pass api_key in the call options.`
and a traceback, exit 1 (#69).

John decided against the proposed startup refusal on 2026-10-19, for two
reasons:

- τ cannot know that an endpoint needs a key. An anthropic-format or
  OpenAI-compatible endpoint without authentication is legitimate.
- The real failure is the provider's refusal, so that refusal is what τ shows.

He also ruled out borrowing another entry's `api_key` for an ad-hoc model,
because that risks sending a token to the wrong API.

**Env keys sent to the wrong host.** A vendor's environment variable was chosen
by provider id, not by endpoint (#75). Two concrete cases:

- An entry with provider `anthropic`, its own `base_url` (LiteLLM, a proxy) and
  no `api_key` sent `ANTHROPIC_API_KEY` to that host.
- `OpenAICompletionsProvider.__init__` read `OPENAI_API_KEY` itself. Every
  provider on that wire with no key therefore sent OpenAI's key, including
  `groq` and `local-llm`.

## 2. The wires send without a key

`register_api(api, factory, sends_without_key=True)` declares that a wire's
provider sends a request when no key resolves. `tau_llm.client._resolve_request`
refuses a missing key only for a wire without the flag. The refusal message
ends with the credential hint from §3.

| Wire | `sends_without_key` | With no key |
|---|---|---|
| `anthropic-messages` | yes | `extra_headers={"X-Api-Key": Omit(), "Authorization": Omit()}` |
| `openai-completions` | yes | no `Authorization` header |
| `google-generative-ai` | no | the client refuses before sending |

On the anthropic wire, both headers are omitted for two reasons:

- With `api_key=""`, SDK 1.0.0 raises
  `TypeError: "Could not resolve authentication method…"` before anything is
  sent (ran).
- With `api_key=None`, the SDK reads `ANTHROPIC_API_KEY` and
  `ANTHROPIC_AUTH_TOKEN` from the environment itself. Omitting both headers
  keeps either from going out (`test_keyless_model.py` sets both and asserts the
  server saw neither).

The google wire stays refusing because `genai.Client(api_key=None)` and
`api_key=""` both raise `ValueError: No API key was provided…` at construction
(ran). The alternative is to send a placeholder key and strip its header. That
is the fabricated value Fail Early forbids.

**Divergence from the request as relayed.** John asked for the ad-hoc entry's
`api_key` to default to `""`. "No key" is applied at the request boundary
instead, because the core drops a falsy key at several sites before it reaches
the client (`agent_loop.py`'s `if settings.api_key:`, `compaction.py`, the
extension `complete` path). The effect is the same, and it also covers a
configured entry that has no key.

`"api_key": "not-needed"` for a local server keeps working. It is now just a key
the server ignores, and it is no longer needed.

## 3. A 401 says what to set

`ProviderHttpError` takes `provider_id` and `key_sent`. On a 401 its `hint` is
`tau_llm.providers.credential_hint(provider_id, base_url, key_sent=…)`,
appended to `str()`. A caller that passes neither gets no hint. With no key
sent:

```
HTTP 401 from model 'claude-x' at 'https://api.anthropic.com': x-api-key header is required. To send a key, set ANTHROPIC_API_KEY or "api_key" on the model's "models" entry in τ's config.json.
```

That is the real api.anthropic.com answer to the keyless request (ran).

The hint offers the environment variables only where §5 would read them. For an
entry with its own endpoint, it offers `"api_key"` and says which endpoint the
variables are limited to:

```
… To send a key, set "api_key" on the model's "models" entry in τ's config.json. ANTHROPIC_API_KEY is read only for https://api.anthropic.com.
```

With a key sent, "set ANTHROPIC_API_KEY" would be wrong advice whenever the
entry's `api_key` was the key, because §5 rule 1 lets the entry win. Tectum's
review of `0f3f81c` measured that hint on a deliberately bad entry key. The
hint now names where the refused key came from, in §5's order:

```
… invalid x-api-key. The key sent was refused. It came from "api_key" on the model's "models" entry in τ's config.json when set, else from ANTHROPIC_API_KEY.
```

Only 401 gets the hint. A 403 means a key was sent and lacks permission.

`tau -p` now prints any `ProviderHttpError`, `ProviderConnectionError` or
`ProviderStreamError` as one `tau: error:` line, exit 1. Exit 2 stays reserved
for usage and config errors. This also removes the traceback half of #73.

## 4. The indication

`get_models`, the serve hello and attach list each model as a `ModelRecord`.
That record gains `has_api_key`:

- `true`: the request would carry a key.
- `false`: it would carry none.
- `null`: the entry doesn't build (it has an `error`).

The reading is `tau_llm.providers.resolves_api_key(provider, key, base_url)`.
The TUI picker labels such a model `name (no API key)` and still offers it
(`tau_agent_core.flows.models_without_key`).

The field sits on `ModelRecord`, not on `ModelSpec`. `ModelSpec` is what
`AgentSession.get_model()` returns, and `_record_agent_spec` persists that into
the session tree. A key's presence is a fact about the environment at read
time, and writing it into history would make history wrong as soon as the
environment changes.

## 5. Which key a request carries

`tau_llm.providers.resolve_request_key(provider_id, api_key, base_url)` is the
only place τ resolves a key. The client sends what it returns, and
`resolves_api_key` reports it. The rules:

1. A non-empty `api_key` (the entry's, or `options["api_key"]`) wins.
2. Otherwise the vendor's `api_key_env` is read, but only when
   `ProviderSpec.reads_env_for(base_url)` holds. That requires the request's
   endpoint to be the vendor's registered `base_url`. The comparison ignores
   the case of the scheme and host and any trailing slashes, but not the path,
   so `…/v1` is a different endpoint. No `base_url` means the registered one.
3. A vendor with no registered endpoint never lends its variables.

`OpenAICompletionsProvider` reads no environment variable.

**Breaking change (0.14.0).** An entry with its own `base_url` and no `api_key`
no longer receives the vendor's environment key. To keep a gateway working, put
its key in the entry's `api_key`.

## 6. Not built

- **Keyless sending on the google wire.** The SDK refuses (§2).
- **Bearer tokens.** τ has no `auth_token` path, so `ANTHROPIC_AUTH_TOKEN` is
  never sent. A gateway that wants a Bearer token needs an entry-level
  credential, which is how an entry names its key source: the open design
  question in #76.
- **An entry naming its own variable** (e.g. `"api_key_env": "LITELLM_KEY"`).
  Also #76.
- **An indicator on `get_state` or serve `CursorState`.** A host reads the
  selected model's `has_api_key` from `get_models` by name. The TUI shows it in
  the picker, and the 401 hint covers the moment it matters.
