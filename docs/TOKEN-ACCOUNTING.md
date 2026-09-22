# Token accounting

**Built 2026-09-18.** τ estimated every token count as `len(text) / 4` and
returned a bare `int`, so a display and a compaction trigger could not tell a
measurement from a guess. Measured against 1018 turn-pairs of τ's own recorded
traffic, that heuristic under-counted on **84.3%** of turn transitions, median
ratio 0.772 — and under-counting is the direction that overflows a window.
`tau_llm/tokens.py` replaces it with three routes and a label; automatic
compaction gains a second threshold so a turn can no longer blow the window
partway through and lose itself.

Every number below was measured. §7 is the live end-to-end run; §8 names the
script behind each offline number.

## 1. What was wrong

Four things, in increasing order of how much they cost.

**`estimate_tokens` was `math.ceil(chars / 4)`.** Ported from pi
(`compaction.ts:266`), whose docstring says the heuristic "is conservative
(overestimates tokens)". Against τ's own traffic it does the opposite: median
0.772, IQR 0.265 wide, under on 84.3% of transitions. `tau_agent_core/usage.py`
already forbade this shape by name — *"Fail-Early forbids the `len(text) // 4`
style approximation that fabricates a count which looks real"* — and the
compaction module did it anyway.

**A count had no provenance.** Four consumers read the same bare `int`:
`should_compact`, the TUI header, `ExtensionContext.get_context_usage`, and
`estimate_span_tokens`. None could ask whether the number was billed, tokenized
or invented.

**A zero `usage` report was treated as an anchor.** `estimate_context_tokens`
takes the newest assistant turn that reported usage as exact truth and counts
only what follows. `_get_assistant_usage` accepted any non-empty dict, and a
`Usage()` of all zeros is a non-empty dict — so one turn where the provider
reported nothing priced *every message before it at zero*. Found by a test, not
by a user, which is luck.

**A usage anchor outlived the compaction that invalidated it.** The same
anchoring, from the other side: an assistant turn billed before a compaction was
billed against the conversation that compaction then folded away. Measured live
on 2026-09-18, the first request after a mid-turn compaction read **22,588
tokens against a billed 5,734** — ratio 3.94, back over the hard limit that had
just fired. That is a compaction loop, not a display fault.

**Position does not catch it**, which cost two wrong fixes before the right one.
A compaction keeps a recent tail, and that tail's assistant turn sits *after* the
summary in the flattened path while having been billed *before* the fold. Walking
backwards and stopping at the summary finds the stale anchor first, every time.
The boundary has to be wall-clock: `AgentSession` records when it compacted and
passes it as `usage_valid_after`, and a reloaded session recovers the same
boundary from the newest compaction entry's timestamp. The summary stop is kept
as well, for the order where the summary really is newer than every usage on the
path. With the boundary in, the same request reads 5,233 against 5,706.

**A single overlong turn could not be compacted at all.** `find_cut_point`
walks backwards accumulating tokens until it passes `keep_recent_tokens`, then
takes the first cut point at or after where it stopped. When the newest message
alone passes the target there is no such cut point, and the fallback was
`cut_points[0]` — the *earliest*, which keeps the whole conversation and makes
`prepare_compaction` return `None`. With the shipped `keep_recent_tokens=20000`,
any conversation whose last message exceeds 20k tokens compacted nothing.

## 2. Three routes and a label

`TokenCount(tokens, source, exact, includes_template)` is the return type.
`source` is one of `usage`, `tokenizer`, `classes`, `calibrated`, `none`.

| Route | Class | Exact | Includes template |
|---|---|---|---|
| The provider's own report | — (read off `Usage`) | yes | yes |
| The model's `tokenizer.json` | `TokenizerCounter` | yes, of the text | no |
| Character composition | `CharClassCounter` | no | no |
| Either, plus the online fit | `ContextCalibrator` | no | yes |

`exact` and `includes_template` are separate because they fail separately. A
tokenizer is exact about the text it encodes and knows nothing about the chat
template wrapped around it on the wire; the class estimator is inexact about
both. Adding two counts degrades to the weaker of the pair in each field.

