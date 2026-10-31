# D10: Package name during development

Status: accepted (Phase 1)

## Context

The old and new packages must import in one venv so `parity/` can run both.
0.15 installs `tau_llm`, `tau_agent_core`, `tau_coding_agent`, `tau_meta`,
and optionally `tau_jmfts`; it has no `tau` package.

## Decision

- Python package `tau_next`; native module `tau_next._core` (built by
  `crates/tau-py`, stubbed by `_core.pyi`). Distribution `ffwf-tau-next`.
- Rust crates `tau-core`, `tau-store`, `tau-py`.
- Ships as **τ 0.16.0, "Trusty"**: the τ repository absorbs this one.
- Final distribution and import names are set at absorption (open).

## Alternatives

| option | why not |
|---|---|
| Rename to `tau` now | Fragile beside 0.15 in one venv. |
| Branch inside the old repo | A different build system; no side-by-side install. |
| Ship as `ffwf-tau` 1.0 | Replaced by the 0.16.0 absorption. |

## Consequences

- Both packages import in one venv.
- Cost: every doc says `tau_next` until absorption; a grep fixes it.
- Enforced by Phase 0's install test and `tests/scaffold`.

## At absorption (2026-10-31)

The τ repository merged Trusty's history (`rewrite Tau in Rust`), and the
grep was run. The names are now:

- Python package `tau`, native module `tau._core`.
- Distribution `ffwf-tau`, one wheel. Installing 0.16 upgrades the 0.15
  metapackage of the same name in place.
- 0.15's four sub-distributions (`ffwf-tau-llm`, `ffwf-tau-agent-core`,
  `ffwf-tau-coding-agent`, `ffwf-tau-jmfts`) end at 0.15. If they are left
  installed beside 0.16 they do no harm, because their import names
  (`tau_llm`, `tau_agent_core`, ...) do not collide with `tau`.
- `parity/` is deleted. 0.15 stays reachable at tag `v0.15.0` and on branch
  `0.15.x`, which takes its hotfixes.
