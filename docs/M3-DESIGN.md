# M3 — E-Strategy / ReasoningBank: induction under a verifiable signal

**Status:** design spec, for review before code. **Repo seam:** a `tau_jmfts.ext`
extension (`memory:strategy` B-SEP subtree) + an experiment harness under
`experiments/m3/`. JMFTS stays mechanism; the induction policy, the store shape, and the
gate are all Tau-side.

**References:** `docs/RESEARCH-INTEGRATION-EVALUATION.md` §5 (the E-Strategy / E-Skills /
E-Affect catalog and the shared B-SEP infra list), `docs/M2-RESULTS.md` +
`docs/ALPHA-SELECTOR-SPEC.md` (the read-layer this builds *on top of* — strategy retrieval
reuses the α-selector's classify-then-route muscle), and the memories
[[bsep-extensions-are-patterns-not-products]] (the toolbox bar and the α→M3→M5→M4
sequence), [[jmfts-mechanism-tau-policy-entity-namespace]], and
[[tree-as-truth-model-input-invariant]] (the versioned-store design in §3 is a direct
instance of modify-the-tree-for-context-engineering).

---

## 1. What M3 is, and why it runs chess first

E-Strategy is the first **induction** experiment: an agent distills reusable reasoning
strategies — from its own successes *and* failures — into a retrievable store, and those
strategies lift later tasks. ReasoningBank is the model to emulate; the load-bearing
requirement it hands us (and that Self-Rewarding warns about) is that induction only works
when task outcomes are **externally verifiable** — otherwise the agent grades its own
homework and the loop drifts.

That requirement is what selects the domains. A crisp external signal is a formal-domain
luxury; the honest *non-coding* induction tasks (learn a user's preferences, adapt
communication) have no external signal — their signal is the user's own satisfaction, which
is exactly E-Affect / M4's territory, evaluated by a different apparatus (preference
prediction, not correctness). So **M3 deliberately does not claim domain-generality** — the
non-coding arm is dropped, not deferred-into-a-weak-QA-leg, and the boundary ("induction
needs a verifiable signal; where none exists you need M4's apparatus") is itself the
finding. ReasoningBank-specific tuning and any less-structural generalization come *after*
the memory track.

Two arms, in this order:

- **Arm A — Los Alamos chess (the clean-room mechanism proof, primary, first).** A 6×6
  chess variant with (a) a dense verifiable signal, (b) near-zero pretraining
  contamination — the model knows the pieces but not the 6×6 specifics, so induced strategy
  has real headroom and the memorization confound largely vanishes, (c) unlimited cheap
  trajectories from self-play, and (d) a *built-in poison*: the model's standard-chess
  priors, misapplied to the variant. It proves the induction loop + the versioned store +
  the poison gate under controlled conditions before we spend the expensive coding-harness
  budget. Same oracle-ceiling-first discipline as the α-selector.

- **Arm B — coding (ecological validity, second, smaller).** Does the machinery survive
  contact with a real codebase? Debug-one-repo as the workhorse; feature-same-repo as the
  transfer leg. Confirmatory, not the mechanism proof.

The flip from a coding-primary framing is deliberate: chess isolates the *mechanism* free of
repo-authoring cost, difficulty-calibration noise, and — most importantly — the "did it
recall training data?" confound, so a null or a poison-gate failure there is unambiguous.

**Why Arm A carries the program, not just M3's loop.** MANIAC playing this game at 4-ply in
1956 was brute search — the big system's compute muscle. A local LLM cannot out-search a
4-ply minimax; it loses the search race outright. So "can induced strategy climb the ladder
toward 4-ply" is not a chess question — it is *can memory substitute for search depth*, which
is *can knowledge-via-retrieval let a small system match a big one*. The first machine to play
Los Alamos chess did it by searching; M3 asks whether a model that cannot search deeper can
instead **remember**. That is the entire B-SEP thesis at scale-model size, closing a 70-year
loop through the same game — and it is the real reason Arm A runs first and is built to bear
weight: its result speaks for the research program, not merely for this experiment's induction
loop.

---

## 2. The claims to establish

| | Claim | Arm | Signal it turns on |
|---|---|---|---|
| **C1** | induction lift: self-induced strategy > no-memory baseline | A (then B) | win-rate / test-pass vs the no-memory ablation |
| **C2** | transfer, not memorization: lift survives on *held-out* tasks | A (novel positions / stronger opponent), B (held-out bugs) | lift on unseen instances, not just replays |
| **C3** | self-authored vs curated: agent-induced approaches hand-written; **auto-induce → agent self-curates** is the headline condition | A (then B) | condition ladder in §6 |
| **H1a** | negative transfer / gating: a strategy right in context A degrades context B; a relevance gate mitigates | A (standard-chess prior on the variant) | poisoned-vs-gated delta |
| **H1b** | adversarial poisoning / provenance: a deliberately harmful induced memory degrades and the gate catches it | A (injected falsehood), B (injected bad lesson) | gate-on vs gate-off recovery |

The no-memory ablation (C1 baseline) is not optional — it is the measuring stick every other
condition is read against.

---

## 3. The strategy store — a versioned JMFTS tree (the reusable core)

This is the most reusable artifact in M3 and the design is a straight instance of
modify-the-tree-for-context-engineering. It is **git, modeled in the JMFTS tree**:

- **Strategy doc = the materialized head.** One evolving parent node per family
  (`memory:strategy/<family>`) holding the current consolidated strategy (title + one-line +
  heuristics/constraints, per the E-Strategy engine spec).
- **Strategy-log docs = an immutable, append-only child sequence.** Each modification is a
  new child (CR-1 sibling positions already order appends temporally). Nothing is ever
  destroyed; history lives in the children. This is the "don't overwrite" invariant — and it
  binds the **log only**. The head is a materialized view and *may* be rewritten on
  consolidation because it is always reconstructible from the immutable log. We do **not**
  fork per-version head snapshots (decided: unnecessary — un-poisoning re-consolidates with
  the falsehood identified and commanded out, it does not need to reproduce a prior head
  byte-for-byte).
- **Deferred consolidation via `structured_content`.** New log children land
  `{"consolidated": false}`. A consolidation pass (offline / sleep-time-style) reads the
  unconsolidated ones, rewrites the head to fold them in with de-dup, flips them to `true`.
  Between passes the *truth* = head + unconsolidated children.
- **The "footer" is a tree-read, not a search.** Retrieve head content **plus** its
  `consolidated == false` children → "my latest considerations not yet rewritten into the
  strategy." Speed knob: consolidate several at a time; until then the footer carries the
  fresh signal.
- **Poison discovery = walking the log.** Because the log is immutable, an agent can audit
  *where* a bad lesson entered and when, then command it out on the next consolidation. This
  capability falls out of not-overwriting; we do not build it.
- **History digestion = the RAPTOR muscle.** When the child log grows long, cluster +
  summarize into digest nodes (the episodic-axis operation from [[m1-raptor-over-conversation-determination]]).
  Long-term history stays available but compressed.

**Retrieval is two steps, cleanly separated** (and this is where the α-selector is reused):
1. **Find** the relevant strategy doc(s) for a task via `hybrid_search` — the
   classify-then-route step; H1a negative transfer is a *retrieval* error here (retrieved a
   standard-chess heuristic for a variant position). The query is a **task abstraction, not the
   raw task state**: for chess this is the position→motif representation of §4.6, and getting
   it right is what makes C2 interpretable — keying on surface state is the M2 wrong-
   granularity trap, and it is specified and validated before C2 runs.
2. **Assemble** head + unconsolidated-footer via tree-read, inject the top-k before the task
   (B-IN read) or expose via a `recall_strategy` tool (B-SEP read).

---

## 4. Arm A — Los Alamos chess (mechanism proof)

### 4.1 The game
6×6 board, files a–f, ranks 1–6. Back rank per side **R N Q K N R**, second rank six pawns.
**No bishops.** The queen keeps full (orthogonal + diagonal) movement — only the bishop
*pieces* are absent. No castling, no two-square first pawn move, no en passant (the 1956
MANIAC ruleset). Pawn promotes on the last rank (Q/R/N). Twelve pieces per side on 36
squares — small enough for a real minimax opponent and cheap games.

### 4.2 The signal (dense, for both measurement and credit assignment)
Static eval = material (P1 N3 R5 Q9) + λ·mobility (legal-move count) + center-control bonus.
Terminal = win/loss/draw. The **dense** eval is what solves chess's temporal
credit-assignment problem for distillation: attribute a lesson to the *moves that swung the
eval past a threshold*, not to every move of a won game (a win does not mean each move was
good — the opponent may have blundered). Learn from swung-toward (success) and swung-away
(failure) moves alike — the ReasoningBank success-and-failure discipline made concrete.

**Validation gate before the credit signal is trusted.** Distillation attributes on
eval-swing but the experiment grades on game *outcome*, so if the two diverge —
mobility-weighted evals can misvalue positions — we would be distilling toward one target and
grading by another, a quiet slippage. Before the eval-swing is used as the credit signal,
check the correlation between per-move eval-swing and final game result on a sample of games;
if it is weak, retune λ or fall back to outcome-attributed credit rather than distilling on a
signal that does not track winning. This is a one-time precondition, not a per-run cost.

### 4.3 Legal moves via grammar-constrained decoding (direct W-series reuse)
Each turn, generate the position's legal-move list and build a per-turn grammar
`root ::= "<move1>" | "<move2>" | …` in coordinate notation (`e2e4`). The model reasons
(thinking-ON, `--reasoning-budget` to force the end-of-think tag — the α-selector method),
then emits exactly one grammar-bound legal move. This removes the "we spent the experiment
fighting illegal moves" failure mode entirely and ties M3 back to the constrained-decoding
track. An induced "rule" that *would* produce an illegal move is simply never emittable —
its harm shows up as bad legal choices, not as parse errors (Fail-Early: no illegal-move
fallback to invent).

### 4.4 Curriculum vs. measuring stick (kept separate)
- **Curriculum (generates trajectories to distill from):** self-play and/or play-vs-greedy.
  Cheap and unlimited — the practical win over authoring N bugs.
- **Measurement (reads the conditions against each other):** agent-with-strategy vs a
  **fixed, deterministic opponent ladder** — **greedy (1-ply material) as the floor, then
  2 / 3 / 4-ply minimax on the same eval.** We start at greedy and **advance the agent as
  far up the ladder as the strategy system can reach, capped at 4-ply** (historically 4-ply
  MANIAC beat a day-one novice and lost to a strong player — a well-calibrated ceiling for a
  local-LLM agent). Never measure on self-play — opponent blunders muddy attribution.

Opponent depth is the difficulty dial: greedy is likely too weak alone (a no-strategy agent
that already wins has no headroom → false null), so the reportable result is *how high up the
ladder each condition climbs*, not a single win-rate.

### 4.5 The built-in poison (H1a, for free)
The model's standard-chess priors are a natural poison for the variant: assuming bishops
exist, two-square pawn pushes, castling, standard opening theory. These are *induced or
prior* strategies that are wrong here. H1a tests whether the relevance gate demotes them; a
lucky-win-induced bad heuristic ("rush the queen out early") is the same probe from the other
direction. H1b adds a *deliberately injected* falsehood into the log to test the provenance
half.

### 4.6 The retrieval key — position→query and what a strategy is keyed on (C2 rests here)
C2 (transfer to novel positions) lives or dies on this. The trap: if the key is **surface
placement** (which piece sits on which square), retrieval only fires on near-identical boards,
no transfer can appear, and a C2 null is a *representation* failure masquerading as an
induction failure — the M2 wrong-granularity trap on a chessboard. So the key must be a
**tactical abstraction of the position, not the raw board**, and a strategy is keyed on the
**precondition-pattern it applies to**, written in that same vocabulary; retrieval matches
current-position features against strategy preconditions (the §3 classify-then-retrieve step).

**But we do not hand-author the motif vocabulary.** Neither the owner nor the harness is
qualified to name the strategic motifs of a game with essentially no prior play — hand-coding a
feature set presupposes the very strategy that should *emerge*, and given the novelty (these
runs likely compute the majority of all Los Alamos chess games ever played) a human-authored
motif set is as likely to poison the strategy space as to guide it. So:

- **The model generates the abstraction** — position descriptors and strategy preconditions in
  its own emergent vocabulary. No human motif list.
- **Review before intervene, not a design-time commitment.** Read the emergent vocabulary
  across a sample; intervene *only* on a specific pathology (e.g. descriptors that are pure
  surface — "knight on c3" — with no tactical content), and then by a prompt nudge ("describe
  the tactical situation, not the placement"), never by imposing a taxonomy. The emergent
  vocabulary is itself a documented finding: *what strategic language an LLM spontaneously
  develops for a game it has never seen* is exactly what this track exists to surface.
- **Generalization is checked against the engine, post-hoc, as a null-disambiguator — not a
  pre-abort gate.** We have no external similarity oracle and cannot author one, but the minimax
  engine *is* the domain's own authority on which positions are tactically alike (same
  best-move-type / eval structure). If C2 comes back null, ask post-hoc whether key-similarity
  tracks engine-similarity: if the key clusters engine-alike positions, the null is a real
  transfer failure; if it does not, the null is representation noise. This turns an otherwise
  uninterpretable null into a diagnosable one *without* hand-authoring the yardstick.

This has a consequence for the authored condition (C3), handled in §6.

---

## 5. Arm B — coding (ecological, second, smaller)

- **Debug-one-repo (workhorse):** N seeded bugs in one real repo, each with a failing test;
  signal = test red→green. Carries C1, C2 (hold out novel bugs), H1b (inject a bad lesson).
- **Feature-same-repo (transfer leg):** small features each specified by a to-pass test;
  tests whether repo-debug strategy transfers to feature work.
- **Calibration:** seed difficulty so the no-memory baseline lands ~40–70% — too-easy bugs
  give no headroom, too-hard give no signal.

Kept deliberately smaller than Arm A: chess proves the mechanism; coding proves it survives a
real codebase. Commit/merge (procedural) is out — it is M5's E-Skills. Architecture planning
(no clean signal) is out — it is M4's E-Affect.

---

## 6. Conditions (crossed on Arm A, lighter on Arm B)

| id | condition | what it isolates |
|---|---|---|
| **C0** | no-memory (empty store, no induction) | the C1 baseline / measuring stick |
| **C1** | self-induced (distill after each task, retrieve before) | the ReasoningBank claim |
| **C2** | self-induced **+ agent self-curates** (consolidation/self-review is an agent action) | the headline τ-persona condition, per §3 |
| **C3** | prior-authored strategy (see note) | a human/engine prior *probe*, not an assumed ceiling |
| **P**  | poisoned × {gate-on, gate-off} | H1a (prior/negative-transfer) & H1b (injected) |

Strict isolation between conditions and seeds: a fresh `memory:strategy` root per
condition/seed so no store bleeds into another (the M2 cache-separation hygiene).

**C3 is not a credible "ceiling" here — reframe it (from §4.6).** The same reason we don't
hand-author motifs means we can't hand-author a trustworthy *strategy* either: a human-written
Los Alamos strategy doc is speculative and may itself be poison. So C3 is a **prior probe**,
not a ceiling, informative in both directions — if human-authored strategy *beats*
self-induced, the prior was good; if it *underperforms C0*, human speculation is net-poison and
C3 doubles as an H1 case. For a genuine strong-authored ceiling the domain-grounded option is
**engine-distilled strategy** (heuristics extracted from minimax play) — non-speculative
because it comes from the domain's own authority. Which C3 we run (human-prior probe,
engine-distilled ceiling, or both) is a build-time decision.

