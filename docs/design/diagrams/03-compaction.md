# 03 Compaction (not yet spiked)

An extension, not a core operation (D3). Its `pre_sample` hook builds a
shorter line and moves the cursor onto it; the sample sends `walk(at)` (I1).

```
 k0 ─ s ─ u1 ─ a1 ─ r1 ─ a2 ─ u2 ─ a3 ─ u3          the old line, unchanged
       └ σ  User (summary)   by extension, derived_from [a2, d2]
         └ u2'  copied_from u2
           └ a3'  copied_from a3
             └ u3'  copied_from u3     ◀ A.at   pre_sample: cursor.move(u3')

 sample walk(u3') = k0 s σ u2' a3' u3';  commit a4 under u3'
 LCA(u3, a4) = s: the hook's move was a branch at s
```

- `σ` sits under the kept prefix (`s`). `derived_from` names the last
  summarised node and, for a cold summary, the detached-root reply (`d2`).
- A copied batch is copied with its `Results`, or a step there would re-run
  it.
- **Across a model change:** if the summarised range held a `Config` the
  tail did not copy, `config_at(u3')` is the older config. To keep the
  current model, the extension copies the `Config` in force under `σ`.
- Open until Phase 5: what to keep verbatim, warm versus cold summaries, and
  when to compact.
