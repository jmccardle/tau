# α-selector — a query-time recency policy (M2 build-debt → B-SEP read layer)

**Status:** design spec, for review before code. **Repo seam:** Tau policy only — JMFTS
already ships the mechanism (`hybrid_search`'s opt-in `recency_weight` / `recency_halflife_days`,
`cdd5a13`). Nothing changes server-side.

**Reference:** `docs/M2-RESULTS.md` (the determination this discharges) and the memory
`m2-recency-is-a-trade-not-an-upgrade`. Follows [[jmfts-mechanism-tau-policy-entity-namespace]]:
the weight *knob* is JMFTS mechanism; *choosing the weight per query* is Tau policy, and
that policy is what this builds.

---

## 1. What M2 proved, and the one thing it did not build

M2 established, robustly, that read-time recency is a **per-query trade**:

- On STALE (implicit-conflict / state queries) it is a large win: `new>old` 0.11 → 0.91.
- On LoCoMo (specific-past-fact recall) it is uniform harm: R@1 0.362 → 0.206, monotone.
- The discriminating variable is a **property of the query, observable at query time** —
  the STALE breakdown showed the recency lift swings 0.373 across *query dimensions*
  (dim1/dim2 restate the stale premise → +0.80/+0.86; dim3 never does → +0.49) but only
  0.047 across *instance types* (T1/T2). LoCoMo shows uniform harm across all four
  categories.

So the right variable is known and it is legible at query time. What does **not** exist is
the runtime function that reads that variable off a raw query — **without** STALE's oracle
dim-labels — and decides how much recency to apply. That function is the α-selector. Until
it exists, M2's recency result is a design finding, not a shippable capability, and every
B-SEP extension that wants recency at read time (E-Affect's "state beats trait" first of
all) inherits the gap.

**This spec builds that function and measures whether it recovers the win M2 could only
describe.**

---

## 2. The precise claim to establish

A **single policy, blind to which corpus a query came from**, applied per-query, that:

| | STALE `new>old` | STALE `new@5` | LoCoMo R@1 |
|---|---|---|---|
| baseline (α=0 everywhere) | 0.11 | 0.845 | **0.362** |
| global default (α=1/365d everywhere) — *the M2 trade* | **0.91** | 0.845 | 0.206 |
| **α-selector (per-query)** — *the target* | **≫ 0.11, toward 0.91** | ≥ 0.845 | **≈ 0.362** |

The selector's job is to buy most of the middle row's STALE column **while paying none of
its LoCoMo cost** — the number M2 explicitly could not produce because a global weight
cannot. The blindness constraint is load-bearing: the classifier sees only query text, so
"win both" cannot be smuggled in by knowing the corpus.

---

## 3. Design — the classifier

### 3.1 Signature (the Tau-policy seam)

```
select_recency(query: str) -> RecencyPolicy      # RecencyPolicy = (weight: float, halflife_days: float)
```

In the experiment this is a standalone function; in production it sits between "agent forms
a retrieval query" and the `hybrid_search` call — the read-time analogue of the write-time
policies M3/M5 will add. It is pure Tau policy: it returns numbers that JMFTS already
accepts.

### 3.2 First cut — zero-shot, grammar-constrained, thinking-ON

Reuse the exact pattern M2 validated (grammar-constrained rating with thinking enabled —
the 0.29 → 2.45-bit finding says thinking is load-bearing for this class of judgement, and
`enable_thinking:false` would destroy the signal):

- **Prompt** asks the state-vs-fact question directly: *"Answering this question — does it
  require the person's CURRENT state (the freshest matching memory is the right answer), or
  a SPECIFIC PAST FACT that is correct regardless of when it was recorded?"* plus 2–3
  anchored examples drawn from the M2 taxonomy (a "does she still live in Seattle" state
  case; a "when did X go to the support group" fact case).
- **Grammar** binds the answer to a small ordinal, making an invalid class structurally
  impossible (Fail-Early: no parse-failure policy to invent):
  ```
  root ::= [0-2]        # 0 = past-fact (no recency), 1 = mild-state, 2 = strong-state
  ```
  **Primary run is binary** (`root ::= [0-1]`, off/on) because the core M2 finding is
  "on for state, off for fact"; the 3-class ordinal is the first documented extension,
  since M2 *also* showed a mild/strong split *within* STALE (dim3 +0.49 vs dim1/2 +0.80).
- **Class → policy** is a documented constant table, not learned:
  `0 → (0.0, —)`, `1 → (0.5, 365d)`, `2 → (1.0, 365d)`. Half-life is fixed at the M2 best
  (365d) for the first cut — M2 showed t½ must match the corpus timescale and both corpora
  span years, so per-query t½ is deliberately *out of scope* here (noted as a follow-up).

Why zero-shot first: no training data, Tau-idiomatic (reuses the constrained-completion
machinery), and it keeps STALE+LoCoMo as a **held-out eval** rather than a training set. If
it underperforms, we will have priced the learned escalation rather than guessed at it.

### 3.3 Escalation path (only if 3.2 misroutes)