**C2 is not compute-matched to C1** — it is C1 plus self-curation, strictly more model calls,
so name the claim before reading it. M3's headline claim is **"self-curation is worth its
cost"** (C2 vs. C1 as-is, §9) — no matching needed, the extra spend is part of what is being
judged. If we instead want the *mechanism* claim "self-curation improves the strategies"
independent of spend, add **C1′ = self-induced with a curation-matched induction budget**
(extra distillation passes, no curation step) as the control. Default: run the "worth its
cost" claim; add C1′ only if the result makes the mechanism question live.

---

## 7. The gate — self-healing credit assignment (vs a static-provenance baseline)

Two gate designs, compared:
- **Baseline — static provenance/relevance.** Source-tag each log child (which task/context
  produced it) + an LLM applicability check at retrieval ("is this strategy safe here?").
  Cheap, but blind to whether a memory actually *helps*.
- **Self-healing — *conditional* credit assignment (recommended).** *Not* co-occurrence:
  demoting a strategy because it was *present* in a lost game is `P(strategy present | loss)` —
  marginal, and exactly the M2 marginal-vs-conditional error. A good strategy present in a game
  later lost to an unrelated blunder gets demoted; a poison that rode along in a win escapes.
  The signal we actually want is conditional: `P(bad eval-swing | this strategy was *used* in
  this move)`, built from the **strategy-hit trace** (§8 — used vs. merely present) crossed
  with the **move-level eval-swing** (§4.2). Wire that conditional attribution *into* the gate:
  a strategy is demoted when the moves that actually invoked it tend to swing the eval the
  wrong way — not when it merely sat in the store during a loss. This is coupled to
  failure-induction (the same signal that learns from a loss detects a poison), and chess-first
  is right *for this specifically*: bandit-style demotion needs many plays per strategy to
  clear the nondeterminism floor, and cheap plentiful games supply them.

