Experiment record (2026-10-05). Measured runs; do not edit the numbers.

# τ on free-threaded CPython (3.14t, 3.15.0rc3t)

**Question.** Why won't τ run under free-threaded / nogil Python, and how deep
are the issues?

**Answer.** It does run. On 3.14.8t and 3.15.0rc3t, with the GIL off, the full
suite passes with exactly the same counts as on regular 3.14. `tau -p`,
`tau --mode rpc` and the Textual TUI each completed a tool-using turn, and 32
sessions running in parallel threads in one process gave correct answers.
**Depth: dependency-level, and only on 3.15t,** where some wheels don't exist
yet and two packages have to be built from source. On 3.14t nothing had to be
built for τ's own dependency set. No dependency re-enables the GIL. We found
no failure that only happens under free-threading, apart from one benign
race in a warn-once idiom, which we demonstrated with a stress test, and one
unattributed failure in 95 repeated runs of the thread-touching test files
(details in §5).

Evidence tags: **ran** = executed here, output read · **read** = seen in
source · **inferred** = reasoned from ran/read · **assumed** = not checked.

## 1. Setup

Interpreters (uv-managed, python-build-standalone):

| Venv | Interpreter | `sys._is_gil_enabled()` |
|---|---|---|
| `v314`  | CPython 3.14.8 (GIL)                 | True |
| `v314t` | CPython 3.14.8 free-threading build  | False |
| `v315t` | CPython 3.15.0rc3 free-threading build | False |

Install (ran), from the worktree root, into venvs outside the tree:

```bash
$PY -m venv $SCRATCH/v314t
$SCRATCH/v314t/bin/python -m pip install --upgrade pip
$SCRATCH/v314t/bin/python -m pip install -e ./tau-llm -e './tau-agent-core[dev]' \
    -e './tau-coding-agent[dev]' -e ./tau-jmfts -e ./tau-meta
```

Resolved versions (identical across the three venvs): pydantic 2.13.5,
pydantic-core 2.46.5, httpx 0.28.1, anyio 4.15.1, textual 8.2.8, rich 15.0.0,
anthropic 1.11.0, google-genai 2.28.0, jiter 0.17.0, websockets 16.1.1,
prompt_toolkit 3.0.53, pillow 12.3.0, nats-py 2.16.0, aiohttp 3.14.4,
mypy 2.1.0, ruff 0.16.10, pytest 8.4.2, pytest-asyncio 1.4.0.

## 2. Install results

**3.14t: rc=0, no source builds** (ran). Every compiled dependency resolved to
a `cp314-cp314t` wheel: aiohttp, ast_serialize, cffi, charset_normalizer,
cryptography, frozenlist, jiter, librt, markupsafe, msgpack, multidict, mypy,
pillow, propcache, pydantic_core, wcwidth, websockets, yarl. The only wheels
built were τ's five editable installs.

**3.15.0rc3t: rc=0, but one source build** (ran):
- `pydantic_core-2.46.5.tar.gz` was compiled from source (Rust/maturin). The
  wheel came out as `cp315-cp315t-linux_x86_64`. This **needs a Rust
  toolchain**, which this box has and a clean CI image would not.
- `frozenlist-1.8.0-py3-none-any` and `websockets-16.1.1-py3-none-any` fell
  back to their pure-Python builds (no cp315t wheel). They work, just without
  the C speedups.
- `cryptography` and `ast_serialize` installed as `cp315-abi3.abi3t` (the new
  free-threaded stable ABI).

**Optional extra `tokenizers` (`tau-llm[tokenizers]`)**, which CLAUDE.md's
setup line does not install (ran):
- 3.14t: no cp314t wheel, so `tokenizers-0.23.2.tar.gz` was built from source
  (Rust) and succeeded. Its deps `hf_xet` and `pyyaml` had cp314t wheels.
- 3.15t: `tokenizers`, `hf_xet` and `pyyaml` were all built from source and
  all succeeded.
- No test depends on it: the suite's counts were identical with and without
  it installed (ran).

