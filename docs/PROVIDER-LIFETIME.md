# Provider & HTTP-client lifetime — a prerequisite for W14 (C2 branch sub-agents)

**Status: RESOLVED (2026-07-12).** Fixed by the provider pool in `tau-llm/src/tau_llm/client.py`;
**W14 is unblocked.** See [§8](#8-what-was-actually-built) for what shipped and how each
acceptance criterion was met.
**Found:** 2026-07-12, while reviewing W9's prefix-stability work.

§1–§7 below are preserved as the original diagnosis. They are still the reason the fix looks
the way it does — in particular [§5](#5-the-design-question-why-this-is-not-just-a-patch), the
silent cross-routing trap, which is *why* the pool is keyed the way it is. Do not "simplify"
the pool key without reading §5 first.

---

## 1. The finding

`tau_llm.client.stream_simple` — *the* single entry point τ-agent-core uses to talk
to τ-llm — constructs a **brand-new `ProviderRegistry` on every call**:

```python
# tau-llm/src/tau_llm/client.py:58-69
provider_name = getattr(model, "provider", "openai")
registry = Registry()                      # <-- fresh, EMPTY, every call
try:
    provider = registry.get(provider_name) # <-- therefore ALWAYS raises KeyError
except KeyError:
    provider = OpenAICompletionsProvider(  # <-- so a new provider EVERY completion
        api_key=options.get("api_key"), base_url=base_url,
    )
    registry.register(provider_name, provider)  # <-- into a registry discarded on return
```

`ProviderRegistry.__init__` sets `self._providers = {}`. A fresh registry is always
empty, so `registry.get(...)` can never succeed.

Three consequences, in increasing order of importance:

1. **The `try` branch is dead code.** It has never executed. The `register(...)` call
   writes into an object that is garbage the moment the function returns.
2. **Every completion builds a new `OpenAICompletionsProvider`, and therefore a new
   `httpx.AsyncClient`** (`openai.py:402`, `_get_client`, which caches one client
   *per provider instance*). There is **no `aclose()` anywhere in τ**.
3. **The registry's own docstring is false.** It advertises a "Singleton-like registry
   … registered at application startup and looked up during agent loop execution."
   That lookup never happens.

## 2. What this is NOT

**It is not a memory or file-descriptor leak.** That was my first hypothesis and
measurement falsified it. CPython's refcounting reclaims each provider — and its
client — as soon as the stream is dropped:

| after | live `httpx.AsyncClient`s | open fds |
|---|---|---|
| baseline | 0 | 7 |
| +1 / +5 / +10 sequential completions | 1 | 7 |
| +8 concurrent completions (the C1 fan-out shape) | 0 | 7 |

Flat. Nothing accumulates. Any fix should be justified on the grounds below, **not**
on a leak that does not exist.

(Caveat worth keeping: this depends on refcounting. The clients are closed by GC, not
by us — τ never calls `aclose()`, so cleanup is implicit and would regress under a
non-refcounting runtime.)

## 3. What it costs — measured

A new client per call means **no HTTP keep-alive between completions**: every
completion pays a fresh TCP connect. Measured against the live llama.cpp box
(`192.168.1.100`), 20 sequential completions, identical payloads, server pre-warmed:

| | wall clock | per call |
|---|---|---|
| fresh client per call (**τ today**) | 2.51 s | 125.7 ms |
| one client reused | 1.67 s | 83.3 ms |
| **overhead of the churn** | | **+42 ms/call — 51 % slower** |

That is on a **LAN, over plaintext HTTP**, which is the *cheapest possible* case. A
TLS endpoint (any cloud provider) pays a full handshake per call instead of a bare TCP
connect, so the gap widens substantially.

## 4. Why it blocks W14

W14 (C2) fans out N concurrent sub-agents, each running its own completions. The churn
is per-completion, so it multiplies by the fan-out. More decisively, the *correctness*
trap in §5 is only reachable when **one run uses more than one model** — which is
exactly what C1's model-registry routing introduced and what W14's sub-agents (a cheap
model for branches, the main model for the primary lane) are designed to do.

Today the bug is masked. W14 is the feature that unmasks it.

## 5. The design question (why this is not just a patch)

**The obvious fix is a correctness bug.** "Make `Registry` the singleton its docstring
already claims" — hoist it to module level and let the lookup succeed — silently
breaks multi-model routing.

The registry is keyed on **`provider_name`** (`"openai"`), but the provider bakes
**`base_url` and `api_key`** into itself at construction (`openai.py:398-399`), and
`_get_client` bakes them into the client's base URL and `Authorization` header. So the
first model to run wins the `"openai"` slot, and every later model with a different
endpoint reuses it. Demonstrated:

```
model A base_url: http://192.168.1.100:8080/v1
model B base_url: http://192.168.1.100:8080/v1    <-- should be api.openai.com
same object?      True
```

Model B's completion — **and its API key** — goes to model A's server. HTTP 200. No
error. Fabricated-looking-but-plausible output, which is the failure mode this project
treats as the worst possible one.

So the dead code is *accidentally load-bearing*: it is the only reason multi-model
routing works at all. **Anyone "fixing" the dead branch without re-keying the cache
introduces a silent cross-routing bug.** That is why this is written down instead of
patched in passing.

Three further constraints any fix must satisfy:

- **Event-loop affinity.** An `httpx.AsyncClient` is bound to the loop it is used on.
  A naive module-level cache breaks across loops — the TUI runs one long-lived loop,
  but the test suite calls `asyncio.run(...)` per test, so a cached client would be
  reused on a closed loop.
- **Cache key.** At minimum `(provider_name, base_url, api_key)`. Note this makes the
  key **secret-bearing**, which wants care about where it is stored and logged.
- **Ownership / teardown.** Someone must eventually `aclose()`. Whose job — the
  `AgentSession`? Process exit? A context manager? τ has no answer today, which is
  precisely why it currently relies on GC.

## 6. Acceptance criteria for the fix

1. Sequential completions to the **same** model reuse one connection (the 42 ms/call
   overhead in §3 goes away — re-run the A/B benchmark).
2. Completions to **different** models/endpoints in one process go to the **right**
   server with the **right** key. A regression test asserts the §5 cross-routing bug
   cannot come back — it is the one that fails silently.
3. Clients are closed **explicitly**, not by GC.
4. The suite's per-test `asyncio.run` loops do not reuse a client across loops.
5. Either the `ProviderRegistry` docstring becomes true, or the class goes. It must not
   keep describing behaviour it does not have.

## 7. Reproductions

All three are one-file scripts; the measurements in §2/§3/§5 come from them:
`fds`+`gc` census (no leak), the 20-call A/B (churn cost), and the two-model
cross-routing demo (the trap).

## 8. What was actually built (2026-07-12)

A **provider pool** in `tau-llm/src/tau_llm/client.py`. `stream_simple` no longer constructs
anything; it calls `_get_or_create_provider(provider_name, base_url, api_key)`.

- **Key = `(provider_name, resolved_base_url, sha256(api_key))`.** This is the whole answer to
  §5: a distinct endpoint or a distinct key is *necessarily* a distinct pool entry, so the
  cross-routing bug is unrepresentable rather than merely unlikely. The key **hashes** the
  api_key so the pool dict never holds a raw secret as a key (the provider object still holds
  one — unavoidable). `base_url` is resolved against `OpenAICompletionsProvider.DEFAULT_BASE_URL`
  first, so an explicit `https://api.openai.com/v1` and an omitted `base_url` collide *on
  purpose* — they name the same server, and fragmenting them would silently forfeit keep-alive.
- **Per event loop, via `weakref.WeakKeyDictionary` keyed on the running loop.** An
  `httpx.AsyncClient` is bound to the loop that built it; a flat module-level dict would hand
  back a client bound to a *closed* loop as soon as one `asyncio.run()` ends and the next
  begins — which is precisely what the test suite does, once per test. A loop's pool entry now
  dies with the loop.
- **Explicit teardown.** `OpenAICompletionsProvider.aclose()` (idempotent) plus
  `tau_llm.client.aclose_providers()`, which closes and drops every provider for the current
  loop. Wired into the two *pre-existing* shutdown paths — `Parley.on_unmount` (TUI) and
  `run_print`'s `finally` (headless) — in both cases **after** `emit_session_shutdown`, since a
  shutdown hook may itself make one last LLM call.
- **`ProviderRegistry` is DELETED**, not repaired. Once the pool landed it had zero legitimate
  callers (its only ones were its own module, a re-export, and the dead branch in §1). Per
  Fail-Early — no dead code, no docstring describing behaviour the class does not have — the
  module is gone. §6.5 is satisfied by removal rather than by rewriting the lie.

### Acceptance criteria (§6) — all met

| # | Criterion | Evidence |
|---|---|---|
| 1 | Same-model completions reuse one connection | Re-ran the §3 A/B live: **62.9 → 36.7 ms/call, 26.2 ms saved (42 % faster)**. Absolute numbers differ from §3's original run (server conditions vary); the *effect* reproduces. |
| 2 | Different models route correctly; regression test for §5 | The §5 reproduction now prints `same object? False`, distinct keys, distinct `base_url`s. The suite asserts on the **actual `httpx.AsyncClient` construction kwargs** (`base_url` + `Authorization`), not just object identity — the weaker check would pass even if routing were broken. |
| 3 | Clients closed explicitly, not by GC | `aclose_providers()` at both shutdown paths. |
| 4 | Per-test `asyncio.run` loops do not share a client | `WeakKeyDictionary` keyed on the loop; a test asserts two `asyncio.run` calls do not share. |
| 5 | The registry docstring becomes true, or the class goes | **The class went.** |

Note §2 ("this is not a leak") was **not** retested — it is orthogonal to this fix, and the
finding stands. The clients were never leaking; they were *churning*. The fix is justified on
§3 (cost) and §5 (correctness), exactly as §2 insisted it must be.