`Model.tokenizer` — a `tokenizer.json` path or a HuggingFace repo id — selects
route 2. Unset gets route 3. Set and unloadable **raises**: an operator who
named a tokenizer and got an estimate would have a typo hidden behind a
plausible number. `counter_for(model, fallback=True)` is the one exception, for
a display that must render something rather than kill a UI thread.

## 3. Why character classes and not characters

A single chars-per-token ratio cannot hold both prose and code. Fitted on 2198
real τ messages against Qwen3's tokenizer, non-negative least squares:

| Feature | tokens each | ratio to whitespace |
|---|---:|---:|
| digit | 1.235 | 7.6× |
| punctuation | 0.696 | 4.3× |
| uppercase | 0.281 | 1.7× |
| whitespace | 0.162 | 1.0× |
| lowercase | 0.121 | 0.7× |
| word (per word) | 0.362 | — |

(`assistant` row; `toolResult` and `default` differ in the third decimal and in
the non-ASCII weight.) A digit costs about 7.6 times what a space costs, so any
fixed ratio is wrong about at least one of prose and JSON.

Held out on 18 sessions the fit never saw:

| | within 10%, per message | within 10%, 32-message span |
|---|---:|---:|
| `chars / 4` | 26.0% | 28.7% |
| character classes, per role | **77.1%** | **95.0%** |

The second column is the one that matters, and it matters because the error is
**noise rather than bias**. Over spans of k consecutive messages the class
estimator's spread shrinks roughly as 1/√k — p5–p95 goes 0.807–1.100 at k=1 to
0.906–1.046 at k=32. `chars/4` on the same spans does not move at all: median
0.821 at k=1 and 0.814 at k=8. Averaging fixes noise and cannot touch bias.

Two fit choices earned their place. **No intercept**: a pooled fit chose +15.56
tokens per message as a constant, and the p10 held-out message is 58 characters,
so on short turns the constant *was* the estimate — user messages came out at
median ratio 1.595. **Per role**: splitting `assistant` / `toolResult` /
`default` took user messages from 4.4% to 82.4% within 10%. Tool results remain
the weakest arm at 60.0%; they are the most heterogeneous text in a transcript.

The coefficients are a **labelled prior, not a claim about any tokenizer but
Qwen3's**. The durable part is the shape — a digit costing several times a
space. The calibrator is what corrects the scale for a different endpoint.

## 4. The calibrator

`billed ≈ a·messages + r·payload`, solved online by least squares over five
running sums, per session. `a` absorbs per-message chat-template framing and the
amortized system prompt and tool schemas; `r` absorbs the payload's scale error.
An observation is recorded once per assistant turn that reported usage: the
request that produced it held every message before it, so that index is
`messages` and the provider's own
`prompt_tokens` (`total_tokens - output_tokens`) is the target.

Both parameters are constrained. `a ≥ 0` because framing cannot refund tokens;
`r ∈ [0.8, 1.5]` because a payload needing to be doubled is measuring something
else. **An unconstrained solve on real traffic produced −85.2 tokens/message and
a payload scale of 0.716 on one model** — the bounds are there because that
happened, not in case it might.

Measured cumulatively against billed `prompt_tokens` over 808 turns in 36
sessions (compaction-affected turns excluded — see §6):

| context | uncalibrated tokenizer | tokenizer + calibration |
|---|---:|---:|
| 5–10k | 0.749 | 1.113 |
| 10–20k | 0.827 | **1.039** |
| 20–40k | 0.880 | **1.028** |
| 40–80k | 0.930 | **1.027** |
| 80k+ | 1.055 | **1.016** |

The uncalibrated arms all share one shape — they start near 0.35 and climb —
which is the signature of a *missing constant*: the system prompt and the tool
schemas are in the bill and in no local count. Nothing about a longer session
fixes them, because they never learn the offset. The calibrated arms enter the
1.00–1.05 band at 10k and stay. From 20k on their p5 never falls below 1.00, so
the estimate over-reports; a number that is never low is one a compaction
trigger can be built on, because the failure direction is an early compaction
rather than a refused request.