**GIL re-enable check** (ran: `gilcheck.py` imports each module in a fresh
subprocess with `warnings.simplefilter("always")` and reads
`sys._is_gil_enabled()` afterwards). We checked 39 modules: pydantic,
pydantic_core, httpx, anyio, h11, jiter, anthropic, google.genai, rich,
textual, prompt_toolkit, websockets, nats, PIL.Image, msgpack, aiohttp, yarl,
multidict, frozenlist, propcache, markupsafe, charset_normalizer, cryptography,
cffi, wcwidth, tokenizers, pytest, pytest_asyncio, syrupy, cairosvg,
textual_dev, mypy, tau_llm, tau_agent_core, tau_coding_agent(.app/.cli/.serve),
tau_jmfts and tau_meta. **Every one leaves the GIL disabled and emits no "GIL
has been enabled" RuntimeWarning, on both 3.14t and 3.15t.**

`PYTHON_GIL=0` combined with `-W error::RuntimeWarning` also imports the whole
τ stack cleanly (ran). Forcing the GIL off changes nothing, because nothing
was turning it back on.

## 3. Test suite

Command (ran), from the worktree root with `HOME` pointed at an empty temp dir:

```bash
HOME=$SCRATCH/home-$TAG PATH=$SCRATCH/$V/bin:$PATH \
  $SCRATCH/$V/bin/python -m pytest -rf --tb=line -p no:cacheprovider
```

| Interpreter | Passed | Failed | Skipped | Deselected | Wall |
|---|---|---|---|---|---|
| 3.14.8 (GIL)        | 6446 | 0 | 157 | 6 (`-m llama`) | 587 s |
| 3.14.8 (GIL), rerun under load | 6446 | 0 | 157 | 6 | 501 s |
| **3.14.8t**         | **6446** | **0** | 157 | 6 | 700 s* |
| **3.15.0rc3t**      | **6446** | **0** | 157 | 6 | 630 s* |

\* These ran while other jobs were using the CPU, so the wall times don't say
anything about the interpreters (inferred).

Both runs show the same single warning, `coroutine 'EventBus.emit' was never
awaited` in `test_event_bus_is_reentrant_safe`. It also appears on the GIL
build, so it predates this work. The same 7 TUI snapshots pass on all three
interpreters. The suite contains no `skipif` keyed on the GIL or on threading
(read), so the free-threaded runs exercised the same tests the GIL run did.

**There are no 3.14t-only or 3.15t-only failures to categorize.**

**Repeated runs of the thread-touching test files**
(`test_hook_dispatcher_threading`, `test_submit_threadsafe`,
`test_session_picker`, `test_serve*`, 132 tests per run; ran, `repeat.sh` =
loop of `pytest -q` over those files):

| Interpreter | Runs | All-pass runs |
|---|---|---|
| 3.14.8 (GIL) | 35 (15 with a concurrent full suite) | 35 |
| 3.14.8t | 55 (10 with the 3.15t full suite running concurrently, 25 with a concurrent full suite) | 54 |
| 3.15.0rc3t | 20 | 20 |

One 3.14t run, in the first batch of 10, had `1 failed, 131 passed in 41.24s`,
about twice the normal ~20 s. The script only kept the summary line at that
point, so **we don't know which test failed.** It did not come back in the
next 45 runs on 3.14t, including 25 with a full suite competing for CPU. It
looks like a timing-sensitive test running long on an overloaded box, but
that is **inferred, not shown**. A free-threading race is not ruled out.

## 4. τ running for real (ran)

| Head | Interpreter | Provider | Result |
|---|---|---|---|
| `tau -p --mode json --tools read,ls,grep,find` | 3.14t, `PYTHON_GIL=0` | haiku-4.5 (live) | rc=0; 2 turns; parallel `find`+`grep`+`read`; correct one-line answer |
| `tau --mode rpc` (scripted client: `get_state`, `prompt`, wait for `agent_end`, `get_last_assistant_text`) | 3.14t | haiku-4.5 (live) | both responses ok; 3 tool executions; correct answer; rc=0 |
| `tau -p --mode json` | 3.15t | haiku-4.5 (live) | rc=0; same 3 tools; correct answer |
| TUI (`tau` in a real pty, typed prompt, Ctrl+J, Ctrl+Q) | 3.14t and 3.15t | fake OpenAI server | answer and 4 tool results rendered; no traceback; exit status 0 on both |