H1 succeeds if the gated condition recovers most of the poisoned condition's lost
performance; the interesting comparison is self-healing vs static, not gate vs no-gate alone.

---

## 8. Metrics

- **Primary — success, per rung (the powered comparison):** the headline is the **per-rung
  win-rate and material-margin curves** across the opponent ladder — *not* a single "how high
  it climbs" number. Ladder-height is a ~4-value ordinal, too coarse against the
  nondeterminism floor to carry the result; report it as a summary and read the per-rung curves
  as the test. Coding = test-pass rate.
- **Efficiency — often where the effect lives first:** steps/tokens-to-success; chess =
  eval-losing blunders per game. Induction usually buys *less flailing* before it buys the
  outcome, so this is not a secondary nicety.
- **Strategy-hit trace — a gate input, not just a metric:** whether a retrieved strategy was
  actually *used* in a move (attribution, not presence). §7's conditional gate consumes this
  crossed with eval-swing, so it is wired *and* reported, not merely reported.
- **Qualitative library artifact (a headline deliverable):** because memories are inspectable
  JMFTS docs, we show *what an induced strategy library looks like* after N games — the good
  heuristics, the one poisoned entry, and the gate catching it. Under the patterns-not-
  products bar, this artifact may land harder than any win-rate delta.

---

