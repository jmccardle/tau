# As-built metrics, Phase 4 (`python/tau_next`)

Measured 2026-10-30 on master at `58260e0` (working tree clean apart from this
directory). The same methods as Phase 3d (`docs/as-built/metrics/README.md`),
pointed at `python/tau_next`. It measures; it judges nothing. Verdicts are in the
reviewers' documents, not here.

## Tools

| purpose | tool | version |
|---|---|---|
| CC, LOC, methods per class | `radon` via `scripts/py_baseline.py` (unchanged) | 6.0.1 |
| per-module table | `radon`, `tools/py_modules.py.txt` | 6.0.1 |
| coverage (line + branch) | `coverage.py`, `tools/coverage.toml` | 7.17.0 |
| import graph | `grimp`, `tools/py_imports.py.txt` | 3.18 |
| rendering | graphviz `dot` | 2.43.0 |
| ratchet | `scripts/ratchet.py` | repo |
| tests | pytest 9.1.1, Python 3.12 | 736 passed, 4 skipped |

radon, coverage.py and grimp live in a throwaway venv, not in `pyproject.toml`.
Counting rules are those of `py_baseline.py`: methods are counted once as
functions, nested functions fold into their parent, "lines" are physical lines.

## Size and complexity: `python/tau_next` against τ 0.15 and `crates/`

Raw data: `data/python.json` (src and tests, same fields as 3d's
`tau015-python.json`), `data/python-modules.json`, `data/modules-table.md`.

| | trusty `python/tau_next` | trusty `crates/*/src` (3d) | τ 0.15 `src` |
|---|---:|---:|---:|
| files | 25 | 18 | 176 |
| lines | 7,184 | 7,119 | 99,233 |
| functions | 455 | 315 | 3,301 |
| CC max | **21** | 30 | 73 |
| CC p50 / p90 / p99 | 2 / 9 / 15 | - / 8 / 21 | 2 / 8 / 21 |
| CC mean | 3.70 | 3.32 | 3.87 |
| functions over CC 10 | 25 (5.5%) | 17 (5.4%) | 222 (6.7%) |
| functions over CC 15 | **2** (0.4%) | 5 (1.6%) | 85 (2.6%) |
| functions over CC 20 | 1 | 4 | 35 |
| function LOC max / p90 | 49 / 24 | 102 / 24 | 336 / 44 |
| functions over 50 lines | 0 | 6 | 258 |
| file lines max / p90 | 713 / 638 | 867 / 750 | 6,510 / 1,256 |
| files over 800 lines | 0 | 1 | 32 |
| largest class by methods | 42 (`Driver`) | 47 (`tau-py::Session`) | 216 (`AgentSession`) |
| classes over 25 methods | 1 | 3 | 22 |

Tests (`tests/`, whole directory, same tool): 49 files, 11,171 lines, 763
functions, CC max 18 / p90 6 / mean 3.32, 6 over CC 10, 1 over CC 15, one file
over 800 lines (856), one function over 50 lines (59).

### Per module