The three live runs used about 13k input and about 0.7k output tokens in
total (about $0.02). `local-llm` was down, and its sandboxed config held only
the `haiku-4.5` entry.

**Many sessions in one process** (`multisession.py`): 32 OS threads, each
running `asyncio.run` on its own `create_agent_session(...)`. Each session
sends 20 prompts, and every prompt makes a 4-way parallel tool batch
(find/grep/read/ls) against `fakeoai.py`, then a text turn. That is 640 prompts
and 2560 tool calls per run, each final answer checked exactly.

| Interpreter | Wrong answers | 32-thread wall | Speedup over serial |
|---|---|---|---|
| 3.14 (GIL) | 0 | 4.87 s | 1.78× |
| 3.14t | 0 | 1.70 s | 4.12× |
| 3.15.0rc3t | 0 | 1.53 s | 4.53× |

The fake server is a GIL-bound 3.11 `ThreadingHTTPServer` and probably caps
the free-threaded numbers (inferred). An early run failed with an httpx
`ReadError` until the server's listen backlog was raised from 5 to 512. That
was the fake server's fault, not τ's (ran: the same run is clean once the
backlog is raised).

Import cost: `import tau_coding_agent.app`, best of 5, takes 0.47 s on 3.14,
0.53 s on 3.14t and 0.53 s on 3.15t (ran). That's about 12% slower startup
on the free-threaded builds.

## 5. Thread-safety survey (read, with stress runs where noted)

τ is an asyncio program. OS threads appear only at the call sites in this
table:

