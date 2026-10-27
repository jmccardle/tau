# 05 Compaction as branch-from-prefix

Compaction is an extension, not a core operation. Its `pre_sample` hook builds
a shorter line out of nodes: it keeps a verbatim prefix, appends a summary node
(a `User` node by the extension, Q30) under the prefix's tip, then appends
copies of the recent nodes, `Config` nodes included, under the summary. It
returns the copy of `at` as the new context tip (Q25). The driver applies I6,
samples `walk(tip)`, and appends the reply under the tip (or under I6's
restating `Config`); the
step's move onto that reply commits the whole new line. Until then every node
the hook built is uncommitted. The old line stays committed and untouched.

`model.md` decides the mechanism (a returned tip, `copied_from`,
`derived_from`, I1, I6) but not the compaction extension's internals. What
those internals leave open is labelled **OPEN** below; nothing is guessed.

## Before: the cursor's line

```
 k0 Config(big) ─ s System ─ u1 User ─ a1 Assistant(tools: c1) ─ r1 Result ─ a2 Assistant(end)
   ... a2 ─ u2 User ─ a3 Assistant(end) ─ u3 User  ◀ A.at      (one line, continued)
 verbatim prefix : k0, s                     (kept as it is; the new line shares it)
 summarised      : u1 .. a2                  (replaced in the model's view by one summary)
 recent tail     : u2, a3, u3                (copied)
 next(walk(u3)) = Sample, and the hook decides the context is too long
```

## The hook builds the new line (all uncommitted until the sample's move)

```
 k0 ─ s ─ u1 ─ a1 ─ r1 ─ a2 ─ u2 ─ a3 ─ u3 ◀ A.at         the old line, committed, unchanged
       │
       └ σ  User (summary)    by extension(compact)   derived_from: [a2, d2]
         └ u2'  User          copied_from: u2         by extension(compact)
           └ a3' Assistant    copied_from: a3
             └ u3' User       copied_from: u3         ◀ pre_sample returns u3' as the context tip

 walk(u3') = k0 s σ u2' a3' u3'      what the model will see (I1): the context is this walk
```

Notes on what is drawn:
- `σ`'s parent is `s`, the tip of the verbatim prefix. Its `derived_from`
  lists `a2`, the last node of the range it summarised (the range is
  `walk(a2)` below `s`: `u1 .. a2`), and `d2`, the detached-root reply that
  produced the text when the summary was made by a cheap model. An in-context
  summary lists only `a2`.
- `σ` is a `User` node by the extension (Q30): legal on every API's wire.
- A copy repeats a body verbatim under a **different parent**, so it has its
  own context (I1). Its author is the extension: a copy makes the extension the
  author (Q7).
- A copied batch (an `Assistant` with calls) must be followed by copies of
  all its results. A move onto a copied batch that is not complete on the
  walk is refused, so the driver never runs a copied call (I3). If the hook
  crashes partway through the copy, the copy is left unlanded, uncommitted.
- The summary may be computed in context, or on a detached root by a cheap
  model (`04-uncommitted-and-detached-roots.md`); `σ`'s `derived_from` then
  points at that root's reply.
- The tail copies `Config` nodes too, verbatim. None sits in this tail. A
  copied `Config` counts as explicit under I6.

## The sample, and the move that commits it

```
 pre_sample returns u3'            I6: config_at(u3') = big = config_at(u3)    no restating Config
 provider.sample( walk(u3'), config_at(u3') )
 reply a4 Assistant by provider, parent u3'
 move A: u3 ──▶ a4   by core        commits walk(a4): k0 s σ u2' a3' u3' a4

 k0 ─ s ─ σ ─ u2' ─ a3' ─ u3' ─ a4 ◀ A.at         the new line, now committed
       └ u1 ─ a1 ─ r1 ─ a2 ─ u2 ─ a3 ─ u3          the old line, still committed (Q2)
 LCA(u3, a4) = s       the move is a branch at s
```

If the sample produces nothing (a 401, an abort before the first part),
no reply is written, `A.at` stays `u3`, and `σ u2' a3' u3'` remain uncommitted.

## Compaction across a model change (I6 at a sample)

```
 old line:   k0 Config(big) ─ s ─ u1 ─ a1 ─ k1 Config(cheap) ─ u2 ─ a2 ─ u3 ◀ A.at    config_at(u3) = cheap
 new line:   k0 ─ s ─ σ ─ u2' ─ a2' ─ u3'                                           config_at(u3') = big

 config differs, and no node in (LCA(u3, u3'), u3'] = (s, u3'] = σ u2' a2' u3' is an explicit
 Config, so the driver restates before sampling (or reuses a restatement already under u3'):

 k0 ─ s ─ σ ─ u2' ─ a2' ─ u3' ─ c  Config(cheap), by core     (restating, I6)
                                  └ a4  reply, sampled with the cursor's own config (cheap)
```

The reply is appended under `c`, the restating `Config`, because the sample
runs after the restatement (I6). Had the tail copied `k1`, the copy would sit
between `s` and `u3'` as an explicit `Config`, `config_at(u3')` would already
be cheap, and nothing would be restated.

## Open points, labelled with the question or section that leaves them open

- **OPEN, Phase 5 (`plan.md`), not in `model.md`:** the warm in-context
  summary built on uncommitted nodes under `at`, and the speculative candidate built
  ahead of time and committed only if the cache has expired. Neither is drawn.
- **OPEN, Q25:** the choice of the verbatim prefix's tip. The plan says "the
  deepest verbatim prefix"; `model.md` does not define how the extension picks
  it, only that the result is a node.

Reads with: `model.md` §1 (copied_from, derived_from), §3 (uncommitted: nodes built
for a sample that produced nothing), §4 I1 (parent is context), I4, I6 (config
follows the cursor at a sample), §7 (`pre_sample` returns a context tip), Q7,
Q25, Q26, Q30; `glossary.md` terms copied_from, derived_from, context, tip, LCA,
config (restating), extension.
