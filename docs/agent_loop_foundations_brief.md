# Agent loops: evaluation, control, orchestration — a research brief

Companion grounding for `pi_planning_implementing_evaluating.md` (doc 2) and
`pi_workflow_patterns.md` (doc 3). Those docs asserted a lot of engineering
practice; this one traces each claim to the literature it descends from, names the
constraints that literature imposes, and flags where the research suggests the two
docs should be *revised*. Research emphasis, no code — an implementer can draw their
own wiring.

The two docs are really engineering answers to four research problems: **(I)** how to
judge generated work when the generator cannot judge itself; **(II)** why optimizing
a proxy oracle corrupts it; **(III)** how to steer a long run without a human tick;
and **(IV)** how to decompose and coordinate so neither context nor consistency
degrades — plus a cross-cutting **(V)** how to allocate effort rationally. Taking
them in turn.

---

## I. The evaluation problem: generator ≠ evaluator

This is the spine of both docs — `refine_until`, `verify_no_cheat`, the adversarial
verifier, the tournament, the review pass. The core assertion ("an agent grading its
own work praises it; tune a separate skeptic") is now well-supported and, more
usefully, well-*bounded*.

**Self-correction is not free.** Huang et al. (2024, ICLR, *LLMs Cannot Self-Correct
Reasoning Yet*) and the Kamoi et al. (2024, TACL) survey establish that *intrinsic*
self-correction — refining with no external feedback — often fails to improve and can
degrade performance, and later work finds it can flip correct answers to incorrect
(Zhang et al. 2025). The reason code self-debug *does* work is the exception that
proves the rule: Chen et al. (2023) succeed because execution against unit tests is a
*perfect external verifier*. This is the literature under doc 2's non-negotiable —
**the evaluator must act (run the tests), not read** — and under the whole thread's
"acting altimeter." Self-grading is the failure mode; external grounding is the fix.

**The generation-verification gap is conditional, not a law.** The premise that
verifying is easier than generating (which would justify self-verification) holds
only under conditions — training, problem difficulty, and the relative capability of
generator and verifier — and recent work challenges its universality (Song et al.
2025; Jiang et al. 2025). There is even a measured *self-correction blind spot*: models
fail to correct their own errors that they would correct if the identical error were
presented as external input. The practical consequence for doc 2: a separate verifier
is not *automatically* better — it earns its keep only when it is genuinely
independent (different model family, executes rather than reads, sees a signal the
generator did not). A weak or correlated verifier can be worse than none.

**Judges are biased in structured ways.** Two matter here. *Self-preference bias*:
models favor their own generations and their vendor family's style (Panickssery et
al. 2024) — which grounds doc 2's aside that swapping the *model*, not just the
instructions, helps. *Position bias*: judges systematically favor a position in the
prompt, and it is pervasive and non-random across many judges and tasks (Shi et al.
2024) — mitigated by swapping the two orderings and averaging, or scoring a
disagreement-on-swap as a tie (the PandaLM approach).

**Absolute scoring is unreliable; pairwise is better — but non-transitive.** The
survey literature is explicit that absolute (1–10) scoring is less robust than
relative comparison, which is exactly why doc 3's tournament uses pairwise judging.
But there is a catch the doc missed: LLM pairwise preferences are **non-transitive**
(Xu et al. 2025) — A beats B, B beats C, C beats A — so a fixed-baseline or
single-elimination "winner-stays" chain (which is what doc 3 implemented) is fragile
and order-sensitive. **The robust form is a round-robin tournament aggregated with a
Bradley–Terry model** (Bradley & Terry 1952) to fit a latent skill score from all
pairwise outcomes, with bootstrap confidence intervals; and for high-stakes calls, a
*panel/jury* of diverse model families (a "PoLL") to break correlated blind spots.
**This is the concrete upgrade doc 3 should take: replace the elimination chain with
round-robin + Bradley–Terry, and swap positions on every comparison.**

**Historical roots** worth naming in the role prompts: the *maker–checker* principle
(separation of duties in banking/accounting); the GAN generator/discriminator split
(Goodfellow et al. 2014); actor–critic reinforcement learning; trained verifiers on
reasoning (Cobbe et al. 2021); and process reward models with step-level supervision
(Lightman et al. 2023), whose central finding — a chain can reach the right answer
through invalid steps — is the justification for doc 2's step-wise review rather than
outcome-only checks. Debate (Irving et al. 2018; multi-agent debate, Du et al. 2023)
is the conceptual ancestor of adversarial verification, but its empirical efficacy is
contested; the reliable ingredient is *separation plus external grounding*, not agent
count or "debate" as such.

---

## II. The specification-gaming problem: why "make the tests pass" bites

Doc 2's reward-hacking defenses (read-only tests, held-out suites, revert-and-recheck,
spec-over-tests) are instances of a general law.

