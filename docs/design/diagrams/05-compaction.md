# 05 Compaction as branch-from-prefix

Compaction is an extension, not a core operation. Its `pre_sample` hook builds
a shorter line out of nodes: it keeps a verbatim prefix, appends a summary node
(a `User` node by the extension, Q30) under the prefix's tip, then appends
copies of the recent nodes, `Config` nodes included, under the summary. It
then **moves the cursor onto the copy of `at`** (Q25). The sample sends
`walk(at)` as it now stands (I1), with `config_at(at)`, and the reply is
committed under `at`. The old line stays on the tree, untouched.

`model.md` decides the mechanism (a hook's move, `copied_from`,
`derived_from`, I1) but not the compaction extension's internals. What those
internals leave open is labelled **OPEN** below; nothing is guessed.

## Before: the cursor's line

```
 k0 Config(big) ─ s System ─ u1 User ─ a1 Assistant(tools: c1) ─ r1 Results ─ a2 Assistant(end)
   ... a2 ─ u2 User ─ a3 Assistant(end) ─ u3 User  ◀ A.at      (one line, continued)
 verbatim prefix : k0, s                     (kept as it is; the new line shares it)
 summarised      : u1 .. a2                  (replaced in the model's view by one summary)
 recent tail     : u2, a3, u3                (copied)
 next(walk(u3)) = Sample, and the hook decides the context is too long
```

## The hook builds the new line and moves onto it

```
 k0 ─ s ─ u1 ─ a1 ─ r1 ─ a2 ─ u2 ─ a3 ─ u3          the old line, unchanged
       │
       └ σ  User (summary)    by extension(compact)   derived_from: [a2, d2]
         └ u2'  User          copied_from: u2         by extension(compact)
           └ a3' Assistant    copied_from: a3
             └ u3' User       copied_from: u3         ◀ A.at   pre_sample: cursor.move(u3')

 move record: { cursor: A, from: u3, to: u3', by: extension(compact) }
 walk(u3') = k0 s σ u2' a3' u3'      what the model will see (I1)
```

Notes on what is drawn:
- `σ`'s parent is `s`, the tip of the verbatim prefix. Its `derived_from`
  lists `a2`, the last node of the range it summarised (`walk(a2)` below `s`:
  `u1 .. a2`), and `d2`, the detached-root reply that produced the text when
  the summary was made by a cheap model. An in-context summary lists only
  `a2`.
- `σ` is a `User` node by the extension (Q30): legal on every API's wire.
- A copy repeats a body verbatim under a **different parent**. Its author is
  the extension: a copy makes the extension the author (Q7).
- A copied batch (an `Assistant` with calls) is followed by a copy of its
  `Results`. If it were not, the cursor landing on the copied batch would
  have `next == RunTools`, and a step would run the calls again: the cursor
  never looks at children, and nothing refuses that move. An extension that
  copies a batch copies its `Results` too. If the hook crashes partway
  through the copy, the copied nodes stay on the tree as side nodes, and
  nobody's walk passes through them.
- The summary may be computed in context, or on a detached root by a cheap
  model (`04-side-nodes-and-detached-roots.md`); `σ`'s `derived_from` then
  points at that root's reply.
- The tail copies `Config` nodes too, verbatim. None sits in this tail.

## The sample, and the commit

```
 sample phase: pre_sample moved A to u3'      config_at(u3') = big = config_at(u3)
 provider.sample( walk(u3'), config_at(u3') )
 pre_reply, then commit a4 Assistant by provider under at = u3', move A: u3' ──▶ a4 (by core)

 k0 ─ s ─ σ ─ u2' ─ a3' ─ u3' ─ a4 ◀ A.at         the new line
       └ u1 ─ a1 ─ r1 ─ a2 ─ u2 ─ a3 ─ u3          the old line, still on the tree
 LCA(u3, a4) = s       the hook's move was a branch at s
 cursor.moves(since_step=True) = [ u3 ──▶ u3' by extension(compact), u3' ──▶ a4 by core ]
```

If the sample produces nothing (a 401, an abort before the first part),
nothing is committed for the reply, `A.at` stays `u3'` (the hook's move
stands), and `next` is still `Sample`.

## Compaction across a model change

```
 old line:   k0 Config(big) ─ s ─ u1 ─ a1 ─ k1 Config(cheap) ─ u2 ─ a2 ─ u3 ◀ A.at    config_at(u3) = cheap
 new line:   k0 ─ s ─ σ ─ u2' ─ a2' ─ u3'                                           config_at(u3') = big
```

The new line's config is `config_at(u3')`, which is big: the summarised range
held `k1`, and the tail did not copy it. Nothing restates it (I6 is retired).
The sample runs with big. An extension that means to keep the cursor's model
copies the `Config` in force into the new line, for example as a copy of `k1`
directly under `σ`, so that `config_at(u3') = cheap`. That is the extension's
choice, visible in the tree.

```
 k0 ─ s ─ σ ─ k1' Config(cheap), copied_from: k1 ─ u2' ─ a2' ─ u3' ◀ A.at    config_at(u3') = cheap
```

## Open points, labelled with the question or section that leaves them open

- **OPEN, Phase 5 (`plan.md`), not in `model.md`:** the warm in-context
  summary built ahead of time under `at`, and the speculative candidate built
  ahead of time and used only if the cache has expired. Neither is drawn.
- **OPEN, Q25:** the choice of the verbatim prefix's tip. The plan says "the
  deepest verbatim prefix"; `model.md` does not define how the extension picks
  it, only that the result is a node.
- **OPEN:** whether the compaction extension copies the `Config` in force by
  default (last section). `model.md` leaves config across lines to frontends
  and extensions (I6, retired).

Reads with: `model.md` §1 (copied_from, derived_from), §4 I1 (a sample sends
its walk), I6 (retired), §5 (a hook's move, branching), §6 (where things are
committed), §7 (`pre_sample`), Q7, Q25, Q26, Q30; `glossary.md` terms
copied_from, derived_from, LCA, config, extension.
