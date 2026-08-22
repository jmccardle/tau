# v0.9.3 — three vendors, context files that arrive, and `--resume`

Written from the commits between `v0.9.2-fullhistory` and master, so the
release commit, the GitHub release body and the site all say the same thing.

0.9.2 asked what a stranger's first hour looks like. `docs/PLAN-0.9.3.md` §7
listed eight items on that path; all eight are built, and two things nobody
planned turned up while building them — τ's own system prompt had never
reached a model, and every model was being served over the OpenAI wire
whatever it declared.

---

## Vendors

τ spoke one wire protocol. It now speaks three.

* **`anthropic-messages`** — Anthropic, via the official `anthropic` SDK.
* **`google-generative-ai`** — Google Gemini and Gemma, via `google-genai`.
* **`openai-completions`** — unchanged, still the default.

Both new clients are optional extras and import their SDK lazily:

```
pip install 'ffwf-tau-llm[anthropic]'
pip install 'ffwf-tau-llm[google]'
```

A plain install pulls neither, `import tau_llm` works without them, and a
missing one raises an error naming the extra rather than a bare
`ModuleNotFoundError` from an unrelated import.

The Google vendor registers as **`gemini`**, which is the `backend` value
`~/.tau/config.json` entries have carried since before the client existed —
including the one in the shipped template. Registering it as `google` would
have left every existing entry resolving to the OpenAI wire.

### τ dispatches on the model's `api` now

This is the fix under the two clients, and it was a live defect on its own.
`_get_or_create_provider` returned `OpenAICompletionsProvider` unconditionally
and read `model.provider` only as a cache-key component; `model.api` was not
read at all. A model declaring `api="openai-responses"` — a protocol τ has
never implemented, and a legal value of the old type — was served over the
completions wire, and nothing said so.

* `Model.api`, `AssistantMessage.api` and `AssistantMessage.provider` widen
  from a fixed set of names to any string. `AssistantMessage.provider` was
  pinned to `"openai"`, so a perfectly legal `Model` naming any other vendor
  raised a validation error when the vendor was copied onto the message.
* An unknown `api` **raises**, naming what was asked for and what is
  registered. It does not fall back to the OpenAI class, because that fallback
  was the bug.
* A vendor is now a six-field record rather than a class, so an
  OpenAI-compatible backend costs no new code.

### Reasoning signatures cross vendors safely

Anthropic's thinking blocks and Gemini 3's function calls both carry a
signature the vendor validates on replay. Sending one vendor's token to
another is a request that fails.

* `ThinkingContent.provider_signature` and `ToolCall.provider_signature` are
  namespaced by vendor.
* The OpenAI writer **refuses** a foreign signature rather than forwarding it:
  it raises under `Model.strict_reasoning_formats`, and otherwise warns once
  per payload shape and drops the token. The tool call still replays with its
  id, name and arguments intact — the transcript is not broken to avoid
  leaking a field.
* A Gemini 3 function-call signature is replayed **on every setting**,
  including `reasoning_replay="off"`. It is reasoning-derived but it is not
  chain-of-thought — it is a token the API validates, and omitting it is a 400
  on every multi-turn tool conversation. Signatures on text and thinking parts
  do follow `reasoning_replay`.
* Signatures persist to session JSONL as base64 text, because a resumed Gemini
  3 session that lost its signature fails on its next turn.

### What was measured rather than assumed

Three design questions were settled against the live API on a free-tier key,
not by reading. Records are in `docs/probe-results/`.

* Sending a tool-call id to a model that does not expect one is **accepted**,
  by every model tested including one pi classifies as id-less. So
  `requires_tool_call_id` defaults to true and τ ships **no per-model table**
  — a table that can only ever be wrong is worse than none.
* Answering two same-name tool calls by name alone did **not** mis-pair on any
  model tested, so the ~20 lines of guards proposed for it were not written.
* One verdict was caught wrong by the probe's own control and is kept in the
  record marked void. A "nested images rejected" result was really Google
  refusing a 1×1 PNG as an ordinary part.

