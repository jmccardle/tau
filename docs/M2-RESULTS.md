# M2 — Generative-Agents scoring over JMFTS: determination

Run 2026-07-17 on the midlife GPU-dev instance. Retrieval-only, no LLM, no judge.
Corpora: STALE (400 instances × 50 sessions = 20,000 docs) and LoCoMo (10 conversations,
5,463 turns, 1,531 scorable questions). Scripts: `experiments/m2/`.

## Verdict

**The gate passes, but not in the shape the plan assumed.** Recency is not a retrieval
improvement. It is a **trade**, and the two legs disagree by design:

| | STALE (staleness) | LoCoMo (recall) |
|---|---|---|
| baseline (α=β=0) | `new>old` **0.11** | R@1 **0.362** |
| best recency (α=1, t½=365d) | `new>old` **0.91** | R@1 **0.206** |
| importance (β=1) | not tested | R@1 **0.243** |
| effect | catastrophic inversion **fixed** | recall **degraded, both terms** |

Every recency setting and every importance setting hurt LoCoMo recall. No setting of
either term helped both corpora.

**So: ship recency as a per-query policy, never as a global default.** A single global α
cannot serve both query classes. This is the load-bearing finding, and it was not on the
plan's map — §7 asked whether recency beats plain vector search "on multi-session recall
**and** on the STALE probe", implicitly expecting one answer. The answer is two answers.

## Determination (b) — staleness: PASS, emphatically

Baseline retrieval is not merely unhelpful on implicit conflict, it is **actively
wrong**: with α=0 the stale session outranks the fresh one ~89–91% of the time on dim1/dim2
(`new>old` = 0.110 / 0.092). That is worse than chance, and it is mechanical — dim1/dim2
restate the stale premise ("does the user *still live in Seattle*?"), so the M_old session
is the best lexical and semantic match for the query. Retrieval faithfully returns the
thing the query looks like, which is exactly the wrong thing. dim3 (implicit downstream
planning, which never restates the premise) starts far higher at 0.435 — consistent with
that reading.

At α=1.0 / t½=365d: `new>old` rises to **0.91 / 0.95 / 0.92**, `new@1` roughly quadruples
(0.100 → 0.407 on dim1), and — the part that matters — `new@5` is **unchanged from
baseline** (0.845 → 0.845, 0.900 → 0.897, 0.787 → 0.815). The stale session is demoted
(mean rank 1.25 → 9.29) without the fresh one being retrieved any less often.

This is the Mem0 0.40→0.84 lever reproducing on our substrate, larger because our baseline
is worse.

### The caveat that matters most

**`new>old` is partly tautological for a recency term.** M_new is *by construction* later
than M_old, so any recency boost mechanically improves it. Read alone, this metric cannot
fail. Three things keep the result honest:

1. The **baseline of 0.11** is a real, independent measurement — it says plain retrieval
   prefers stale evidence 9 times in 10. That number owes nothing to the recency term.
2. **`new@5` is the guard**, and it holds flat at α=1/365d. The term is not buying rank
   inversion by sacrificing relevance.
3. The metric **is** gameable, and we caught it doing so: at α=5/t½=180d, `new>old` hits
   0.985–0.993 — the best in the grid — while `new@5` **collapses** from 0.845 to 0.585.
   That configuration is close to sorting by date and discarding the query. **The highest
   `new>old` in the grid is not the best configuration**, which is precisely why the grid
   was run instead of a single tuned number.

Half-life dominates α. t½=30d destroys everything (`new@5` 0.547) because the corpus spans
years — decay must match the corpus timescale, not a default.

## Determination (a) — multi-session recall: FAIL, on both terms

**Neither term beats plain vector search. Both strictly hurt, monotonically in their
weight, and they stack.** Baseline wins at every k.

