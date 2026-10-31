# D14: Loading a subset of a long history

Status: **deferred** (2026-10-28). Every body stays in memory, and open
replays the whole log (D12). Revisit when a real session's memory or open
time is a problem.

## Context

A session keeps every node it ever wrote: abandoned branches, the line a
compaction departed from, and every compaction copy (D3). All of it is in
memory, and open replays the whole log. `docs/benchmarks.md` measures it:
after 20 compactions of a 1M-token session, 83 MB of bodies and 150 MB of
process memory; open takes 372 ms (141 MB, 283 MB peak). Each compaction
adds ~5.5 MB to the log.

Pruning is not an option: node ids are permanent, any node can be gone to,
and the log is the only copy of the tree.

## What was tried

A first version (built, then reverted; `61a8083`..`21de4d4`): a body is in
memory only while it is on some cursor's walk, is a `Config`, or is not yet
synced. A call that needs a stored body is refused with `Stored(ids)`; the
driver loads them and makes the call again. Measured on the same machine:

| | all in memory | that version |
|---|---|---|
| bodies in memory after 20 compactions | 83 MB | 3.3 MB |
| process memory after 20 compactions | 150 MB | 47 MB |
| open after 20 compactions: time / memory / peak | 372 ms / 141 MB / 283 MB | 193 ms / 17 MB / 50 MB |
| open with no compaction (1M tokens) | 12.6 ms | 20.5 ms |
| go-to | ~1 µs | ~20 µs |

Rejected for its costs, not its numbers:

- **It widened the core/driver protocol.** Reading a node, a walk, a move,
  or a go-to could be refused, and every caller needed a load-and-retry.
  The core stopped being "the data is here".
- **It evicted a compaction while it was being written.** The copy has no
  cursor on it until the move, so its save evicted it and the summary read
  it straight back.

## Requirements for a later version

- **Load the appropriate subset of a long history:** what the session's
  cursors need, not every line ever written.
- **No new refusal or retry at the core/driver boundary.** What core holds,
  it holds; the driver never catches "not loaded".
- **Nothing a cursor or an extension is building leaves memory.** A
  compaction's copy is not evicted before its move.
- Disk format unchanged unless a measurement asks for it.

An option that fits: decide residency at **open** only (read the node
headers and the cursors' walks; leave the rest in the log), and load
another line through an explicit call that happens before core sees it,
never as a refusal. Not designed yet.

## Alternatives considered with the first version

| option | why not, then |
|---|---|
| A memory budget with an eviction order | Knobs to tune, and a cache to build. |
| A `Job::Load` before a sample | A new job kind for the same reason as the refusal. |
| Text stored by content (hash-keyed, shared) | Mostly a disk saving. |
| Segmented, compressed, indexed log | Disk is cheap; it only speeds open. |
| A faster encoding | Decode speed only. |
| SQLite or another store | A second durability model (D12). |