| module | lines | functions | CC max | CC mean | >10 |
|---|---:|---:|---:|---:|---:|
| `_config_layers.py` | 713 | 44 | 21 | 5.0 | 4 |
| `_trust.py` | 640 | 48 | 11 | 3.29 | 1 |
| `providers/gemini.py` | 638 | 34 | 15 | 5.5 | 3 |
| `providers/anthropic.py` | 514 | 26 | 15 | 4.88 | 2 |
| `providers/_wire.py` | 467 | 33 | 13 | 4.15 | 1 |
| `providers/openai.py` | 439 | 24 | 14 | 5.04 | 2 |
| `driver.py` | 435 | 42 | 9 | 2.26 | 0 |
| `effects.py` | 428 | 21 | 13 | 4.76 | 2 |
| `tools.py` | 331 | 20 | 15 | 4.3 | 3 |
| `handles.py` | 293 | 39 | 2 | 1.08 | 0 |
| `cli.py` | 254 | 11 | 13 | 5.36 | 2 |
| `providers/_walk.py` | 253 | 19 | 11 | 3.63 | 1 |
| `protocols.py` | 246 | 20 | 8 | 1.85 | 0 |
| `base_tools.py` | 245 | 18 | 3 | 1.22 | 0 |
| `providers/_anthropic_request.py` | 209 | 10 | 13 | 5.5 | 2 |
| `events.py` | 205 | 10 | 16 | 3.6 | 1 |
| `__init__.py` | 198 | 0 | 0 | 0 | 0 |
| `_config.py` | 156 | 8 | 10 | 5.25 | 0 |
| `trace.py` | 149 | 10 | 4 | 1.8 | 0 |
| `_startup.py` | 142 | 5 | 12 | 4.4 | 1 |
| `providers/mock.py` | 77 | 5 | 7 | 4.0 | 0 |
| `_dotenv.py` | 67 | 2 | 10 | 9.0 | 0 |
| `step_limit.py` | 55 | 4 | 4 | 2.25 | 0 |
| `providers/__init__.py` | 22 | 1 | 2 | 2.0 | 0 |
| `clock.py` | 8 | 1 | 1 | 1.0 | 0 |

By package: `providers/` is 9 files and 2,619 lines (36% of the package);
`driver.py` + `effects.py` + `events.py` + `handles.py` + `protocols.py` +
`tools.py` + `base_tools.py` + `step_limit.py` + `trace.py` + `clock.py` is
2,395 lines; `_config*`, `_trust`, `_startup`, `_dotenv`, `cli` is 1,972; `__init__.py` is 198.

### Top 20 functions by CC

| CC | where | function | LOC |
|---:|---|---|---:|
| 21 | `_config_layers.py:287` | `_merge_models` | 49 |
| 16 | `events.py:127` | `EventStream.offer` | 18 |
| 15 | `tools.py:159` | `_params` | 48 |
| 15 | `providers/anthropic.py:331` | `_thinking_problems` | 33 |
| 15 | `providers/gemini.py:464` | `_thinking_problems` | 22 |
| 14 | `providers/openai.py:226` | `_Stream._call` | 23 |
| 14 | `providers/openai.py:261` | `_Stream._read` | 31 |
| 13 | `effects.py:219` | `SampleRun._delta` | 28 |
| 13 | `cli.py:219` | `main` | 36 |
| 13 | `tools.py:120` | `_kind` | 16 |
| 13 | `_config_layers.py:512` | `_Resolver.model` | 44 |
| 13 | `providers/anthropic.py:215` | `_Stream._delta` | 21 |
| 13 | `providers/_anthropic_request.py:105` | `_thinkless_loop` | 26 |
| 13 | `providers/_wire.py:393` | `Connection.sample` | 49 |
| 13 | `providers/gemini.py:301` | `_Stream._text` | 27 |

