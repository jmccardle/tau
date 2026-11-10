# D26: Token counting

Status: accepted 2026-11-07; T1 built in stage 8E-6 (*As built*, in §10); T2 to T7 not built. Drafted
2026-11-05 from the owner's ruling of that day and revised the same day after two more rulings: (a)
the seven open questions of the first draft are accepted as recommended, and tokenizers,
calibration history and heuristics become optional, in layers (§0); (b) compaction must work
without any counter (§5). The four questions that remained were decided on 2026-11-07 (*Decisions
on the open questions*, at the end). D25 is reserved ("every frontend is a client of tau serve").

Amends D24 §1 (the built-ins and their order), §2 (compaction's estimate, cut and checks), §3
(normalized usage; `/usage` says how counts are made) and §9 (prune's notice); D15 (normalized
usage gains fields; a provider may render a request); D13 (`ModelConfig` gains `tokenizer` and
`chat_template`; `Price` gains `cache_write_1h`); D19 (a built-in may offer the counter; `tau ext
list` shows an extension's notes); D10 (a second distribution, `ffwf-tau-tokens`). Supersedes
D24's "0.15's calibrated per-class counter is dropped" and every use of characters over four.

## Context

τ has two kinds of count today: a reply's `meta.usage` (what the server billed), and characters
over four (`tau._lines.tokens_of`) for everything else, which is compaction's trigger after the
newest reply, its cut, its checks and notices, and prune's notice. 0.15 measured the rule at a
median of 0.772 of the truth, low on 84.3% of turns (`v0.15.0:docs/TOKEN-ACCOUNTING.md` §1), and
Claude 4.7 and later use about 30% more tokens for the same text.

Mixing the two units causes the two failures seen in testing:

1. **The trigger counts more than the fix measures.** The soft trigger reads the bill, which
   includes the tool definitions, the hidden tool prompt (286 tokens on Opus 5.5), the
   per-message overhead and the tokenizer's real rate. The cut and the checks count characters,
   which include none of them. The new line fits by characters, the next bill is over the soft
   limit again, and the end of every turn compacts.
2. **The fit check fails after the summary is paid for.** `_check_fits` runs after the
   summarizer has replied. When the head is small next to what the bill includes, the line is
   not shorter, so nothing is written, and the next turn's end pays and fails again.

0.15 had the pieces unjoined: a labelled count, a character-class regression fitted to Qwen3,
an HF tokenizer nothing set, and a calibrator that lived for one session. It never counted a
request before sending it.

**The owner's rulings (2026-11-05).** Not every user wants tokenizers, a calibration history or a
fitted heuristic, so they are optional. What every user gets is what the servers report, used
honestly. Both failures above are failures of unit, not of precision: they are fixed by measuring
the cut, the checks and the next bill in one unit, which reported counts alone can do (§5).

## Decision

### 0. The layers

| layer | what it adds | ships in | on by default | new dependencies | writes |
|---|---|---|---|---|---|
| **core** | `Count`, `Basis`, every usage fact (§2), proved spans (§3), the meter and the counter interface (§4), compaction's checks (§5) | `ffwf-tau` | always | none | nothing new |
| **`tokens`** built-in | the counter: calibration store, a heuristic fitted on proved spans, drift detection, the bound (§6) | `ffwf-tau` | yes; `tau ext disable tokens` | none | `$TAU_HOME/calibration/` |
| **`tokens` extra** | local tokenizers for the `tokens` extension: tiktoken, HF, chat templates, fingerprinting, downloads, `tau tokens fetch` (§7) | `ffwf-tau-tokens`, installed by `pip install 'ffwf-tau[tokens]'` | used when installed | none in Python; Rust crates only in the second wheel | `$TAU_HOME/tokenizers/` |
| **`token_calls`** built-in | the providers' count endpoints before a sample (§8) | `ffwf-tau` | no; `tau ext enable token_calls` | none | through `tokens` |

Each layer works without the ones below it. With no counter the only counts are `reported`, and
whatever is not reported yet is given a **ceiling**, never a guess (§1, §4).

### 1. Count and basis (core)

A **count** is `tau.Count(tokens, basis, bound)`. `upper` is `tokens + bound`.
**Basis** is one of these five, from strongest to weakest:

| basis | meaning | bound | needs |
|---|---|---|---|
| `reported` | the server's bill for this exact request, or the difference between two bills whose shared prefix is proved (§3) | 0; 8 where the newest reply's thinking is subtracted (§3.2) | core |
| `rendered` | τ rendered the request as the server will and counted it with the server's tokenizer, and a reported count has confirmed both | 0 | the extra |
| `calibrated` | text counted exactly by a confirmed tokenizer, plus fitted integer constants that have matched every bill since they were fitted | 0 | the extra |
| `heuristic` | a fitted estimate | a 95% bound on the error, from that model's residuals (§6.4) | `tokens` |
| `ceiling` | no estimate: `tokens` is 0 and `bound` is the node's UTF-8 bytes plus 16 per message-bearing node (§4) | the ceiling | core |

**Sums.** Tokens add and bounds add. The sum's basis is the weakest of its parts. A
`calibrated` or `rendered` source that misses a bill drops to `heuristic` until it is confirmed
again (§6.3). No count is `reported` before the request is sent.

**What each consumer does:**

- **The usage line** is unchanged (reported counts; `ctx` is the reply's `input + output` over
  the window).
- **`/usage`** ends with one line on how pre-send counts are made: `counts: reported only, with
  byte ceilings for what is not sent yet (no counter; tau ext enable tokens)`, or `counts: by
  the tokens extension (/tokens for each model)`. It reads `TreeHandle.counter` (§4).
- **Printing.** A pre-send count shown anywhere (notices, prune, the tree screen) is printed
  with `~` when heuristic and `≤` (its `upper`) when a ceiling.
- **Soft limit** (`post_step`): the committed reply's reported `input + output` (D24).
- **Hard limit** (`pre_sample`) and **fit checks** (§5): `upper` of the pre-send count.
- **Reserve** keeps its D24 meaning (room for the reply and the summary). The uncertainty margin
  is the bound, per request.

### 2. Usage facts (core)

Normalized `usage` gains these keys. Each is absent when the server does not report it.

| key | Anthropic | OpenAI chat | Gemini | llama.cpp |
|---|---|---|---|---|
| `reasoning` (part of `output`) | `output_tokens_details.thinking_tokens` | `completion_tokens_details.reasoning_tokens` | `thoughtsTokenCount` | — |
| `tool_prompt` (part of `input`) | — | — | `toolUsePromptTokenCount` | — |
| `written_1h` (part of `written`) | `cache_creation.ephemeral_1h_input_tokens` | — | — | — |
| `cached` | as today | as today | `cachedContentTokenCount` (as today) | `timings.cache_n` when `usage` lacks it |

- Gemini's `input` becomes `promptTokenCount + toolUsePromptTokenCount`, keeping "`input` counts
  every prompt token". One fixture with a built-in tool confirms that it is billed as input.
- `meta` also keeps Anthropic's `service_tier`, `speed`, `inference_geo` and `server_tool_use`
  counts, OpenAI's prediction tokens, and llama.cpp's `timings`; `usage_raw` is unchanged.
- **Drift markers** go in `meta`: the served model id, `system_fingerprint`, and llama.cpp's
  `build_info` and model size. The llama.cpp provider reads `/props` and `/v1/models` once per
  base_url per process for them (a 404 means "not llama.cpp" and is not an error), and keeps the
  server's `n_ctx` and `chat_template` with them.
- **`price.cache_write_1h`** (optional, defaulting to `cache_write`) prices `written_1h`.
  Pricing it as a five-minute write would be a silent error now that τ reads it.
- **`context_window` stays config only** (D24 ruling 9). `/usage` says when a server reports a
  different window (llama.cpp `n_ctx`; vLLM `max_model_len` through §8).
- **`stop_reason: model_context_window_exceeded`** (Anthropic) maps to a length stop.

### 3. Proved spans (core)

**3.1 Every family.** A **span** is the nodes added between two consecutive requests on one
walk: the reply of request n, then the inputs before request n+1. Its cost is
`input(n+1) − input(n)`. That cost is exact only if request n is a prefix of request n+1 as the
server sees it. A span is **proved**, and its count is `reported`, in one of three cases:

- **Anthropic:** `cache_read(n+1) == input(n)`. τ already puts a cache breakpoint on the last
  block of every request (`_anthropic_request.breakpoints`).
- **llama.cpp:** `timings.cache_n(n+1) == prompt_n(n) + cache_n(n)`.
- **OpenAI and Gemini:** the provider renders request n and finds it to be a byte prefix of
  request n+1, with the same model, system and tool set. Neither server's cache read is exact
  enough to prove it. This needs no tokenizer: a provider **may** implement `render(walk, model,
  tools) -> Rendered`, the request body it would send and which node each message came from.
  Pure, no network. The `tokens` extra counts the same rendering (§7).

Proved spans are read with `TreeHandle.spans(at=None) -> list[Span]`, computed in core from the
walk's replies. A copy inherits its original's span through `copied_from` when the copy contains
the whole span, so the first request after a compaction is mostly exact (old τ's "first request
after compaction unanchored" gap). Proved spans are also the heuristic's only training data
(§6.2); whole requests are never used, because their hidden overhead would bias the fit.

**3.2 The newest reply as replayed.** Its count is `output_tokens`, less `reasoning` when the
model keeps only the last turn's thinking and a new user turn begins. On Anthropic the bound is
8, since `thinking_tokens` "may differ … by a small number".

### 4. The meter and the counter interface (core)

**`Cursor.count(at=None) -> RequestCount`**, async, on `TreeHandle` like `model(at=)`: the walk
to `at` with the resolved model, its provider and the tools of that cursor's last sample (the
base offer if none). Compaction, prune and the tree screen all use it, with or without a
counter.

**`RequestCount`**: `total: Count`; `base: Count` and `base_nodes: int` (the walk's first nodes
and the request's overhead, counted as one); `nodes: Mapping[int, Count]` (each message-bearing
node after the base); `why: str | None` (why the total is weaker than `rendered`, e.g. `no
counter: tau ext enable tokens`, or `no tokenizer for local:qwen; pip install
'ffwf-tau[tokens]'`). The **overhead of a request** is what the server adds around the nodes:
tool definitions, the hidden tool prompt, per-message overhead, the generation prompt.

**The meter** is what core answers with no counter. For a walk whose replies carry reports:

- **The base** is the first request's reported `input`: the prefix, the overhead and whatever
  else that request held. It is an over-count of prefix plus overhead, in the safe direction.
- **Nodes in a proved span**: the span's `reported` cost, split over its nodes only as one lump
  that starts at a `User` or `Assistant` node (a cut never lands inside a span).
- **The newest reply**: §3.2.
- **Everything else** (nodes after the newest request, nodes in an unproved span): a `ceiling`.
- **`total`** is the newest request's reported `input`, plus its reply (§3.2), plus the ceilings
  after it. Before any reply, the whole walk is ceilings.

A **ceiling** rests on one assumption: every token of a byte-level tokenizer covers at least one
byte, and the 16 per node covers the template's special tokens. T2 checks it against every
recorded fixture: no proved span's reported cost may exceed its nodes' ceilings. Images, when τ
has image parts, get their published formulas instead (§7.3).

**The counter** is an object an extension offers as its `counter` attribute, as it offers
`tools`. At most one loaded extension may offer one; two is an `ExtensionLoadError` naming both,
as two tools with one name are (D19 §2). The **`Counter`** protocol has two methods:

- `count(job: CountJob) -> CounterAnswer`: synchronous, CPU only, no network; the driver runs it
  in a thread. `CountJob` holds the walk, the resolved model, the tools, the provider's
  `Rendered` (if it renders), and what core already knows: the base, the proved spans and the
  newest request's report. The answer gives a `Count` for each node core did not count from a
  report, optionally a finer `base` (prefix nodes plus overhead), and a `why`.
- `observe(observation: Observation) -> None`: the driver calls it after every reply with
  `usage`, whatever else is loaded. An **observation** is the walk's node ids, the usage, the
  drift markers, the tool set's hash, the walk's newest proved span (if any), and its `kind`
  (`reply`, or `count_call` from §8).

**Core keeps every report.** Core composes the `RequestCount`: a counter fills only what no
report covers, and core checks the result (each count the counter returned is for a node it was
asked about, the sum rule holds). A counter that breaks this fails the count with a
`DefinitionError` naming its extension.

**`TreeHandle.counter`** is the key of the extension offering the counter, or `None`.

### 5. Compaction under the new counts (core figures, either way)

- **One unit everywhere.** `tau._lines.characters`, `tokens_of` and `CHARS_PER_TOKEN` are
  deleted. The cut, the crowded and fit checks, the notice, prune's result and the tree screen's
  walk size all use `Cursor.count`. The soft limit stays the reported `input + output`; the hard
  limit is `count(walk).total.upper > hard`. The same formulas run with and without a counter;
  a counter only makes `upper` tighter.
- **Check before paying.** Before opening a summarizer, compaction requires both of these:

  ```
  upper(base) + upper(tail) + allowance < limit
  upper(head) > allowance
  ```

  The **summary allowance** is `min(reserve / 2, 8192)` tokens. If either fails, no summarizer
  runs. At the soft limit the clients are told once per cursor, as `_crowded_out` does today. At
  the hard limit the step fails. Because the base includes the overhead, failure 1 stops: a line
  that passes is under the soft limit by the same measure the next bill uses.
- **Choose the tail after the summary.** The summarizer sees the whole walk, so the summary
  covers the tail too. Once its length is known, the tail is the longest suffix, starting at a
  `User` or `Assistant` node, that fits `limit − base − summary`. `derived_from` names the last
  node not copied. The line can fail only if the summary alone does not fit, which the allowance
  already ruled out unless the summary ran past it. That case fails with both figures and is
  never retried automatically on the same walk. Failure 2's post-summary refusal is gone, not
  guarded.
- **The summary's count** is the summarizer reply's reported `output − reasoning`, plus the
  wrapper text's ceiling (or its count, with a counter). Copies inherit proved spans (§3.1).
- **The notice** prints both figures from one count: `compacted 182k → 23k tokens`, `~` or `≤`
  as §1 says, and `(reported counts only)` when there is no counter.

**Without a counter**, the soft limit and the fit check need nothing more: the trigger is the
bill, the tail is proved spans, the summary is its reported output. The hard limit has no
pre-send count, so something else must stand in for it:

- **Decided (Q1): the hard limit against a ceiling.** `upper` is the newest reported request,
  its reply, and the byte ceiling of what came after (§4). The ceiling is loose, about three to
  four times the real count on English and code, so a large tool result can trip the hard limit
  early, and compaction then runs mid-turn. That costs a summary sooner than needed, which is
  accepted; it never sends a request the window cannot hold, as long as the ceiling assumption
  holds (T2 checks it). Typical turns add little after the newest report, so the soft limit still
  does most of the work. The ceiling is a fallback of fallbacks, mainly against an oversized
  first message before any reported count exists: no stronger defence is built. After the first
  response, the provider's reported counts carry the session.
- **Rejected: compact on the server's context-length refusal.** The hard limit would be the
  server itself: a refused request triggers compaction, then the sample is retried once. It is
  never early. Against it: the summarizer sees the whole walk, so its own request is refused too
  unless `model` has a larger window; it reverses D24 ruling 8 ("no compaction on a provider's
  'prompt too long'"); each provider's refusal must be recognised, and LiteLLM and proxies word
  it differently; and every trip costs a failed round trip first.

**The order of estimates for content no report covers** (a tool result added since the last
reported count, the nodes of a line not yet sent), best first:

1. a **local tokenizer** that matches the model (the `tokens` extra, T4 to T6): `rendered` or
   `calibrated`;
2. the **calibrated or fingerprinted heuristic** the `tokens` built-in learns from repeated use
   of the same model (T3): `heuristic`, with a bound;
3. the **byte ceiling**, only when neither is available.

In a long conversation with a model τ has history with, the byte ceiling is almost never the
active estimate. In T1 only the third exists. The place the others plug in is the counter (§4): a
counter's `count` fills the nodes the meter left as ceilings, and `Counts.count` in
`tau._meter` is the one place that merges its answer, so adding the first two is adding a counter,
not changing compaction.

**With the `tokens` extension on**, compaction gains real pre-send checks: the same formulas,
with heuristic, calibrated or rendered counts in place of ceilings.

### 6. The `tokens` built-in (the counter)

`tau.Tokens`, key `tokens`, on by default, in the main wheel, no dependencies. It offers the
counter (§4). Its option is `download` (default `true`, §7.4); its command is `/tokens`, one line
per model on how it is counted: `anthropic:opus: calibrated from 41 replies; heuristic ±6% for new
nodes`, `local:qwen: rendered (hf:Qwen/Qwen3-8B@1a2b3c)`. Load order puts it after `token_calls`
and before `usage`; nothing depends on that, since the counter is reached through the driver, not
through hooks.

**When it is off**, nothing is written under `$TAU_HOME/calibration/`: the store is opened only by
this extension, at its first observation, and core's meter answers every count.

**Where the fitting lives: in `tau-core`.** The features of a `Body`, the fit, drift, CUSUM and
the bound are pure functions over frozen values: `features(body)`, `fold(record, observation)
-> record`, `predict(record, features) -> Count`. tau-py exposes them in `tau._core`, and
tau-store exposes `CalibrationStore` (lock, merge, atomic write). The extension is their only
caller. That is a clean call: the built-in is a class in the same package as `_core`, it passes
values in and gets values back, and Rust never calls into Python. Moving it into Python would
mean writing NNLS by hand (Python gains no dependency) and losing the property tests core
already runs. Layer and crate are different things here: this code is in the main wheel's
native module, but nothing in core's own behavior calls it.

**What it counts with, by family, when the extra is not installed:**

- **Up to the newest request, and proved spans:** `reported`, from core.
- **New nodes:** `heuristic`, from the fitted character-class weights (§6.1).
- **Tool overhead before the first observation:** on Anthropic, the published tool-use system
  prompt per model and `tool_choice` class (286 for Opus 5.5 auto/none; 675/804 for Opus 4.7),
  shipped as data, plus the heuristic on the schema text. A cache read that stops at the system
  breakpoint (the first request on a new line) then gives tools and system exactly.
- **OpenAI hosted, Gemini and open models** are `heuristic` the same way; the extra makes them
  `calibrated` or `rendered` (§7).

**6.1 What is solved.** For a request with nodes `m_i` and tool set `T`:

```
billed_input = Σ_i (text(m_i) + per_message[role(m_i)]) + request[T] + ε
```

- `text(m)` is the exact count when the extra supplies a confirmed tokenizer. Otherwise it is
  `w_role · x(m)`, where `x` is the node's feature vector: counts of lowercase, uppercase,
  digit, punctuation, whitespace and non-ASCII characters, and words.
- `per_message[role]` is an integer constant per role: system, user, assistant text, assistant
  with calls, results.
- `request[T]` is an integer constant per tool-set hash and `tool_choice` class. It covers the
  hidden prompt, the tool definitions and the generation prompt.
- **With a tokenizer**, `ε` must be 0. A proved span removes `request[T]`, so about three spans
  with different role mixes solve `per_message` exactly by integer elimination. The first
  request of each tool set then gives `request[T]`. Constants become `calibrated` after 3
  further exact matches.
- **Without a tokenizer**, `w_role` (7 non-negative weights per role) is fitted by non-negative
  least squares on proved spans, with no intercept. 0.15 showed that an intercept dominates
  short messages. The integer constants are fitted as above, from the residuals.
- **The shipped heuristic prior** covers Anthropic and o200k-style models, fitted by a script
  from the T2 fixtures and the owner's sessions (coefficients only, no text) and regenerated
  each release.

**6.2 The data kept** is sufficient statistics, not observations: for each role, `XᵀX` (7×7),
`Xᵀy` and `n`. Sums from two processes merge by adding, so two `tau serve` processes on one
model never lose each other's updates.

**6.3 Drift.** In an exact layer, one miss of any size marks the constants `stale`: counts drop
to `heuristic`, the constants are refitted and return after 3 exact matches, and the clients
are told once (`anthropic:opus: requests now cost 42 more tokens than calibrated;
recalibrating`). A miss is never averaged in. In the heuristic layer, stored sums decay by
λ = 0.995 per observation to follow slow change, and a two-sided CUSUM on the signed relative
residual catches a jump, after which the sums keep 5% of their weight as a prior. A changed
**drift marker** (§2, plus the template sha256 and tokenizer commit from the extra) resets the
constants and returns the tokenizer to `candidate`. `tau tokens reset MODEL` deletes a record;
`tau tokens show MODEL` prints it in words.

**6.4 The bound.** The record keeps the last 200 relative residuals of heuristic predictions on
proved spans. For a heuristic count of `p` tokens, `bound = ⌈p · q95 · (1 + 2/√n)⌉ + 2`, where
`q95` is the 95th percentile of their absolute values (the maximum while `n < 20`) and `+ 2`
covers tiny nodes. With no observations the prior has `q95 = 0.35`: 0.15's class counter spanned
0.81–1.10 per message on its own tokenizer, and tokenizers differ by up to 30%. The error is
noise, not bias (0.15: 0.91–1.05 over 32 messages), so it shrinks on longer spans as well as
with `n`. A heuristic count's `upper` is never above the node's ceiling: the smaller wins.

**6.5 The calibration record.** One file per (provider, base_url, model):
`$TAU_HOME/calibration/<provider>/<sha256(base_url + "\n" + model)[:16]>.json`. tau-store writes
it with a lock file, re-read, add, write-to-temp and rename, at mode 600.

```json
{"version": 1,
 "key": {"provider": "openai", "base_url": "http://localhost:8080/v1", "model": "qwen3"},
 "markers": {"served_model": "qwen3", "system_fingerprint": null,
             "build_info": "b7965-…", "template_sha256": "…"},
 "tokenizer": {"id": "hf:Qwen/Qwen3-8B@1a2b3c", "state": "confirmed",
               "source": "fingerprint", "agree": 41, "disagree": 0},
 "constants": {"state": "calibrated", "per_message": {"system": 4, "user": 5, "assistant": 6,
               "calls": 9, "results": 5}, "request": {"<toolset sha>|auto": 1210}, "matches": 38},
 "heuristic": {"roles": {"user": {"xtx": [[…]], "xty": […], "n": 140.2}, "…": {}},
               "residuals": [[0.031, 812], …], "cusum": [0.4, -0.1]},
 "observations": 450, "updated": "2026-11-05T14:02:11Z"}
```

`tokenizer` is `null` when the extra is not installed. `version` mismatch: a record from a newer
τ is left alone and not used (the count's `why` says so). An older one is upgraded when written.

### 7. Tokenizers: the `tokens` extra and `ffwf-tau-tokens`

**7.1 Packaging.** The distribution is `ffwf-tau` (D10), so the extra is installed with
`pip install 'ffwf-tau[tokens]'`. It pulls in a second wheel built from this repository:

| piece | where | what |
|---|---|---|
| `crates/tau-tokens` | new crate | `tokenizers` (`default-features = false`, `fancy-regex`, no `onig`, no `http`), `tiktoken-rs` (the `o200k_base` and `cl100k_base` tables embedded), `minijinja` + `minijinja-contrib`; fingerprint scoring. Bytes in, counts out: no IO, no PyO3. |
| `crates/tau-tokens-py` | new crate | PyO3 bindings, built as `tau_tokens._core`; counting releases the interpreter; pyclasses frozen, the memo behind a mutex. |
| `tokens/pyproject.toml` | new | maturin, `manifest-path = "../crates/tau-tokens-py/Cargo.toml"`, `module-name = "tau_tokens._core"`, `python-source = "python"`, `features = ["abi3"]`; distribution `ffwf-tau-tokens`. |
| `tokens/python/tau_tokens/` | new | `__init__.py` (`__version__`) and the hand-written `_core.pyi`, held by `mypy.stubtest` like `tau/_core.pyi`. |
| `pyproject.toml` | amended | `[project.optional-dependencies] tokens = ["ffwf-tau-tokens==0.16.0"]`. |

- **The base wheel does not change.** `tau-py` gains no dependency: the two native modules share
  no Rust types, and `tau_tokens` speaks plain Python values (strings, lists of dicts, ints) to
  the `tokens` extension. `check.sh` fails if `cargo tree -p tau-py -e normal` lists
  `tokenizers`, `tiktoken-rs` or `minijinja`. That is the guard; size is reported, not gated.
- **`ffwf-tau-tokens` depends on nothing**, not even `ffwf-tau`: it is useless alone, and a
  cycle between the two would gain nothing that §7.2 does not check.
- **Size** (estimates, measured in T4): `_core.abi3.so` is 5.6 MB today and stays so;
  `tau_tokens._core` is about 9–10 MB (`tokenizers` 2–3 MB, minijinja 1 MB, the two tables
  5.3 MB raw). The tables stay embedded; if the compressed tokens wheel passes 20 MB, they
  become downloads like HF files.
- **Public surface.** `tau_tokens` has one caller, the `tokens` extension, but its surface is a
  group in `docs/api/groups.toml`, so a change is a diff to review in `surface.txt`.
- **Not used:** `openai-harmony`, since gpt-oss's HF template already renders harmony and the
  crate appears to load its vocabulary itself at run time, which a no-IO crate cannot allow (to
  check in T4). `hf-chat-template` is not used either, but its byte-for-byte test corpus is
  worth adopting. Python gains no dependency (no `transformers`, no `tiktoken`).

**7.2 Discovery and lockstep.**

- **Discovery is `import tau_tokens`**, once, when the `tokens` extension is built. A
  `ModuleNotFoundError` whose `name` is `tau_tokens` means "not installed"; any other error fails
  the start (D19 §6). This is not a fallback: basis labels every count, and `why` says the
  tokenizers are missing, so a weaker count never passes for an exact one.
- **Lockstep.** Both wheels take their version from the Cargo workspace (`version.workspace =
  true`; maturin reads it). The extra pins `==` that version; `check.sh` fails when the pin is not
  the workspace version (PEP 440 form). At load, `tokens` compares `tau_tokens.__version__` with
  `tau.__version__`; a mismatch is an `ExtensionLoadError`: `ffwf-tau-tokens 0.16.1 does not
  match ffwf-tau 0.16.0: pip install 'ffwf-tau[tokens]==0.16.0'`. Both wheels are tagged,
  built and published together.
- **`tau ext list`** shows it as a **note** under the `tokens` row: `tokenizers: ffwf-tau-tokens
  0.16.0` or `tokenizers: not installed (pip install 'ffwf-tau[tokens]')`. Notes come from an
  optional extension method `notes() -> list[str]`, called after load; `ExtensionSource` gains
  `notes`, and `--json` carries them.
- **A model with `tokenizer` or `chat_template` set while the extra is not installed** is a
  `ConfigError` at start, raised by the `tokens` extension: `model local:qwen sets tokenizer
  hf:Qwen/Qwen3-8B, which needs the tokenizers: pip install 'ffwf-tau[tokens]'`. `tokenizer:
  none` needs nothing. With `tokens` off, the keys are unused, as `price` is with `usage` off;
  `tau config check` warns.

**7.3 What the extra counts, by family.**

- **OpenAI hosted:** text exact with tiktoken (`o200k_base` for gpt-4o, 4.1, o-series, `gpt-5*`;
  `cl100k_base` for gpt-4 and 3.5); per-message overhead and tool rendering are integer
  constants fitted per model (§6.1). The Cookbook values (3 per message, +1 per name, +3 for the
  reply, the function formula) are priors only: they cover older models. **No known encoding**
  (gpt-6 today) is not assumed `o200k`: `heuristic` until fingerprinting confirms one.
- **Gemini:** google-genai's map, shipped as data: Gemma 3 SentencePiece for gemini-2.x and
  3-preview, HF `google/gemma-4-E4B-it` for 3.5-flash, 3.1-flash-lite, 3.1-pro-preview. Models it
  omits (3.8-flash, 3.6-flash) start with both as candidates. Rendering of contents, function
  declarations and signatures is unpublished, so Gemini reaches `calibrated` at best.
  Fingerprinting compares `candidatesTokenCount` with visible text.
- **OpenAI-compatible open models:** `rendered` from HF `tokenizer.json` plus the chat template,
  rendered with minijinja and `minijinja-contrib`'s Python string methods, the way
  `apply_chat_template(..., tools=…, add_generation_prompt=True)` does, over the provider's
  `Rendered` messages and tools. Template sources, in order: the model's `chat_template`
  setting; llama.cpp `/props` `chat_template` (§2); the HF repo's `chat_template.jinja` or
  `tokenizer_config.json`. The first reported `input` must equal the rendered count. A mismatch
  with an exact tokenizer keeps the text exact and fits the difference as constants
  (`calibrated`, the usual Mistral case); a difference that is not constant is `heuristic`, and
  the clients are told once. gpt-oss goes this way too: its template renders harmony.
- **Fast recounts.** HF tokenizers split the input at special tokens first, so a rendered count
  is the sum of the counts of the pieces between them. Those pieces are memoized by hash, and a
  long conversation costs about one new message per recount. Templates that rewrite earlier
  turns (Qwen3 drops earlier reasoning) stay exact, because every recount renders the whole walk.
- **Images,** once τ has image parts (today's `Part` and `Body` have none): Anthropic
  `⌈w/28⌉·⌈h/28⌉` after the tier's resize (max edge 1,568 px and 1,568 tokens; 2,576 px and 4,784
  for Claude 4.7 and later), OpenAI's patch and tile formulas. Exact from pixel sizes. These are
  formulas, not tokenizers, so they will belong to core's meter when image parts land.

**7.4 Tokenizer identity and fingerprinting.**

The **tokenizer catalogue** is a data file in `tau_tokens` of about 15 base tokenizers:
`o200k_base`, `o200k_harmony`, `cl100k_base`, Llama 3 (128,256), Qwen (about 151k), Gemma 3/4
(262,144), Tekken, DeepSeek, GLM, Kimi, and others: for each, the id, where to get it (an
embedded tiktoken table, or an HF repo pinned to a commit), the vocabulary size, and model-id
patterns that give a prior.

| evidence | from | effect |
|---|---|---|
| model id pattern (`qwen3*`) | config, served model id | prior weight only |
| `n_vocab` | llama.cpp `/v1/models` `meta.n_vocab` | hard filter (allowing for padding to a multiple of 64 or 128) |
| logprob tokens | only if a user turns `logprobs` on | hard filter: every token must be in the vocabulary |
| output residual | `output − reasoning − encode(visible text)` per reply with no calls; llama.cpp returns raw reasoning, so it is included there | must be the same integer on every reply |
| input residual | proved span cost `− Σ encode(span text)` | must be the same per role mix |

Apply the hard filters, rank survivors by prior, fetch only the top two, score by residuals. A
candidate is **confirmed** when its residuals are identical over at least 3 clean pairs, one of
them at least 200 tokens; it is **dropped** as soon as two disagree. The choice is recorded in
the calibration record and told once (`local:qwen: identified tokenizer hf:Qwen/Qwen3-8B from 3
replies`). With none confirmed the model stays `heuristic` and `why` says so. Matching counts
identify a shared tokenization stack, not lineage (Chen, arXiv 2608.29930), which is all counting
needs.

**The setting.** `ModelConfig.tokenizer`, a model key (D13), overrides fingerprinting:
`tiktoken:o200k_base`, `hf:Qwen/Qwen3-8B[@REVISION]`, `file:/abs/tokenizer.json`, or `none` (no
local tokenizer, no fingerprinting). `chat_template` (`hf`, `server`, `file:PATH`) overrides the
template source. A set tokenizer is still checked against bills; on disagreement the clients are
told (`tokenizer hf:Qwen/Qwen3-8B does not match the server's counts on 3 of 3 replies`) and the
count drops to `heuristic`. It is never swapped for another.

**Files.** HF files are fetched with httpx (already a dependency) into
`$TAU_HOME/tokenizers/hf/<owner>/<repo>/<commit>/` (`tokenizer.json`, `tokenizer_config.json`,
`chat_template.jinja`, and `source.json` with the URL, commit and sha256 of each). Gated repos
use `HF_TOKEN`; a 401 or 403 names the repo. Fetching is automatic, on first need, in the
background, never delaying a sample; until it lands the count is weaker and `why` says so. It
tells huggingface.co which repos are used, never what is in the conversation;
`extensions.tokens.download = false` turns it off, and `tau tokens fetch MODEL` fetches ahead.
Offline-safe: embedded tables, cached files, `file:` paths, the calibration store. **A missing
tokenizer is reported:** a set tokenizer that cannot be loaded makes `Cursor.count` raise
`TokenizerUnavailable` (a `LookupError`), the hard limit fails the step with it, and `tau config
check` reports it. `tau tokens fetch` without the extra exits 1 with the `pip install` line.

### 8. Opt-in extension: count calls

`token_calls`, a built-in off by default (`tau ext enable token_calls`), calls the provider's
`count_call` before a sample whose interval straddles the hard limit (`tokens ≤ hard < upper`),
or before every sample with `when: "always"`. It hands each answer to the counter as an
observation of kind `count_call` (`TreeHandle.observe`), so it needs a counter: loaded without
one, it is an `ExtensionLoadError` (`token_calls needs a counter: tau ext enable tokens`).
Turning it on is an explicit act, so a conflict with it stops the start.

- **Anthropic `count_tokens`** (free, its own rate limit) is "an estimate" that may "include
  tokens added automatically … You are not billed for system-added tokens". It is fitted as
  `bill = count + offset`, one integer per model, `calibrated` once the offset holds 3 times.
- **OpenAI `POST /v1/responses/input_tokens`** (chat messages converted to Responses input) and
  **Gemini `countTokens`** (input only): fitted the same way.
- **llama.cpp `/apply-template` + `/tokenize`** and **vLLM `/tokenize`** (messages and tools):
  exact, `rendered` at once. vLLM's `max_model_len` reaches `/usage`'s window note (§2).

A failed call is told to the clients and the sample goes on with the local count. Count calls
stay off by default even for local servers: nothing should be required.

### 9. Placement

| crate or package | gains | IO |
|---|---|---|
| `tau-core` | `Count`, `Basis`, the sum rule, ceilings; proved spans; the meter and composing a `RequestCount`; for `tokens` only: features of a `Body`, the fit, drift, CUSUM, the bound | none; no new dependencies |
| `tau-store` | `CalibrationStore`: read, lock, merge, atomic write | `$TAU_HOME/calibration/`, only when `tokens` opens it |
| `tau-py` | bindings: `Count`, `RequestCount`, `Span`, the calibration functions, `CalibrationStore` | — |
| `tau-tokens`, `tau-tokens-py` | §7.1 | none in Rust |
| Python, `tau` | `render` on providers; `Cursor.count`, `spans`, `counter`, `observe` on handles; the driver's `observe` call; `Tokens`, `TokenCalls`; `tau tokens`; HF downloads (httpx) | `$TAU_HOME/tokenizers/` |

### 10. Staging

Each stage is one implementer. The owner records fixtures with `scripts/record_provider.py` (new,
T2), which saves request and SSE bodies under `tests/fixtures/providers/<family>/<case>/`,
redacted of keys: Anthropic Opus 5.5 (three turns with tools and thinking, plus one after a
6-minute gap, a cache miss); OpenAI gpt-5.x (three turns with tools); Gemini 3.x (three turns with
tools and thoughts, one with a built-in tool); llama.cpp + Qwen3 (three turns, plus `/props` and
`/v1/models`); vLLM gpt-oss (three turns). T1–T3 need only the main wheel.

**T1, compaction on reported counts (core, main wheel only).**
- **Builds:** `Count`, `Basis` (all five) and the sum rule; ceilings; Anthropic proved spans
  from today's `cached`; the meter; `RequestCount` and `Cursor.count`; the `Counter` protocol,
  `CountJob`, `TreeHandle.counter` and the one-counter rule, with nothing filling it yet; every
  §5 change with the recommended hard limit; `≤` and `(reported counts only)` in notices; prune
  and the tree screen on `Cursor.count`; `/usage`'s counts line.
- **Tests:**
  - Harness, failure 1: a `FakeProvider` whose scripted `usage` adds a 6000-token hidden
    overhead and reports cache reads. One soft compaction writes a line whose next reported count
    is under the soft limit, so the next turn's end samples no summary. When base and tail alone
    exceed it, no summary is sampled and the clients are told once.
  - Harness, failure 2: a head smaller than the allowance; nothing is sampled.
  - Harness: the tail is shortened after a long summary, and the line is under the limit.
  - Harness: the hard limit trips on a large tool result by its ceiling, with no counter.
  - Harness: a test-only extension offering a counter fills only unreported nodes; one that
    returns a count for a reported node fails the count; two counters fail the load.
  - Rust: the sum rule and the meter as properties.

**T1 as built (stage 8E-6).** Everything above is built, with these differences:

- **Where it lives.** The meter, `Count`, `RequestCount` and the counter interface are Python
  (`tau._counts`, `tau._meter`), not `tau-core`. Every input they read is a reply's `meta.usage`,
  which core keeps as opaque text and Python already parses (`tau._usage.reading`); writing the
  meter in Rust would have meant parsing it twice. T3's fit, drift and bound are numeric and have
  property tests, and they move to `tau-core` as §6 says; `Count` and the meter stay where the
  usage is read.
- **Proved spans** are proved by the arithmetic: the second request reports `cached` equal to the
  first's `input` (and `input` is positive, and no other reply sits between them). Anthropic is
  the provider that satisfies it, since τ puts a cache breakpoint on the last block of every
  request. A provider whose `cached` never equals the previous `input` simply has no proved spans,
  and its nodes are ceilings. llama.cpp and the byte-prefix proof for OpenAI and Gemini are T2.
- **`Counter` has `count` only.** `observe` and `Observation` have no caller until the `tokens`
  built-in (T3), so they land with it. `CountJob` carries the walk, the model, the tools, the
  base, the nodes a report covers and the nodes it does not (`unreported`); `CounterAnswer`
  carries a count for some of those and a `why`. A count for any other node is a
  `DefinitionError` naming the extension. A finer `base` in the answer lands with T3.
- **`Cursor.count`** is `TreeHandle.count(at=None)`, with `at` defaulting as `model(at=)` does.
- **Nodes inside a lump have no count of their own** in `RequestCount.nodes`, and a tail may start
  only at a `User` or `Assistant` node that has one. Where a report-less line is a compaction's copies, a
  copy that holds a whole lump takes the lump's count from its original's walk, so the first
  request after a compaction is mostly exact.
- **Compaction.** The pre-send checks are the first formula of §5 and the summary's size
  (`upper(head) > allowance`) is not checked on its own: when the first formula holds and the
  trigger fired, the head is larger than the allowance by construction, so the post-summary check
  that the summary is shorter than the head it replaces (by the same node counts) is what is
  left. `/compact` runs the same checks against the hard limit. A `/compact` with no `keep` set
  keeps at most half of what the line costs, so asking for it summarizes something; a head that
  holds nothing but an earlier summary is "nothing to compact yet". A summary that was paid for
  and does not fit is told once and not asked for again at the soft limit while the limits are
  the same; the hard limit and `/compact` always try. When the turn in progress began with a
  user message that the summary would replace, that message is copied verbatim after the summary.
  A `reserve` of less than 2 tokens leaves no allowance and is a `ConfigError`.
- **The base is the prefix and the overhead only** (§4 said the first request's whole `input`,
  which holds the first message too and made a long first message count as overhead). It is the
  first bill less the body nodes before the first reply: a whole lump comes out at its cost, any
  other node at `lowest_tokens` of its text, its bytes over 8 after every run of one repeated
  character is cut to four (the tokenizers have single tokens for long runs of spaces or dashes,
  which no ratio covers). A base made so is `heuristic`. **No exact route exists**, and none was
  found: every bill holds the prefix, the overhead, the head and the tail together, so telling the
  prefix from a node needs a lower bound on that node, and a lump gives one only for nodes a
  second request proved. (The summarizer's bill is no exception: it is the whole branch and the
  prompt.) This is the one place a ratio of bytes to tokens is used, only to take a node out of a
  bill, never to count one; text that is long runs of something else (`ab` over and over) may
  break it. On a line with bills, a base too small only skews the plan and shows in the next
  bill. The first request after a compaction is the exception: it is counted with the old line's
  base, so it can go out under-counted by up to about 1/16 of the bytes it counts by ceiling
  (reviewed: ~300 tokens for 22 kB of `abab…`). That eats into the room kept for the reply; the
  request it counts by bytes is under the hard limit, so it does not pass the window. T3's
  tokenizer replaces it.
- **A line with no report yet carries the old line's base.** The line a compaction writes has no
  bill (a prune's line too, unless its first rewritten node is the first user message: that line
  inherits nothing and is counted by ceiling until its first bill), but its prefix and overhead are the old line's: the meter reads the base
  of the line its first derived node came from. A summary is derived from the summarizer's reply
  (not, as in D24, from the last node it replaced) so that its count is that reply's reported
  `output` and the ceiling of its wrapper, in the meter and in the plan alike; the notice and the
  request that goes out are counted once, after the line is written, and the hard limit counts
  again before it sends. A line with no report and no old line has the ceiling of its tool
  definitions as its base, with the ceilings of the prefix nodes.
- **The summarizer is checked too.** Its request is the branch (without a message not yet
  answered, which the tail keeps verbatim) and the prompt, with room left for the summary, and
  it must fit the window of the model that writes it. If the whole branch does not, the
  summarizer is opened at the end of the head (the tail, longer by the user messages after the
  head's last reply, is then kept whole); if the head does not fit either, the step fails and
  says to set `extensions.compaction.model` to a model with a larger window.
- **The hard limit never sends what it cannot show fits**, short of the base gap above. A ceiling is loose, and a request that
  is over only by it (a 30,000-character paste into a 32k model) is refused, as D26 §5 says:
  compaction runs first, and if it cannot make room, or has nothing to compact, the step fails
  with the figures, and when part of the request is counted only by its bytes the message says
  so and names the ways past it (shorten or split it, a larger `context_window`, or
  `compaction.auto=false`). Early compaction from a ceiling is accepted.
- **Known gap: reasoning.** `Reading` does not carry `reasoning` until T2, so a summary's count is
  its whole `output`, thinking included. That over-counts what the new line carries, which is the
  safe direction; no count is subtracted by a reply's `output` (only a lump's cost, or the byte
  bound above, comes out of a bill), so an `output` that includes thinking cannot make the base too
  small. T2 subtracts `reasoning` as §3.2 says.
- **`keep`** is the most tokens copied (the longest tail within it, or the newest turn whole), not
  at least that many, because a cut may land only at the start of a lump. Measured by the nodes'
  upper counts, so on a provider without proved spans (every node a ceiling) 20000 is about 5000
  to 6000 real tokens.
- **The soft latch** (a summary paid for that did not fit is not asked for again) holds while the
  limits are the same and the failure's node is on the cursor's line; moving to another branch
  releases it. A notice is told once per cursor, limit and part that is too big (prefix, newest
  node, tail, summary, shorter), so a different failure is told too.
- **The meter keeps the last 16 measured walks**, not every one, and no walk of its own: a long
  `tau serve` asks about a new tip every turn.
- **`Provenance` is `Basis`** (`tau.Basis`, and `Count.basis`): core's `Provenance` is the pair
  `copied_from`/`derived_from`, and one word named two glossary concepts.
- **Prune** compares the two lines by their ceilings, since a line that is not sent has no other
  count, and prints both as `≤`.
- **The tree screen** prints the walk's request count as `Count`s do (`show`).
- **`/usage`'s counts line** reads `counts: reported only, with byte ceilings for what is not
  sent yet`, or `counts: reported, and by the NAME extension for what is not sent yet`; there is
  no `tau ext enable tokens` to name until T3.

**T2, usage facts and every family's spans (core).**
- **Builds:** §2's keys, markers, `timings`, `/props`, `price.cache_write_1h`, the window note;
  llama.cpp spans; `render` and byte-prefix spans for OpenAI and Gemini; the recording script.
- **Tests:** each fixture replayed through its provider gives normalized `usage` (golden);
  malformed counts still drop `usage`; the Anthropic fixture yields proved spans and the
  cache-miss turn none; **no proved span costs more than its ceilings**, on every fixture.

**T3, the `tokens` built-in (main wheel only).**
- **Builds:** `Tokens` as the counter; features, NNLS, constants, drift, the bound, the shipped
  prior; `CalibrationStore`; `observe`; `/tokens`; `tau tokens show|reset`.
- **Tests:**
  - Rust: synthetic spans from known weights recover those weights; merge is commutative;
    CUSUM fires on a step change and not on noise.
  - Two processes updating one record lose nothing (`tests/core`).
  - Harness: a `FakeProvider` whose usage comes from a test-side deterministic counter. The
    bound shrinks over 30 replies, and a changed `system_fingerprint` resets it.
  - Harness: with `load: {tokens: false}`, a session with replies leaves `$TAU_HOME/calibration/`
    absent, and compaction still works on reported counts.
  - Harness: the hard limit with a large heuristic tool result, where `upper` crosses and
    `tokens` does not.

**T4, `ffwf-tau-tokens`.**
- **Builds:** §7.1–§7.2: the two crates, `tokens/pyproject.toml`, `tau_tokens` and its `.pyi`;
  tiktoken, HF and minijinja; split-and-memo counting; `ModelConfig.tokenizer` and
  `chat_template`; the ConfigError; notes in `tau ext list`; downloads; `tau tokens fetch`;
  `TokenizerUnavailable`; the lockstep check; the size and build-time report.
- **CI** (one Linux x86_64 runner, `.forgejo/workflows/ci.yml`):
  - `gate`: `maturin develop` builds both modules; `check.sh` adds stubtest for
    `tau_tokens/_core.pyi`, the lockstep check and the `cargo tree` guard, and runs the whole
    suite with the extra installed.
  - `wheels`: builds both wheels for each Python in the matrix (cp312-abi3, cp314t), then tests twice from
    `/tmp`: a clean venv with `ffwf-tau` alone runs the suite with `-m "not tokenizers"` plus the
    tests that need the extra absent (the ConfigError, the note, `fetch` exiting 1); a second
    venv with both wheels runs everything. A test marked `tokenizers` errors, never skips, when
    `tau_tokens` cannot be imported.
- **Tests:**
  - Rendered strings and token ids for Qwen3, Llama 3, DeepSeek, Gemma 4 and gpt-oss, produced
    once offline with `transformers.apply_chat_template` and checked in. The render must match
    byte for byte and the ids exactly.
  - Offline from cache. A missing configured tokenizer raises. A gated repo's 403 names the repo.
  - A version mismatch between the two modules fails the load with the `pip install` line.
  - `tests/core/test_threads.py` covers `tau_tokens` on 3.14t: importing it leaves the GIL off.

**T4 as built, in part (branch `tokens-crates`).** The half of T4 that stands alone is built: the
two crates, the second wheel, `tau_tokens` and its stub, tiktoken, Hugging Face tokenizers and
chat templates, split-and-memo counting, the lockstep check, the dependency guard, the size and
build-time report. The catalogue, the identity strings and the fingerprint scoring of T6 are built
too, as far as they are data and arithmetic. **Not built**, because each needs the `tokens`
extension (T3) or the config: `ModelConfig.tokenizer` and `chat_template`, the `ConfigError`, the
notes in `tau ext list`, downloads and `tau tokens fetch`, `TokenizerUnavailable`, and the check of
`tau_tokens.__version__` against `tau.__version__` at load. What those will call is the group
*Tokenizers* of `docs/api/surface.txt`.

*What "exact" was held to.* `scripts/tokens_goldens.py generate` asks the reference
implementations (`transformers` 5.22.2, `tokenizers` 0.23.3, `tiktoken` 0.14.0, Jinja2 3.1.6) and
writes what they print to `tests/fixtures/tokens/`; the tests compare `tau_tokens` with that.
`transformers` is what vLLM runs. Nothing here was compared with a live llama.cpp or vLLM: the
llama.cpp server at hand was unreachable when this was built.

| tokenizer (repository at its pinned commit) | 23 texts: ids equal | 20 requests: rendered text and its ids equal | refused by both |
|---|---|---|---|
| `o200k_base`, `cl100k_base` (against `tiktoken`) | 23 of 23 each | no template | |
| Qwen3 (`Qwen/Qwen3-8B`) | 23 | 20 | 0 |
| Llama 3 (`unsloth/Llama-3.2-3B-Instruct`) | 23 | 19 | 1 |
| Tekken (`mistralai/Mistral-Nemo-Instruct-2407`) | 23 | 16 | 4 |
| Gemma 3 (`unsloth/gemma-3-4b-it`) | 23 | 12 | 8 |
| Gemma 4 (`google/gemma-4-E4B-it`) | 23 | 19 | 1 |
| gpt-oss (`openai/gpt-oss-20b`) | 23 | 18 | 2 |
| DeepSeek (`deepseek-ai/DeepSeek-V3.1`) | 23 | 14 | 6 |
| Phi-4-mini (`microsoft/Phi-4-mini-instruct`) | 23 | 18 | 2 |
| GLM (`zai-org/GLM-4.6`) | 23 | 19 | 1 |
| template only: Qwen3-Coder, Llama 3.3, DeepSeek-R1, phi-4, Hermes 4, Granite 4, MiniMax-M2, Kimi K2 | not fetched | 147 of 160 rendered equal | 13 |

No mismatch is left open: every text and every request is either equal or refused by both
(`tests/tokens/test_artifacts.py`; the same in Rust, `crates/tau-tokens/tests/artifacts.rs`). A
request is "refused by both" when the model's template raises on it, as Gemma 3's does on a tool
result and Mistral's on two user messages in a row. For the nine real tokenizers the two
references agree with each other on every text, so `tokenizer.json` alone is what a server's
`transformers` tokenizer counts with. Beyond the 23 texts, `scripts/tokens_goldens.py sweep`
encoded every Unicode code point (1,112,064 of them, each beside a letter, a space, a digit and a
line break) and 5,000 random mixtures with all eleven tokenizers in both implementations: 0 of
819,544 texts differ. That is the answer to whether the pure-Rust regex engine agrees with the
Oniguruma that the reference uses. Six templates written for the tests (`tests/fixtures/tokens/
templates`) are compared whole, 120 renderings, byte for byte.

*Differences from the text above, and why.*

- **`tokenizers` 0.23.2**, the newest release that is not a candidate; its feature is
  `fancy-regex` as §7.1 says. (1.0.0-rc.4 renames it `regex`.) `esaxx_fast` (C++) and
  `progressbar` are off too. Nothing the wheel is built from needs a C compiler or a system
  library, which `scripts/check_tokens_wheel.py` checks; both crates pass `cargo check` for
  Windows (MSVC and GNU), macOS (arm64 and x86_64) and Linux aarch64.
- **The memo is keyed by the text of a piece, not by its hash**, so a count read from it cannot be
  another text's. A piece is what the tokenizer itself splits off between added tokens (special
  or not), after its own normalization, and the memo also records whether the piece starts the
  text: a Metaspace tokenizer that marks only the first word counts the same piece differently
  there. The count runs the library's own steps on the library's own pieces, and a property test
  holds it to `len(encode(text))` on four kinds of tokenizer. It keeps at most 16 MiB of text.
  A recount still normalizes and splits the whole text: for a 737 kB request (400 messages,
  164,697 tokens of Qwen3), rendering takes 3 ms, the first count 166 ms, the count after one
  more message 33 ms, and a full `encode` 240 ms.
- **A request crosses as JSON text.** `tau_tokens` is given lists and dicts and writes them with
  Python's own `json.dumps`, so the template sees keys in the order given and `1.0` as a float. A
  whole number beyond 64 bits is refused, because nothing downstream can tell it from a float.
- **The template engine is MiniJinja with Python's behavior put in** where a rendering differed:
  what `{{ value }}` prints (`None`, `True`, `1e-05`, `{'a': 1}`, `('a', 1)`), `tojson` as
  `json.dumps` with all four options, the `sequence`, `iterable` and `number` tests, `strip`,
  `title` and `splitlines`, `trim`, `escape`, tuples from `items` and `dictsort`. It **refuses**
  (a `TemplateError`, no count) what it does not have: unary `+`, `'%s' % x`, the `center`,
  `wordcount`, `truncate` and `xmlattr` filters, `pprint` and `attr` (removed here because
  MiniJinja's differ), `cycler`, `joiner`, the `callable` test, `str.partition` and `zfill`, the
  `{% generation %}` tag, and `int` of text that is not a number (Jinja2 gives 0). Two
  **differences are silent** and not closed: a template that reads a dict's method without
  calling it (`x.items is defined`: true in Python for any dict, here only when the dict has a
  key `items`), and the title case of a few digraph letters (`ǆ`). None of the seventeen real
  templates does either. This is why §1 makes a `rendered` count wait for a reported count to
  confirm it; the crate labels nothing.
- **`strftime_now` prints the time the caller passes** (`render(..., now=datetime)`); the crate
  reads no clock, and a template that reads one without it is refused. The formatting is C's in
  the "C" locale, written here; week numbers and a few rarer directives are refused. A server's
  date is its own: T5 has to pass the date the server will print.
- **Reasoning goes in under both names.** `transformers` 5 hands a message's reasoning to the
  template as `reasoning_content` and as `thinking` whichever the caller used; so does this. A
  server on an older `transformers` does not, so T5 should send both names itself.
- **Tool-call arguments must be what the server passes.** DeepSeek's template fails on arguments
  given as a dict; Gemma 4's, GLM's, MiniMax's and Qwen3-Coder's fail on a string. Both libraries
  agree in every case; which one a server passes is the provider's `Rendered` to state.
- **Special tokens are the seven named ones of `tokenizer_config.json`** (`bos_token` to
  `mask_token`). `special_tokens_map.json` and a tokenizer class's own defaults are not read.
  `continue_final_message` is not offered. Nothing is added around a text (no BOS): llama.cpp
  adds one where the GGUF says so and the template does not print it, a constant for T5.
- **The catalogue has 11 tokenizers, not about 15.** Kimi K2 has no `tokenizer.json` (it ships a
  tiktoken-format file), and Mistral's newer models ship only `tekken.json`; both need a loader
  that is not built. Where a model's own repository needs a login (Llama 3, Gemma 3) the
  catalogue names an open copy of the same tokenizer. `o200k_harmony` is the Hugging Face file
  of gpt-oss, since rendering needs its special tokens. Gemma 3 is the Hugging Face tokenizer
  (262,145 ids), where google-genai 3.0.0 uses a SentencePiece file of 262,144; its model map
  was re-read and is in the catalogue's patterns, as is tiktoken's.
- **A vocabulary size is a filter with a list, not only padding.** A served model reports its
  embedding size, which is larger than its tokenizer by no fixed rule: Qwen3 151,936 for 151,669
  ids, gpt-oss 201,088 for 200,019, DeepSeek 129,280 for 128,815, GLM 151,552 for 151,365. The
  catalogue lists these (`served`), read from each repository's `config.json` and held to it by
  a test, beside the padding to 64 or 128 that §7.4 names.
- **Mistral's `tokenizer.json` is known not to be what Mistral's own tokenizer does**
  (`transformers` says so when it loads it: `fix_mistral_regex`). It equals the reference here,
  and a server that counts with `mistral-common` will differ: the `calibrated` case of §7.3.
- **`openai-harmony` was not examined**: gpt-oss's own template renders 18 of 20 requests as
  `transformers` does and both refuse the other two, so it is not needed. `hf-chat-template`'s
  corpus was not adopted; the goldens here come from `transformers` itself.
- **Sizes.** `tau_tokens/_core.abi3.so` is 12.2 MB, not the 9–10 MB estimated; the wheel is
  5.1 MB compressed, a quarter of the 20 MB at which the tables would become downloads. The main
  module is unchanged at 5.6 MB, and `Cargo.lock` gained 69 packages without changing the version
  of any the main wheel uses.
- **Tests.** The real tokenizer files (170 MB) are fetched, not checked in:
  `scripts/tokens_goldens.py fetch` downloads each at its pinned commit and checks its sha256. A
  test that reads them is skipped with that command as its reason when they are absent, and
  fails when `TAU_TOKENS_FILES=require`, which CI's `gate` sets after fetching. The goldens are
  hashes for the real models and whole text for the templates written here. The free-threading
  test is `tests/tokens/test_tokenizer.py`, not `tests/core/test_threads.py`, so that the main
  wheel's tests do not import the second wheel.
- **`tau_tokens` is a group of `docs/api/groups.toml`** (`package = "tau_tokens"`), as §7.1 says;
  `scripts/api_docs.py` learned that a group may belong to another package, and
  `scripts/stub_docs.py` holds both stubs' docstrings to the built modules.

**T5, rendered and calibrated counts.**
- **Builds:** §7.3 in the `tokens` counter: open models (`rendered`, confirmation), OpenAI
  hosted (`calibrated`) and Gemini (`calibrated`).
- **Tests:** llama.cpp, OpenAI and Gemini fixtures. The second request's count equals its
  reported input (`rendered`, or `calibrated` after 3). A Mistral-style template mismatch ends
  `calibrated` with a constant offset.

**T6, fingerprinting.**
- **Builds:** the catalogue, filters, scoring, confirm and drop, notices.
- **Tests:** Rust: residual series from tokenizer A score A over B within 3 replies. Python: the
  llama.cpp fixture identifies Qwen with no setting. A set tokenizer that disagrees drops to
  `heuristic` with a notice.

**T7, `token_calls`.**
- **Builds:** the five count calls, the `count + offset` fit, `TreeHandle.observe`.
- **Tests:** recorded count-call fixtures (the owner records them), the straddle rule through the
  Harness, and the load error without a counter.

Image formulas land with image parts, which is not part of this ADR's stages.

### 11. Glossary entries proposed

- **count** (`tau.Count`: `tokens`, `basis`, `bound`, `upper`) and **basis**
  (`tau.Basis`: `reported`, `rendered`, `calibrated`, `heuristic`, `ceiling`), with the sum
  rule; **ceiling** (a node's UTF-8 bytes plus 16 per message-bearing node).
- **request count** (`tau.RequestCount`: `total`, `base`, `base_nodes`, `nodes`, `why`);
  **base (of a request count)**; **overhead (of a request)**.
- **`Cursor.count`**; **meter** (core's answer with no counter).
- **counter** (an extension's `counter` attribute; at most one); **`Counter`** (`count`,
  `observe`); **`CountJob`**; **`CounterAnswer`**; **`TreeHandle.counter`**.
- **span** (between two requests on a walk); **proved span**; **`Span`**;
  **`TreeHandle.spans`**.
- **`Rendered`**; **`Provider.render`**.
- **observation** (`Observation`, kinds `reply` and `count_call`); **`TreeHandle.observe`**.
- **`Tokens`** (key `tokens`, on by default, option `download`, command `/tokens`).
- **calibration record** (§6.5) and **calibration store** (`CalibrationStore`,
  `$TAU_HOME/calibration/`); **per-message constant**; **request constant**; **stale**;
  **drift marker**; **heuristic prior**; **bound**.
- **`tokens` extra** (`ffwf-tau[tokens]`); **`ffwf-tau-tokens`** and **`tau_tokens`** (the
  tokenizer wheel and its module); **lockstep**.
- **notes (of an extension)** (`notes()`, `ExtensionSource.notes`).
- **tokenizer** (the `ModelConfig.tokenizer` setting and its four forms); **`chat_template`**
  (model key); **tokenizer catalogue**; **candidate**, **confirmed**, **dropped**;
  **fingerprinting**; **tokenizer cache** (`$TAU_HOME/tokenizers/`); **`TokenizerUnavailable`**.
- **`tau tokens`** (`show`, `reset`, `fetch`).
- **`TokenCalls`** (key `token_calls`, off by default, option `when`); **`count_call`**
  (provider).
- **summary allowance** (compaction).
- Usage keys **`reasoning`**, **`tool_prompt`** and **`written_1h`**; **`cache_write_1h`**
  (`Price`). **meta** is amended with the drift markers.
- Amended: **off by default** (`agents`, `token_calls`); **built-in** (adds `tokens`,
  `token_calls`); **Usage** (the counts line); **Compaction** (the estimate is `Cursor.count`).

**Banned words.** Every name above passes rule 3: "per-message overhead" names what chat
templates add around a message, "node" is used throughout, and "head" appears only as
compaction's head (D24).

## Alternatives

| option | why not |
|---|---|
| Keep characters over four, with a larger reserve | It is biased low (median 0.77), and averaging cannot fix a bias. The owner ruled it out. |
| Require count calls before every sample | The owner ruled it out. It doubles round trips, and some servers have no count endpoint. |
| One global chars-per-token ratio per model | Digits cost about 7.6 times what whitespace costs (0.15 §3), so a single ratio is wrong for either prose or JSON. |
| Calibrate on whole requests (0.15's `a·messages + r·payload`) | The hidden overhead is folded into the slope. It needed clamps after it produced −85 tokens per message. Proved spans have no overhead. |
| Everything in the main wheel (the first draft) | The owner ruled that tokenizers are optional. It would also nearly triple the native module for users who never run a local model. |
| Tokenizers as a cargo feature of `tau-py` | Two builds of one distribution with the same name; pip cannot choose between them. A second wheel is what an extra can install. |
| Tokenizers in Python (`tokenizers`, `tiktoken` wheels) behind the extra | Two more dependencies, slower through the binding (0.15 measured 1.5 Mchar/s), and no free-threaded guarantee we hold ourselves. |
| Fitting in Python inside `tokens` | NNLS by hand, and the property tests move out of core. The call from the extension into `_core` is plain values (§6). |
| Compaction refuses to run without a counter | The owner ruled it out: reported counts are enough for the soft limit and the fit check, and only the hard limit needs a stand-in (§5). |
| Compact on the server's context-length refusal | §5: the summarizer is refused too, it reverses D24 ruling 8, and every trip costs a failed request. Rejected (Q1). |
| Fall back to a similar tokenizer silently | The owner ruled it out. A wrong tokenizer looks exact. A weaker basis with a stated reason is honest. |
| Store calibration in the session | It would have to be learned again for every session, and it describes the model, not the conversation. |

## Facts checked today

Read today: Anthropic's tool-prompt table, `usage` fields, `count_tokens` terms, thinking
replay and image formula; Gemini's `UsageMetadata`; this repository's `pyproject.toml`, Cargo
workspace and CI workflow. From the research notes only, to re-check when built: tiktoken's model
map, google-genai's Gemma map, vLLM `/tokenize`, OpenAI `input_tokens`, llama.cpp `timings` and
`/props`, whether `openai-harmony` fetches, and that every family's tokenizer gives at least one
byte per token (T2's ceiling test).

## Decisions on the open questions

The first draft's seven were accepted as recommended (2026-11-05) and are folded in: the shipped
prior (§6.1), automatic downloads with `download: false` (§7.4), the fixed summary allowance
(§5), `price.cache_write_1h` (§2), `context_window` from config only (§2), count calls off for
local servers (§8), and Gemini's `input` (§2). The last four were decided on 2026-11-07.

1. **The hard limit without a counter: the byte ceiling** (UTF-8 bytes plus 16 tokens per
   message node, §4), not the server's refusal. It is a fallback of fallbacks, mainly against an
   oversized first message before any reported count exists; nothing stronger is built. After
   the first response, the provider's reported counts are relied on. The ceiling also applies to
   content not yet counted, for instance a big tool result added since the last reported count;
   compaction starting early there is accepted. For such content the order of estimates is: a
   local tokenizer match, then the calibrated or fingerprinted heuristic from repeated use of the
   same model (the `tokens` built-in), then the byte ceiling only when neither is available (§5).
2. **The names.** `pip install 'ffwf-tau[tokens]'` installs a second wheel, `ffwf-tau-tokens`,
   imported as `tau_tokens`. Every message about a missing package prints the exact install
   command.
3. **The ceiling's 16 tokens per node.** 16 for T1; T2 checks it against the recorded fixtures
   and raises it if a fixture's per-message overhead comes close.
4. **`tokenizer` set with `tokens` off.** A `tau config check` warning, not an error, and the
   other estimates are used. This is one case of the general rule that config for an extension
   that is not loaded is a warning, which the config-hardening work (stage 8E-6, `fix-config`)
   implements; this ADR only records it.