```
 alpha  half-life   beta     n       R@1       R@5      R@10      R@20
   0.0          -    0.0  1531     0.362     0.602     0.691     0.771   <- baseline wins
   0.0          -    0.5  1531     0.290     0.601     0.689     0.771
   0.0          -    1.0  1531     0.243     0.593     0.689     0.770   <- importance only
   0.0          -    5.0  1531     0.109     0.538     0.672     0.770
   1.0       365d    0.0  1531     0.206     0.509     0.669     0.763   <- best recency
   1.0       365d    1.0  1531     0.162     0.485     0.661     0.762   <- both: damage adds
   1.0        30d    0.0  1531     0.075     0.243     0.381     0.672   <- worst
```

**Recency**: the expected and desirable shape. LoCoMo asks about specific *past* events
whose evidence is a deliberately old turn, so a term promoting recent evidence should hurt.
**If recency had helped here too, the metric would be suspect.** The disagreement is the
validation.

**Importance**: damages R@1 (0.362 → 0.243 at β=1) while barely touching R@20 (0.771 →
0.770). That shape is diagnostic — it is displacement, not degradation. Importance is a
**query-independent prior**: it cannot know which document answers the question, so all it
can do is push its favourites into the top slot, evicting whatever the query actually
matched. Deeper ranks are unaffected because the same documents are still there, just
reordered near the head.

### Why importance first looked inert — and why that explanation was WRONG

The first scorer produced a **degenerate distribution**: of 5,882 turns, **5,655 (96.1%)
scored 1**, mean 1.08. The obvious reading was granularity — "a raw dialogue turn genuinely
is mundane, and Generative Agents scores *observations*, not turns."

**That reading was wrong.** A 2×2 probe (rubric × thinking, 200 seeded turns,
`rubric_probe.py`) isolated the real cause:

| condition | mean | % at 1 | distinct | **entropy** |
|---|---|---|---|---|
| GA poignancy / nothink | 1.12 | 95.5% | 3 | **0.29 bits** |
| GA poignancy / **think** | 3.06 | 10.0% | **10** | **2.45 bits** |
| retrievability / nothink | 1.23 | 94.5% | 5 | 0.40 bits |
| retrievability / think | 3.23 | 41.5% | 8 | 2.32 bits |

Entropy is the number that matters: a term that assigns everything the same value cannot
reorder anything, whatever that value is.

- **Rubric: +0.11 bits.** Noise. Rewriting the scale for retrievability instead of
  poignancy changed essentially nothing.
- **Thinking: +2.17 bits.** The whole effect. Same turns, same prompt — reasoning finds
  real spread across all ten values where one-shot decoding lazily anchors to 1.

**The collapse was an artifact of the `enable_thinking:false` workaround**, which existed
only because a grammar binds the model's first token and on a reasoning model that token is
inside `<think>`. This is a measured cost for the llama.cpp lazy-grammar-thinking work
(`docs/lazy-grammar-thinking-pr.md`): the workaround was not an ergonomic tax, it was
**destroying the signal outright**. Anything that scores with a grammar and no thinking
should be re-examined.

Cost of the fix: **1,503 completion tokens vs 3**. Re-scoring all 5,882 turns with thinking
is ~7.2h at `-np 2`, which is why the observation layer below was the efficient route.

### Importance at observation granularity: best signal, worst recall

RAPTOR over the 10 conversations → 121 cluster summaries, scored with GA + thinking, then
propagated to member turns (a turn inherits its observation's importance; max where nested;
99.9% coverage). This is the best-conditioned importance signal in the experiment:

| scorer | entropy | % at 1 |
|---|---|---|
| turn-level, nothink | 0.29 bits | 96.1% |
| turn-level, think | 2.45 bits | 10.0% |
| **RAPTOR summary, think** | **2.77 bits** | **0.8%** |

**And it made recall worse than the inert scorer did:**

| β=1 importance | R@1 | R@5 | R@10 | R@20 |
|---|---|---|---|---|
| baseline (none) | 0.362 | 0.602 | 0.691 | 0.771 |
| turn-level (inert) | 0.243 | 0.593 | 0.689 | **0.770** |
| **RAPTOR (sharp)** | **0.205** | **0.472** | **0.629** | **0.753** |