## 9. Determination & thresholds

Per claim, and any outcome is publishable under the toolbox bar:
- **C1 win:** self-induced's per-rung win-rate / material-margin curves sit strictly above
  C0's outside the noise floor (ladder-height is the summary; the curves are the test) / it
  passes more tests than C0.
- **C2 (the one we most want):** self-curated ≥ raw self-induced and approaches C3
  hand-curated — evidence that the agent editing its own strategy doc is worth the loop.
- **C3 read (a probe, not a ceiling):** the induced-vs-prior gap read in both directions —
  human-authored may sit above, level with, or (as poison) below C0; engine-distilled, if run,
  is the non-speculative strong baseline.
- **H1a/H1b win:** poisoned degrades measurably; the gate (self-healing preferred) recovers
  most of it; static-provenance is the comparison floor.
- **Null (honest):** induction does not separate from C0 above noise, or the gate cannot
  recover the poison. Then M3 documents *that* — with chess's clean conditions, a null here
  is a real statement about induction's limits, not an artifact.

---

## 10. Reusable patterns (the documented deliverables)

1. **The versioned-tree strategy store (§3)** — head + immutable log + deferred
   consolidation + footer + poison-audit + RAPTOR digest. The general
   modify-tree-for-context-engineering pattern; not chess- or strategy-specific.