Calibrated character classes and a calibrated real tokenizer land on top of each
other — 1.016 against 1.019 at 80k+. The calibration does the work and does not
much care which payload it scales.

**Warm-up is unpriced.** Below three observed turns there is no fit, and the
first fitted point lands at 1.113.

## 5. Two limits

`CompactionSettings` now resolves through `compaction_limits(window, settings)`
into a `CompactionLimits(soft, hard, window)` with `soft ≤ hard ≤ window`
enforced, raising otherwise.

- **hard** = `window - reserve_tokens`. Checked at **every agent-loop turn
  boundary**, inside the turn. Crossing it compacts immediately and the loop
  continues on the compacted context.
- **soft** = `floor(hard * soft_limit_ratio)`, default 0.8. Checked at the **end
  of the user turn**, where the check already lived. Crossing it compacts before
  the next turn starts. A ratio rather than a fixed gap below `hard`, because the
  gap has to scale: 20000 tokens below the hard limit is a sensible warning
  distance on a 128k window and below zero on a 1000-token one.

`hard_limit_tokens` and `soft_limit_tokens` pin either outright, which is how a
test asks for a 20k ceiling on a 172k model.

The difference between them is when the caller may wait. A soft crossing can
wait, because the turn is over. A hard crossing cannot: the next request in the
same turn is the one the provider refuses. Twenty tool calls into a search, the
old behaviour lost the turn.

`try_compaction_limits` returns `None` for a window smaller than its own
margins. That is a real state rather than a swallowed error — such a model
cannot be auto-compacted at any threshold — and it is what the pre-existing
`context_window <= reserve_tokens` guard already said.

## 6. Mid-turn compaction persists first

The loop takes a `MidTurnCompactor` callback and holds no compaction policy of
its own; the window, the settings and the session log all live a layer up. It is
offered exactly one point per turn — after the tool results are appended and the
`turn_end` hooks have run, before the next request. Anywhere else would either
drop a tool result its own tool call still refers to, or rewrite a list the loop
is midway through reading.

When it fires, **the turn so far is persisted before the cut**, and the
compaction goes through the ordinary tree path. A summary that existed only in
the loop's local list would be a model input the session tree never saw, which
is the invariant τ is built on. The cost is that the loop's remaining messages
must not be written twice, which `_TurnPersistence` tracks: persistence used to
be a single write at the tail, so "have the inputs been written" was answerable
by where you were in the function, and it no longer is.

A consequence worth stating on its own, because it is not only a property of
this code: **any implementation that sums the active path reports roughly twice
the real context after a compaction**, because the path keeps every message
while the bill does not. Five of 36 sessions in the measurement set showed a
>20% drop in billed input — those are compactions — and including their 263
subsequent turns moved one bucket's ratio from 1.515 to 2.022. τ does not have
this bug, because `estimate_context_tokens` anchors on the provider rather than
summing; a reimplementation easily would.

## 7. Measured live, end to end

`local-llm` — llama.cpp `b1637-9c7a7553`, `Qwen3.8-27B-absolute-heresy-Q4_K_M`,
`n_ctx 172032` — with the hard limit pinned to 20,000 and the soft to 16,000 so
the run finishes in a minute. One prompt asking the model to read eight
`tau_agent_core` files with the `read` tool, one call at a time. No tokenizer
configured, so every pre-compaction reading is the character-class estimator on
an endpoint its coefficients were not fitted against.

```
  [   0.0s] request  n=  2 predicted=      0
  [   5.3s] request  n=  4 predicted=  1,690
  [   6.9s] request  n=  6 predicted=  4,474
  [   9.3s] request  n=  8 predicted=  5,908
  [  11.1s] request  n= 10 predicted=  9,565
  [  13.7s] request  n= 12 predicted= 11,757
  [  15.9s] request  n= 14 predicted= 18,070
  [  38.7s] *** MID-TURN COMPACTION ***
  [  38.7s] request  n=  4 predicted=  5,233
  [  42.1s] request  n=  6 predicted= 12,290
```