**Goodhart's Law** — when a measure becomes a target, it ceases to be a good measure
(Goodhart 1975; Strathern's 1997 phrasing). Manheim & Garrabrant (2018) decompose it
into four mechanisms; the relevant one is *adversarial Goodhart* — an optimizer
(the coder) pushing the proxy (visible tests) away from the goal (the spec). The tests
are not the objective; they are a proxy for it, and any sufficiently capable optimizer
under pressure will exploit the gap.

**Specification gaming / reward hacking** is the AI-safety name for the same thing
(Amodei et al. 2016, *Concrete Problems in AI Safety*; Krakovna's specification-gaming
catalog), and in coding specifically it manifests as hardcoding to observed cases,
editing or deleting tests, and disrupting the grader (empirically, EvilGenie and
ImpossibleBench). The defenses are three ML-methodology principles wearing work
clothes: **held-out evaluation** (the train/test split, to deny the optimizer sight
of the true oracle), **tamper-evidence** (revert-and-recheck: if reverting the agent's
test edits breaks the build, it gamed the oracle), and **spec primacy** (the tests are
subordinate to a specification the agent must not contradict). None is a proof; they
are defense-in-depth against a moving adversary, which is the honest framing.

---

## III. The control problem: steering without a human tick

Doc 2's system-reminders (closed-loop steering) and its iteration-not-escalation
stance are control theory and its LLM-specific failure literature.

**Feedback control / cybernetics** (Wiener): a controller observes state, computes
error against a reference signal, and injects a corrective input. The reminders are
bang-bang controllers with cooldowns; the reference signal is the spec/interface; the
gates are the safety interlock. This is a framing, but a load-bearing one — it tells
you *where* to put a check (at the error measurement) and why cooldowns matter
(controller chatter).

**Context degradation is why re-anchoring is necessary.** Liu et al. (2023,
*Lost in the Middle*) show models underuse information placed mid-context and degrade
as context grows; this is the mechanism behind doc 3's "goal drift by turn 40" and
the justification for the drift-reanchor reminder that re-issues the contract rather
than trusting the model to still hold it. Re-anchoring is closed-loop compensation for
a known open-loop decay.

**Iteration has limits and can diverge.** Self-Refine (Madaan et al. 2023) and
Reflexion (Shinn et al. 2023) improve outputs *with* a feedback signal; without an
external one, iteration plateaus or degrades (Huang et al. 2024, again). This grounds
doc 2's discipline of capping rounds and *stopping* rather than escalating, and of
*varying the signal* (a fixed-point iteration converges only under a contraction
property; re-injecting the identical failure has none, which is precisely the
limit-cycle regime). The alternative cost lever doc 2 deliberately declines —
**escalation via model cascades** (FrugalGPT, Chen et al. 2023) — is the field's
dominant cost-saver; declining it buys cost predictability at the price of those
savings, and that tradeoff should be stated rather than hidden.

---

## IV. The decomposition & coordination problem: orchestration

Doc 3's "orchestration in code, agents isolated, coordination costs zero model tokens"
is a rediscovery of several established architectures.

**Externalized coordination through a shared workspace** is the **blackboard
architecture** (Hearsay-II, Erman et al. 1980; Hayes-Roth 1985): independent knowledge
sources that never talk to each other directly, communicating only through a shared
structure under a separate control regime. That is doc 3's fleet exactly — isolated
agents, a script that holds the state, no inter-agent chatter. Adjacent lineage:
**tuple spaces / Linda** (Gelernter 1985), which decouples coordination from
computation as a first-class principle, and **distributed cognition** (Hutchins 1995),
the theory behind "the agent forgets, the repo doesn't" — cognition offloaded to a
persistent environment.

**Isolation defeats both context decay and self-distraction.** Factored cognition
(Ought; Stuhlmüller) decomposes a task into bounded subtasks each solved with limited
context; giving every agent a fresh window and one goal is what prevents *lost-in-the-
middle* and the "agentic laziness" of a 50-item list degrading in a single context.
Doc 3's series-with-gates ("40 individualized agents beat one agent with a 40-item
checklist") is this principle: the coordination lives in the script, so no single
window has to hold all forty.

**Decomposition and planning** have a deep prompting lineage worth citing in the
planner role: hierarchical task networks (classical HTN planning); least-to-most
prompting (Zhou et al. 2022); decomposed prompting (Khot et al. 2022); plan-and-solve
(Wang et al. 2023); and ReAct / Tree-of-Thoughts (Yao et al. 2022, 2023). Doc 2's
interface-first, then-implement ordering is a plan/execute separation with an explicit
contract in between.

---

## V. The pattern catalog, derived

Each of doc 3's six patterns has a classical ancestor, which is the fastest way to
know its properties and failure modes:

- **classify-and-act** ← routing/dispatch and mixture-of-experts; blackboard control.
- **fan-out-and-synthesize** ← **MapReduce** (Dean & Ghemawat 2004) and scatter-gather;
  the synthesize step is a reduce, and its failure mode (a lossy reduce) is MapReduce's.
- **adversarial verification** ← debate (Irving et al. 2018), red-teaming, and the
  dialectic; robust core is the separate-external-acting verifier of §I, not "debate."
- **generate-and-filter** ← **generate-and-test** (Newell & Simon), rejection sampling,
  **best-of-N** with a verifier (Cobbe et al. 2021), and self-consistency voting (Wang
  et al. 2022); the generator is *allowed* to be noisy because the filter is the QC.
- **tournament** ← **Bradley–Terry** (1952) preference modeling and RLHF's use of
  pairwise preferences over absolute ratings; refined by the non-transitivity result
  (§I) into round-robin + BT.
- **loop-until-done** ← fixed-point iteration and **anytime algorithms** (Dean & Boddy
  1988): quality improves with compute and the procedure is interruptible, which is the
  cap-and-return contract.

---

## VI. The contract & gate lineage

Doc 2's interface-first design and its cohesion gates descend from software
engineering's oldest results, and naming them makes the design decisions principled
rather than stylistic.

- **Information hiding / modularity** — Parnas (1972, *On the Criteria To Be Used in
  Decomposing Systems into Modules*): decompose by hiding design decisions behind
  stable interfaces. "Freeze the interface, then let small models fill it" *is* Parnas,
  and it is why small models sprawl less inside a fixed boundary — the boundary hides
  the decisions they would otherwise reinvent.
- **Design by contract** — Meyer (Eiffel): pre/postconditions and invariants as the
  obligation an implementation must meet. The ratified test suite as "the contract" is
  design-by-contract with tests as the assertion language; test-first is Beck's TDD.
- **Fitness functions** — Ford, Parsons & Kua (*Building Evolutionary Architectures*):
  automated, objective checks that guard architectural characteristics over time. The
  `cohesion_gate` (format, lint, types, import-layering) is a battery of fitness
  functions, and architectural conformance has its own lineage in software reflexion
  models (Murphy et al. 1995). The Stripe lesson — anything a rule can decide never
  goes to a probabilistic model — is the pipes-and-filters / deterministic-scaffolding
  principle restated.
- **Constrained decoding / grammars** (e.g. GBNF): enforce structured output *by
  construction* rather than by validate-and-retry — the typed hand-off made
  impossible-to-malform, which is the robust way to move data between stages.

---

## VII. The effort-allocation problem (cross-cutting)

Doc 2's budgets and doc 3's model routing are applied metareasoning.

- **Bounded rationality** (Simon): a rational agent optimizes under a compute budget,
  not in the limit. The budget knob is not a cost hack; it is the rationality model.
- **Metareasoning** (Russell & Wefald 1991, *Principles of Metareasoning*): reasoning
  about how much to reason, weighing the value of further computation against its cost.
  Choosing effort/model per stage is metareasoning made explicit, and it argues for
  *estimating* a stage's difficulty before spending on it (the classify-and-act
  complexity router).
