# D9: 0.15 sessions come in through a one-way converter

Status: accepted (Phase 1, 2026-10-26)

## Context

People have 0.15 session files. The new session is nodes and move records,
and nothing else (I7). 0.15's loader kept the last line per id and guessed a
reopen position (I7, Why). Its nodes could be written in two parts and then
finalized, and its tool results had a layout of their own.

The new tree has rules the old files never had: every root is a `Config`
node (Q5), a batch is answered per call (I4, D7), and every cursor position is
a move.

## Decision

- **One-way converter**, planned for Phase 7 (plan §3): it reads a 0.15
  session file and writes a new session. Nothing writes 0.15 files, and
  nothing reads them natively.
- The new store, load, and replay code know only the new format (D6, I7).
  The converter is a separate tool that produces nodes and moves through the
  normal append and move checks (I5), so its output is valid by construction.
- **Open (no question number yet):** the converter's details. The docs
  leave these undecided:
  - which cursor `at` a converted session opens at, since 0.15 guessed it
    and I7 forbids guessing;
  - how a 0.15 batch of tool results becomes one result per call and in what
    order;
  - what happens to 0.15 content with no counterpart (side nodes, paused
    hook waits, hook state), and what `copied_from` or `derived_from`
    records;
  - the on-disk store format (D12; the record encoding is a Phase 3
    choice), which the converter must target.
  These need a question and an ADR before Phase 7. Do not assume answers.

## Alternatives

| option | why not |
|---|---|
| Native read of 0.15 files (plan option 2) | Imports 0.15's guessing into the loader and breaks "reload is replay". The store would carry two formats forever. |
| No migration path | Costs users their history for no design benefit. |
| Convert on every load | Hides the conversion; a converted session should be a visible, one-time act. |

## Consequences

- The runtime has one format and one loader.
- Conversion is testable alone: input a fixture, assert a valid session.
- Cost: converted history is approximate where 0.15 files are ambiguous.
  The converter must fail on input it cannot map, not invent a position
  (CLAUDE.md, fail early).
- Cost: until Phase 7, no 0.15 session can be opened.
- Enforced by: I5 and I7 on whatever the converter writes; converter tests
  belong to Phase 7 and are not specified yet.
