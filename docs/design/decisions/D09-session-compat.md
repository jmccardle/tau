# D9: 0.15 sessions come in through a one-way converter

Status: superseded (2026-10-29): no converter. τ 0.16 keeps its sessions in
the same store, `$TAU_HOME/sessions`, beside 0.15's `.jsonl` files, which it
neither reads nor converts. Converting them is left to whoever wants it; the
reasoning below stands for anyone who writes one.

Original status: accepted (Phase 1)

## Context

People have 0.15 session files. 0.15's loader guessed a reopen position and
wrote nodes in two parts; the new tree has rules the old files never had
(Config roots, one `Results` per batch, every position a move).

## Decision

- **A one-way converter**, Phase 7: it reads a 0.15 file and writes a new
  session through the normal append and move checks, so its output is valid
  by construction. Nothing reads 0.15 files natively.
- **Open, before Phase 7:** where a converted cursor opens (I7 forbids
  guessing); how a 0.15 tool-result group becomes one `Results`; what
  happens to content with no counterpart; the on-disk format it targets.
  Each needs a question and an ADR.

## Alternatives

| option | why not |
|---|---|
| Native read of 0.15 files | Imports 0.15's guessing into load; two formats forever. |
| No migration | Costs users their history. |
| Convert on every load | Hides the conversion. |

## Consequences

- One format, one loader. The converter fails on input it cannot map.
- Cost: no 0.15 session opens before Phase 7.
