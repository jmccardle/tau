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
public `Machine` with a scripted provider and tools:

| scenario | what |
|---|---|
| `depth` | One agent going forward; `walk(at)` and the walk as JSON (a stand-in for building the request) at every sample, by context size, for three node sizes. Core time per turn excludes the read. Memory only. |
| `lookup` | `node(id)`, `config_at`, `lca` on a 1M-token session with 64 branches. |
| `jump` | Forward with a go-to to a random recent node every 5 turns. |
| `store` | The same agent through the store, then a reopen. |
| `compact` | At 1M tokens: a mock classifier over the walk, the kept nodes copied as a new line, a mock summary on top, a move; five cycles. |
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

## Results: 2026-10-28

Machine: 4 vCPU Intel Xeon @ 2.80 GHz, 15 GB, ext4 on a virtio disk (a cloud
VM; fsync is fast, ~0.3 ms median), Linux 6.18, Rust 1.97. Tree bench at
`631c4e2` (after the one-sync-per-save and constant-time `config_at` fixes).

## Tree and store

### depth: one agent going forward, walk(at) at every sample (memory only)

| shape | context | nodes on walk | walk | walk as JSON | turn (core, no read) |
|---|---|---|---|---|---|
| tiny | 0k→10k | 326 | 1.7 µs | 73.7 µs | 24.3 µs |
| tiny | 10k→100k | 3152 | 22.8 µs | 769.5 µs | 50.0 µs |
| tiny | 100k→250k | 7886 | 60.4 µs | 2.17 ms | 70.9 µs |
| tiny | 250k→500k | 15752 | 129.2 µs | 4.90 ms | 110.9 µs |
| tiny | 500k→750k | 23618 | 248.5 µs | 7.20 ms | 128.7 µs |
| tiny | 750k→1000k | 31484 | 455.5 µs | 9.63 ms | 125.3 µs |
| chatty | 0k→10k | 62 | 0.5 µs | 25.0 µs | 15.8 µs |
| chatty | 10k→100k | 542 | 4.0 µs | 390.4 µs | 18.3 µs |
| chatty | 100k→250k | 1342 | 9.9 µs | 1.44 ms | 24.8 µs |
| chatty | 250k→500k | 2682 | 26.6 µs | 1.97 ms | 36.8 µs |
| chatty | 500k→750k | 4012 | 29.6 µs | 2.68 ms | 48.3 µs |
| chatty | 750k→1000k | 5352 | 57.4 µs | 5.53 ms | 62.3 µs |
| bulky | 0k→10k | 12 | 0.3 µs | 17.5 µs | 34.9 µs |
| bulky | 10k→100k | 62 | 0.8 µs | 172.2 µs | 23.2 µs |
| bulky | 100k→250k | 152 | 1.8 µs | 548.4 µs | 26.8 µs |
| bulky | 250k→500k | 302 | 3.7 µs | 2.22 ms | 49.5 µs |
| bulky | 500k→750k | 442 | 3.6 µs | 1.59 ms | 47.9 µs |
| bulky | 750k→1000k | 592 | 5.3 µs | 2.03 ms | 49.6 µs |

### lookup: a node by id, config_at, lca (a 1M-token session with 64 branches)

| node(id) | config_at(tip) | lca(tip, tip) | depth | nodes |
|---|---|---|---|---|
| 0.0 µs | 0.0 µs | 735.7 µs | 29985 | 33788 |

### jump: forward with a go-to to a random recent node every 5 turns (1M tokens, chatty)

| go-to | first walk after it | nodes on walk | nodes in session |
|---|---|---|---|
| 0.8 µs (p99 1.4 µs) | 24.7 µs (p99 56.4 µs) | 4231 | 7752 |

### store: one agent through the store, then reopen

| shape | context | create over first half | turns | turn | syncing saves per turn | save p50 | save p99 | log | open | RSS after open |
|---|---|---|---|---|---|---|---|---|---|---|
| chatty | 100k (second half stored turn by turn) | 3.21 ms | 27 | 3.89 ms | 10.0 | 236.7 µs | 1.11 ms | 0.5 MB | 1.44 ms | 49 MB |
| chatty | 1000k (second half stored turn by turn) | 21.07 ms | 267 | 5.48 ms | 10.0 | 301.3 µs | 1.72 ms | 5.4 MB | 19.56 ms | 49 MB |
| bulky | 100k (second half stored turn by turn) | 2.55 ms | 3 | 4.66 ms | 10.0 | 324.0 µs | 1.04 ms | 0.4 MB | 471.3 µs | 49 MB |
| bulky | 1000k (second half stored turn by turn) | 7.26 ms | 29 | 5.34 ms | 10.0 | 379.2 µs | 1.44 ms | 4.2 MB | 5.78 ms | 49 MB |

### compact: at 1M tokens classify, copy, summarize, move; 5 cycles (chatty, stored)

