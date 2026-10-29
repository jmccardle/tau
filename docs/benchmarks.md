# Benchmarks

Two benchmarks of the session tree and its log: what the access pattern
of agents costs, and how the log could be encoded. They are the evidence
for D12's costs and for D14. The numbers below come from one machine; rerun
them on yours and compare the shape more than the absolute values.
Disk sync latency in particular varies by orders of magnitude between
machines.

## Running

Both are std-timed, print Markdown, and need nothing outside the
repository except, for the second, a directory of text.

```bash
source ~/.cargo/env

# 1. The tree and the store (~8 min for everything on 4 cores).
cargo bench -p tau-store --bench tree --locked                 # every scenario
cargo bench -p tau-store --bench tree --locked -- depth store  # or some

# 2. Log encodings. Its own workspace and lockfile, never built by CI.
cd crates/bench-formats
cargo run --release --locked                 # corpus: ~/.cargo/registry/src
cargo run --release --locked -- DIR [DIR...] # or any directories of .rs/.md
```

Scenarios of `benches/tree.rs` (in `crates/tau-store`), each driving the
public `Machine` with a scripted provider and tools. Through a store, every
save is followed by `Store::evict` and every `Stored` refusal by
`Store::load`, as a driver does (D14):

| scenario | what |
|---|---|
| `depth` | One agent going forward; `walk(at)` and the walk as JSON (a stand-in for building the request) at every sample, by context size, for three node sizes. Core time per turn excludes the read. Memory only. |
| `lookup` | `node(id)`, `config_at`, `lca` on a 1M-token session with 64 branches. |
| `jump` | Forward with a go-to to a random recent node every 5 turns. |
| `store` | The same agent through the store, then a reopen. |
| `compact` | At 1M tokens: a mock classifier over the walk, the kept nodes copied as a new line, a mock summary on top, a move; five cycles. |
| `cycles` | Twenty such compactions, once without eviction and once with, each in its own process so RSS is its own; reopens in a child process; a go-to into the first, long-evicted line and back. |
| `cursors` | k cursors branched off one 1M-token session, interleaved job by job through one store. |
| `parallel` | k sessions at 1M tokens, one thread each, flat out (no model latency). |
| `load` | A log after five compactions: bytes by record kind, decode against replay. |

Shapes: **tiny** (80–120 B nodes, 8 tool rounds a turn), **chatty**
(400–1,000 B, 4 rounds), **bulky** (16 KB results, 4 rounds). Tokens are
counted as 4 bytes. RSS columns are the whole process's resident memory at
that point, so they include earlier scenarios in the same run.

`crates/bench-formats` builds a log from real text: consecutive slices of
the corpus (sorted by path), so nothing repeats except by copying, in five
cycles of 150 turns and a D3-style compaction (the newest quarter and every
user node copied, a summary on top). Every record round-trips through each
format and is checked equal.

## Results: 2026-10-29, after D14

Machine: 4 vCPU Intel Xeon @ 2.80 GHz, 15 GB, ext4 on a virtio disk (a cloud
VM; fsync is fast, ~0.3 ms median), Linux 6.18, Rust 1.99. Tree bench at
`33bd080`; `store`, `compact`, and `cycles` at `30bb9d1` (open streams the log
and leaves the past in it). Timings on this VM move by 10–30% from run to run; compare
shapes, and repeat a run before reading much into a small difference.

### D14: before and after, same machine, same day

"Before" is `a24c592` (all bodies resident), run just before "after".

