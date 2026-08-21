# Research → τ: Medium-Term Advancement Evaluation Plan

Status: **DRAFT / speculative** (2026-07-16). This is an *evaluation* plan, not a
build plan. It reads two external research reports against τ's actual code and the
existing G / JMFTS / fork roadmaps, decides what each cited resource is worth to us,
and defines the experiments that would turn "the report says X" into "we measured X
on our stack." Nothing here is scheduled work until an experiment returns a verdict.

The two source reports (both live in `docs/` as PDFs, and as markdown in the owner's
vault):

- **MEM** — *Knowledge Substrate for Persistent, Continuously-Learning LLM Agents.*
  Memory architectures, consolidation/forgetting, self-improvement, skill libraries.
- **GCD** — *Grammar-Constrained & Structured LLM Decoding for Decode-Pass-Conserving
  Agent Harnesses (2024–2026).* Grammar compilers, jump-forward, small specialist
  models, the reasoning-vs-constraint literature.

**Ground rule (owner's standing "Fail-Early"):** the reports are not truth. Every
item labeled for adoption below carries an experiment that must *pass on our stack*
before it becomes roadmap. We validate by trying, and the results drive the roadmap —
not the reports' self-reported percentages.

---

## 1. The thesis: the two forks converge, they don't parallel

τ has tracked constrained-generation (the G-series) and JMFTS-as-substrate as two
backlogs merged only by scheduling (the W-series). The reports make the deeper claim
that they are **one system** seen from two ends, and τ already built the three seams
where they meet:

| Convergence point | Constrained-gen side (GCD) | JMFTS side (MEM) | τ seam that already exists |
|---|---|---|---|
| **Retrieval-review** | N verdict completions, `choices` grammar, ~1 forward pass each under jump-forward | N JMFTS search hits, each judged include/exclude/examine-children | `ctx.complete(constraints=…)` (C1/G3) + `spawn_branch` (C2); demoed in `examples/60_retrieval_review.py` |
| **The cascade** | "tool calling is retrieval-and-assembly, not reasoning → a cheap tier owns it" (Needle, FrugalGPT, cascade routing) | — | **C1 Piece B** (route compaction/sub-tasks to a cheap/constrained model) — the open debt with no pi precedent. GCD supplies the missing design and a concrete cheap tier. |
| **Skills-as-code** | a skill's payload is a constrained tool call; SKILL.md three-tier disclosure | a skill library is a JMFTS subtree, embedded by description, executed by branch sub-agents | `jmfts_*` tools + `spawn_branch` + the enrich embedding path |

**Consequence for the roadmap:** the highest-leverage next items are not "more memory
features" or "more grammar features" — they are the *bridges*: finishing C1 Piece B
(the cascade), and building the first **separate-doctree extension** (memory/skills)
that reads back either as a tool call or an auto-injection. Both reports point at the
same two primitives τ already has (`ctx.complete`, `spawn_branch`) and the same store
(JMFTS) as the substrate.

---

## 2. The two behaviors, made precise

The owner's framing — extensions come in two flavors, and the preference is for new
extensions to own **independent subtrees** — maps cleanly onto mechanisms τ already
has (JMFTS plan §2.4, §6). Naming them so the rest of the doc can reference them:

- **B-SEP ("separate doctree").** The extension owns a JMFTS subtree under its own
  root and `usetype` namespace (`psych:*`, `skill:*`, `memory:strategy`, …), *not*
  under any conversation. It is populated by background work (a `session_shutdown` /
  idle job — the MEM report's "sleep-time compute") and is shared across conversations
  and working directories. This is the owner's stated preference and the correct
  default: an affective profile, a skill library, and a strategy bank are all
  cross-conversation assets, not artifacts of one chat.
  - **Read-back is deliberate:** an agent-facing tool (`jmfts_search`-shaped, scoped
    to the extension's root) the model calls when it decides it needs the data.
- **B-IN ("in the conversation's doctree").** The extension writes *into* the active
  conversation subtree — either a foreign document hung off a message (invisible to
  model input, per the whitelist), or an auto-injection at `before_agent_start` that
  becomes a **durable `customMessage`** (tree-as-truth: no hidden channels; the
  injection is an inspectable entry, not an intercepted prompt).
  - **Read-back is automatic:** relevance-triggered, injected without the model asking.

A mature extension is usually **B-SEP for storage + both read-backs**: it collects and
consolidates in its own subtree continuously (B-SEP write), and the *same* data is
reachable either by a tool call (B-SEP read) or auto-injected when relevant (B-IN
read). The owner's SEANCE/LangExtract affective-profile example is exactly this shape,
and it is the template the extension catalog (§5) generalizes.

**Why independent subtrees are the right default** (beyond the owner's preference):
scoping. A JMFTS search scoped to `parent_id = <extension root>` searches exactly that
extension's data; conversation search stays clean; delete/retention policy is
per-extension; and provenance (the MEM report's #1 hardening ask) is structural — a
`skill:*` node's lineage says where it came from. Mixing extension data into
conversation subtrees would forfeit all four.

---

## 3. Inventory — MEM report (memory substrate)

Labels: **discard** (not for τ) · **conceptual** (informs thinking / cautionary,
no port) · **patterns** (reimplement on our stack, no code adoption) · **code**
(download/adopt actual code, weights, data, or a standard).

| Item | Label | τ-specific verdict |
|---|---|---|
| **RAPTOR** (recursive cluster-summarize tree) | **code** | JMFTS *already implements* RAPTOR + extract-facts. Wire enrich to run it over finished conversation subtrees. Caveat: RAPTOR's unit is *children-of-a-node* = a **cluster**, but a mirrored conversation is a **chain** — this is exactly what CR-5 (path summarization) exists to fix. Experiment M1. |
| **Generative Agents** — recency×importance×relevance scoring + reflection tree | **patterns** | The scoring formula (`α·recency + β·importance + γ·relevance`) is trivially implementable over JMFTS columns (`importance`, `last_access`). Cleanest starting point for episodic read/write. Experiment M2. |
| **Mem0** — hybrid retrieval (vector+BM25+entity+temporal), ADD-only + recency resolution | **patterns** | The fusion is *exactly* what JMFTS's `/search/*` primitives already expose (vector, bm25, fulltext). Copy the fusion + read-time recency sort; do not adopt the library. |
| **Reflexion** — verbal RL into an episodic buffer | **patterns** | Template for the work-review leg: reflect on a completed/failed task, write the lesson, retrieve next time. Ground the pass/fail label in an external signal (test/tool exit), not self-score. |
| **ReasoningBank** — retrieve→extract→consolidate *strategy items*, success **and** failure; MaTTS | **patterns** | The model to emulate for self-improvement. Runs entirely on a vector store (= JMFTS). Strategy items live in a `memory:strategy` B-SEP subtree. Experiment M3. |
| **ACE** — Generator/Reflector/Curator, incremental **delta-merge** playbook | **patterns** | The load-bearing lesson is the *anti-pattern*: never let consolidation rewrite the whole memory blob (context collapse / brevity bias). Append/merge small structured items. This is a hard constraint on any consolidation job we write. |
| **Voyager** — growing skill library, verified code indexed by description embedding | **patterns** | Archetype for the skill library: verified code, embedded by NL description, retrieved and composed. Maps onto SKILL.md + `spawn_branch`. Experiment M5. |
| **SKILL.md / Anthropic Agent Skills** (open standard, three-tier disclosure) | **code** | Adopt the *format*. Store SKILL.md as JMFTS docs; embed the `description` (discovery layer); load the body on match; execute bundled scripts via C2 branch sub-agents. τ's owner already uses Claude Code skills — same standard. Experiment M5. |
| **Sleep-time compute** (Letta) — async idle agent edits memory | **patterns** | The scheduling wrapper for every B-SEP consolidation job. In τ this is a `session_shutdown`/idle worker, not a live-loop cost. Code exists (`letta-ai/sleep-time-compute`) but we reimplement on our loop. |
| **Letta heartbeats / autonomous turns** | **conceptual** | The mental model for driving consolidation with no user in the loop. τ's equivalent is an idle/shutdown hook, not a heartbeat flag on every tool call. |
| **A-MEM** — Zettelkasten self-evolving linked notes | **conceptual** | The self-evolving-neighbor idea is a good consolidation primitive (JMFTS has links). But 2 LLM calls/event is expensive; adopt the *idea* (update neighbors on new note) only if M3 shows link-following helps. |
| **Zep / Graphiti** — bi-temporal edge validity | **conceptual** | The **bi-temporal validity model** (`valid_from`/`valid_to`/`ingested_at`) is the single best idea for defeating staleness — implement as columns, not as Graphiti. The Neo4j graph store itself: **discard** unless multi-hop retrieval proves necessary. |
| **LangMem** — three-type taxonomy, hot-path/background split | **conceptual** | Adopt the taxonomy (episodic/semantic/procedural) and the hot-path-tool vs background-manager split (= B-IN vs B-SEP). Do **not** adopt the library (pre-1.0, 60s p95 in benchmarks). |
| **HippoRAG** — Personalized PageRank over a KG | **discard** (for now) | Only if internal multi-hop Recall@k is materially below target with hybrid dense+BM25 + a Postgres recursive CTE. Default: stay on JMFTS's existing hybrid search. |
| **SEAL** — RL self-edit of weights | **discard** | Out of scope for a durable substrate. Authors themselves report catastrophic forgetting on repeated edits. Keep learning in the store. |
| **Online LoRA / adapters, ROME/MEMIT knowledge editing** | **discard** | Same reason. Revisit only if a stable, heavily-reused capability must be retrieval-free for latency — none of τ's stories need that. |
| **Self-Rewarding / Meta-Rewarding LMs** | **conceptual** (cautionary) | Weight-update, and its lesson is a *warning*: self-critique loops overfit their own judge (score saturation, reward hacking). Directly informs M3 — ground work-review in **external verifiable signals**, not pure self-scoring. |
| **Self-Refine, AWM (workflow memory)** | **conceptual** | Self-Refine = single-episode, pair with a store for durability. AWM = success-only (blind to failure); ReasoningBank subsumes it. |
| **Experience Compression Spectrum** (L1/L2/L3 survey) | **conceptual** | Framing: distilled skills (L2) beat raw-trace retrieval (L1). Justifies investing in M3/M5 over raw episode hoarding — but see the verbatim caveat below. |
| **LoCoMo / LongMemEval / BEAM / MemoryAgentBench** | **code** (as eval data) | Download as internal eval datasets. Public benches measure recall well, forgetting poorly — so we *add* a forgetting probe. Experiment M2/H1. |
| **STALE benchmark** — implicit-conflict staleness | **code** (as eval data) | Download; build a staleness probe from it. The staleness failure mode (job-change example) is real and undefended and hits exactly the "learn from experience" loop. |
| **AgentPoison / MINJA / MemoryGraft** (poisoning) + A-MemGuard | **conceptual** (hardening) | Not experiments to run *for* capability, but the reason B-SEP needs provenance + a **review gate** before an induced skill/experience becomes retrievable. Experiment H1. |
| **"Verbatim Chunks Beat Extracted Artifacts"** (2601.00821) | **conceptual** | Caution against over-aggressive extraction: **keep raw episodes retrievable** even after extracting semantic notes. τ already does — the conversation subtree is never rewritten. Reinforces a decision we made. |
| **SEANCE + LangExtract** (from the owner's cited affective-computing paper) | **code** | Download both. SEANCE = sentiment/affect indices; LangExtract = Google's schema-driven extraction. These are the engine of the flagship B-SEP extension (§5, E-Affect). Experiment M4. |

---

## 4. Inventory — GCD report (constrained decoding)

| Item | Label | τ-specific verdict |
|---|---|---|
| **llama.cpp `json-schema-to-grammar` / GBNF** | **code** (already integrated) | τ reaches this via `response_format`/`json_schema` on the server. Nothing to add; know its gaps (no mixed `properties`+`anyOf`, anchored patterns, no nested `$ref`). |
| **llama.cpp lazy grammars** (`grammar_lazy` + triggers) | **code** | The native mechanism for reason-then-constrain. τ has *already written* the upstream PR (`docs/lazy-grammar-thinking-pr.md`) that makes constrained calls able to think — removing the `enable_thinking:false` workaround. This is the **highest-value, lowest-cost** GCD item because the code exists. Experiment E5. |
| **Jump-forward / fast-forward decoding** | **code** | The core speedup. This *is* the turboquant fork Phases A–D → emits `n_ff_total`. Unblocks G6/G7. Experiment E2. |
| **llama.cpp n-gram speculation** (`--spec-type ngram-mod`) | **code** (config-only) | No download, no draft model — turn it on. Natural fit for repetitive structured output. Watch for batch-1 regression. Experiment E3. |
| **Draft-model speculative decoding** (`--model-draft`, EAGLE-3) | **conceptual** | A large-batch win that can *regress* at batch-1 on a fast target. Only if E3 shows n-gram insufficient and the target is slow enough to amortize. |
| **Cactus Needle** (26M, MIT, SAN arch, query+tools→JSON) | **code** | The concrete cheap tier for the cascade (C1 Piece B). Download `Cactus-Compute/needle` (14MB INT4), test as (a) tool router and (b) argument pre-filler. Strong on latency + structural correctness, weak on tool *selection*. Experiment E1. |
| **Cascade / router pattern** (FrugalGPT, AutoMix, cascade routing) | **patterns** | The design C1 Piece B needs and pi has no precedent for: cheap tier owns retrieval-and-assembly, escalate to the big model on a confidence gate. Needle is one cheap tier; a quantized small Qwen is another. |
| **Field-ordering: reasoning-before-answer** (Instructor Tabular-CoT) | **patterns** | The single most cross-verified schema best practice. Any τ-side schema/`grammar.py` helper and any tool that wants CoT must emit the reasoning field first (required-properties-first gotcha). Bake into the design guideline (§6). |
| **Align grammar to the model's native tool-call format** (2510.07248; suppression 2606.25605) | **patterns** | Critical: constraining to plain JSON when the model emits `<tool_call>` XML makes the native path unreachable — actively suppressing correct behavior. **Confirms τ's existing raise-on-constraint+tools policy** and points at trigger grammars on native tokens as the only safe constrained-tool path. Experiment E6. |
| **Example selection for symbol coverage** (Levy et al.); dottxt "one bad example dominates" | **patterns** | Few-shot engineering for grammar fills: select 1–3 examples for structural coverage, clean bad ones. Applies to every constrained-fill tool. |
| **JSONSchemaBench** (~9.5k schemas) | **code** (as eval harness) | Adopt as the constrained-decoding efficiency/coverage/quality bench. Includes the GSM8K lift (80.1→83.8%). Experiment E4. |
| **BFCL-v3 / Mobile Actions / SealTools / OpenFunction / ToolAlpaca** | **code** (as eval data) | Function-calling correctness benches for E1 (Needle) and E4 (constrained tool calls). |
| **Structural tags** (NVIDIA/vLLM/SGLang) + **XGrammar-2** (TagDispatch, Cross-Grammar Cache) | **conceptual** | The *agentic* frontier (dynamic per-tool grammar switching). τ's serving stack is llama.cpp, whose lazy grammars approximate structural tags; the cross-grammar substructure cache is a fork-relevant idea, not a drop-in. Informs the fork roadmap, not τ core. |
| **XGrammar / Outlines / guidance / lm-format-enforcer** (the compiler zoo) | **conceptual** | τ standardizes on llguidean-lark (what jump-forward accelerates). These inform the mask-cache / coalescence / token-healing *ideas* but are different serving stacks — no port. `lm-format-enforcer`: **discard** (client-side token filtering is the LMQL mistake the recon rejects). |
| **NVIDIA grammargen** (command-evidence → Lark) | **conceptual** | The "compile a signature straight into a constraint" idea — relevant if τ ever auto-generates grammars from tool schemas beyond what `--jinja` does server-side. |
| **SimpleTool** (parallel per-argument heads) | **conceptual** | Requires model retraining + a custom serving loop — not a drop-in. The portable insight: arguments are weakly causally dependent → the entropy taxonomy (structural vs name vs value tokens) informs which fields to constrain hard vs leave free. |
| **Token healing / Domino** | **conceptual** | Token-boundary bias at the prompt/grammar seam. Portable in spirit to GBNF (encode the prefix into the grammar); revisit only if E4 surfaces boundary artifacts. |
| **Tam et al. "Let Me Speak Freely?" / Grammar-Aligned Decoding (ASAp) / Format-Tax decomposition** | **conceptual** | The reasoning-degradation literature. Its synthesis — *reason-first, constrain-later; degradation is mostly prompt-level not decoder-level* — is **already τ policy**. Format-Tax's decomposition sets E4's threshold: >3% gap ⇒ fix schema/prompt before the decoder. |
| **gbnfgen / json-schema-to-gbnf / gbnf crate** | **discard** | τ uses llguidance-lark, not hand-rolled GBNF generators. |

---

## 5. Proposed JMFTS-based extensions (the B-SEP catalog)

Each is optional, ships in `tau_jmfts.ext`, owns an independent subtree (B-SEP), and
reads back via a scoped tool and/or a `before_agent_start` injection (B-IN). Ordered
by how much of the convergence (§1) they exercise, i.e. by learning value.

### E-Affect — the affective/psychological profile (the owner's flagship)
- **Behavior:** B-SEP storage (`psych:*` subtree, one root per user/identity), B-SEP
  read (a `recall_profile` tool) **and** B-IN read (inject relevant profile facts at
  `before_agent_start` when the incoming prompt is affect-relevant).
- **Engine:** SEANCE (affect indices) + LangExtract (schema-driven extraction) run as a
  `session_shutdown` background job over recent conversation text — the model is never
  interrupted (sleep-time pattern). Writes structured `psych:observation` docs; a
  consolidation pass (ACE delta-merge discipline) rolls them into a `psych:profile`.
- **Feeds on inventory:** SEANCE+LangExtract (code), sleep-time (pattern), ACE
  delta-merge (pattern), bi-temporal validity (conceptual — a profile fact can go
  stale: "state beats trait"), provenance/review-gate (hardening).
- **Experiment:** M4.

### E-Strategy — the work-review / self-improvement bank (ReasoningBank)
- **Behavior:** B-SEP storage (`memory:strategy`), B-SEP read (`recall_strategy` tool)
  + B-IN read (inject top-k strategies before a similar task).
- **Engine:** after a task whose success/failure is **externally verifiable** (test
  pass, tool exit, a constrained self-check), distill a strategy item (title + one-line
  + heuristics/constraints), learning from success *and* failure. Consolidation
  de-dups.
- **Feeds on:** ReasoningBank + Reflexion (patterns), Self-Rewarding (cautionary — the
  external-signal requirement), ACE (delta-merge).
- **Experiment:** M3.

### E-Skills — the skill library (Voyager + SKILL.md)
- **Behavior:** B-SEP storage (`skill:*`, one doc per SKILL.md), B-SEP read (three-tier
  disclosure: names+descriptions always available; body loads on match; scripts execute
  via `spawn_branch`).
- **Engine:** induce skills from verified successful traces; lifecycle create/deepen/
  deprecate (Trace2Skill/TroVE — utility = usage×success) in the sleep-time job; a
  **review gate** before an induced skill becomes broadly retrievable (poisoning
  defense). A skill's execution payload is a **constrained tool call** — this is where
  E-Skills touches the GCD fork directly.
- **Feeds on:** SKILL.md (code), Voyager (pattern), skill-induction wave (conceptual),
  poisoning defenses (hardening).
- **Experiment:** M5.

### E-Retrieve — retrieval-review context injection (already partly demoed)
- **Behavior:** B-IN. On `before_agent_start`, fan out JMFTS search over a project
  knowledge index, judge each hit with a constrained verdict completion (`choices`
  grammar, C1/G3), fold survivors into one durable `customMessage`.
- **Engine:** the exact `examples/60_retrieval_review.py` pattern, promoted from demo to
  extension; this is the single richest convergence artifact (constrained verdicts +
  JMFTS search + branch fan-out).
- **Feeds on:** everything in §1's retrieval-review row; benefits most from jump-forward
  (E2) making verdicts ~free.
- **Experiment:** rides E2's telemetry (measure forced-share on the verdict calls).

**Common infrastructure these share** (build once): a small `B-SEP store helper` that
creates/opens an extension root by `usetype`, a scoped-search wrapper over `JmftsClient`
(`parent_id = ext root`), the columns the MEM report asks for on extension docs
(`importance`, `last_access`, `valid_from/valid_to/ingested_at`, `usage/success count`,
`source/provenance`), and a background consolidation scaffold (sleep-time). None of
these exist yet; all four extensions want them.

**Seam reality check** (from a 2026-07-16 audit of the actual code — three gaps that
turn "reuse a hook" into net-new code, so they're sized honestly here):

- **B-IN auto-injection is net-new.** `before_agent_start` *exists* as a mutating hook
  (chains `system_prompt`, accumulates durable `message`s — exactly the no-hidden-channel
  injection B-IN needs), but **nothing in `tau-jmfts` wires it today** — retrieval is
  deliberately tool-only (B-SEP read). E-Retrieve and E-Affect's B-IN read-back are the
  first users of that hook and cost real wiring, not a config flip.
- **The sleep-time job has no idle-turn host.** τ's lifecycle hooks are `session_start`
  / `session_shutdown` only — there is no Letta-style heartbeat or autonomous idle turn.
  `enrich.py` already runs its embedding pass on `session_shutdown`; that is the sleep-time
  analog we have. A *continuous* background consolidator (the MEM report's ideal) is
  therefore either a `session_shutdown` job or a **separate out-of-band process** hitting
  JMFTS directly — a design choice each B-SEP extension must make, not a given.
- **`JmftsClient` has no triples/links/batch-embed wrappers.** It wraps documents,
  subtree, indexes, and `search` — but the A-MEM link-table idea, the Zep-style graph,
  and the ontology layer (JMFTS plan §6) all need **net-new client methods over existing
  server routes** (and CR-3 batch-create for the importer/fork). So "conceptual" labels
  on A-MEM links / graph aren't just research caution — they're also unwrapped surface.

---

## 6. The inference-tightening question (constrained-gen-aware design)

The owner's question: *to what degree must tools and extensions be designed to support
constrained-generation output, and what does good design look like, to maximize
inference speed from the conception of an agent's design?* The reports answer it as a
**design discipline**, summarized here as the guideline the extension catalog and any
new tool should follow:

1. **Classify every completion by GCD's entropy taxonomy.** A completion is some mix of
   (a) near-zero-entropy **structure** (`{`,`}`,`:`, keys, fixed literals), (b)
   predictable **names** (tool name, enum), (c) high-entropy **values/reasoning**.
   Structure and names are grammar-forced and, under jump-forward, ~free. Only (c) costs
   forward passes. **Design to maximize (a)+(b) and minimize (c) to exactly what the
   task needs.**
2. **Verdict/classifier completions → `choices`/enum grammar.** A retrieval-review
   verdict, a router decision, a yes/no gate: nearly 100% forced, ~1 forward pass. This
   is the cheapest possible completion and the reason E-Retrieve scales.
3. **Reason-first, constrain-later — never constrain the reasoning.** A constraint binds
   the *first* token; on a reasoning model that token is inside `<think>`. Either disable
   thinking (today's workaround) or use lazy grammars triggered after the reasoning
   delimiter (E5, the written PR). Field order: **reasoning field before answer field**,
   always.
4. **Do not compile τ's own tools into a grammar for the main loop.** The chat template
   owns the tool-call wire format; the `--jinja` server builds the lazy tool grammar;
   jump-forward accelerates it for free. τ's leverage is `tool_choice` passthrough +
   *proving* the effect (repair-counter → 0, forced-share telemetry). Fighting the
   template breaks per model family (§5.1 of the GCD plan).
5. **Align any hand-authored constraint to the model's native tokens.** Plain-JSON
   grammar on a Qwen `<tool_call>` model suppresses the native path (E6). If we ever
   constrain tools, it must be a trigger grammar on the model's own special tokens.
6. **The cascade: let a cheap tier own (a)+(b).** A Needle-class model or quantized small
   model extracts argument *values* and picks structure; splice them into the grammar as
   fixed literals; the big model only does (c) or verifies. This is C1 Piece B's design.
7. **Two north-star metrics, instrumented from the start:** *forced-token fraction*
   (`n_ff_total`, needs the fork — E2) and *repair count* (already instrumented; should
   be 0 under `--jinja`). Everything above is measured by these two.

**Net answer to "how much must extensions support constrained output":** every
*classifier/verdict/router* completion should be constrained (huge speed win, near-zero
risk). Every *reasoning/generative* completion should be free text, optionally with a
constrained *extraction* step after. Tools should be **shaped so their arguments are
low-entropy and extractable** (small enums, explicit optional fields for missing data,
shallow nesting) — that is what lets the cheap tier and jump-forward do their work. A
tool designed with a deep, free-form argument blob forfeits all of it.

---

## 7. Experiments / research passes

Each experiment names **what to download**, **what to run**, **the determination**, and
**the threshold** that flips a label. Grouped into three tracks; within a track, ordered
by cost-to-value. These are the "make complete determinations" passes the owner asked
for — none commits to a build.

### Constrained-gen track (validates GCD adoption + unblocks G6/G7)

- **E5 — lazy-grammar-thinking PR (do first; code already written).** Build llama-server
  from branch `pr/lazy-grammar-thinking` (`docs/lazy-grammar-thinking-pr.md`). Run τ's
  constrained `ctx.complete()` against `qwen36-35B-IQ4_XS` **with thinking on**.
  *Determination:* does a constrained call reason and still land the verdict in
  `content` (not `reasoning_content`)? *Threshold:* if it holds, the `enable_thinking:
  false` workaround is retired and constrained reasoning is unlocked — the single
  biggest quality win in the GCD fork, at near-zero cost. If it doesn't, file upstream
  and keep the workaround.
  - **RESULT — CONFIRMED at the server layer (2026-07-16).** The PR build (already on
    midlife, `repos/llama-pr-thinking-grammar/build-llg`, CPU+llguidance) vs stock master,
    same **llguidance** request (`start: "include" | "exclude"`), Qwen3.5-2B: PR →
    `content="include"` after 854 chars of free reasoning; stock → `content=""`,
    answer trapped in `reasoning_content` (the bug). The fix reaches the llguidance
    sampler exactly as the PR claims.
  - **New finding — the workaround-retirement is two-part, not one.** The fix is only
    reliable when the constrained call **also carries a reasoning budget**
    (`reasoning_budget_tokens`, from base commit `99f3dc32`). Without it a small model
    can reason past `max_tokens` and return empty `content` — which τ's `verify_output`
    would raise on as a false `ConstraintViolation`. So the τ-side change is: gate the
    `enable_thinking:False` blanket (`openai.py:266`) on a model capability **and** send
    `reasoning_budget_tokens` on grammar/choices calls when that capability is on.
  - **Confirmed on the production 35B (2026-07-16), PR CUDA+llg build :8080.** Same
    `start: "include" | "exclude"` grammar with thinking ON against `qwen36-35B-IQ4_XS`:
    the verdict lands in `content` (`"exclude"`) after real reasoning — both with and
    *without* a reasoning budget. Refinement of the two-part finding: the 35B self-terminates
    its reasoning (~730 tok) well under `max_tokens=2000` and needs no budget, so
    `reasoning_budget_tokens` is a **safety belt** (guarantees the verdict emits before the
    cap on ramblier/smaller models or tight budgets — the 2B's empty-content failure was it
    running past `max_tokens`), not a hard requirement on a capable model with headroom. The
    τ-side change should still send it (deterministic across models), but the fix itself holds
    on the 35B unaided.
  - **Remaining (code, not experiment):** a `Model` capability flag + reasoning-budget
    source, the `_apply_constraints` change, a fake-SSE payload test, and a live
    end-to-end through τ against the PR server. This is the follow-on the determination
    unblocks, tracked separately from "running the track." **E4 now prices this follow-on:
    ~43 pts of reasoning accuracy per constrained call that currently forces think-OFF.**
- **E2 — jump-forward fork + `n_ff_total` (the speedup).** Execute turboquant Phases
  A–D (roadmapped, ~230 LOC). Replay a fixed multi-tool transcript + the E-Retrieve
  verdict fan-out against (stock, fork). *Determination:* forced-token share on our
  models/hardware, and t/s delta. *Threshold:* this *is* G6; also the grammar-agnostic
  verification signal (a constraint that forced 0 tokens wasn't applied). Everything in
  §6's metric #7 depends on it.
  - **RESULT — BUILT and verified (2026-07-16).** Phases A–D implemented on branch
    `jump-forward` off `pr/lazy-grammar-thinking` (so the ff query reuses the PR's
    reasoning-budget-aware `grammar_should_apply()` gate) in
    `repos/llama-pr-thinking-grammar`. ~200 LOC across `common/llguidance.cpp`
    (`llama_sampler_llg_ff_tokens` via `llg_matcher_compute_ff_tokens` + hardened accept),
    `common/sampling.{h,cpp}` (`common_sampler_ff_tokens`, gated on `grammar_should_apply`),
    `tools/server/server-context.cpp` (slot `ff_tokens`/`n_ff_total`, query+stream after the
    free sample, single-decode injection in `handle_last_sampled_token` with only the last
    forced token carrying logits), and the `ff_n` timings field. Behind an
    `LLAMA_JUMP_FORWARD` env toggle (default on) for bisectable A/B.
    - **Byte-equivalence holds (invariant #1).** ff ON vs OFF (`LLAMA_JUMP_FORWARD=0`), temp 0
      + fixed seed, is byte-identical across 5 grammars (txn / forced-only / json-keys /
      choice-only / long-span) on Qwen3.5-2B; only pass count and `ff_n` differ. Zero
      `llg error` lines (invariant #4: real constraint death is loud, benign end-of-grammar
      stop is silent).
    - **Forced-token share:** 64–93% on structured grammars (a fixed forced literal jumps
      25/27 tokens; a TXN record 20/28; a JSON-keys template 14/22).
    - **Wall-clock generation speedup** (CPU 2B, median N=5, ON vs OFF, same output):
      **3.54x** forced-only (2 forward passes for 29 tokens = **14.5x fewer passes**),
      **2.4x** on the TXN and JSON-keys templates, **1.23x** half-free. Pure unconstrained
      generation is within **0.1%** (the per-token ff query is free when no grammar is
      active — no regression).
    - **Full-model GPU A/B (2026-07-16), the determination number** — `qwen36-35B-IQ4_XS` on
      the 4090, ff ON vs `LLAMA_JUMP_FORWARD=0`, byte-equivalence PASS on all 5 grammars:

      | grammar | forced share | OFF ms | ON ms | speedup | eff t/s ON |
      |---|--:|--:|--:|--:|--:|
      | forced-only | 93% | 158.1 | 18.4 | **8.58x** | **1574** |
      | json-keys | 75% | 175.2 | 66.6 | 2.63x | 481 |
      | txn | 75% | 177.0 | 69.1 | 2.56x | 463 |
      | half-free | 52% | 156.8 | 93.2 | 1.68x | 290 |
      | tool-call | 50% | 133.4 | 85.8 | 1.56x | 280 |

      Baseline free decode is ~180 t/s; a mostly-forced constrained call now runs at **1574
      t/s effective (~8.6x the model's own decode rate)** because only the free tokens spend a
      forward pass. As predicted, the GPU ratios exceed the CPU ones (a forward pass costs
      more relative to sampling/injection overhead). Speedup tracks forced share exactly.
    - **Edge cases pass:** stop-string inside a forced span truncates cleanly
      (`finish=word`); `n_predict` shorter than the span clamps (`finish=limit`); grammar
      completion forces EOS (`finish=eos`); 12 concurrent mixed grammar+free requests all
      stay valid (no `i_batch` corruption). Unit test `test_ff_tokens` +
      `test_ff_mask_staleness` green (open-Q4 resolved: llguidance v1.0.1 invalidates the
      cached mask on consume, so no stale-mask trap).
    - **`n_ff_total` delivered** — the grammar-agnostic verification signal (§6 metric #7): a
      constraint that forced 0 tokens was not applied. Exposed as `timings.ff_n`.
    - **v1 scope exclusions** (guarded, revisit later): disabled when a draft model is
      configured (spec+ff batch interplay) and when `n_probs > 0` (intermediate forced
      tokens carry no logits). **This closes G6**; G7 builds on it.
    - **Deployment:** the `build-jf-cuda` 35B now serves `local-llm` on `:8080` (tmux `jf35`,
      jump-forward ON) — a byte-equivalent superset of the old `pr35` build with the
      constrained-call speedup; revert to the exact `pr35` binary via `build-cuda-llg` if
      wanted. Nothing committed on the fork (branch `jump-forward`, per the no-AI-PR rule).
- **E3 — n-gram speculation (config-only, cheap).** Turn on `--spec-type ngram-mod` on
  the current server. Measure t/s on structured output at **batch-1**. *Determination:*
  keep or drop. *Threshold:* llama.cpp community shows batch-1 regressions — if t/s
  drops, drop it; if it lifts on the repetitive tool-JSON path, keep.
  - **RESULT — mostly settled (2026-07-16).** (1) **Stock master has no parameter-free
    n-gram / prompt-lookup mode** — every `--spec-*` flag requires a *draft model*
    (`--spec-draft-hf` / `--model-draft`). The GCD report's `--spec-type ngram-mod` is a
    fork feature (spiritbuun's DFlash), not stock. (2) **Baseline: the production target
    `qwen36-35B-IQ4_XS` is MoE-A3B and already decodes at 180.9 t/s** batch-1 on the
    4090. At that speed a draft model almost certainly *regresses* (draft latency
    dominates a fast target — the doc's own batch-1 warning). *Determination:* drop
    draft-model speculation for this target; the real win (n-gram forcing on the
    repetitive tool-JSON path) is a **fork** capability that composes with E2, not a
    stock config flip. Confirm with one draft-model A/B only if we want the negative
    number on record. Side-confirmed: `timings` present and populated (E4/G4 substrate
    works); `n_ff_total` absent on stock (E2's premise).
- **E1 — Cactus Needle cascade probe.** Download `Cactus-Compute/needle` (HF weights,
  MIT, ~14MB INT4) + `github.com/cactus-compute/needle`. Feed it τ's real tool schemas
  (`read`/`write`/`bash`/`jmfts_search`) + a query set (BFCL-v3 / our own transcripts,
  CPU inference). Measure tool-selection accuracy, argument correctness, latency vs the
  35B doing the same. *Determination:* is Needle viable as (a) a tool **router**, (b) an
  argument **pre-filler** spliced into the grammar, or (c) neither? *Threshold* (GCD):
  tool-selection <~90% after fine-tune ⇒ use for argument extraction on an
  already-selected tool only, not routing. Directly sizes C1 Piece B's cheap tier.
  - **RESULT — determined (2026-07-16), Needle 26M on CPU (JAX/Flax), N=20 hand-built
    queries over τ's real tools (`read`/`write`/`bash`/`grep`/`jmfts_search`).** Assets:
    `Cactus-Compute/needle` HF weights + `github.com/cactus-compute/needle` (JAX runtime,
    `generate(constrained=True)` — Needle is itself constrained-gen native). Numbers:
    - **tool-SELECTION accuracy: 65% (13/20)** — well under the 90% threshold.
    - **argument correctness given correct selection: ~92% (12/13)** — where it picks right,
      the args are almost always right (one 26M-scale token hallucination garbled a `query`).
    - **latency: ~2.5 s median / 3.3 s p90 on CPU** (would be far lower on Cactus/Metal;
      README claims 6000 t/s prefill on-device).
  - **Failure shape (verified raw, not a parser artifact):** 4/20 returned a literal empty
    tool-call list `"[]"` — Needle *declines to call any tool* on valid tool-requiring prompts
    ("df -h", "ps aux", "create a file…", "find TODOs"); 2 mis-selected (`API_KEY` search →
    `read`; "recall what I **saved**…" → `write`, the verb captured it). The empty-list misses
    returned in ~0.5 s vs ~2.5 s for real calls — a clean tell, confirmed by dumping raw output.
  - **Determination: (b) argument PRE-FILLER, not (a) router** — exactly the GCD report's
    prediction ("strong on structure, weak on selection"). For C1 Piece B this means: the cheap
    tier does **not** own tool *choice*; a route decision stays with a constrained verdict on the
    big model (or a fine-tune — Needle is finetunable locally). Needle's role is filling
    low-entropy argument values *after* the tool is chosen (§6 #6's (a)+(b) split), where its
    92% structural accuracy pays off. Re-running after a task-specific finetune (README: 45 min,
    2B tokens) is the only path to promote it to routing — logged, not scheduled.
- **E4 — constrained-vs-unconstrained on our tasks.** Download JSONSchemaBench (~9.5k
  schemas) + a function-calling bench (BFCL-v3). Measure the accuracy gap constrained vs
  free, on our models. *Determination:* is any degradation ours (schema/prompt) or the
  decoder's? *Threshold* (Format-Tax): >3% gap ⇒ fix schema/prompt (reasoning-first
  ordering, fewer enums) before touching the decoder.
  - **RESULT — determined (2026-07-16), GSM8K N=60, `qwen36-35B-IQ4_XS`, PR CUDA+llg :8080.**
    (First-signal on GSM8K — the report's own cited constrained-decoding task — not the full
    JSONSchemaBench/BFCL harness, which is a build. Accuracy over the non-truncated denominator;
    budget truncation is an orthogonal artifact, see the methodology note.) Four arms:
    - **A free text (think-ON)** → 94.9% (56/59)
    - **B json `{answer}` (think-ON)** → **98.3%** (57/58)
    - **C json `{reasoning,answer}` reason-first (think-ON)** → 98.2% (55/56)
    - **D json `{answer}` (think-OFF, `enable_thinking:false`)** → **55.0%** (33/60)
  - **Determination — the degradation is entirely OURS (thinking suppression), zero decoder tax.**
    A ≈ B ≈ C: constraining `content` to a JSON schema while thinking runs free in
    `reasoning_content` is **statistically indistinguishable from free text** (constrained arms
    nominally *higher*, and they parse cleanly where free-text last-number parsing is fragile —
    a second point for structured output). Format-Tax threshold (>3% ⇒ decoder) is **not met**:
    the json_schema decoder path is free. The whole story is D: **suppressing thinking costs 43
    points** (98% → 55%). This is a live, quantified confirmation of §6 #3 (reason-first, never
    constrain the reasoning) and the direct business case for E5's code follow-on — τ's
    grammar/choices path *still* forces `enable_thinking:false` (`openai.py:266`), and E4 prices
    that workaround at ~43 pts of reasoning accuracy. Landing the lazy-grammar-thinking change
    (E5) converts every grammar/choices call from the D regime to the B/C regime.
  - **Methodology note (Fail-Early):** the first run (`max_tokens=1400`) returned an implausible
    57% for a thinking 35B; a diagnostic showed 33% of hard cases hit `finish_reason=length` with
    empty `content` (thinking ran 3800–4500 chars, never reaching the answer) — a pure truncation
    artifact, not accuracy. Raised to 6000 tok (under the 8192 per-slot ctx) and excluded the
    residual 1–4 truncated cases/arm from the denominator; arm D never truncates (no thinking).
    The determination is robust to the exclusion — fixing truncation only *widens* the gap to D.
- **E6 — native tool-call-format suppression.** On `qwen36-35B`, compare (plain-JSON
  grammar + tools) vs (trigger grammar on native `<tool_call>` tokens) vs (no
  constraint). *Determination:* does plain-JSON constraint suppress the native tool
  path? *Threshold:* confirms/re-scopes τ's raise-on-constraint+tools policy; if a
  trigger grammar on native tokens is safe, it opens a constrained-tool path the current
  policy forecloses.
  - **RESULT — CONFIRMED (2026-07-16), on `qwen36-35B-IQ4_XS`, PR CUDA+llg server.** The
    report's suppression warning is real *on our stack*, but it enters through
    `json_schema`, not `grammar`. Full collision matrix (tools = one callable `read_file`;
    prompt "read /etc/hostname"):
    - **native (tools, no constraint)** → `tool_calls=1` `read_file(path=/etc/hostname)`,
      finish `tool_calls`, reasoning-first (the PR build reasons *then* calls). Baseline works.
    - **grammar + tools** → **HTTP 400** `"Cannot use custom grammar constraints with tools."`
      The server hard-blocks it; the collision can't even be issued.
    - **json_schema + tools** (both `response_format:json_schema` and top-level `json_schema`)
      → **HTTP 200, `tool_calls=0`, raw schema-JSON in `content`**. The native `<tool_call>`
      path silently vanishes — no error, exactly the 2510.07248 failure mode.
    - **response_format:json_object + tools** → `tool_calls=1` (too loose to suppress; coexists).
    - **tool_choice=required + tools** → `tool_calls=1`, constrained *and* native (jinja's own
      lazy tool grammar). This is the safe constrained-tool door §6 #4/#5 predicted.
    - **tool_choice=none + grammar** → allowed, constrains `content` (tools disabled → no collision).
  - **Determination:** τ's raise-on-constraint+tools policy is **validated by measurement and
    already correctly scoped.** The guard at `openai.py:222` fires for *any* decode constraint
    (grammar/choices/json_schema) when `tools` are declared and `tool_choice != "none"` — placed
    *before* the constraint-type branch, so it catches the silent `json_schema`+tools case the
    server does NOT (200, tools dropped), not just the server-rejected grammar case. The code
    comments (openai.py:188–192, 222–228) already describe both behaviors; this experiment is
    their live confirmation. The `_RESERVED_BODY_KEYS`/`extra_body` guards close the same hole
    from the `extra_body={"grammar":…}` / `tools`-as-body-option angles. **No code change needed.**
    The only *opened* path is `tool_choice=required` as a native-token trigger grammar — a future
    constrained-tool option, not a current gap.

### Memory-substrate track (validates MEM adoption; builds the B-SEP scaffold)

- **M1 — RAPTOR-over-conversation (JMFTS already has it).** Run JMFTS RAPTOR +
  extract-facts (via enrich) on a real finished τ conversation subtree. *Determination:*
  does RAPTOR-as-is produce useful hierarchical summaries over a **chain**, or is CR-5
  (path/root→node summarization) required because RAPTOR expects clusters? *Threshold:*
  poor chain summaries ⇒ CR-5 moves from "later" to "next."
- **M2 — Generative-Agents scoring + hybrid retrieval, with an internal eval.** Download
  LoCoMo + LongMemEval + STALE. Add the columns (`importance`, `last_access`, validity
  interval) and the `α·recency+β·importance+γ·relevance` + BM25/entity fusion over
  JMFTS. *Determination:* does it beat plain vector search on multi-session recall
  **and** on the STALE staleness probe? *Threshold:* if hybrid+recency doesn't lift
  staleness resolution (Mem0 saw 0.40→0.84 by read-time recency sort), the scoring
  weights or the validity model need work before any B-SEP memory extension ships.
- **M3 — E-Strategy prototype (ReasoningBank).** Build the `memory:strategy` B-SEP
  subtree + a work-review pass gated on an **external** signal (test/tool exit). Run a
  repeat-task suite (a task, then a similar task with strategies retrieved).
  *Determination:* do distilled strategies improve repeat performance without
  self-score overfitting? *Threshold:* no lift, or lift that decays across iterations
  (Self-Rewarding saturation) ⇒ the signal isn't external enough.
- **M4 — E-Affect prototype (SEANCE + LangExtract).** Download SEANCE + LangExtract.
  Build the `psych:*` B-SEP subtree + the `session_shutdown` extraction job + both
  read-backs. *Determination:* does an injected affective profile measurably change
  responses on affect-relevant prompts, and does "state beats trait" hold (recent
  observations outrank stored traits)? *Threshold:* the owner's flagship — its success
  defines the reference shape for all B-SEP extensions.
- **M5 — E-Skills prototype (Voyager + SKILL.md).** Adopt the SKILL.md format; store a
  `skill:*` subtree with three-tier disclosure; execute via `spawn_branch`; induce +
  deprecate (TroVE utility) in the sleep-time job; add the review gate. *Determination:*
  does a growing verified-skill library improve repeat-task performance (Voyager's
  claim) on our tasks? *Threshold:* library helps late (Voyager: matters most after
  80+ iterations) — measure over a long run, not a few tasks.

### Hardening track (runs alongside; gates anything that stores induced content)

- **H1 — staleness + poisoning probes.** From STALE build an implicit-conflict eval;
  add provenance columns + a review gate before induced skills/experiences become
  retrievable. *Determination:* can a poisoned "successful experience" (MemoryGraft
  shape) enter a B-SEP subtree and be retrieved trigger-free? *Threshold:* any B-SEP
  extension that ingests external content (E-Skills, E-Strategy) does not ship without
  passing this.

---

## 8. Sequencing — what feeds the roadmap

Not a schedule (owner drives that), but the dependency shape the experiments imply:

1. **E5 first.** Code is written, cost is a server build, and it unlocks constrained
   *reasoning* — which every constrained completion in every extension benefits from.
2. **E1 + E2 size the cascade.** E1 (Needle) and E2 (jump-forward `n_ff_total`) together
   turn C1 Piece B from "a config key with no precedent" into a measured design: cheap
   tier + forced-token accounting. This is the convergence bridge (§1).
3. **M2 is the memory gate.** No B-SEP memory extension (E-Affect, E-Strategy, E-Skills)
   should ship until the retrieval scoring + staleness model is validated — otherwise
   we build four extensions on a substrate that can't resolve stale facts.
4. **M4 (E-Affect) is the reference B-SEP extension.** It exercises the full shape
   (B-SEP store + both read-backs + sleep-time + delta-merge + validity) on the owner's
   own motivating example; the shared B-SEP scaffold (§5) falls out of building it once.
5. **H1 gates induction.** Anything that turns experience into retrievable content
   (E-Strategy, E-Skills) waits behind the provenance/review-gate probe.

**Cross-links:** [[constrained-gen-jmfts-workstream]] (the delivered W0–W15 line and the
C1-Piece-B / CR-2 debts this plan's experiments would discharge or reshape),
[[tree-as-truth-model-input-invariant]] (why B-IN injections are durable `customMessage`
entries, not intercepted prompts), [[reasoning-replay-divergence-from-pi]] (prefix
stability under the fan-out E-Retrieve depends on).