The inert scorer could only displace the *head* — 96% neutral factors left deep ranks
untouched (R@20 flat at 0.770). The sharp scorer perturbs the *whole* ranking, because now
~99% of documents carry a confident non-neutral factor. **Better signal, worse retrieval,
monotonically.**

### The conclusion this forces — and the challenge it survived

Importance fails because it is a **query-independent prior**, and recall is a query-dependent
task. Sharpening a prior that does not know what was asked just lets it reorder more
confidently in a direction uncorrelated with the question.

**This was challenged hard, and the challenge changed how we know it — not the conclusion.**
The GA poignancy rubric might simply have been the *wrong* importance rubric: it scores AUC
**0.551** — chance — as a classifier of LoCoMo evidence-ness (`validity.py`), so it never knew
which turns were evidence, and the damage above is what boosting a random prior looks like.
That AUC-0.551 measurement is also the **random-importance control** the earlier draft was
missing, obtained by measurement rather than assumed.

A second rubric — "how much durable, specific, factual information" — scores AUC **0.840** on
the same turns. It genuinely knows which turns are evidence, and it had never been through the
recall ablation. If any importance signal were going to help, it was this one.

**It also hurt** (pilot on conv-26, 149 questions): R@5 0.608 → 0.570 → 0.482 as β rises,
monotone, delta **−0.038**. On the conversation where importance best predicts evidence-ness,
boosting it still degrades recall.

The reason is now precise rather than hand-waved. AUC measures a **marginal** property — "is
this turn evidence for *some* question." Retrieval needs a **conditional** one — "is this turn
evidence for *this* query." A term can be 0.84 on the first and worthless on the second, and
it inflicts damage whenever a high-importance off-topic turn outranks the low-importance turn
that answers the actual question. A query-independent multiplier competes with the query
signal; it cannot add to it, however well-calibrated it is in aggregate.

**So importance is a write-time signal, not a read-time ranking term** — now demonstrated
across two rubrics spanning AUC 0.55 to 0.84, not argued from one. It belongs in retention and
consolidation (what to keep, roll up, forget), where a marginal evidence-ness prior is exactly
right and no query exists to be conditional on. Generative Agents places it in read-time
scoring, but never evaluates recall of specific evidence — so this is a limit on transferring
the term to a retrieval task, not a contradiction of the paper.

### Method notes surfaced by these controls

- **The scorer is nondeterministic at temperature 0.** Re-scoring an identical configuration
  agrees with itself at only ρ 0.83–0.92 / 70–82% exact (`budget_sweep.py`, the cells where
  the budget does not bind). This is MoE routing under continuous batching at `-np 2`, and it
  sets the floor: no rating-agreement number below ~0.9 should be read as a real difference.
- **`enable_thinking:false` was destroying the signal, not taxing it** (0.29 → 2.45 bits).
  Every grammar-constrained call that disables thinking needs re-auditing — jmfts's own
  `summarization_disable_thinking` first, which generated the RAPTOR summaries used here.
- **Per-request `reasoning_budget` is silently ignored on this build**; the budget is a server
  flag (`--reasoning-budget N`, which forces the end-of-thinking tag so a truncated thought
  still yields a grammar-bound answer). A budget of 500 does not reduce spread — it biases the
  score *high* (mean 4.46 vs 2.90), because truncation cuts the model off before it reasons its
  way down to a low rating.
- **The recency trade is a per-*query* effect, confirmed** (`breakdown.py`): within STALE the
  recency lift varies by 0.373 across query dims but only 0.047 across instance types, and the
  legible cause is lexical — dim1/dim2 restate the stale premise (so retrieval is pulled toward
  M_old and recency has the most to fix), dim3 does not. That is observable at query time, so a
  per-query α selector conditions on the right variable.

## Consequences for B-SEP

- **E-Affect's "state beats trait" bet survives** — it is a state-query bet, and state
  queries are exactly where recency won. But it must carry its own α, scoped to affect
  reads. A global recency sort would quietly degrade every episodic recall in the system.
