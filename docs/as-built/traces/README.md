# Traces

One timeline across Rust and Python for one turn. Observations only; nothing here
was optimized.

## Capture

```python
from tau_next import trace

with trace.capture("turn.json"):  # or trace.start(path) ... trace.stop()
    ...  # drive a session / Driver as usual
```

`trace.start` makes `tau_next._core` record its `tracing` spans (`core.*`,
`store.*`, `py.*`) with `tracing-chrome`; the driver's spans (`hook`, `sample`,
`tool`, `driver`) are timed on the native clock (`_core.trace_clock`) and merged
into the same file by `trace.stop`. Python spans get one track per asyncio task
(`python task N`); native spans are on the thread that ran them.

Why merge instead of emitting Python spans through `tracing`: the Chrome
format's begin/end events must nest per thread, and the driver's effects
interleave on one thread across `await`s. Complete (`X`) events on a track per
task have no such constraint, and the Rust side stays thin (`trace_to`,
`stop_trace`, `trace_clock`). The cost is a clock offset of a few microseconds
between the two halves.

`scripts/trace_turn.py` runs the representative turn (user message, `read` tool
call, result, final reply; `FakeProvider`, every hook point on) in memory and on
disk, and writes `turn.json`, `turn-stored.json`, and `summary.md` here.
View: open the JSON in <https://ui.perfetto.dev> or `chrome://tracing`.

## Cost when unused

- `tau-core` depends on `tracing` with `default-features = false`: the facade
  plus `tracing-core` and `pin-project-lite`. No `std`, no `attributes`, no IO.
  (`tau-store` and `tau-py` use the same; `tau-py` alone adds `tracing-chrome`
  and `tracing-subscriber`, which pulls in `std`.)
- Core bench `depth`, "turn (core, no read)" at 0k to 10k context, median of
  three alternating runs on a loaded shared machine (noise about +-5 us):

  | shape | before | after |
  |---|---|---|
  | tiny | 21.4 us | 20.2 us |
  | chatty | 15.5 us | 16.6 us |
  | bulky | 23.5 us | 23.7 us |

  No measurable change. (In the bench no subscriber exists at all.)
- `Session.at()` from Python, best of 7, no spans / spans compiled in, no
  subscriber / subscriber installed but no trace running: about 114 ns / 90 ns /
  110 ns. All within noise. With a trace running: about 1,960 ns.

## One turn

Wall time from `send` to quiet, in memory: 829 us untraced (median of 30), 1,437
us traced. Tracing itself is not free: this turn makes about 250 spans.

| part | us | share |
|---|---|---|
| Python driver, unspanned (asyncio, hook plumbing, harness) | 633 | 44% |
| bindings, native side (lock, conversion out, call) | 406 | 28% |
| effects: `read` tool | 203 | 14% |
| effects: fake provider | 87 | 6% |
| bindings, Python side (arguments in, call) | 63 | 4% |
| core | 30 | 2% |
| effects: hooks | 15 | 1% |

On disk (`turn-stored.json`), this machine's NVMe with two runs: fsync was 54%
of the traced turn in one and 81% in the other (about 0.4 to 2 ms per fsync:
four log syncs, two pad syncs, two directory syncs). Untraced wall time swung
from 4.9 to 27 ms between runs. Full per-span tables: `summary.md`.

## Observations

- Core is 2% of a turn in memory (30 us over 15 `feed`s, 16 `next_job`s, 4
  writes). The bindings cost more than the core by a wide margin (about 15x).
- The driver polls the session heavily: 49 `cursors()` and 33 `busy()` calls
  for one two-sample turn, against 15 feeds. Each is cheap (about 1.5 us) but
  together they are as much as all of core.
- `py.lock` is called 126 times at about 0.2 us each; the lock is never
  contended in a single-threaded turn. Much of what shows as "bindings native
  side" is the spans' own cost on tiny calls.
- Stored, the log and its fsyncs dominate (the store's encode and write is
  under 2%). Per turn: 4 `fsync_log`, and a pad write (file fsync, rename,
  directory fsync) twice. The scratchpad rewrites cost four of the eight fsyncs.
- Hooks are almost free (1 to 2 us each with a trivial extension).
- The `read` tool is the largest single effect (200 us): the Rust call plus the
  driver's task hop, not file IO.
- `driver.request` (`walk` plus `config_at` conversion for the sample) is 2
  calls at about 5 us here; it grows with the walk (see `docs/benchmarks.md`,
  "walk as JSON").