| | before | after |
|---|---|---|
| bodies in memory after 5 compactions (`compact`) | 21.9 MB (all) | 1.9 MB |
| bodies in memory after 20 compactions (`cycles`) | 83.4 MB (all) | 3.3 MB |
| RSS of the process after 20 compactions (`cycles`) | 150 MB | 47 MB |
| `walk` at 1M tokens, tiny (31k nodes) / chatty | 360–410 / 36–41 µs | 150–175 / 21–27 µs |
| walk as JSON at 1M tokens, tiny / chatty / bulky, three runs each | 9.3–10.0 / 3.4–3.6 / 1.8–1.9 ms | 10.0–15.7 / 3.4–5.0 / 1.9–4.7 ms |
| `lca` at 30k nodes deep | 698 µs | 259 µs |
| go-to to a recent node (`jump`) | 0.7 µs | 21 µs (p99 28 µs) |
| first walk after it | 24 µs | 11 µs |
| stored turn, chatty, 1M tokens | 4.6 ms | 4.2 ms |
| compaction pause at 1M tokens (`compact`) | 13–20 ms | 21–35 ms |
| walk p99 with 1–64 branched cursors (`cursors`) | 119–136 µs | 33–50 µs |
| replay of 5 compactions (`load`) | 16.9 ms | 13.2 ms, then evict 8.2 ms |
| reopen after 5 compactions (`compact`) | 80 ms | 60 ms |
| reopen after 20 compactions (`cycles`): time / RSS / peak RSS | 372 ms / 141 MB / 283 MB | 193 ms / 17 MB / 50 MB |
| reopen with no compaction, chatty / bulky, 1M tokens (`store`) | 12.6 / 4.1 ms | 20.5 / 6.3 ms |
| go-to into a line evicted 20 compactions ago (5,350 bodies) | n/a | 12 ms |

The "before" run's `cursors` rows for k = 1 and 4 overlapped a test build
on the same machine; their save p99 (8–12 ms) is that, not the store.

What changed, and why:

- **Memory follows the cursors, not the history.** Bodies in memory grow
  with the context at the tips (~1–3 MB here) instead of with everything
  ever written; the process's RSS falls with them (150 → 47 MB after 20
  compactions).
- **The walk is faster** (parents in their own vector), and so is `lca`,
  which no longer builds walks. Walk as JSON is the same work as before and
  within this machine's noise, perhaps somewhat slower; storing nodes
  inline instead of boxed was tried and was no better.
- **A go-to checks its target's walk** for stored bodies: O(depth), ~20 µs
  at 5k nodes, where it was a lookup.
- **Compaction pays a reload.** The save of the copy evicts the new line,
  since no cursor is on it until the move; the summary reads it back
  (1.7–3.3k bodies, a few ms). An extension that keeps a cursor on the line
  it builds would not pay it.
- **Going back reads the line in log order:** 5,350 bodies of a line
  evicted 20 compactions ago load in ~12 ms; the return to the main line,
  evicted meanwhile, ~8 ms.
- **Open leaves the past in the log.** It streams the log once, decodes
  each node without building its body, then reads back only the cursors'
  walks. After 20 compactions it opens in half the time at an eighth of
  the memory (17 MB RSS, 50 MB peak, for a 110 MB log). The cost is a
  session whose bodies are nearly all on the walks (no compaction): its
  walk records are decoded twice, ~1.6× the old open. A first attempt,
  decoding everything and then evicting, kept RSS at the open-time size:
  the allocator keeps freed small blocks rather than returning them.

## Tree and store

### depth: one agent going forward, walk(at) at every sample (memory only)

