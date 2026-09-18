# CLAUDE.md

τ ("tau") is a Python agent harness — a programmable coding-agent library with
an optional TUI. This file is the **map**: where things are, what gates them,
and the handful of rules that apply to every change. It deliberately holds no
implementation detail, because implementation detail written here goes stale
silently and is read on every request.

Everything else has an owner:

| You want | Read |
|---|---|
| Why a piece of code is shaped the way it is | `docs/INDEX.md` → the design record it names |
| What is open, what shipped, what is owed | `ROADMAP.md` |
| How to write or revise a design record | `.claude/skills/feature-doc` |
| How to cut a release | `.claude/skills/release` + `docs/RELEASING.md` |
| How to write up finished work | `.claude/skills/worklog` |

τ began as a port of the TypeScript [pi-mono](https://github.com/badlogic/pi-mono),
and a checkout lives at `~/Development/pi`. **Parity with pi is not an
objective** (decided 2026-09-03). pi is a reference for how a ported behaviour
was originally derived, never an authority on what τ should do; "pi does it this
way" is not a reason, and a divergence needs no justification beyond its own.

## Packages

Five independently-installable distributions, each `src/`-layout, each with its
own `tests/` beside it. The three that carry the program stack, bottom-up:

| Directory | Imports as | Installs as | Depends on | Holds |
|---|---|---|---|---|
| `tau-llm/` | `tau_llm` | `ffwf-tau-llm` | — | Wire protocols, streaming events, message and tool types |
| `tau-agent-core/` | `tau_agent_core` | `ffwf-tau-agent-core` | `tau-llm` | Agent loop, tools, sessions, extensions, compaction, capability registry — headless |
| `tau-coding-agent/` | `tau_coding_agent` | `ffwf-tau-coding-agent` | `tau-agent-core` | The heads: Textual TUI, CLI entry points, backends |

Two more are easy to forget, which is why the pre-commit hook gates **five**
`src` trees:

| Directory | Imports as | Installs as | Holds |
|---|---|---|---|
| `tau-jmfts/` | `tau_jmfts` | `ffwf-tau-jmfts` | JMFTS client and enrichment extension; optional |
| `tau-meta/` | `tau_meta` | `ffwf-tau` | A guessable name resolving to `ffwf-tau-coding-agent[tui]`; one module holding `__version__` |

Dependencies point one way only. A lower package must never import a higher one.

**τ has three heads** — the Textual TUI, `tau -p`, and `tau --mode rpc` (plus
`tau --mode repl`, which shares the TUI's path). A user-visible *derivation*
written inside a head reaches exactly one of them. So a reading belongs in
`tau_agent_core` as a pure function, and a head renders it and never computes
it. Before writing a derivation inside a head, name which of the others would
not get it.

## The rest of the tree

| Path | What it is |
|---|---|
| `docs/` | 82 design records, one per non-trivial change; `docs/INDEX.md` is the index |
| `docs/library/reference/` | Generated from `@agent_facing` markers — edit the docstring, not the page |
| `docs/probe-results/` | Measured runs; exempt from the prose scans, because editing one falsifies it |
| `.claude/skills/` | `feature-doc`, `release`, `worklog` — invoked, not read by default |
| `examples/` | 40 runnable extension and SDK examples; the numbered ones build on each other |
| `scripts/` | Repo tooling: doc builds, coverage and leakage gates, probes |
| `experiments/` | `backend-probe`, `m2`, `m3` — research runs, exempt from the scans |
| `docker/` | The release matrix's per-Python-version images |
| `venv/` | The in-repo virtualenv every gate requires by absolute path |
| `pyproject.toml` (root) | pytest, mypy and ruff config for the whole repo — not a package |
| `bump-version.sh`, `package.sh` | Release mechanics; `docs/RELEASING.md` owns their use |
| `~/.tau/config.json` | User config, outside the repo. Default model is `local-llm`, a local OpenAI-compatible server |

## Commands

```bash
# Setup — editable installs into the in-repo venv.
# [dev] pulls in [tui] + [jmfts] + [bus] + [images]; a plain install cannot run pytest.
# Neither the anthropic nor the google-genai SDK is needed: both providers import
# lazily and both test files stub the SDK. Install the extras only to call the API.
python -m venv venv && source venv/bin/activate
pip install -e ./tau-llm -e './tau-agent-core[dev]' -e './tau-coding-agent[dev]' -e ./tau-jmfts

# Tests — config is in the ROOT pyproject.toml, so run from the repo root.
# asyncio_mode=auto: an async test needs no decorator.
pytest                                               # whole suite
pytest tau-llm/tests/test_openai_provider.py::test_x # one test
pytest -k tool_call                                  # by name substring
pytest -m llama                                      # opt-in; deselected by default

# Static checks — the same four the pre-commit hook runs, same scope.
venv/bin/ruff check   tau-llm/src tau-agent-core/src tau-coding-agent/src tau-jmfts/src tau-meta/src
venv/bin/ruff format --check <same five>
venv/bin/mypy <same five>
venv/bin/python scripts/strip_comment_blocks.py <same five> --check

# Doc gates
venv/bin/python scripts/build_agent_docs.py      # regenerate docs/library/reference/, then commit it
venv/bin/python scripts/check_docs_coverage.py   # reports file:line per fault; NOT in the hook yet

# Run it
tau                          # TUI
tau --mode repl              # prompt line, no screen takeover
tau -p "summarize @README.md" # headless transcript
tau -p --mode json "…"        # JSONL lifecycle events
```

`run_agent_loop.py` is **not** a headless τ runner — it shells out to `pi -p` to
build τ. Don't use it to run τ.

## Gates

`.githooks/pre-commit` runs four checks over the five `src` trees: `ruff check`,
`ruff format --check`, `mypy`, and the comment-block scan. Activate it once per
clone with `git config core.hooksPath .githooks`. It requires the in-repo venv by
absolute path and errors rather than falling back to a PATH tool, because a
different version passing locally and failing elsewhere is the failure it exists
to prevent.

**Tests are not gated**, and neither is `scripts/check_docs_coverage.py` — the
tree is not clean enough for a hook that would always fail. `ROADMAP.md` carries
the number.

**mypy must run on all five trees in one invocation.** Running it on one package
alone reports false errors.

## Every commit here is published

From 0.10.3 onward this history **is** the public history: commits go to
`github.com/jmccardle/tau` as they are made, unsquashed. `origin` is the private
development host and gets the same commits. A commit subject, a test fixture, a
docstring and a `docs/` file are all public the moment they are pushed, and
`git push --force` is no longer a way to unsay something.

`tau-coding-agent/tests/test_no_host_addresses.py` enforces the consequence over
three surfaces. The five `src` trees plus `examples/` and `scripts/` reject a LAN
address and a home directory, because a user installs or copies out of them. The
prose that ships — `docs/`, `README.md`, `ROADMAP.md`, `CLAUDE.md` — rejects a
home directory and the private git host, because there the problem is disclosure
rather than breakage. The four `tau-*/tests` trees take the strict pattern, added
2026-09-13: they are published like prose, but a fork also *runs* them, so both
halves of the risk land there.

The private host's address is written down in **two test constants and nowhere
else** — that file's `PROSE_LEAKS` and `test_packaging.py`'s `PRIVATE_HOST`,
which gates the PyPI metadata. Both exist to forbid the value elsewhere. Nothing
else may contain it, and a document describing the leak pattern must not repeat
it.

## Writing code here

- **"Fail Early" — no silent fallbacks.** A fallback, a placeholder or fabricated
  data is an anti-pattern: raise instead. This is the repo owner's standing rule
  and several source docstrings cite it by that name.
- **A comment is ONE line, and it states a dependence or an assumption** — an
  ordering constraint, an invariant the code does not show, a workaround tied to
  a named upstream version. A run of two or more own-line `#` comments is prose
  that outgrew its place; the hook rejects it. Tool directives (`# type:`,
  `# noqa:`, `# fmt:`, `# pragma:`, a shebang, a coding line) are exempt, because
  they are code.
- **A comment is never a load-bearing interface.** If prose must be readable from
  code, put it where `__doc__` reaches it.
- **Every module, class and function gets a Google-style docstring.** Rationale
  does not live there — it goes in a `docs/` record and comes back as a one-line
  citation by name.
- **Streamline the docstring of anything you touch. Permission is standing; do
  not ask.** Shorter and more useful than you found it, in the same commit, never
  as a sweep. `.claude/skills/feature-doc` has what to cut and what to keep.
- **An agent-facing object carries `@agent_facing(topic=…)`** from `tau_llm.docs`
  — a typed no-op read out of the AST. Adding or changing one means writing the
  docstring, running `scripts/build_agent_docs.py`, and committing the
  regenerated `docs/library/reference/` in the same change.

## Reading the code

- `Reference: PHASE-N-SUBPHASE-M.md` headers are a port-era convention and those
  files were retired in 2026-08. The citation resolves to nothing — read it as
  provenance and do not go looking. `docs/SUBPHASE-0.0.md` is the one survivor of
  that naming and is live; 24 source files cite it for the core data contracts.
- Reading the corresponding pi file is often the fastest way to understand ported
  code. It is not a check to pass.