A lightweight learned classifier on `query_embedding ⊕ surface cues` ("still / now /
currently / these days / anymore" → state; "when did / what was / how many" → fact). This
needs labels, and the only labels we have are the two eval corpora — so it must run on a
**held-out split** (train on a subset of STALE dims + LoCoMo categories, evaluate on the
rest) to avoid the classifier memorising the two datasets. This is a fallback, documented
now so the decision is measured, not improvised.

---

## 4. The eval harness (`experiments/m2/alpha_selector.py`)

Reuses everything M2 built. Structure:

1. **Load both manifests** (`stale_manifest.json`, `locomo_manifest.json`, already on
   GPU-dev). STALE contributes 3 probing queries × 400 instances = 1,200 queries; LoCoMo
   ~1,531 scorable questions. ≈ 2,700 classification calls.
2. **Classify every query once**, thinking-ON, concurrency-2 on the `jf35` local-llm
   (:8080). **Persist `{query_uid → class}` to JSON** (M2's idiom) so re-scoring the
   retrieval leg is free and deterministic-to-cache. The classifier never sees the corpus
   tag or the oracle label.
3. **One search per query** through the existing seam — `repo.hybrid_search(query,
   limit=50 (STALE) / max(KS) (LoCoMo), methods=["vector"], parent_id=<root>, now=<manifest
   now>, **policy)` where `policy` is `{}` for class 0 and `{recency_weight, recency_halflife_days}`
   otherwise. `now` stays the manifest's per-instance value (end-of-conversation, per
   `eval_stale.py`'s TIME REFERENCE note).
4. **Score against each corpus's own metric** — STALE `{new>old, new@1, new@5, mean rank}`
   (copy `eval_stale.rank_of`), LoCoMo `{R@1,5,10,20}` (copy `locomo_recall`'s recall loop).
5. **Report three policies side by side**: baseline (α=0), global-default (α=1/365d — the
   M2 trade), **selector** (per-query). The selector column against the other two IS the
   determination.
6. **Classifier validation, reported but never fed back**: confusion of the predicted class
   against the oracle label (STALE dim1/2 = strong-state, dim3 = mild-state, LoCoMo = fact).
   This says *where* it misroutes (e.g. does it read dim3 as fact? does it over-apply
   recency to LoCoMo cat-2?) — diagnosis, not input.

Everything except the classify-and-route step is lifted from the two existing scripts; the
scoring functions are identical so the numbers are directly comparable to the M2 tables.

---

## 5. Determination & thresholds

- **Win (ships as the recency policy):** selector STALE `new>old` ≫ 0.11 with `new@5` ≥
  baseline, **and** LoCoMo R@1 within noise of 0.362 (say ≥ 0.35). This is the "win both"
  M2 could not reach; it makes read-time recency a real τ capability.
- **Partial (escalate):** helps STALE but nicks LoCoMo → the classifier over-applies
  recency. Two levers before giving up: 3-class ordinal (§3.2) so borderline queries get
  α=0.5 not α=1, or the learned classifier (§3.3).
- **Null (honest negative):** the classifier cannot separate the classes above the metric's
  noise. Then recency stays a **caller-supplied** knob ("the caller must know the query
  type") and we document that an *automatic* selector is not feasible zero-shot on this
  signal — itself a useful boundary for anyone building on the substrate.

Any of the three is a publishable result under the toolbox bar
([[bsep-extensions-are-patterns-not-products]]): we are characterising *when* a query-time
recency policy is feasible, not chasing a headline number.

---

## 6. Reusable pattern (the documented deliverable)

The generalisable artefact is **"classify-then-weight": a query-time classifier that routes
a read-time scoring term.** It is not specific to recency — it is the template for applying
*any* scoring term whose value depends on a property of the query rather than the corpus.
The precondition M2 handed us is exactly the one that makes it feasible: the discriminating
variable must be a **query** property (recency passed this test; a query-independent
importance prior failed it, §M2 — which is *why* importance is a write-time term and not a
candidate for this pattern). The `docs/M2-RESULTS.md`-style writeup will state the pattern,
the seam, and the precondition, so a tau/jmfts user can decide whether their own scoring
idea qualifies before building it.

---

## 7. Ops & cost

- Runs on GPU-dev midlife; both corpora already ingested; `jf35` local-llm on :8080 serves
  the classifier. ≈ 2,700 short thinking-completions at ~11 t/s / concurrency-2 ≈ a few
  minutes; caching the classifications makes every re-score free.
- No JMFTS change, no migration, no new ingestion. The only new artefact is
  `experiments/m2/alpha_selector.py` plus its cached-classification JSON and a results block
  appended to `docs/M2-RESULTS.md`.
- Nondeterminism floor from M2 applies: the classifier self-agrees only ρ≈0.83–0.92 at
  temp 0 under MoE batching, so borderline single-query flips are noise; the corpus-level
  metrics (n=1,200 / n=1,531) average over it.

---

## 8. Out of scope (named, not skipped)

- Per-query **half-life** selection (fixed at 365d here).
- The **production wiring** of `select_recency` into τ's live retrieval path — this
  experiment validates the policy; wiring it behind the real `hybrid_search` caller is a
  follow-up once the eval says the policy is worth wiring.
- The answer-generation + judge leg (the Mem0-commensurable 0.40→0.84 number) — still a
  deliberate M2 deferral; retrieval metrics remain the proxy.