Also fixed while proving the client live: **the Google SDK's automatic
function calling was on by default.** It had nothing to execute, because τ
passes function declarations rather than callables — but AFC running would
have replaced τ's agent loop, its tool-execution events and its permission
checks with the SDK's, silently. It is now explicitly off.

---

## The system prompt and project context files

**Before this release, neither τ's own base prompt nor any project context
file had ever reached a model on the TUI or headless path.** The plan doc said
the loader was live and shadowed by a default config value; only the second
half was true. `TauBackend` constructs `AgentSession` directly and passed
`config.get("system_prompt", "")` straight through, so the loader behind
`create_agent_session` was never called from either surface.

* **Discovery walks, like pi's.** The agent directory's file first, then every
  ancestor of cwd, root-most first, at most one file per directory, deduped by
  resolved path. Names: `AGENTS.override.md`, `AGENTS.md`, `AGENTS.MD`,
  `CLAUDE.md`, `CLAUDE.MD`. `CLAUDE.md` was not a name τ knew at all.
* **Worktree shadowing.** A worktree nested inside its own main repo suppresses
  the main repo's same-named file, so the walk does not load both.
* **Context files compose with `system_prompt`** instead of being switched off
  by it. Setting that key used to turn project context off with nothing saying
  so.
* **`--no-context-files` / `-nc`** turns discovery off. It is run-level, so a
  mid-session `/model` switch cannot hand the files back.
* **Every block names its source** — `<project_instructions path="…">` — so a
  prompt cannot carry instructions whose origin it does not state. The walk
  really does reach `/`, so a `CLAUDE.md` in `$HOME` is read on every run.
* **A found-but-unreadable file raises**, naming the path and the escape hatch.
  Decoding is strict. A prompt silently missing its project instructions looks
  exactly like a model ignoring them.
* **A real coding-agent prompt ships**, 520 characters, and
  `tau_default_config.json` no longer carries a `system_prompt` key. Setting
  one is now an override on purpose.

---

## Sessions

* **`--resume` works.** It had been rejected in both modes since it was added,
  with help text saying "TUI only" and an error blaming headless mode.
* **A session picker**, as a Textual modal: fuzzy filter over name and first
  and last message, `Tab` widens the scope from this directory to all, `Enter`
  picks, `Esc` leaves the filter before it leaves the dialog.
* **One action, three surfaces.** `--resume`, `/resume` and the command
  palette resolve through the same handler. `/resume <ref>` names a session
  directly, using the same path / id / id-prefix grammar as `--session`.
* **The sidebar starts closed.**
* **The `branchOf` lane tag is gone.** Three of its four consumers used it to
  answer "does this entry belong to the conversation being looked at?", which
  is ancestry from the cursor, not write provenance. The two agree for a
  sub-agent and disagree for a fork, so a three-way fork returned three
  mutually exclusive alternatives as one conversation and the picker counted
  four messages for a two-message session.

---

## Backends that are not OpenAI-shaped

Prompted by a field report against an OpenAI-compatible gateway.

* **A backend with no SSE is now reachable.** `Model.stream` (default true)
  plus a per-call option. `"stream": True` used to be hardcoded in the request
  body *and* reserved, so no config, `extra_body` or per-call option could
  reach it. The buffered path shares the same final-message builder, so it
  inherits the same guards.
* **The timeout is reachable.** `httpx.Timeout(300.0, connect=10.0)` was fixed
  at client construction with no override anywhere. Now a constructor argument
  and a per-call `request_timeout`. An unusable value raises rather than
  reverting to the default.
* **`context_window` and `max_tokens` are reachable.** They were hardcoded at
  128000 and 4096 for every model in existence, with no config key reaching
  either.