| Site | What crosses threads | Verdict |
|---|---|---|
| `tools/{read,write,edit,find,grep}.py`, `asyncio.to_thread` | Pure functions over args; `find`/`grep` poll an `AbortSignal` | **Safe.** `AbortSignal` (`tau_llm/abort.py:42`) takes a `threading.Lock` on every read and write. Stress [2]: 16 pollers, all saw the abort, on all three interpreters (ran) |
| `agent_session.submit_threadsafe` (`agent_session.py:2083`) | `loop.call_soon_threadsafe` plus a `concurrent.futures.Future`; the task registry is touched only on the loop thread | **Safe by design**: the foreign thread hands work to the loop and owns no shared state. `test_submit_threadsafe` passes under free-threading |
| `serve/remote.py`, `request_blocking` | `threading.Event` handshake, then `run_coroutine_threadsafe` | **Safe**: same marshalling pattern |
| `serve/daemon.py:776,904`, `to_thread(catalog.resolve_ref/list)` | `FileSessionCatalog` reads files and keeps no in-memory state | **Safe** |
| `chat_widgets.py:454`, `session_picker.py:205`, `@work(thread=True)` | `catalog.list()` in a worker; results applied with `call_from_thread`, and only the UI thread writes widget state | **Safe**, and the docstrings already state the discipline |
| `tau_jmfts/store.py`, `_append_now`/`_finalize_now` in `to_thread` | Writers hold `_append_lock`. Readers (`append_at`'s `parent_id not in self._ids`, `entries()`) read `_ids`/`_entries` on the loop thread **without** the lock | **Needs a look, but the race isn't new.** Free-threaded builtins stay internally consistent (per-object locks), so this can't corrupt memory. The logical race between an unlocked reader and a locked writer existed under the GIL too (inferred) |
| `tau_llm/tokens.py`, `_CLASS_OF` and `_COUNTER_CACHE` | Lazily filled idempotent caches | **Benign**: the worst case is computing the same value twice or loading a tokenizer twice. Stress [1]: 16 threads × 20 passes × 64 texts gave 0 mismatches on all three; 31.2 s with the GIL vs 12.2 s on 3.14t (ran) |
| Warn-once sets (`providers/anthropic.py:93-94,262`, `google.py:89-90`, `openai.py:65-67`, `backends.py:925`) | `if k not in S: S.add(k); log.warning(...)`, a check-then-act | **A real race, and visible only under free-threading.** `warnonce.py`, 16 threads × 3000 trials: **0/3000 duplicate warnings with the GIL, 8/3000 on 3.14t** (ran). The cost is a duplicated log line, and only when several sessions share a process across threads. Not worth a lock |
| `providers/base.py:231-232`, `_API_FACTORIES`/`_PROVIDER_SPECS` | `register_*` does `if api in D and not replace: raise` and then `D[api] = f` | Same check-then-act shape. It matters only if extensions register providers from several threads at once. Today registration happens at import or load time on one thread (read) |
| `session_store._session_listeners` | Module-level list; `append`/`remove`; emitted over a `list()` copy | **Safe**: the copy is atomic under free-threading |
| `providers/anthropic.py:130`, `lru_cache` | C `lru_cache` | **Safe**: thread-safe on free-threaded builds (assumed from CPython docs; not stressed) |

Stress [4] (`stress.py`): 8 threads, each with its own loop, each running
30 find/grep/read tool calls. Output was identical to a single-thread
reference on all three interpreters. Speedup was 0.65× with the GIL, 3.34× on
3.14t and 2.49× on 3.15t (ran).

## 6. Fixes made

None. Nothing failed, so there was nothing small and clearly correct to fix.
The one race we demonstrated produces a duplicated log line. A lock there
would be ceremony, not a fix. The JMFTS store's unlocked readers are a design
question older than free-threading and belong in their own change.

**Side finding, not related to free-threading** (ran): every suite run
created an empty `$HOME/.tau/serve.log`. The real `~/.tau/serve.log` on this
box is also 0 bytes, which suggests a normal suite run touches the
developer's real `~/.tau` (inferred). `serve/cli.py:229` builds the path from
the import-time `TAU_DIR`. We didn't pin down which test does it.

## 7. Depth assessment, and what shipping on 3.14t would take

**Depth: dependency-level, and shallow.** Nothing in τ itself is a problem.
τ is pure Python and its wheels are `py3-none-any`, so there is nothing to
build for `t`. The ecosystem caught up between 3.13t and 3.14t:
pydantic-core, jiter, the aio-libs stack, cryptography, pillow and websockets
all ship `cp314t` wheels now, and none of them re-enables the GIL.

To ship on 3.14t:
1. **CI**: add `"3.14t"` to the matrix in `.github/workflows/publish.yml`
   (`actions/setup-python` accepts the `t` suffix; assumed, not tried here).
   Today the matrix is 3.11–3.14 only (read). This is the only change needed
   to *claim* support. We didn't make it on this branch because it changes
   the release gate.
2. **Packaging metadata (optional)**: a
   `Programming Language :: Python :: Free Threading :: 2 - Beta` classifier
   (or higher) on the five `pyproject.toml` files. None of them carries
   classifiers today (read).
3. **3.15t, later**: wait for `pydantic-core` cp315t wheels, or put a Rust
   toolchain in CI. The same applies to `[tokenizers]` on both `t` builds.
4. **Hardening, if the many-sessions-in-threads embedding becomes a
   supported use**: make the warn-once sets atomic (a `set.add`
   return-value pattern or a small lock), and put the JMFTS store's readers
   under `_append_lock`. Neither blocks anything today.

**What free-threading buys τ**: almost nothing for one interactive session,
since τ is I/O-bound asyncio on a single loop. It pays off when you embed
several sessions in one process on threads: 2.9× faster wall time in §4
(1.70 s vs 4.87 s), and threaded tool work like `find`/`grep` over big trees
(§5 stress [4]). The cost is about 12% slower import.

## Files

- `gilcheck.py`: imports each dependency in a fresh subprocess and reports the GIL state and warnings
- `stress.py`: shared-cache, AbortSignal, warn-once and per-thread-tool stress
- `warnonce.py`: the race demonstration (§5)
- `fakeoai.py`: offline OpenAI-compatible streaming server (parallel tool calls, then text)
- `multisession.py`: N sessions on N threads in one process
- `rpcclient.py`: scripted `tau --mode rpc` client
- `tui_pilot.py`: drives the real TUI in a pty