- The **per-query policy** requirement is new work that M2 surfaced: something must decide
  "is this a state question or an episodic question?" before choosing α. That decision is
  Tau policy. It is not in any current plan item.

## Threshold, restated against the plan

> *"if hybrid+recency doesn't lift staleness resolution (Mem0 saw 0.40→0.84 by read-time
> recency sort), the scoring weights or the validity model need work before any B-SEP
> memory extension ships."*

Staleness resolution lifted 0.11 → 0.91. **The threshold is met and the gate opens** — with
the amendment that what ships is a scoped, per-query term, not a global one.

## The α-selector — the per-query policy, built and it works (2026-07-17)

M2 closed with "a per-query α selector does not exist and is now on the critical path."
It exists now. Spec: `docs/ALPHA-SELECTOR-SPEC.md`; code: `experiments/m2/alpha_selector.py`;
this is Tau policy (JMFTS already ships the `recency_weight` knob — choosing it per query
is the new part). A **zero-shot, grammar-constrained classifier** (`root ::= [0-2]`,
thinking-ON under `--reasoning-budget 2000`), given only the query text and blind to which
corpus it came from, routes recency per query: class 0 → no recency, 1 → α=0.5, 2 → α=1
(t½=365d). It classified all 2,731 queries (STALE 1,200 + LoCoMo 1,531) and the retrieval
eval reused the exact `hybrid_search` seam; the whole thing is DB-read-only.

| STALE ordinal (n=1200) | new>old | new@1 | new@5 | | LoCoMo (n=1531) | R@1 | R@5 | R@10 | R@20 |
|---|---|---|---|---|---|---|---|---|---|
| baseline (α=0) | 0.212 | 0.175 | 0.844 | | baseline | 0.362 | 0.602 | 0.691 | 0.771 |
| global (α=1/365d) — *the M2 trade* | 0.929 | 0.412 | 0.853 | | global | 0.206 | 0.509 | 0.669 | 0.763 |
| selector — zero-shot | 0.752 | 0.375 | 0.855 | | selector — zero-shot | 0.340 | 0.594 | 0.687 | 0.769 |
| **selector — few-shot** | **0.917** | 0.430 | 0.858 | | **selector — few-shot** | **0.340** | 0.595 | 0.689 | 0.771 |
| oracle (perfect routing) | 0.913 | 0.441 | 0.863 | | oracle | 0.362 | 0.602 | 0.691 | 0.771 |

**The result that reframes M2: the recency "trade" is a routing artifact, not an intrinsic
tension.** With perfect routing (the `oracle` row) you get global's *full* staleness gain
(new>old 0.929) **and** baseline's *full* recall (R@1 0.362) **simultaneously** — the ideal
corner, no compromise. The policy has no built-in tradeoff; the only reason a global weight
must choose between STALE and LoCoMo is that it applies the same α to both kinds of query.
Route correctly and both maxima are available at once.

**The zero-shot classifier already captures ~75% of it, untrained.** Selector new>old
0.752 sits three-quarters of the way from baseline (0.212) to the ceiling (0.929), while
costing only 0.022 of LoCoMo R@1 where the global weight cost 0.156. Every point of the
remaining gap is **pure classifier error** — the confusion table shows ~25% of fact queries
wrongly routed to recency (the small LoCoMo cost) and ~25% of state queries wrongly routed
to none (the STALE shortfall):

```
locomo/fact (expect 0): {0: 1150, 1: 212, 2: 169}   n=1531   75% correct
stale dim3   (expect 1): {0:  92, 1:  67, 2: 241}   n= 400
stale dim1/2 (expect 2): {0: 198, 1:  65, 2: 537}   n= 800   67% correct
```

Because the ceiling *is* the perfect corner, there is **no policy ceiling in the way** —
improving the classifier converts directly into closing both gaps, making classifier quality
the single lever.