| cycle | before | after | classify | copy (nodes) | save copy | summarize | pause total | next sample walk | log |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 1000k | 294k | 6.34 ms | 2.96 ms (1739) | 8.48 ms | 3.47 ms | 21.25 ms | 58.7 µs | 6.9 MB |
| 2 | 1001k | 342k | 10.14 ms | 1.88 ms (2160) | 6.52 ms | 2.52 ms | 21.06 ms | 57.3 µs | 12.5 MB |
| 3 | 1000k | 386k | 6.22 ms | 1.22 ms (2550) | 6.23 ms | 2.05 ms | 15.72 ms | 45.2 µs | 18.1 MB |
| 4 | 1000k | 428k | 5.36 ms | 1.52 ms (2914) | 7.87 ms | 2.34 ms | 17.09 ms | 65.0 µs | 23.6 MB |
| 5 | 1000k | 467k | 8.68 ms | 4.70 ms (3255) | 10.26 ms | 6.44 ms | 30.08 ms | 96.5 µs | 29.1 MB |

Open after 5 cycles: 87.83 ms for 31665 nodes; RSS 57 MB.

### cursors: k cursors branched off recent nodes of one 1M-token session, interleaved

| k | turns/s | save p50 | save p99 | saves with >1 Sync | walk p99 | log after | open after | nodes |
|---|---|---|---|---|---|---|---|---|
| 1 | 207 | 279.0 µs | 1.24 ms | 0 | 91.8 µs | 5.5 MB | 13.86 ms | 5432 |
| 4 | 342 | 456.9 µs | 2.23 ms | 0 | 135.2 µs | 5.7 MB | 16.09 ms | 5672 |
| 16 | 474 | 1.03 ms | 1.79 ms | 0 | 143.9 µs | 6.7 MB | 18.60 ms | 6632 |
| 64 | 581 | 1.04 ms | 2.27 ms | 0 | 109.1 µs | 10.5 MB | 38.98 ms | 10472 |

### parallel: k sessions at 1M tokens (chatty), one thread each, flat out, go-to every 4 turns

| k | prebuild (all) | turns/s (all) | turn p50 | save p50 | save p99 | sample read p99 (walk+JSON) | RSS |
|---|---|---|---|---|---|---|---|
| 1 | 29.47 ms | 34 | 28.14 ms | 470.7 µs | 2.07 ms | 6.60 ms | 57 MB |
| 2 | 77.93 ms | 76 | 25.13 ms | 416.0 µs | 1.99 ms | 6.47 ms | 62 MB |
| 4 | 153.35 ms | 110 | 32.37 ms | 417.1 µs | 4.74 ms | 7.93 ms | 91 MB |
| 8 | 360.81 ms | 126 | 57.44 ms | 744.6 µs | 12.31 ms | 20.68 ms | 154 MB |
| 16 | 831.40 ms | 140 | 103.18 ms | 2.48 ms | 24.63 ms | 37.98 ms | 278 MB |
| 32 | 3.22 s | 150 | 170.90 ms | 5.07 ms | 49.27 ms | 62.00 ms | 588 MB |

### load: a 1M-token session after 5 compactions (chatty), reopen split

| record | count | bytes | share |
|---|---|---|---|
| Write | 31774 | 28.3 MB | 95% |
| Move | 6 | 0.0 MB | 0% |
| Label | 0 | 0.0 MB | 0% |
| Sync | 19121 | 1.4 MB | 5% |

Body text is 21.9 MB of the 29.7 MB; the context at the tip is 458k tokens.

| decode JSON | replay (`Session::load`) | drop |
|---|---|---|
| 46.73 ms | 12.69 ms | 12.74 ms |

## Log encodings

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
  is 5–455 µs depending on node count; turning it into JSON is 2–10 ms. A
  driver should not rebuild or convert the whole context per sample; nodes
  never change, so a cache keyed by node id holds.
- **Navigation is free.** A node by id is an index; a go-to is ~1 µs; branch
  count does not affect a walk, only depth. `lca` is O(depth) (~0.7 ms at
  30k nodes) and should stay off per-step paths.
- **A stored turn is its syncs.** ~5 ms for ten syncing saves a turn here.
  It does not grow with depth. Many sessions on one disk contend on sync
  (p99 save 2 ms with one session, ~50 ms with 32). Cursors in one session
  share syncs instead (the dirty flag is per session).
- **Flat-out throughput** saturates near 150 turns/s on 4 cores, mostly
  request building; with real model latency of seconds per sample, the CPU
  and disk carry on the order of a thousand concurrent 1M-token agents, and
  memory (~10–20 MB each, today) runs out first.
- **Compaction pauses** 15–30 ms at 1M tokens, without the model. The cost
  is permanent: every cycle adds ~5.5 MB to the log, all of it loaded on
  open and resident. D14 (deferred) records an attempt and its numbers.
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