2. **Classify-then-retrieve** — strategy retrieval is the α-selector's routing pattern reused
   at the write-memory read-back; negative transfer is a retrieval error with a classifier
   gate.
3. **Self-healing credit-assignment gate (§7)** — a poisoning defense that needs no oracle,
   only the failure signal induction already collects.
4. **Grammar-constrained legal action space (§4.3)** — constrain the agent to only-valid
   actions per step; the W-series muscle applied to a task domain, not just a rating.

Each gets a `docs/M3-RESULTS.md` writeup stating the pattern, the seam, and *when it
applies* — so a tau/jmfts user can decide whether their own induction idea qualifies before
building it.

---

## 11. Ops & cost

- Runs on GPU-dev midlife; `jf35` local-llm on :8080 serves the agent (thinking-ON,
  `--reasoning-budget` to bound ruminators — restart/restore-in-finally + `ps`-verified, the
  α-selector ops hygiene). Prod jmfts untouched.
- Chess needs a small 6×6 engine (legal move-gen + material/mobility eval + greedy→4-ply
  minimax) — a few hundred lines, cheap to run, no ingestion. Coding needs a seeded repo +
  test harness.
- **Shared B-SEP infra this is the first real user of** (per RESEARCH-INTEGRATION §5, none
  exist yet, sized honestly): the B-SEP store helper (open a root by `usetype`), the scoped
  search wrapper (`parent_id = ext root`), the extension-doc columns
  (`usage/success count`, `source/provenance`, `structured_content.consolidated`), and a
  consolidation scaffold. The experiment can run consolidation **synchronously/offline** and
  retrieval **explicitly** — so it does *not* block on the two known seam gaps (B-IN
  auto-injection wiring and the sleep-time idle-turn host); those are production concerns,
  named in §12.

---

## 12. Out of scope (named, not skipped)

- **Domain-generality / any non-coding arm** — dropped by design (§1); the persona domain is
  M4's, evaluated differently.
- **ReasoningBank-specific tuning and less-structural generalization** — after the memory
  track, by the owner's call.
- **Production B-IN auto-injection wiring** and the **sleep-time idle-turn host** — the
  experiment uses explicit retrieval + offline consolidation; wiring these behind the live
  loop is a follow-up (the §5 seam-reality gaps).
- **MaTTS (memory-aware test-time scaling)** — ReasoningBank's compounding trick; a possible
  extension once the base induction loop is proven, not first-cut.

---

## 13. Build sequence

1. The versioned-tree store (§3) on the shared B-SEP helper — the spine both arms need.
2. The 6×6 chess engine + grammar-constrained move interface + opponent ladder.
3. Arm A conditions C0/C1/C2/C3, then the poison + gate legs (H1a/H1b).
4. Read Arm A; only then size Arm B (coding) as the ecological confirmation.
5. `docs/M3-RESULTS.md` — the four patterns (§10) with when-it-applies.