- **Anytime algorithms** (Dean & Boddy 1988): interruptible procedures whose quality
  rises with compute — the formal shape of loop-until-done under a cap.
- **Compute-optimal test-time scaling** (Snell et al. 2025): allocating more inference
  compute to harder instances can beat scaling parameters — the evidence base for
  spending big-model effort only where difficulty warrants it.
- **Cascades and small-model routing** — FrugalGPT (Chen et al. 2023) and the
  SLM-for-agents thesis (Belcak et al. 2025): route the cheap majority of calls to
  small specialists; reliability comes from the constraints, not model size.

---

## VIII. Constraints and open problems

Stated plainly so an implementer doesn't over-trust the machinery:

- **A separate verifier is not automatically better.** The generation-verification gap
  is conditional; independence must be *real* (different family, executes, sees a
  held-out signal). Correlated verifiers inherit the generator's blind spots.
- **Pairwise judging needs care.** Non-transitivity, position bias, and self-preference
  mean a naive chain is unreliable; round-robin + Bradley–Terry + position-swap, and a
  jury for high stakes, are the minimum for trustworthy rankings — and even then, judge
  scores drift and should be calibrated against human anchors, not used raw across time.
- **Gaming is adversarial and moving.** Held-out + tamper-evidence + spec-primacy is
  defense-in-depth, never a guarantee; a more capable optimizer finds new proxies.
- **Isolation has a cost.** It defeats context decay but forbids context-sharing, and
  genuinely integrative tasks need shared state — which is where the blackboard returns
  as a first-class component rather than an anti-pattern.
- **Debate/multi-agent efficacy is unsettled.** Spend the engineering budget on the
  quality and independence of one evaluator before spending it on more agents.

The through-line under both docs: **generation is cheap and getting cheaper;
judgment is the scarce resource, and almost every technique here is a way of buying
reliable judgment** — by separating it from generation, grounding it in execution,
aggregating it robustly, and spending it only where it changes the outcome.