**We pulled that lever, and few-shot closed nearly the whole gap (2026-07-17).** A 36-minute
A/B on a 600-query stratified sample first isolated the cheap gain, then a full re-run
confirmed it: the same prompt with **six hand-written exemplars** (no verbatim corpus
queries → no leakage) and a refined class-2 trigger — *an explicit staleness marker OR a
"since the user [stated fact]" conditioning premise* — lifts the selector from 0.752 to
**0.917 new>old, which reaches the oracle ceiling (0.913)**, while LoCoMo R@1 holds
**exactly at 0.340**. The confusion shows why it is a strict improvement, not a trade:

```
                          zero-shot                    few-shot
stale dim1/2 (want 2): {0:198, 1:65, 2:537}   ->   {2: 800}          perfect
stale dim3   (want≥1): {0: 92, 1:67, 2:241}   ->   {0:2, 1:296, 2:102}   99.5% routed, better-calibrated
locomo/fact  (want 0): {0:1150,1:212,2:169}   ->   {0:1069, 1:433, 2:29}  severe (α=1) errors 169->29
```

The dim2 "since the user…" premises now route to recency without exception (the entire STALE
shortfall was that one pattern), and dim3 re-calibrates from mostly-strong to mostly-mild,
which matches its genuinely weaker recency benefit. On LoCoMo the severe errors nearly vanish
(169→29); what remains are *mild* misroutes (α=0.5), and the earlier finding predicts —
correctly — that mild recency barely dents recall, so R@1 is unmoved. **The feared LoCoMo
regression from the binary-accuracy proxy (0.725→0.655) did not appear in the actual metric**
— confirming, again, that binary classification accuracy is the wrong lens and retrieval is
the right one. Few-shot is now the shipped classifier; the residual 0.022 LoCoMo gap is the
~28% of fact queries still catching mild recency, and it costs almost nothing.

**Consequences.** Read-time recency is now a real τ capability, not just a documented
trade — it ships as `select_recency(query) → (weight, halflife)` in front of `hybrid_search`,
and E-Affect (M4) is its first consumer for the "state beats trait" reads. The reusable
pattern — **"classify-then-weight": a query-time classifier routing a read-time scoring
term** — is validated, with its precondition made concrete (the discriminating variable must
be a query property, which recency passed and a query-independent importance prior failed).

## Blockers / open items for review

1. **Re-audit every grammar-constrained call that disables thinking.** The 0.29 → 2.45 bit
   jump says the workaround can silently flatten a signal to nothing while erroring
   nowhere. jmfts's own `summarization_disable_thinking` is the first thing to check — the
   RAPTOR summaries in this very experiment were generated with thinking off.
2. **Importance as a retention signal is untested** — the read-time result is decisive and
   negative, but the write-time use (what to keep/roll up/forget) is where the term
   plausibly belongs and no experiment covers it.
3. **Retrieval-only is a proxy.** Mem0's 0.40→0.84 is *answer quality*; ours is ranking. A
   model can still reason badly over correctly-ranked evidence. The answer-generation +
   judge leg (~4,800 generations) is the honest completion.
3. **α and t½ are corpus-fitted.** t½=365d suits STALE's multi-year span. There is no
   evidence yet about what half-life a τ session timeline wants, and the 30d result proves
   getting it wrong is worse than not having the term.
4. **Only `methods=["vector"]` was fused.** BM25 needs an index build per subtree; the
   determination isolates the recency lever rather than testing full hybrid.
5. ~~**A per-query α selector does not exist.**~~ **BUILT (2026-07-17), and few-shot took it
   to the ceiling** — see "The α-selector" above. Zero-shot captured ~75% of the
   perfect-routing ceiling; a six-exemplar few-shot prompt closed the gap (STALE new>old
   0.752 → 0.917 ≈ oracle 0.913, LoCoMo R@1 unchanged at 0.340). The selector is a shippable
   capability. Remaining follow-ups are optional: per-query half-life (fixed at 365d here),
   wiring `select_recency` into τ's live retrieval path, and the last 0.022 of LoCoMo recall
   (mild misroutes on ~28% of fact queries — low value, since the cost is already near zero).
6. **GPU headroom is thin** — llama-server (17.4 GB) + embedder peaked at 23.0 of 24.5 GB.
   It held, but a larger batch or a second model would OOM.