The turn did not end at 15.9s. It crossed the hard limit, compacted, and the
next request went out on a four-message context. The compaction entry records
`coveredEntries=14 coveredTokens=18070 tokensBefore=22908`, and its summary opens
`No prior history.` followed by a split-turn prefix summary — the `is_split_turn`
path, because there was no prior *turn* to summarize, only the inside of this one.
That is the case §1's cut-point defect made impossible.

Each prediction against the billed `prompt_tokens` of the reply it preceded:

| # | messages | predicted | billed | ratio | source |
|---:|---:|---:|---:|---:|---|
| 1 | 4 | 1,690 | 1,747 | 0.967 | classes |
| 2 | 6 | 4,474 | 4,663 | 0.959 | classes |
| 3 | 8 | 5,908 | 6,040 | 0.978 | classes |
| 4 | 10 | 9,565 | 9,740 | 0.982 | classes |
| 5 | 12 | 11,757 | 11,882 | 0.989 | classes |
| 6 | 14 | 18,070 | 18,456 | 0.979 | classes |
| 7 | 4 | 5,233 | 5,706 | 0.917 | calibrated, **after the fold** |
| 8 | 6 | 12,290 | 13,059 | 0.941 | calibrated |

Median 0.967, and nothing outside 0.917–0.989. Row 7 is the one that took three
attempts: it read **22,588 against 5,734** until the anchor boundary went in.

Two things this run says that the offline numbers cannot. The estimator reads
**low** — 2 to 8 percent — which is the direction that needs the reserve, and is
why §5's margin is not decoration. And the first request of every turn reads 0,
because nothing that turn has produced is persisted yet and the loop has not
reached a boundary; see §9.

## 8. How each number was produced

| Claim | Script |
|---|---|
| `chars/4` median 0.772, under on 84.3% | `est_bias.py` over `~/.tau/sessions` |
| tokenizer vs `chars/4` on the same pairs | `exact_vs_est.py` |
| online causal calibration, constrained | `calibrate.py` |
| feature-set comparison, coefficients | `features.py` |
| span accumulation, by-session split, throughput | `spans.py`, `spans2.py` |
| cumulative curve against billed `prompt_tokens` | `curve.py`, `curve2.py` |

Those scripts were session scratchpads, not repo tooling; they are named so a
reader knows what a claim rests on, not so it can be re-run unchanged. The
`tokenizers` throughput figure — 1.5 Mchar/s against 11.9 Mchar/s for the class
counter, an 8× gap — is from `spans.py` §C on 12.9M characters.

## 9. Not in this pass

- **The first request of a turn is measured against the persisted path.** The
  loop publishes its live context to `AgentSession.context_estimate` at each turn
  boundary, so the reading is right from the second request on; before that it
  omits the turn's own user message. It reads 0 on a fresh session's first
  request, which is visibly wrong in a header.
- **No head renders the provenance yet.** `ContextUsageEstimate.count` carries
  `source` and `exact`; the TUI header, `tau -p` and the RPC `get_session_stats`
  payload all still show a bare number. The estimate is better; the display does
  not yet say how.
- **Calibration does not persist.** It lives on the `AgentSession` and dies with
  it, so a resumed session pays the three-turn warm-up again. The state is
  already a serializable five-sum `CalibrationState` for exactly this reason.
- **No tokenizer ships and none is fetched.** `Model.tokenizer` is read and
  nothing sets it; there is no download, no cache, and no default for any model.
  Route 2 is available and unused.
- **The image price is an assumption.** `ESTIMATED_IMAGE_TOKENS = 1200` is the
  old `4800 chars / 4` carried over. Providers tile images differently and τ has
  measured none of them. A count containing an image reports `exact=False` even
  behind a real tokenizer, which is the honest part.
- **One tokenizer, one provider family.** Every coefficient here is Qwen3
  against llama.cpp. The shape should carry; the values will not.
- **`tiktoken` is not a fourth route**, and no vendor `count_tokens` endpoint is
  called.