* **`python -m tau_llm.catalog`** fills a config entry from
  [models.dev](https://models.dev) — context window, max tokens, whether the
  model reasons, and its thinking-level map. Nothing is vendored; the tool
  fetches when asked and prints an entry for you to inspect. `--base-url` is
  required and never guessed.
* **`tau_llm.compat`** auto-detects two things from the endpoint URL:
  `max_tokens_field` and `supports_usage_in_streaming`. Detection is inverted
  from pi's on purpose — τ names only `api.openai.com` and `openai.azure.com`
  and leaves everyone else on the classic spelling, because an unrecognised
  endpoint is usually a local llama.cpp.

### Four failures that used to arrive unattributable

1. **A tool call with no name was built and executed.** A gateway that never
   populates `function.name` produced a nameless call, the tool lookup missed,
   and the run reported `Unknown tool: ` — blaming the model for the gateway's
   violation — then repeated it until `max_turns`. It now raises where the
   fault is still attributable, naming the call id, the model and the base URL,
   so an operator can identify which deployment behind a shared gateway is at
   fault.
2. **An error could carry no content at all.** `httpx.ReadTimeout`,
   `ConnectError` and `RemoteProtocolError` all stringify to `""`, so a dropped
   connection surfaced as `RuntimeError: Streaming error: ` with nothing after
   the colon. Messages now always lead with the exception type and append HTTP
   status and response body when present. A gateway sending `{"error": "..."}`
   as a string used to turn a readable 400 into an opaque transport error.
3. **A keepalive frame crashed the turn.** A proxy emitting `data: []` raised
   into the broad handler. It is skipped now — and both this skip and the
   pre-existing malformed-JSON skip log at debug level, because a turn that
   produced nothing with no record of why is the failure this whole set exists
   to remove.
4. **The timeout was unreachable** — see above.

---

## SDK

* `create_agent_session()` takes `no_tools: "all" | "builtin" | None`. The
  tri-state was reachable only through the CLI, and `"builtin"` had no
  behaviour inside `tau_agent_core` at all — an SDK caller who passed it got a
  display label.
* Passing `tools=` and `no_tools=` together now raises. They ask for opposite
  things and neither outranks the other at a call site. `tools=None` and
  `tools=[]` stay legal.

---

## JMFTS store

* **Stop guessing whether text fits.** A 1800-character constant decided
  whether content was embedded whole or chunked, against an embedder whose
  limit is 512 *tokens*. 1800 characters of base64 is about 1350 tokens, so
  dense content took the "short enough" path and the server refused it. The
  proxy is deleted: τ now embeds, and chunks only on the server's typed
  `text_too_long` refusal.
* Stop ordering root siblings.

---

## Packaging and release

* **Python 3.11, 3.12, 3.13 and 3.14 are measured, not claimed.** The matrix
  comment said 3.13 and 3.14 were "being measured separately" and nobody was.
  All four runs come back identical in clean containers.
* `dist/` is gitignored, so a local build no longer leaves an untracked
  directory.
* `publish.yml`'s header comment claimed one publisher identity covers all
  four projects. True in steady state, false for a project that does not exist
  yet — which is the only moment it matters.
* The missing-SDK error hint named `tau-llm[google]`, which does not resolve;
  τ imports as `tau_llm` and publishes as `ffwf-tau-llm`. Fixed, with a test
  that reads the four distribution names out of the `pyproject.toml` files and
  requires every shipped `pip install` hint to name one of them.

---

## Upgrade notes

**If you implement `SessionLog`.** `lane=` is gone from the Protocol and from
every store τ ships. `resolve_cursor` is "last entry wins" again. The contract
suite in `tau_agent_core.testing` has one test inverted: a store must not
reintroduce a cursor filter. `BranchView` keeps `lane` as an in-memory
render-routing key; that is a different thing with the same name.

**If you relied on the default system prompt.** `tau_default_config.json` no
longer ships `system_prompt`, and the default is now τ's own coding-agent
prompt rather than "You are a helpful assistant. Be concise and clear." Set
the key yourself to override.

**If you have a `CLAUDE.md` or `AGENTS.md` above your working directory.** It
is read now, on every run, all the way to `/`. That is the fix, but it is a
change in what reaches the model. `-nc` turns discovery off.

**If a gateway of yours omits `function.name`.** τ used to build and run the
nameless call, and burn up to `max_turns` doing it. It now raises on the first
one. This is louder, and it is the same defect it always was.

**If a model config names an `api` τ does not implement.** It used to be
served over the OpenAI completions wire regardless. It now raises.

**If you call `create_agent_session(tools=..., no_tools=...)`.** That now
raises.
