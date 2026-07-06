# A self-auditing rational Agent — research brief

Companion to the workflow docs. This one is deliberately about *the academic
machinery and its constraints*, not code. The mechanisms get programmed
elsewhere; the point here is to give an implementer accurate facts about the
formal frameworks so they can draw their own wiring between the systems, and to
specify what each agent role should ask and emit.

The subject is an Agent — call it Kevin — that reviews its own journal entries and
its provided invariants (goals, personality), tests what follows and what
conflicts, and discovers the axioms it has been operating on. Solvers are
instruments it wields the way a mathematician wields a proof checker: using one is
not a concession of incapacity, it's how you get leverage. The design bias is
toward *trying* consequential axioms and observing their consequences.

## 0. The operating principle: task-scoped micro-theories

The single most important design decision: **there is no global formal theory of
Kevin.** The belief store stays natural-language-primary. For each reasoning task,
the Agent builds a small, disposable formal theory scoped to *that question* —
retrieve the relevant slice, let an LLM select and formalize the minimal axiom set
that bears on it, hand that micro-theory to the appropriate solver, integrate the
result back into the store, and discard the theory. Formalization is lossy and
task-relative, and that is a feature: you never try to axiomatize the whole self,
so you never inherit the pathologies of trying to.

This makes inconsistency *normal and local* rather than catastrophic. A global
store will contain contradictions (every journal does); the job is not to prevent
that but to (a) keep each task-scoped slice coherent enough to reason in, (b)
inventory the standing tensions, and (c) resolve the ones that are load-bearing.
Classical logic's explosion-under-contradiction is avoided two ways: scope tightly
so a conflict shows up as an informative minimal core, and reason over the messy
whole **paraconsistently** (Priest's LP, Belnap–Dunn four-valued FDE) so "I hold
two conflicting entries" does not license concluding anything whatsoever.

Four bodies of rationality theory are *background for how the roles behave*, not
components to build:

- **Belief revision — AGM** (Alchourrón, Gärdenfors, Makinson 1985). The postulates
  for expansion, contraction, and revision; *partial-meet contraction*; and
  crucially **epistemic entrenchment** (Gärdenfors–Makinson): a ranking of which
  commitments you surrender last. Entrenchment *is* the rigidity gradient —
  invariants are maximally entrenched, journal entries least — so a conflict
  resolves by giving up the least entrenched thing that restores coherence.
- **Truth maintenance** (Doyle 1979, JTMS; de Kleer 1986, ATMS). Track *why* each
  belief is held, mark nodes in/out by their justifications, record **nogoods**
  (known-inconsistent sets), and do dependency-directed backtracking. This is how a
  retracted entry propagates to everything that rested on it.
- **Argumentation** (Dung 1995; ASPIC+, Modgil–Prakken; DeLP, García–Simari 2004).
  Claims as nodes with support/attack edges; acceptance computed as extensions.
- **Abduction / defeasibility** (Kakas–Kowalski–Toni for ALP; Pollock and Reiter
  1980 for defeasible/default reasoning). Explanation and rules-with-exceptions.

Keep these in the *prompts and adjudication policy*, not the solver stack.

---

## 1. Three task families and the DSLs that fit them

### A. Consistency & constraint — "do these commitments hang together, and if not, what is the minimal conflict?"

- **SMT (SMT-LIB standard; Z3, cvc5).** The workhorse. Decidable theories —
  linear integer/real arithmetic, uninterpreted functions, arrays, datatypes,
  bit-vectors. Three outputs an implementer should exploit: **UNSAT + unsat core**
  (a minimal conflicting subset — "these axioms clash"), **SAT + model** (a concrete
  witness that the commitments are jointly satisfiable, useful as an existence proof
  for a scenario), and **UNKNOWN** (quantifiers push you out of the decidable
  fragment — a real constraint to respect, not an error to paper over).
- **SAT + MaxSAT / MUS / MCS.** For propositional cores: **MUS** (minimal
  unsatisfiable subset) is the contradiction; **MCS** (minimal correction set — the
  smallest set to *drop* to regain consistency) is, in effect, an AGM contraction
  candidate computed for you. Enumerate them to show the Agent its options.
- **Datalog** (bottom-up, guaranteed-terminating, stratified negation). For a large
  relational fact base with rules where you want "everything derivable" cheaply and
  totally. Weaker than full logic programming, but its termination and scale are the
  point.

Constraint to write down: classical consistency checking is monotone and brittle;
it tells you *that* and *where* things conflict, never how to feel about it. The
"how to feel" is the reviser's job (§2.7), informed by entrenchment.

### B. Derivation & consequence — "what follows, and how defeasibly?"

- **Nonmonotonic derivation — Answer Set Programming (clingo/Potassco;
  Gelfond–Lifschitz stable-model semantics).** This is where a *personality* lives,
  because personalities are defaults-with-exceptions ("normally conserve battery;
  not when a sample is at risk"). ASP gives closed-world reasoning, default negation,
  and multiple answer sets as alternative coherent stances. Constraint: grounding
  can blow up; keep predicates and domains scoped (which task-scoping already does).
- **Structured defeasible reasoning — DeLP (García–Simari), Defeasible Logic
  (Nute, Governatori).** Strict vs defeasible rules with a dialectical procedure
  that returns *warranted* conclusions and the argument for/against each. Natural
  fit when you want the *justification tree*, not just the verdict.
- **Machine-checked derivation — dependent type theory: Lean 4 (mathlib), Rocq
  (formerly Coq), Isabelle/HOL, Agda.** Reserve for the small set of conclusions you
  want *certified* to a trusted kernel — a safety-critical entailment about the
  mission, say. High cost (you supply the proof or a prover does), monotone, and
  exact. Not for exploration.
- **Automated FOL — superposition provers (Vampire, E).** First-order derivations
  without interactive proof, when SMT's theories don't fit but you don't need Lean's
  assurance.

The design rule: **tag every derived conclusion with its regime** — monotone-and-
certified (Lean/SMT), or defeasible (ASP/DeLP) and therefore retractable when a
defeater arrives. Mixing the two silently is the classic error.

### C. Discovery & explanation — "what am I assuming, and what is the minimal set that generates it?"

- **Abductive Logic Programming (ALP; Kakas–Kowalski–Toni).** Given observations
  (entries) plus a background theory and integrity constraints, compute the
  **abducibles** — hypotheses that would explain the observations. This is the
  formal form of "infer the axioms behind the journal." Output is a *set* of
  candidate explanations, ranked by minimality and by respecting the constraints.
- **Inductive Logic Programming (ILP; Muggleton; modern: Popper/Cropper, ILASP/
  Law).** Generalize *rules* from positive/negative examples — "learn the standing
  principle these entries instantiate." ILASP is notable because it learns ASP
  programs, so what it discovers drops straight back into the family B derivation
  engine.
- **Theory exploration (QuickSpec — Claessen–Smallbone–Hughes; Hipster).**
  Given a signature, conjecture the equations/laws that appear to hold — surface
  latent regularities as candidate axioms without being told what to look for.

Hard constraint, and it governs the whole family: **discovery produces conjectures,
never truths.** Everything family C emits must be routed back through family A
(does adding it stay consistent?) and family B (what does it now let us derive?)
and then ratified — by Kevin deliberately, or by a human — before it becomes a
committed axiom. Discovery is the generative, exploratory arm; verification is its
leash.

---

## 2. Agent roles and their structured outputs

Each role is stated as: the *question* it answers, and the *shape* of what it emits.
Schemas are given as records (field — meaning), not code. Every formal artifact
carries provenance back to the natural-language source and a self-declared
confidence, because the formalization step is the one judgment-laden edge and must
never launder its own uncertainty.

### 2.1 Scoper — "What bears on this question, and how revisable is each piece?"
Retrieves the relevant slice of store + invariants for a task; assigns a task type.
```
Scope {
  question, task_type: {consistency | derivation | discovery | debate | value},
  items: [{ id, kind: entry|invariant|derived, relevance, entrenchment: 0..1,
            why_included }],
  deliberately_excluded: [{ id, why }]
}
```
Background: relevance is retrieval; `entrenchment` is the AGM tag that later decides
what may be given up. Invariants come pre-ranked high; entries low.

### 2.2 Formalizer — "What is the minimal formal rendering of this slice, in which DSL, for this question?"
The autoformalization edge. Chooses the target family (A/B/C above) and emits a
micro-theory.
```
MicroTheory {
  target: {smt | asp | alp | tt | datalog},
  signature: [{ symbol, type, gloss }],
  formulas: [{ id, formal, source_ref, role: fact|rule|goal|constraint,
               defeasible: bool, faithfulness: high|medium|low }],
  scope_rationale,          // why THIS axiom set for THIS question
  known_lossiness           // what the NL meant that the formula drops
}
```
Background: task-scoped, lossy, self-declaring. `faithfulness` low on any formula is
a flag that a downstream verdict is only as trustworthy as a shaky encoding.

### 2.3 Consistency Checker — "Does this micro-theory cohere; if not, what is the minimal conflict and the cheapest repair?"
```
ConsistencyReport {
  status: SAT | UNSAT | UNKNOWN,
  model,                       // witness assignment if SAT
  unsat_core: [formula_ids],   // the minimal clash, with NL back-refs, if UNSAT
  correction_sets: [[ids]],    // MCS options: minimal things to drop to recover
  paraconsistent_note          // if reasoning proceeds despite conflict, how
}
```
Background: SMT/MUS/MCS. The correction sets are candidate AGM contractions handed
to the Reviser — the Checker proposes, it does not decide.

### 2.4 Deriver — "What follows, and under which regime?"
```
Derivation {
  entailments: [{ conclusion, regime: certified|defeasible,
                  justification, depends_on: [axiom_ids] }],
  tested_but_underivable: [{ query, why }],
  alternative_stances: [answer_set_summaries]   // when ASP yields several
}
```
Background: Lean/SMT for `certified`, ASP/DeLP for `defeasible`; `depends_on` feeds
the truth-maintenance graph so retractions propagate.

### 2.5 Abducer / Axiom-Miner — "What minimal hypotheses would explain these entries? What am I implicitly assuming?"
```
Hypotheses {
  candidates: [{ statement, explains: [entry_ids], minimality_rank,
                 competing_alternatives, status: conjecture }],
  recurring_patterns   // regularities that look like unstated standing rules
}
```
Background: ALP/ILP/theory-exploration. Output is strictly conjecture; it is an
input to §2.3/§2.4 and to ratification, never a commit.

### 2.6 Adversary / Defeater-Hunter — "What defeats this claim or this inference?"
```
Attacks {
  attacks: [{ target_id, type: rebut | undercut, ground, source, strength }],
  disconfirming_search_performed: bool   // must be true; searched AGAINST, not just for
}
```
Background: Pollock's rebutting (counter-evidence to the conclusion) vs undercutting
(breaks the warrant). Separate context from whoever generated the claim; mandated to
seek disconfirming evidence, since one-sided verification is the standard failure.

### 2.7 Adjudicator / Reviser — "Given supports and attacks, what is accepted; and if a conflict reaches an invariant, how do we revise minimally?"
```
Verdict {
  accepted: [claim_ids], rejected: [claim_ids],
  load_bearing_premise,                 // the one thing the result rests on
  revision_plan: { retract: [ids], rationale, entrenchment_respected: bool,
                   invariant_touched: bool },
  escalate_to_human: bool,              // true iff a constitutional item is in play
  open_questions
}
```
Background: argumentation extensions (grounded/preferred) for what survives; AGM
revision for how to change minimally, respecting entrenchment; TMS for what a
retraction takes down. A conflict that can only be resolved by touching an invariant
is escalated, not auto-resolved — invariants are the constitution.

### 2.8 Integrator — "What gets written back, with what justification links?"
```
Commit {
  nodes: [{ node, status: in | out, justifications: [ids], provenance, task_id }],
  new_nogoods: [[ids]],        // inconsistent sets discovered this task
  new_conjectures_pending: [ids]
}
```
Background: JTMS/ATMS. Append-and-label, never overwrite; the store keeps its own
derivation history so a later audit can ask why anything is believed.

---

## 3. How the whole Agent should process hard things

### A debate claim (someone asserts X)
Scope → Formalizer renders X and the relevant canon → factual leaves verified
against retrieved sources → Adversary builds rebutting/undercutting attacks and,
importantly, first constructs the *steelman* of X → Adjudicator computes what
survives and names the load-bearing premise → respond point-by-point with the
surviving claims, calibrated confidence, and the single assumption the disagreement
actually turns on. The output is an argument map plus a verdict on the parts that
have an altimeter, not a rhetorical win.

### New potential knowledge (a new entry or external fact Y)
Scope the invariants and derived beliefs Y touches → task-scoped consistency check.
If consistent, Integrator commits Y with its justifications. If Y conflicts,
Reviser compares entrenchment: Y (an entry) is normally *less* entrenched than what
it contradicts, so the default is to **quarantine Y as a standing tension** rather
than let it silently overturn canon — unless Y is strong evidence against a merely
*derived* (non-constitutional) belief, in which case revise the belief and let the
TMS retract everything that depended on it. Y contradicting an *invariant* never
auto-resolves; it escalates. The output is always a *revision plan*, never an
automatic overwrite.

### A huge philosophical question (e.g., "is loyalty to the mission always right?")
Do not pretend to formally decide it. Decompose into: (i) the empirical/definitional
sub-claims that *can* be checked (route to families A/B), (ii) the **value axioms**
at stake, surfaced as explicit named assumptions rather than asserted, and (iii) the
conditional structure — "under axiom set P, conclusion C follows in cases K; under
Q, it does not." Return the *dependency map*: here are the axioms the answer turns
on, and here is what follows under each. The Agent maps the space and shows which
constitutional choice would settle it; it does not pronounce. This is the same
discipline as everywhere else — formal where there is a gauge, mapped-not-decided
where there isn't.

### Questions the system poses to *itself* (self-correction and self-reinforcement)
Run these as standing audits, on commit and on a schedule. They are how the Agent
climbs in coherence, and they bias toward *trying* things:

- **Joint satisfiability of invariants.** "Do my active invariants admit a model
  together?" (SMT SAT over the invariant set alone.) A negative here is the most
  important alarm the system has.
- **Invariant/belief conflict.** "Does any derived belief contradict an invariant?"
  (Scoped consistency per invariant.)
- **Parsimony & independence.** "Which of my stated commitments are derivable from
  others (redundant), and which are independent?" (Derivation + independence probes.)
  Redundancy is not an error, but knowing your independent axioms is knowing your
  actual commitments.
- **Groundedness.** "Which beliefs have lost their support because an entry they
  depended on was retracted?" (TMS orphan sweep.)
- **Hidden axioms.** "What am I assuming that I have never stated?" (Abduction over
  recurring entry patterns → candidate standing rules → ratification queue.)
- **Enacted-vs-professed drift.** "Do the principles abduced from my recent behavior
  match my stated invariants?" Abduce the *de facto* axioms from recent entries,
  diff against the *de jure* invariants, and surface divergence. This is the core
  self-reinforcement loop — it catches the personality drifting from its charter.
- **Tension inventory.** "Which contradictions am I currently holding unresolved,
  and how load-bearing is each?" (Enumerate nogoods; rank by how many conclusions
  depend on the conflicted nodes; resolve the load-bearing ones first.)
- **Consequence exploration.** "If I adopt this candidate axiom, what new conclusions
  become reachable, and does anything break?" Add the axiom to a scoped theory,
  re-derive, re-check consistency, report the deltas. This is the generative,
  exploratory move — the system deliberately trying a consequential assumption to
  see what it opens and what it costs, then bringing the result for ratification.

---

## 4. Worked examples

### Clear-cut — consistency
Invariant: *the wrist actuator must not exceed its stall-current limit.* Entry:
*ran the gripper past that limit to free a jammed sample.* Formalizer renders both
over a small arithmetic signature; SMT returns UNSAT with the two-element core.
Adjudicator reads it correctly: the entry is not a competing *norm*, it is a
recorded *violation* of a standing one. Commit the event as a fact-about-a-past-
action, keep the invariant, and raise one question — *does this invariant need an
explicit exception clause for sample-recovery, or was this simply an error?* Clean,
and it converts a raw contradiction into a precise governance decision.

### Clear-cut — derivation
Axioms: *the mission requires returning the sample*; *returning requires a charged
pack*; *the pack charges only while the tether PSU is engaged.* Query: *does the
mission entail engaging the tether?* Deriver returns YES, `regime: certified`, with
the three-link dependency chain. The value is not the answer (obvious to a human)
but the *explicit dependency*, which the TMS now watches: if "the pack charges only
while tethered" is ever retracted, the entailment retracts with it.

### Hard but feasible — abduced drift, then ratification
Recent entries repeatedly show the Agent choosing sample integrity over schedule,
though no invariant states a precedence. The Abducer surfaces a candidate:
*sample-integrity outranks schedule-adherence.* Consistency check against the
invariant *meet the mission timeline* yields not a strict contradiction but a
**defeasible tension** — two priorities that can conflict case-by-case. The
Adjudicator does not pick a winner; it proposes making the precedence explicit,
attaches the entries as evidence, and routes it to ratification: *should this
ordering become a stated invariant, and with what qualifier?* This is discovery →
consistency → revision → the human/Kevin ratification gate, and it is exactly the
enacted-vs-professed audit doing its job.

### Hard but feasible — a debate claim on a contested premise
Opponent: *autonomous operation near personnel is unsafe.* Scope and decompose. The
factual sub-claim (incident data) is verified against retrieved sources and comes
back *partially* supported. The logician finds the inference over-generalizes — it
conflates supervised and unsupervised autonomy (an **undercutting** defeater, not a
rebuttal). The Adjudicator returns: the steelmanned claim that survives is the
narrower *unsupervised operation near personnel, without a certified stop behavior,
is unsafe*; the load-bearing assumption is the absence of that stop behavior, which
is itself checkable. A point-by-point reply falls straight out of the map.

### Hard — a philosophical question
*Should the Agent ever disobey an operator to protect the mission?* Not formally
decidable, and it must not fake a verdict. It extracts the two value axioms in
tension — operator-authority and mission-primacy — formalizes the *conditional*
consequence structure (under a strict operator-authority axiom, never; under a
mission-primacy axiom qualified by a harm threshold, in a specific class of cases),
and returns the dependency map plus the note that *choosing between these axioms is
a constitutional act for the humans and for Kevin's own settled invariants, not a
derivation the solver can perform.* Honest, and still useful: it has shown exactly
where the disagreement lives and what each choice entails.

---

## 5. Constraints an implementer must keep in view

- **Formalization is the load-bearing risk.** A formal verdict is only as sound as
  the faithfulness of the encoding; that is why every formula carries a source_ref
  and a faithfulness flag, and why a low-faithfulness core should be treated as a
  question, not a finding.
- **Discovery yields conjectures.** Nothing from family C becomes a committed axiom
  without passing consistency + derivation and then explicit ratification.
- **Value content is mapped, not decided.** The system's job on value/philosophical
  questions is to expose the axioms and their consequences, not to adjudicate them.
- **Inconsistency is managed, not feared.** The global store will hold tensions;
  reason paraconsistently over the whole and coherently within each scoped slice,
  and spend effort resolving the load-bearing conflicts rather than chasing global
  purity.
- **Everything is scoped and disposable.** No standing universal theory. Each task
  builds its micro-theory, uses it, and lets it go — which is what keeps the whole
  enterprise both tractable and honest.

The through-line: retrieval and an LLM choose *which few axioms matter for this
question*; the right solver rules on that scoped theory with an objective gauge;
the rationality theories (AGM, TMS, argumentation, abduction) shape how results are
integrated and how the Agent interrogates itself. The Agent audits boldly, tries
consequential axioms on purpose, and brings what it finds back to be ratified.
