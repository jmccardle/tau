# D14: Cold bodies leave memory; text is stored by content; the log is segmented

Status: **proposed** (2026-10-28). Amends D12. Shapes the `tau-py` boundary, so
it is decided before the bindings.

## Context

A session keeps every node it ever wrote: abandoned branches, the line a
compaction departed from, and every compaction copy (D3). Today all of it
is in memory, and opening a session replays the whole log. The measurements
(`docs/benchmarks.md`) show where that goes:

- After 5 compactions of a 1M-token session, the log is ~30 MB for a
  ~460k-token context: 95% node records, 5% `Sync`s. Opening it costs
  ~120 ms, about half decode and half replay, and all ~30 MB stays resident.
  Each cycle adds ~5.5 MB. A session that compacts 100 times would open
  in seconds and hold ~0.5 GB.
- With D3-style copies, ~30% of the stored text repeats text already in the log.
- Text compresses ~7× across a log segment, ~2.5× record by record.
- Faster encodings decode 2.5–4× faster (MessagePack) but change neither
  memory nor what is read.

Most of that is on lines no cursor is on. Pruning is not an option: node
ids are permanent, any node can be gone to, and the log is the only copy of
the tree. A snapshot plus a fresh log sheds only the `Sync` records.

Two facts make a policy-free answer possible:

1. **Nodes are immutable and the tree only grows.** A body never changes
   after its write, so it can live anywhere once it is synced.
2. **Core never needs a cold body.** Its decisions read a node's kind, stop,
   and calls (`next`, `decide`), the tip's calls (`start_batch`), the nearest
   `Config` (`config_at`), and the body being written (V2). The tip is on a
   walk by definition; `Config` bodies are few and small.

## Decision (proposed)

### 1. Residency

- `Session` keeps every node's **header** in memory: id, parent, time,
  author, kind, stop, call ids, nearest `Config` (`configs`), and the body's
  **location** in the log. Roughly 50–80 bytes a node.
- A body is **resident** or **stored** (only a location). `walk(n)` is
  unchanged in meaning: the whole parent chain, nothing filtered (I1). What
  it hands out are headers; a body is fetched by id, and fetching a stored
  one is the store's job (IO), not core's.
- **Hot** bodies stay resident: every node on the walk of some cursor's
  `at`, and every `Config`. Everything else is a **side node** (the glossary
  term: on no cursor's walk) and may be evicted once synced.
- Eviction runs against a memory budget. Order: side nodes farthest from
  any cursor's walk first, then least recently fetched. Nothing marks a
  node cold; a branch goes cold when every cursor has left it, whatever made
  them leave (a go-to, a compaction, a closed cursor). An agent that goes
  back to old history pays one read and makes that line hot again.
- Residency is not state: not persisted, not in I7's equality, invisible to
  hooks except as latency.

### 2. Text by content

- Every text in a body (`Text`, `Thinking`, a call's `args_raw`, an outcome,
  `User`/`System` text) is stored once, keyed by a hash of its content, as a
  `Text` record ahead of the first node that uses it, in the same `Sync`
  batch. Node records name texts by hash.
- In memory, a text is shared (`Arc<str>`) by every node that uses it.
- So a verbatim copy, a copy without its thinking, or a turn copied without
  its tool calls costs no new bytes, on disk or in memory. A summary or an
  edited result is new text. No `copied_from` is needed for this, and copies
  nobody declared are caught too.

### 3. Segments

- The log becomes a sequence of segment files named by the global position
  of their first record. The **tail** segment works as the log does today
  (appends, `Sync`s, torn-tail cut).
- When the tail passes a size threshold it is **sealed**: rewritten as
  zstd-compressed blocks (64–256 KB, each checksummed) plus an **index** of
  the headers it holds and their body locations. It is written under a
  private name, synced, renamed into place, and the directory synced.
  Record positions stay global, so JMFTS replay (D12) and `dropped-<offset>`
  are unchanged.
- Open reads the sealed segments' indexes (headers only), then scans the
  tail as today. Cost grows with node count, not bytes. Bodies of hot nodes
  are read on demand, or eagerly for the cursors' walks.

### 4. Encoding

JSON stays. Sealing rewrites a segment anyway, so a change of encoding
inside sealed blocks (MessagePack with named fields keeps JSON's
self-description and decodes 2.5–4× faster) can come with a version bump
when segments land. BSON was measured and is slower than JSON. postcard is
fastest, but it stores no field names, so any change to a type breaks old logs.

## Alternatives

| option | why not |
|---|---|
| Delete or rotate away old records | Loses nodes; ids must stay retrievable and any node can be gone to. |
| Snapshot plus a fresh log | Sheds only `Sync` and move records (~5% of bytes); every node is still loaded. |
| An encoding change alone | Decode 2.5–4× faster; memory and bytes read unchanged. |
| Compress each record | ~2.5× against ~7× per segment; and every hot read still decompresses. |
| An extension marks lines cold (e.g. compaction) | Assumes how agents behave. "On no cursor's walk" needs no assumption. |
| SQLite or another store | A second durability model (D12 rejected it for the scratchpad). |
| `mmap` the log | JSON must still be decoded to find anything; no index without segments. |

## Consequences

- Core's API changes where the driver reads the tree: `walk` yields
  headers and bodies are fetched by id. `tau-py` exposes exactly this, and
  a Python cache keyed by node id works because nodes never change.
- I7 holds for state; residency is excluded from equality. New property
  tests: eviction never changes what a walk resolves to; a session opened
  from sealed segments plus tail equals one opened from the same records
  unsegmented; a crash at any point of sealing opens as before it.
- Memory ~ headers plus hot bodies. Open ~ node count. Disk grows by new
  text only, compressed once sealed.
- Cost: a go-to into cold history reads and decompresses its line (about
  milliseconds per MB); the sample after it waits for that.
- New dependencies in `tau-store`: zstd and a content hash.
- D12's "one record log plus a scratchpad file" becomes "a segmented
  record log, with an index per sealed segment, plus a scratchpad file".

## Order

Each step stands alone:

1. **Text by content** (core records plus `Arc<str>`; the log format gains
   `Text` records). Saves copies' bytes and memory at once.
2. **Residency** over the current single log, with locations as byte
   offsets. Memory falls; open still scans everything.
3. **Segments**: index plus compressed sealed blocks. Open falls.

## Open questions

- Who fetches stored bodies before a sample: the driver on `Job::Sample`, or
  core emits a job (e.g. `Job::Load(ids)`) first so the wait is explicit
  (D11: only waits are jobs)?
- The budget's scope: per session, or per process across sessions (the
  benchmark's many-agent case argues for per process).
- The hash (an extra dependency; 128 bits is plenty) and whether `Config`
  `data` and `Custom` `data` go by content too.
- The seal threshold, and whether the newest sealed segment stays
  uncompressed for cheap reads by recent go-tos.
- Proposed glossary terms, added on acceptance: header, resident, stored
  body, hot, eviction, text (by content), segment, tail, seal, index.
