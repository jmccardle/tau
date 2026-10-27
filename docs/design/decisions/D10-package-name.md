# D10: Package name during development

Status: accepted, revised in review (Phase 1, 2026-10-26)

## Context

The old and new packages must import in the same venv, so the parity tooling
can run both (plan amendments). Development needs a name that cannot collide
with τ 0.15.

0.15 installs the import names `tau_llm`, `tau_agent_core`,
`tau_coding_agent`, and `tau_meta`, plus the optional `tau_jmfts`. It has no
`tau` package.

## Decision

- The Python package is `tau_next`. The native module is `tau_next._core`,
  built by `crates/tau-py` and described by the hand-written `_core.pyi`.
- The development distribution is `ffwf-tau-next`.
- Rust crates are `tau-core`, `tau-store`, and `tau-py` (the names in the
  glossary: `tau_core`, `tau_store`).
- The rewrite ships as **τ 0.16.0, "Trusty"**. The τ repository absorbs this
  one; `tests/` replaces the 0.15 suite and `parity/` is deleted.
- Final distribution and import names are settled at absorption, not decided
  here. This is open.
- Glossary identifiers use these names: Python names live in `tau_next`, Rust
  names in `tau_core`.

## Alternatives

| option | why not |
|---|---|
| Rename to `tau` immediately | Would not coexist with 0.15 in one venv if any 0.15 name overlapped, and it makes parity tooling fragile. |
| Branch inside the old repo | Different build system (maturin and a Cargo workspace), and the old and new packages could not be installed side by side (plan §0). |
| Ship as `ffwf-tau` 1.0 (the plan's wording) | Replaced by the 0.16.0 absorption in the Phase 0 amendments. |

## Consequences

- Both packages import in one venv; parity tests can compare them.
- Cost: every example and doc says `tau_next` until absorption, then must
  change. A grep-able name makes that mechanical.
- Cost: the final name is not yet known, so nothing outside this repo should
  depend on `tau_next` or `ffwf-tau-next`.
- Enforced by: Phase 0's exit test (the wheel installs in a clean venv next
  to 0.15 and both import) and `tests/scaffold`. No invariant covers it.

## History

Plan default: `tau_next`, renamed at 1.0, shipping as `ffwf-tau` 1.0. The
Phase 0 amendments (2026-10-25) changed the destination to τ 0.16.0
"Trusty" and moved the final-name decision to absorption. The development
name `tau_next` is unchanged.