| shape | context | nodes on walk | walk | walk as JSON | turn (core, no read) |
|---|---|---|---|---|---|
| tiny | 0k→10k | 326 | 1.0 µs | 90.9 µs | 23.3 µs |
| tiny | 10k→100k | 3152 | 9.1 µs | 913.8 µs | 35.0 µs |
| tiny | 100k→250k | 7886 | 21.3 µs | 2.34 ms | 50.0 µs |
| tiny | 250k→500k | 15752 | 76.3 µs | 5.33 ms | 83.9 µs |
| tiny | 500k→750k | 23618 | 102.4 µs | 8.01 ms | 100.8 µs |
| tiny | 750k→1000k | 31484 | 173.7 µs | 12.72 ms | 111.4 µs |
| chatty | 0k→10k | 62 | 0.4 µs | 40.2 µs | 15.4 µs |
| chatty | 10k→100k | 542 | 2.1 µs | 424.8 µs | 15.5 µs |
| chatty | 100k→250k | 1342 | 3.7 µs | 1.04 ms | 24.6 µs |
| chatty | 250k→500k | 2682 | 9.3 µs | 2.23 ms | 30.7 µs |
| chatty | 500k→750k | 4012 | 17.3 µs | 3.65 ms | 37.1 µs |
| chatty | 750k→1000k | 5352 | 26.6 µs | 4.68 ms | 47.3 µs |
| bulky | 0k→10k | 12 | 0.3 µs | 26.1 µs | 31.1 µs |
| bulky | 10k→100k | 62 | 0.6 µs | 254.4 µs | 19.6 µs |
| bulky | 100k→250k | 152 | 1.2 µs | 713.5 µs | 27.2 µs |
| bulky | 250k→500k | 302 | 1.6 µs | 1.51 ms | 34.4 µs |
| bulky | 500k→750k | 442 | 2.3 µs | 2.45 ms | 38.0 µs |
| bulky | 750k→1000k | 592 | 4.3 µs | 3.29 ms | 54.9 µs |

### lookup: a node by id, config_at, lca (a 1M-token session with 64 branches)

| node(id) | config_at(tip) | lca(tip, tip) | depth | nodes |
|---|---|---|---|---|
| 0.0 µs | 0.0 µs | 258.5 µs | 29985 | 33788 |

### jump: forward with a go-to to a random recent node every 5 turns (1M tokens, chatty)

| go-to | first walk after it | nodes on walk | nodes in session |
|---|---|---|---|
| 20.7 µs (p99 27.8 µs) | 11.2 µs (p99 12.3 µs) | 4231 | 7752 |

### store: one agent through the store, then reopen

| shape | context | create over first half | turns | turn | syncing saves per turn | save p50 | save p99 | log | open | RSS after open |
|---|---|---|---|---|---|---|---|---|---|---|
| chatty | 100k (second half stored turn by turn) | 4.75 ms | 27 | 4.57 ms | 10.0 | 268.5 µs | 1.48 ms | 0.5 MB | 2.08 ms | 4 MB |
| chatty | 1000k (second half stored turn by turn) | 10.23 ms | 267 | 5.09 ms | 10.0 | 288.7 µs | 1.61 ms | 5.4 MB | 20.46 ms | 11 MB |
| bulky | 100k (second half stored turn by turn) | 2.77 ms | 3 | 4.23 ms | 10.0 | 311.9 µs | 884.0 µs | 0.4 MB | 588.1 µs | 11 MB |
| bulky | 1000k (second half stored turn by turn) | 7.49 ms | 29 | 4.48 ms | 10.0 | 306.4 µs | 1.31 ms | 4.2 MB | 6.25 ms | 11 MB |

### compact: at 1M tokens classify, copy, summarize, move; 5 cycles (chatty, stored)

| cycle | before | after | classify | copy (nodes) | save copy | reload, summarize, move, evict (reloaded) | pause total | next sample walk | log | bodies in memory |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1000k | 294k | 5.32 ms | 2.20 ms (1739) | 6.16 ms | 7.78 ms (1739) | 21.47 ms | 11.1 µs | 6.9 MB | 1.2 MB |
| 2 | 1001k | 342k | 5.10 ms | 1.30 ms (2160) | 7.06 ms | 8.33 ms (2160) | 21.77 ms | 91.2 µs | 12.5 MB | 1.4 MB |
| 3 | 1000k | 386k | 4.67 ms | 1.38 ms (2550) | 7.11 ms | 8.05 ms (2550) | 21.22 ms | 120.9 µs | 18.1 MB | 1.5 MB |
| 4 | 1000k | 428k | 7.71 ms | 2.17 ms (2914) | 11.37 ms | 13.71 ms (2914) | 34.96 ms | 207.9 µs | 23.6 MB | 1.7 MB |
| 5 | 1000k | 467k | 5.05 ms | 1.62 ms (3255) | 7.57 ms | 12.58 ms (3255) | 26.82 ms | 24.9 µs | 29.1 MB | 1.9 MB |

