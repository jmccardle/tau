# Race results (DRAFT)

> **DRAFT: not a result.** The machine was under load (load average 6.4 before, 8.5 after; the threshold is 2). Rerun `demo/race.py bench` on an idle machine.

Taken 2026-11-17 02:58 UTC; median, min and max of 3 runs after one discarded warm-up run, seconds, versions alternating who goes first.

## Machine

- CPU: 11th Gen Intel(R) Core(TM) i5-1135G7 @ 2.40GHz, 8 threads, 23 GiB
- Kernel: 6.12.116+deb13-amd64 (x86_64)
- 0.15: tau 0.15.0 on Python 3.12.14 (PyPI `ffwf-tau==0.15.0`)
- 0.16: tau 0.16.0.dev0 (protocol 1) on Python 3.12.14 (release wheel built from this checkout)
- tmux tmux 3.5a, TUI pane 160x48
- Load average (1 min): 6.40 at start, 8.53 at end

## Instant take (the fake model answers at once, so this is the harness)

| race | metric | 0.15 median (min-max) | 0.16 median (min-max) | 0.16 vs 0.15 |
|---|---|---|---|---|
| STARTUP | startup: `tau -p` one answer, no tools | 0.51 (0.47-0.56) | 0.44 (0.43-0.45) | 1.16x faster |
| A | A: one-shot, 100 sequential `read` calls | 1.86 (1.68-1.87) | 1.44 (1.39-1.79) | 1.29x faster |
| B | B: reopen 200-turn session, last message drawn | 2.17 (1.94-2.46) | 1.26 (0.94-1.45) | 1.72x faster |
| B | B: reopen, typed text echoed (responsive) | 2.24 (2.00-2.53) | 1.33 (1.01-1.52) | 1.68x faster |

## Paced take (the fake model takes 400 ms to the first token and streams 70 tokens/s)

| race | metric | 0.15 median (min-max) | 0.16 median (min-max) | 0.16 vs 0.15 |
|---|---|---|---|---|
| A | A: one-shot, 100 sequential `read` calls | 54.11 (53.91-54.15) | 53.56 (53.55-53.68) | 1.01x faster |
| B | B: reopen 200-turn session, last message drawn | 3.38 (1.91-3.66) | 1.52 (1.41-1.59) | 2.22x faster |
| B | B: reopen, typed text echoed (responsive) | 3.49 (2.01-3.78) | 1.63 (1.49-1.71) | 2.14x faster |
| B | B: reopen, then a message streamed and answered | 6.46 (4.82-6.76) | 4.00 (3.84-4.38) | 1.62x faster |

Race A requests seen by the fake server were identical in both versions (digest `97f56329e41`).

