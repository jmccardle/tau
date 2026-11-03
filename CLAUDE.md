# CLAUDE.md

τ (tau) is an agent harness: a sans-IO Rust core (`crates/`) under a Python
driver, hooks, providers, tools, and frontends (`python/tau/`). This is τ 0.16,
codename **Trusty**, a rewrite of 0.15. The merge commit `rewrite Tau in Rust`
joined Trusty's history to this repository's.

The design is `docs/design/` (`model.md`, `glossary.md`, `decisions/`).
`docs/plan.md` holds the phases and current status. Read both before changing
anything structural.

τ 0.15 is tag `v0.15.0`, and branch `0.15.x` takes its hotfixes (read it with
`git show v0.15.0:<path>`, or in a worktree). 0.16 was designed fresh. Code from
0.15 may be carried over selectively where it is presentation or plumbing that
fits the new model as-is: TUI widgets, themes, release and docs scripts. Never
carry harness code or concepts from it. Each carried piece names its 0.15 source
in its commit message, and it meets every rule below. `docs/from-0.15.md` lists
what 0.16 does with each 0.15 capability.

Sibling projects are clients of τ and sync with it after 0.16.0:
- `~/Development/tau-code`: TypeScript web and VS Code clients of `tau serve`,
  with a protocol package generated from the schema.
- `~/Development/tau-search`: web search tools shipped as a τ extension.

## Rules

1. 0.15 code comes in only as described above, never as harness behavior. Learn 0.15 behavior by reading it, then write the behavior as a test.
2. No new type, field, or module-level concept without a glossary entry; anything not in the glossary is suspect.
3. Banned identifiers and docstring words (lint in CI): `lane`, `suspension`, `suspend`, `responder`, `side_entry`, `incomplete`, `default_leaf`, `acting_cursor`, `head` (as a frontend), `frame`, `entry` (use `node`), `backend` (use `provider` or `frontend`), `fork` for anything in-session (use `branch`).
4. Tests go through the `Harness` protocol, the public Python API (what `docs/api/groups.toml` lists: `tau`'s exports, and the four public modules `tau.cli`, `tau.commandline`, `tau.tui` and `tau.trace`), or the files and wire formats those read. Every other module is private, and its name starts with an underscore (`_trust`, `_events`, `_driver`, ...); `scripts/api_docs.py --check` fails when one does not. No `unittest.mock.patch`, no `_private` access from tests (both linted). Fakes live at the effect boundary only: `FakeProvider` (scripted samples), `FakeTool`.
5. Each phase ends with a stop for review. Do not start the next phase without sign-off.
6. Diagrams are generated from code after Phase 3. Hand-drawn diagrams exist only in Phase 1.
7. Ratchet in CI from Phase 3, over `crates/*/src` and `python/tau` (src, not
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

`python/tau/_core.pyi` is hand-written; `mypy.stubtest` (in `check.sh`)
holds it to the built module, and `scripts/api_docs.py --check` holds its docstrings equal
to the built module's `__doc__`. The Rust `///` is the source: after changing one, rebuild and run
`scripts/stub_docs.py`.

The public API is documented from code. `docs/api/groups.toml` says what is public;
`scripts/api_docs.py` generates `docs/api/surface.txt` (every public name with its signature: any
API change is a diff to review) and `docs/reference/` (from the docstrings), and `check.sh` fails
when they are stale. A public name needs a docstring (`scripts/ratchet.py` counts the ones that
lack it); the generated files are never edited by hand. Rerun `scripts/api_docs.py` after
changing a public signature.

## Layout

| path | what |
|---|---|
| `crates/tau-core` | Pure Rust. No IO, no async runtime, no PyO3. Behavior lives here. |
| `crates/tau-store` | The only Rust crate that touches the filesystem for sessions (logs, scratchpads). |
| `crates/tau-tools` | The base tools (`read`, `write`, `edit`, `grep`, `find`, `bash`): filesystem and process IO for the model's tools, no PyO3. |
| `crates/tau-py` | PyO3 bindings, built as `tau._core`. Conversion and error mapping only. |
| `python/tau` | Python package. `_core.pyi` is the hand-written stub for the native module. |
| `python/tau/tui` | The terminal UI (D23): `TauApp` over a `Client`, `SessionView`, the widgets, `tau.tcss`, the themes. Imports Textual; `tau -p` never loads it. |
| `tests/behavior` | The behavior suite through the `Harness` protocol, run twice: clients local, then remote over a Unix socket (D21). |
| `tests/serve` | What only a wire has: the handshake, bad messages, each way a stream ends, disconnects, and `tau serve` (`-d`, `--stop`, `--schema`) as real processes. |
| `tests/tui` | The terminal UI through Textual's pilot, over a Harness client, run in both modes (D23); `SessionView` with events alone. |
| `tests/core` | The native module through the public Python API: a hand-run job loop, errors, persistence, threads. |
| `docs/api` | What is public and for whom (`README.md`, `groups.toml`), and the generated `surface.txt`. |
| `docs/reference` | The generated reference, one page per group of the public surface. |
| `scripts/` | Gates: `check.sh` (everything, in CI order). |

## Commands

```bash
source ~/.cargo/env                       # cargo is not on the default PATH here
# venv: Python 3.12 is the floor
~/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12 -m venv .venv
.venv/bin/pip install --group dev        # pip >= 25.1
VIRTUAL_ENV=$PWD/.venv .venv/bin/maturin develop --release   # rebuild after any Rust change
scripts/check.sh                          # the gate
```

## Remotes and CI

- `origin`: dev.ffwf.net `john/tau` (private). `goblin`: forge.goblincorps.com
  `john/trusty`, which runs CI. It still has Trusty's name, and its history
  is the same as this repository's, since the merge. Push to both.
- CI (`.forgejo/workflows/ci.yml`) has one Linux x86_64 runner, label
  `ubuntu`. `scripts/ci-toolchain.sh` installs everything a job needs; to
  reproduce a CI job, run it in a bare `ubuntu:24.04` container.
- Forgejo, not GitHub: `runs-on` is a single string. Job containers cannot
  reach the forge's public URL, so anything that calls back to it (artifact
  upload, cache) fails with ECONNREFUSED; checkout works.
- PyPI publishing for 0.15 ran from GitHub (`.github/workflows/publish.yml`,
  trusted publishing, removed by the merge; see `git show v0.15.0:.github/workflows/publish.yml`).
  A trusted publisher is bound to the workflow's file name, so its maturin
  replacement keeps that name.

## Python versions and wheels

- Floor: CPython 3.12. One `cp312-abi3` wheel covers every GIL build ≥ 3.12.
- Free-threaded: no stable ABI before 3.15 (`abi3t`), so 3.14t gets a
  version-specific `cp314t` wheel. maturin does this automatically from the
  same config.
- `gil_used = false` is PyO3 0.30's default; the module states it anyway, as a
  claim the tests hold it to. `tests/core/test_threads.py` fails on a
  free-threaded build if importing us turns the GIL back on. Every `#[pyclass]`
  is `frozen` unless a glossary entry and an ADR argue otherwise.

## Writing code here

- Fail early: a core `Result::Err` becomes a typed Python exception
  (`create_exception!`, subclassing the closest builtin). No defaults that hide
  a missing decision; ask instead.
- Commits end with `Co-Authored-By:` for the AI contributor.
- Trunk is `master`. Version tags mark releases; `master` is last tag plus
  work in progress.