Open after 5 cycles: 59.59 ms for 31665 nodes; RSS 27 MB.

### cycles: 20 compactions at 1M tokens (chatty, stored), without and with eviction after every save

| cycles | log | nodes | bodies in memory, kept → evicted | RSS, kept → evicted | evict after the move | open | evict on open | bodies after open → evict | RSS after open → evict | peak RSS, open |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 6.9 MB | 7092 | 5.2 MB → 1.2 MB | 22 MB → 22 MB | 1.94 ms | 18.74 ms | 14.1 µs | 1.2 → 1.2 MB | 5 → 5 MB | 6 MB |
| 5 | 29.1 MB | 31655 | 21.9 MB → 1.9 MB | 50 MB → 28 MB | 3.62 ms | 56.36 ms | 42.0 µs | 1.9 → 1.9 MB | 8 → 8 MB | 16 MB |
| 10 | 56.5 MB | 64764 | 42.5 MB → 2.5 MB | 83 MB → 34 MB | 4.46 ms | 145.77 ms | 77.0 µs | 2.5 → 2.5 MB | 12 → 12 MB | 28 MB |
| 20 | 110.2 MB | 135874 | 83.4 MB → 3.3 MB | 150 MB → 47 MB | 2.61 ms | 192.65 ms | 171.0 µs | 3.3 → 3.3 MB | 17 → 17 MB | 50 MB |

After 20 cycles, a go-to to the tip of the first line: refused in 66.7 µs naming 5350 stored bodies, loaded in 11.81 ms, go-to 56.9 µs; a turn there (walk 34.0 µs). Back to the main line, now evicted too: 6266 bodies loaded in 7.63 ms; evicting the first line again 3.04 ms.

### cursors: k cursors branched off recent nodes of one 1M-token session, interleaved

| k | turns/s | save p50 | save p99 | saves with >1 Sync | walk p99 | log after | open after | nodes |
|---|---|---|---|---|---|---|---|---|
| 1 | 241 | 204.3 µs | 1.14 ms | 0 | 33.0 µs | 5.5 MB | 13.08 ms | 5432 |
| 4 | 336 | 460.6 µs | 2.41 ms | 0 | 49.8 µs | 5.7 MB | 14.92 ms | 5672 |
| 16 | 637 | 853.7 µs | 1.26 ms | 0 | 47.1 µs | 6.7 MB | 15.98 ms | 6632 |
| 64 | 673 | 895.0 µs | 2.04 ms | 0 | 46.3 µs | 10.5 MB | 30.88 ms | 10472 |

### parallel: k sessions at 1M tokens (chatty), one thread each, flat out, go-to every 4 turns

| k | prebuild (all) | turns/s (all) | turn p50 | save p50 | save p99 | sample read p99 (walk+JSON) | RSS |
|---|---|---|---|---|---|---|---|
| 1 | 19.70 ms | 36 | 24.98 ms | 436.3 µs | 2.03 ms | 6.79 ms | 41 MB |
| 2 | 79.84 ms | 75 | 24.20 ms | 379.4 µs | 1.88 ms | 8.45 ms | 58 MB |
| 4 | 151.66 ms | 127 | 26.73 ms | 369.7 µs | 4.69 ms | 8.10 ms | 89 MB |
| 8 | 279.20 ms | 144 | 49.26 ms | 547.8 µs | 9.88 ms | 17.01 ms | 158 MB |
| 16 | 555.43 ms | 164 | 85.64 ms | 2.04 ms | 27.43 ms | 30.40 ms | 292 MB |
| 32 | 1.30 s | 176 | 160.31 ms | 5.53 ms | 51.88 ms | 63.72 ms | 592 MB |

