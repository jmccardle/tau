# D14: Bodies off every cursor's walk leave memory

Status: **accepted** (2026-10-29), built in `tau-core` and `tau-store`.
Amends D12. Shapes the `tau-py` boundary.

## Context

A session keeps every node it ever wrote: abandoned branches, the line a
compaction departed from, and every compaction copy (D3). Before D14 all of
it stayed in memory. The measurements (`docs/benchmarks.md`) showed:

- After 5 compactions of a 1M-token session, the log is ~30 MB for a
  ~460k-token context, and all of it was resident. Each cycle adds
  ~5.5 MB; a session that compacts 100 times would hold ~0.5 GB.
- Core decides from little: a node's kind and calls at the tip (`next`,
  `start_batch`), the nearest `Config` (`config_at`), the body being
  written (V2). The driver reads whole walks, but only cursors' walks.

Pruning is not an option: node ids are permanent, any node can be gone to,
and the log is the only copy of the tree.

## Decision

One rule, no tuning:

- **A body is resident** (in memory) while it is on some cursor's walk, is
  a `Config`, or is not yet synced. Every other body is **stored**: only
  the log holds it. `Store::evict(session)` drops them; the driver calls it
  after each save. It does work only after a move or an append, since a
  write at a tip makes nothing cold.
- No budget, no eviction order, no least-recently-used list. Nothing marks
  a line cold: a line goes cold when every cursor has left it, whatever made
  them leave. No assumption about how agents behave.
- **The store keeps each node's offset** in the log, filled as it writes and
  as it scans on open. Core holds no positions. A stored body is one read at
  a known offset, usually from the kernel's page cache: the OS is the cache.
- **A stored node keeps its id and parent** in memory, so `parent`, `lca`,
  `config_at`, and `append` under it work without its body.
- **Core refuses, the driver loads.** A call that needs stored bodies
  (`node`, `walk`, a move, `open`, a go-to) is refused with
  `Error::Stored(ids)`, every stored body it needs, root first, and nothing
  changes. The driver calls `Store::load(session, ids)` and makes the call
  again. A go-to waiting on a busy cursor whose line was evicted meanwhile
  is refused from `next_job` and still waits. So every cursor's walk is
  always resident, a step never waits on disk, and core stays free of IO
  without a new job.
- **Residency is not state:** not persisted, not compared (I7).
  `Session`'s equality leaves out `cooled`; a stored node equals only a
  stored one, so tests compare sessions after loading every body back.

## Alternatives

| option | why not |
|---|---|
| A memory budget with an eviction order (the first draft) | Knobs to tune, and a cache to build; the rule above leaves only what must stay. |
| A `Job::Load` before a sample | A cursor's walk is always resident, so no sample waits; the only load is at a go-to, where a refusal suffices. |
| Text stored by content (hash-keyed, shared) | Mostly a disk saving; under the rule, a compaction's originals leave memory anyway. |
| Segmented, compressed, indexed log | Disk is cheap; it only speeds open. |
| A faster encoding | Decode speed only. |
| Delete or rotate away old records | Loses nodes; any node can be gone to. |
| SQLite or another store | A second durability model (D12). |

Each deferred option can come later behind the same boundary if a
measurement asks for it.

## Consequences

- Memory is node ids and parents (~32 bytes a node) plus the cursors'
  walks, whatever the session's history. Measured in `benches/tree.rs`
  (`cycles`); see `docs/benchmarks.md`.
- Open is unchanged: the log is scanned and replayed whole, then evicted,
  so its peak memory is still the whole log. If that matters, a **header
  cache** (node ids, parents, and offsets up to a log position, checksummed,
  rebuilt by a full scan when missing or bad) fixes it without new crash
  cases.
- Disk is unchanged.
- A go-to into an evicted line reads its bodies back (one read each).
- `tau-py`: a node's body may not be in memory; `Stored` becomes a typed
  exception the driver handles by loading, never the user. Nodes never
  change, so a Python cache keyed by node id stays valid.
- D12's store gains `offsets`, `evict`, and `load`; the log format is
  unchanged.
- Enforced by `crates/tau-store/tests/residency.rs`: a session evicting
  after every save, across reopens, equals one that never evicts once its
  bodies are loaded back, and every cursor's walk stays resident; a go-to
  into a cold line is refused, then loads; a waiting go-to survives
  eviction of its line.