`python.json` stores only the top 15 (`py_baseline.py`'s limit). Ranks 16 to 20
are all CC 12 (`_Resolver.checked`, `_Resolver.settings`, `_startup.select_model`,
`SampleRun._stream`, `gemini._Stream._read`); the full 20 are in
`data/python-top20.txt`.

Largest classes by methods: `Driver` 42, `TreeHandle` 22, `Cursor` 13,
`_Resolver` 11, `anthropic._Stream` 11.

## Rule 7 on `python/`

`scripts/ratchet.py` reads only `crates/*/src`. Its run on this tree
(`data/ratchet.txt`):

```
ratchet ok: 0 allowed violation(s) remain (0 function_cc, 0 type_methods, 0 file_lines)
```

`scripts/ratchet-allow.toml` has no entries and no `[justified]` entries. The
Rust violations that 3d listed (9) are gone. Python is not in the ratchet
("Python is not in the ratchet until Phase 4", allowlist header), so the
following are rule 7's limits applied by hand to `python/tau_next`; none is
enforced:

| rule | item | now | limit |
|---|---|---:|---:|
| function CC | `_config_layers.py` `_merge_models` | 21 | 15 |
| function CC | `events.py` `EventStream.offer` | 16 | 15 |
| methods | `driver.py` `Driver` | 42 | 25 |
| file lines | none over 800 (max `_config_layers.py` 713) | - | 800 |

Functions at exactly CC 15: `tools._params`, `anthropic._thinking_problems`,
`gemini._thinking_problems`. Files within 100 lines of the limit:
`_config_layers.py` 713, `_trust.py` 640, `gemini.py` 638.

## Coverage

`coverage.py`, branch on, `source = tau_next`, over `pytest tests` (736 passed,
4 skipped, 13 s). Reported, not gated. Data: `data/coverage-py.{txt,json}`,
`data/coverage-table.md`. "cover" is coverage.py's combined statement-and-branch
percentage, the same as τ 0.15's "line + branch" figure.

| | trusty `python/tau_next` | τ 0.15 |
|---|---:|---:|
| statements | 3,990 (186 missed) | 34,343 |
| statements covered | 95.3% | 92.1% |
| branches | 1,374 (138 missed, 126 partial) | |
| branches covered | 90.0% | |
| statements + branches | **94.0%** | 90.3% |

Modules under 90%:

| module | cover | missed lines | missed branches |
|---|---:|---:|---:|
| `handles.py` | 88.7% | 16 | 1 |
| `providers/gemini.py` | 88.9% | 30 | 30 |

Next five (90 to 92%): `effects.py` 90.4, `_trust.py` 90.7,
`providers/_anthropic_request.py` 91.0, `providers/anthropic.py` 91.3,
`providers/_walk.py` 91.7. Every other module is at 93.5% or above; 8 modules
are at 100% (`__init__`, `_config`, `_dotenv`, `_startup`, `cli`, `clock`,
`providers/__init__`, `step_limit`).

Uncovered lines of the five worst:

| module | uncovered lines |
|---|---|
| `handles.py` (88.7%) | 104, 107, 113, 119, 122, 126, 187-188, 205, 208, 211, 247, 250, 253, 259, 266 |
| `providers/gemini.py` (88.9%) | 101, 130, 133-136, 138, 217-218, 226, 248, 253, 313, 324, 338, 342, 345, 360, 367, 393, 398, 403, 451, 474, 482, 490, 494-495, 584, 637 |
| `effects.py` (90.4%) | 115-116, 201, 216, 245-246, 257, 262, 273, 284-285, 334, 349, 351, 369-370, 377-379, 393, 398-399, 414 |
| `_trust.py` (90.7%) | 139, 152, 187-188, 192, 230-232, 246-247, 251, 253, 258, 270-271, 275, 291-292, 323, 343, 372-374, 423-424, 438-439, 467, 566, 639 |
| `providers/_anthropic_request.py` (91.0%) | 89, 98-99, 130, 188, 193 |

Partial-branch lines (taken one way only) are in the `Missing` column of
`data/coverage-py.txt` as `a->b`. The 4 skipped tests need root (1), a free-threaded build (1: on a GIL
build there is nothing to re-enable), or `TAU_LIVE_BASE_URL` (2, live endpoint); the real-provider modules are covered by their
wire-format tests with scripted transports, not live calls.

Not comparable with 3d's Rust numbers: those are LLVM line coverage of
`crates/`, driven by the Rust tests plus this same pytest suite (94.8% combined).

## Dependencies

Graph: `svg/imports.svg` (providers shaded blue); edges `data/import-edges.txt`
(97 lines); machine-readable `data/imports.json`; dot source `data/imports.dot`.
grimp resolves `tau_next._core` (a native extension) to nothing, so the 16 edges
to `_core` come from a text scan of `from tau_next._core import` /
`from tau_next import _core` and are included in the 97.

- **Modules:** 25 plus `_core`. **Edges:** 97 (81 among Python modules, 16 to `_core`).
- **Cycles:** none. (grimp's raw output shows one 21-module cycle through
  the package `tau_next`, because `from tau_next._core import ...` is read as
  an import of `tau_next`. With the `_core` edges re-attached to `_core`, the
  strongly connected components are all single modules.)
- **Native `_core` importers (16):** `__init__`, `_config`, `base_tools`,
  `driver`, `effects`, `handles`, `protocols`, `tools`, `trace`,
  `providers/_anthropic_request`, `providers/_walk`, `providers/_wire`,
  `providers/anthropic`, `providers/gemini`, `providers/mock`,
  `providers/openai`. The other 9 modules never touch `_core`:
  `_config_layers`, `_dotenv`, `_startup`, `_trust`, `cli`, `clock`, `events`,
  `step_limit`, `providers/__init__`.
- **Third-party imports:** `httpx`, only from `providers/_wire`, `anthropic`,
  `gemini`, `openai`. Everything else is stdlib.
- **Layering as imported:**
  - `providers/*` imports `_config`, `events`, `protocols` and other
    `providers/*` modules only. It imports none of `driver`, `effects`,
    `handles`, `tools`, `trace`, `cli`, `_trust`, `_startup`.
  - `driver.py` imports `_config_layers`, `clock`, `effects`, `events`,
    `handles`, `protocols`, `trace`. It imports no provider module and not
    `providers`.
  - `providers` (the package) is imported by `__init__`, `_trust` and `cli`
    (all for `builtin_providers`).
  - `cli` is imported by nothing; `__init__` imports `driver`, `providers`,
    and 13 others.
  - `_core` is imported directly by `driver`, `effects`, `handles`,
    `protocols`, `tools`, `base_tools` and the providers.
- Highest in-degree (Python modules): `protocols` 15, `_config` 12, `events` 9,
  `_wire` 6, `_config_layers` 6, `handles` 5. Highest out-degree: `__init__` 15,
  `driver` 8, `cli` 7, `effects` 7.

## Reproduce

```bash
source ~/.cargo/env
T=$(mktemp -d); export TAU_HOME=$T/tauhome; mkdir -p $TAU_HOME   # never the real ~/.tau
python3.12 -m venv $T/v && $T/v/bin/pip install radon==6.0.1 coverage grimp

# size and complexity
$T/v/bin/python scripts/py_baseline.py . --src python/tau_next --tests tests \
    > docs/as-built/phase4/metrics/data/python.json
PATH=$T/v/bin:$PATH python docs/as-built/phase4/metrics/tools/py_modules.py.txt
$T/v/bin/radon cc -s python/tau_next | sort by CC           # ranks 16 to 20

# coverage (the repo's .venv runs the tests; coverage comes in on PYTHONPATH)
PYTHONPATH=$T/v/lib/python3.12/site-packages .venv/bin/python -m coverage run \
    --rcfile=docs/as-built/phase4/metrics/tools/coverage.toml -m pytest -q tests
PYTHONPATH=$T/v/lib/python3.12/site-packages .venv/bin/python -m coverage report \
    --rcfile=docs/as-built/phase4/metrics/tools/coverage.toml --skip-empty

# imports
PYTHONPATH=python $T/v/bin/python docs/as-built/phase4/metrics/tools/py_imports.py.txt
dot -Tsvg docs/as-built/phase4/metrics/data/imports.dot > docs/as-built/phase4/metrics/svg/imports.svg

# ratchet
.venv/bin/python scripts/ratchet.py
```

## Files

| path | what |
|---|---|
| `data/python.json` | `py_baseline.py` output: src (`python/tau_next`) and tests |
| `data/python-modules.json`, `data/modules-table.md` | per-module LOC, functions, CC |
| `data/python-top20.txt` | top 20 functions by CC |
| `data/coverage-py.{txt,json}`, `data/coverage-table.md` | coverage.py reports |
| `data/imports.{json,dot}`, `data/import-edges.txt`, `svg/imports.svg` | import graph |
| `data/ratchet.txt` | `scripts/ratchet.py` output |
| `tools/` | the three throwaway files the above came from (`.py.txt` so ruff does not lint them; run as `python file.py.txt`) |