### load: a 1M-token session after 5 compactions (chatty), reopen split

| record | count | bytes | share |
|---|---|---|---|
| Write | 31774 | 28.3 MB | 95% |
| Move | 6 | 0.0 MB | 0% |
| Label | 0 | 0.0 MB | 0% |
| Sync | 19121 | 1.4 MB | 5% |

Body text is 21.9 MB of the 29.7 MB; the context at the tip is 458k tokens.

| decode JSON | replay (`Session::load`) | evict | bodies in memory, replayed → evicted | drop |
|---|---|---|---|---|
| 53.96 ms | 13.15 ms | 8.17 ms | 21.9 → 1.8 MB | 1.11 ms |

## Log encodings

At `a24c592`, 2026-10-28, Rust 1.97 (unaffected by D14).

12975 records; 11740000 corpus characters used of 57407040.

| format | size | encode | decode | zstd -3, whole log | zstd encode | zstd decode |
|---|---|---|---|---|---|---|
| JSON (serde_json) | 19.9 MB | 47 ms | 69 ms | 2.7 MB (7.4×) | 78 ms | 32 ms |
| CBOR (ciborium) | 18.4 MB | 16 ms | 30 ms | 2.6 MB (7.0×) | 69 ms | 46 ms |
| MessagePack, named fields (rmp-serde) | 18.4 MB | 19 ms | 16 ms | 2.6 MB (7.0×) | 94 ms | 31 ms |
| BSON (bson) | 19.9 MB | 74 ms | 138 ms | 2.7 MB (7.5×) | 68 ms | 34 ms |
| postcard, no field names | 17.1 MB | 12 ms | 10 ms | 2.5 MB (6.7×) | 60 ms | 33 ms |

JSON, zstd -3 per record: 7.8 MB (759 ms).

Text 16.7 MB; distinct by content 11.7 MB (30% is repeats).

## Reading them

- **The walk is cheap; the request is not.** At 1M tokens the walk itself
  is 2–175 µs depending on node count; turning it into JSON is 2–15 ms. A
  driver should not rebuild or convert the whole context per sample; nodes
  never change, so a cache keyed by node id holds.
- **Navigation is cheap.** A node by id is an index; a go-to is ~20 µs
  (it checks the target's walk is in memory); branch count does not affect
  a walk, only depth. `lca` is O(depth) (~0.26 ms at 30k nodes) and should
  stay off per-step paths.
- **A stored turn is its syncs.** ~5 ms for ten syncing saves a turn here.
  It does not grow with depth. Many sessions on one disk contend on sync
  (p99 save 2 ms with one session, ~50 ms with 32). Cursors in one session
  share syncs instead (the dirty flag is per session).
- **Flat-out throughput** saturates near 150 turns/s on 4 cores, mostly
  request building; with real model latency of seconds per sample, the CPU
  and disk carry on the order of a thousand concurrent 1M-token agents, and
  memory (~10–20 MB each, mostly the context itself) runs out first.
- **Compaction pauses** 15–30 ms at 1M tokens, without the model (25–30 ms
  with the reload of the evicted copy). Every
  cycle adds ~5.5 MB to the log, all of it read on open; since D14 only the
  cursors' walks stay in memory.
- **Encodings:** MessagePack with field names decodes 2.5–4× faster than
  JSON and stays self-describing; BSON is slower than JSON; postcard is
  fastest but stores no field names. Compression is ~7× per log segment
  against ~2.5× per record. With D3 copies, ~30% of stored text is repeats.

## Fixed after the first run

- `Store::save` synced once per queued `Sync`; now once per save. Creating
  a store over a 500k-token session built in memory: 1.33 s → ~20 ms.
- `config_at` walked to the root on every committed step (~200 µs at 30k
  nodes); now a lookup (`Session::configs`). Core per turn at 31k nodes
  deep: 3.5 ms → ~0.13 ms.
