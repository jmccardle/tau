# CLAUDE.md

τ next, codename **Trusty**: τ's agent harness rewritten as a sans-IO Rust core
(`crates/`) under a Python driver, hooks, providers, tools, and frontends
(`python/tau_next/`). The τ repository absorbs this one as τ 0.16.0: `tests/`
replaces the 0.15 suite, `parity/` is deleted.

The design is `docs/design/` (`model.md`, `glossary.md`, `decisions/`).
`docs/plan.md` holds the phases and current status. Read both before changing
anything structural.

τ 0.15 lives at `~/Development/tau` (tag `v0.15.0`). Read it to learn
behavior. Never copy from it.

## Rules

1. Copy no code from τ 0.15. Read it only to learn behavior, then write the behavior as a test.
2. No new type, field, or module-level concept without a glossary entry; anything not in the glossary is suspect.
3. Banned identifiers and docstring words (lint in CI): `lane`, `suspension`, `suspend`, `responder`, `side_entry`, `incomplete`, `default_leaf`, `acting_cursor`, `head` (as a frontend), `frame`, `entry` (use `node`), `backend` (use `provider` or `frontend`), `fork` for anything in-session (use `branch`).
4. Tests go through the `Harness` protocol, the public Python API (`tau_next`'s exports and `tau_next.cli.main`), or the files and wire formats those read. Underscore modules (`_trust`, `_dotenv`, `_config_layers`, ...) are private. No `unittest.mock.patch`, no `_private` access from tests (both linted). Fakes live at the effect boundary only: `FakeProvider` (scripted samples), `FakeTool`.
5. Each phase ends with a stop for review. Do not start the next phase without sign-off.
6. Diagrams are generated from code after Phase 3. Hand-drawn diagrams exist only in Phase 1.
7. Ratchet in CI from Phase 3, over `crates/*/src` and `python/tau_next` (src, not
   tests; Python measured with radon): max function CC ≤ 15, no file over 800 lines,
   banned-term count 0, I7 property test green. A type past 25 methods either
   splits along concept lines or carries a one-line justification naming the
   single concept it is (`[justified]` in `scripts/ratchet-allow.toml`, which
   the ratchet prints on every run so a review sees it).

Rule 3 is enforced by `scripts/check_rules.py` (snake, kebab, and CamelCase),
except `head` and `fork`, which have legitimate uses a regex cannot tell apart.
An unrelated use of a banned word is allowed only with a reason on the same line:
`# rules: allow(entry): Python packaging API`. Rule 4 is enforced by
`scripts/check_rules.py` (private imports), ruff SLF001 (private attributes),
ruff banned-api (`unittest.mock`), and `tests/conftest.py` (`monkeypatch`).

`python/tau_next/_core.pyi` is hand-written; `mypy.stubtest` (in `check.sh`)
holds it to the built module.

## Layout

| path | what |
|---|---|
| `crates/tau-core` | Pure Rust. No IO, no async runtime, no PyO3. Behavior lives here. |
| `crates/tau-store` | The only Rust crate that touches the filesystem for sessions (logs, scratchpads). |
| `crates/tau-tools` | The base tools (`read`, `write`, `grep`, `bash`): filesystem and process IO for the model's tools, no PyO3. |
| `crates/tau-py` | PyO3 bindings, built as `tau_next._core`. Conversion and error mapping only. |
| `python/tau_next` | Python package. `_core.pyi` is the hand-written stub for the native module. |
| `tests/behavior` | Phase 2: behavior suite through the `Harness` protocol. Carried into τ 0.16 as its test suite. |
| `tests/core` | The native module through the public Python API: a hand-run job loop, errors, persistence, threads. |
| `parity/` | The API diff against τ 0.15 (the oracle). Migration tooling: deleted when τ absorbs this repo. See `parity/README.md`. |
| `scripts/` | Gates: `check.sh` (everything except parity, in CI order), `parity.sh`. |

## Commands

```bash
source ~/.cargo/env                       # cargo is not on the default PATH here
# venv: Python 3.12 is the floor (matches τ 0.15, which parity/ imports alongside)
~/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12 -m venv .venv
.venv/bin/pip install --group dev --group parity      # pip >= 25.1; parity is optional
VIRTUAL_ENV=$PWD/.venv .venv/bin/maturin develop --release   # rebuild after any Rust change
scripts/check.sh                          # the gate; passes without τ 0.15
scripts/parity.sh                         # the API diff; needs --group parity
```

## Remotes and CI

- `origin`: dev.ffwf.net (private). `goblin`: forge.goblincorps.com, which runs
  CI. Push to both.
- CI (`.forgejo/workflows/ci.yml`) has one Linux x86_64 runner, label
  `ubuntu`. `scripts/ci-toolchain.sh` installs everything a job needs; to
  reproduce a CI job, run it in a bare `ubuntu:24.04` container.
- Forgejo, not GitHub: `runs-on` is a single string. Job containers cannot
  reach the forge's public URL, so anything that calls back to it (artifact
  upload, cache) fails with ECONNREFUSED; checkout works.

## Python versions and wheels

- Floor: CPython 3.12. One `cp312-abi3` wheel covers every GIL build ≥ 3.12.
- Free-threaded: no stable ABI before 3.15 (`abi3t`), so 3.14t gets a
  version-specific `cp314t` wheel. maturin does this automatically from the
  same config.
- `gil_used = false` is PyO3 0.30's default; the module states it anyway, as a
  claim the tests hold it to. `tests/core/test_threads.py` fails on a
  free-threaded build if importing us (alone or beside τ 0.15) turns the GIL
  back on. Every `#[pyclass]` is `frozen` unless a glossary entry and an ADR
  argue otherwise.

## Writing code here

- Fail early: a core `Result::Err` becomes a typed Python exception
  (`create_exception!`, subclassing the closest builtin). No defaults that hide
  a missing decision; ask instead.
- Commits end with `Co-Authored-By:` for the AI contributor.
- Trunk is `master`. Version tags mark releases; `master` is last tag plus
  work in progress.
