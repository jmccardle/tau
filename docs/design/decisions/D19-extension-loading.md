# D19: Extension loading

Status: proposed (2026-10-31). Phase 7, "Extension loading" (`docs/plan.md`). Amends D13
(trust's hash scope; a new top-level key; a second user-file write), D16 §2 (command names),
and fix plan C3 (`examples/chat.py`'s loader becomes the library's). Points marked "(review,
2026-10-31)" were decided in review; they stay proposals until the owner accepts the ADR.

## Context

`tau` cannot load an extension today. The only loader is `examples/chat.py --extension FILE`
(C3), and `tau config check` / `set` know one extension, `StepLimit`, by a hard-coded tuple.
Phase 7 needs the loader every frontend shares (the TUI, `-p`, `tau serve`).

τ 0.15 (`docs/EXTENSION-PACKAGES.md`, `EXTENSION-NAMESPACE.md` at `v0.15.0`) loaded drop-ins
from `~/.tau/extensions`, packages from the `tau.extensions` entry-point group, and `-e
PATH|MODULE`. Users relied on: drop a file in and it runs; `pip install` a package and it
runs; `tau ext list` says what loaded and why not; `tau ext disable` silences a broken one;
`--no-extensions` gives a bare run; `/ext:<id>:<cmd>` reaches a command whose short name
another extension holds. Its authorization store mostly gated code the user had already
installed.

**Facts this decision relies on, checked on 2026-10-31:**

- C3's rule (`examples/chat.py:96-123`): a module with a `str` `name` is the extension;
  otherwise its `extensions` is a non-empty list or tuple of extension objects; anything else
  is an error naming the file and the rule. All seven speculative examples follow it.
- `check_extensions` refuses an empty or non-`str` name, two extensions with one name and a
  misspelt hook, but limits no characters. `assemble_commands` refuses two commands with one
  name across extensions and any named `config`. `Form` does not record its extension.
- The config's top-level keys are `models`, `default_model` and `extensions`, all settable by
  a `Config` node. `UserConfig.save_options` is the one write to the user's file.
- Trust pins the bytes of `.tau/config.json` and `.env`; a record that lacks a hash added
  later is not approved (`_trust.py:374`, the `env_sha256` precedent). `_trust.py` is 635
  lines, against the 800-line ratchet.
- `Driver(extensions=)` takes the list in hook order with `Tau()` first.
- **tau-search** (`~/Development/tau-search`, read only) registers
  `[project.entry-points."tau.extensions"] search = "tau_search.ext.search"`: a module that
  imports `tau_agent_core`, exposes 0.15's `register(api)`, and has no `name` or `extensions`.

## Decision

### 1. One rule for what a source exports

A **source** is one place extension code comes from: a file, a package directory, an
installed package's entry point, or one of τ's built-ins. Every source except a built-in is
read by C3's rule, unchanged: a module with a `str` `name` is the extension; otherwise its
`extensions` is a non-empty list of extension objects, added in list order; anything else is
`ExtensionLoadError` naming the source and the rule.

A `.py` file is imported as a module; a package directory (with `__init__.py`) as a package, so
relative imports work. An entry point must name a module, as tau-search's does; `module:attr`
is refused, so there is one way to write an extension. Files and directories are imported under
a private module name derived from their resolved path (two files with one stem are two
modules); an entry point's module under its real name, so a package keeps one copy of its
state. A built-in is a class in `tau`; each load makes fresh instances.

**Names.** `check_extensions` also refuses a name that is not letters, digits, `_` and `-`: a
`:` would break `/ext:cmd` (§7), a `.` would break `tau config set EXT.FIELD=V`, whitespace
would break a command line. A source's **key** obeys the same rule.

### 2. Sources, keys and load order

| order | source | key | on unless |
|---|---|---|---|
| 1 | built-in (`step_limit` now; Phase 8's standard extensions) | the extension's name | its declared default is off, or `load` says off |
| 2 | installed package: entry point in group `tau.extensions` | the entry point's name (`search`) | `load` says off, or `--no-extensions` |
| 3 | user drop-in: `$TAU_HOME/extensions/NAME.py` or `NAME/` | `NAME` | `load` says off, or `--no-extensions` |
| 4 | project drop-in: `<project>/.tau/extensions/NAME.py` or `NAME/` | `NAME` | the project is not trusted (§4); `load` says off; `--no-extensions` |
| 5 | `-e PATH` or `-e KEY`, in argument order | the file's stem | never: naming it is the consent (§5) |

- The order is hook order: the driver's `tau` first, then this table top to bottom. Within a
  row, sources are sorted by key; extensions inside one source keep their list order. Nearer
  sources come later, as config layers do (D13). Drop-in names starting with `.` or `_` are
  ignored, as is anything that is neither a `.py` file nor a package directory.
- **`session_name` is not a source** (review, 2026-10-31). It is D18's built-in that provides
  `/name`. The startup adds it after `load_extensions` returns, so it has no key, `load` does
  not govern it, `--no-extensions` does not remove it, and `tau ext list` shows it as `always`.
  The driver's checks still apply: a loaded extension named `session_name` is a name clash.
- **One key, two rows: the nearer wins.** The project's `plan_mode.py` **shadows** the user's,
  and `-e ./search.py` shadows the `search` package. A shadowed source is never imported, and
  `tau ext list` shows what shadowed it. This is how you override a built-in or a package, and
  why `-e` on an installed package's own file loads it once. (0.15's RECORD and
  editable-install heuristics for that case are dropped.)
- **One key, two sources in the same row** (two packages both registering `search`, or both
  `ralph.py` and `ralph/`) is an `ExtensionLoadError` naming both; any order would be arbitrary.
- **Two extensions with one name from different keys**, and **two tools with one name**, stay
  refusals, now naming both sources. Tools are not namespaced: a tool's name is what the model
  calls and the log records.
- `$TAU_HOME/extensions/`, or a source in it, writable by group or others is refused with the
  `chmod` to run (D13's existing check, reused).

### 3. On and off: the `load` key

A new top-level config key, an object from source key to `true` or `false`:

```json
{ "load": { "search": false, "compaction": true },
  "extensions": { "search": { "limit": 3 } } }
```

- It merges like every object (D13): by key, nearest layer wins, `null` drops an inherited
  key. An absent key means the source's default from §2.
- **It lives in the config files only; a `Config` node cannot set it**, because loading
  happens once at start, not per branch. It is not inside `extensions.<name>`, which is an
  extension's options, keyed by name, and settable by a branch.
- `"KEY": true` where no source has that key stops the start with an `ExtensionLoadError`;
  `"KEY": false` there is allowed, and `tau config check` warns, so a typo shows.
- **`tau ext disable KEY`** and **`tau ext enable KEY`** write `load.KEY` into the user's file
  through a second `UserConfig` write, `save_load(key, on)`, with `save_options`'s
  read/check/atomic-write path. A key that no source has writes nothing. If the project's layer
  sets the key the other way, the command still writes and says the project's value wins; the
  project's file is never written (D13). `-e` and `--no-extensions` are per-run flags.

### 4. Project drop-ins under trust

The hash scope that Phase 4 carried forward is decided here. **Approval pins `.tau/extensions/`
and `.tau/SYSTEM.md` along with `config.json` and `.env`** (review, 2026-10-31; `SYSTEM.md` is
D20's file, if the owner accepts D20 Open 2).

- The pinned extension files are every regular file under `.tau/extensions/` except
  `__pycache__/`, as (relative path, sha256). The digest of that list is `extensions_sha256`
  in `trusted.json`, with the per-file map beside it for diffs. `SYSTEM.md` gets
  `system_sha256`, like `env_sha256`. A missing directory or file has its own marker, so adding
  the first file untrusts the project, as adding a `.env` does.
- Symlinks, FIFOs and oversize files are refused with the reader `_trust` uses, `SYSTEM.md`
  included: it is never read through a symlink.
- The trust report lists the files with sizes, and "Changes since approval" says which were
  added, removed or changed; it prints no code. A record without `extensions_sha256` or
  `system_sha256` is not approved (the `env_sha256` precedent): the project is asked once more.
- **What is used is what was approved.** The loader serves each module's source from the bytes
  it hashed, through a finder for the source's private module name, and never opens the files
  again. A file changed between the check and the import can't slip in, relative imports still
  work, and nothing writes `__pycache__` into the project. D20 reads `SYSTEM.md` the same way,
  from the approved bytes. Data files an extension reads at run time are pinned at start, not
  re-checked on every read.
- This code goes in its own module (`tau._project_code`); `_trust.py` is near the 800-line
  limit. User drop-ins and packages are not hashed: `TAU_HOME` and the Python environment are
  already the user's own authority (D13).

### 5. `-e` and `--no-extensions`

- **`-e VALUE`** (repeatable) is a path if it contains `/` or ends in `.py`; otherwise the key
  of a discovered source (`tau --no-extensions -e search` runs tau-search alone). A relative
  path is made absolute before the start changes directory (D18 §5, step 0). `-e x.pyc` is
  refused.
- An `-e` source loads even if `load` turns it off or `--no-extensions` is given. A path needs
  no trust, even inside a project: naming it is the consent (C3).
- **`--no-extensions`** turns off rows 2-4; built-ins still follow `load`, `-e` still loads.

### 6. When a source fails: τ does not start

The loader imports every source that is on and runs the driver's definition checks on each
(`check_extensions`, tool and command description, the options type), so every failure can be
pinned to its source; then the checks across sources (names, tools, `load` keys). It reports
**every** failure, and `tau` exits 1 before it opens or creates a session (D18). Nothing is
left on disk. For example:

```
tau: extension "search" failed to load; tau did not start.
  source: package ffwf-tau-search 0.4.0, entry point search = tau_search.ext.search
  ModuleNotFoundError: No module named 'tau_agent_core'
  to start without it: tau ext disable search   (or: tau --no-extensions)
```

The traceback shows only lines in the source's own files. The last line depends on the source
(`-e`: "fix it or drop the flag"; project: disable it or remove the file; a same-row collision:
"uninstall or remove one of them"). In the library this is `load_extensions(...)` raising
`ExtensionLoadError` (an `ImportError`), with the underlying exception as `__cause__`.

This reverses 0.15's #58 (a discovered failure was a notice, and the run went on): a
permissions or bash-guard extension that fails to import would let the session run unguarded.

### 7. Command namespacing: `/ext:cmd`

Every command has a **qualified name**, `/<extension>:<command>`, that nothing can take from
it. The bare `/<command>` works while exactly one extension has that command.

- `assemble_commands` allows two extensions to share a command name (one name twice in one
  extension, or any named `config`, is still refused). `Form` gains **`extension: str |
  None`**: the owner of a command form, `None` for an options form.
- `Client.run(cursor, command, args)` accepts `"a:note"` or `"note"`. A bare name two
  extensions share raises `LookupError`: `"/note is two commands: /a:note, /b:note"`;
  `commandline.parse` raises it before any round trip. An unknown `/x:y` is `UnknownCommand`,
  and the frontend sends it as text (D16).
- Completion after `/` offers the bare name of each unique command and the qualified names of
  shared ones; after `/<extension>:`, that extension's commands.
- **No first-wins and no pins**: load order never decides what `/note` does. `tau` gets no
  special treatment: an extension adding `/model` makes the bare `/model` ambiguous, and
  `/tau:model` still works. `/config` stays reserved. 0.15's literal `ext:` prefix is dropped:
  `/search:fetch`, not `/ext:search:fetch`.

### 8. `tau ext list`

One row per source, in load order, then indented lines with the reason or the contents:

```
tau          always    built-in                            /model, /config
session_name always    built-in (added by the startup)     /name
search       failed    package ffwf-tau-search 0.4.0 (tau_search.ext.search)
             ModuleNotFoundError: No module named 'tau_agent_core' (search.py:15)
ralph        off       ~/.tau/extensions/ralph.py
             load.ralph = false in ~/.tau/config.json
plan_mode    shadowed  ~/.tau/extensions/plan_mode.py, by the project's own
plan_mode    on        project /src/app/.tau/extensions/plan_mode.py
             commands /plan, /plan_mode:add (shared with forest); tools: none
```

- **States:** `always` (`tau`, `session_name`); `on`; `off`, with its reason (the key that set
  it, `--no-extensions`, or "built-in, off by default"); `shadowed`, by what; `failed`, with
  §6's message; `untrusted` (nothing imported; the line names `tau trust`). It imports exactly
  what a start would, never prompts for trust, and exits 1 if any source failed.
- **Flags:** `-e VALUE`; `--no-extensions` lists that flag's result; **`--json` prints one JSON
  object per line**, one per source (review, 2026-10-31).
- The rows come from one library function, `list_extensions(...)`, returning `ExtensionSource`
  records, which a TUI panel renders too. It is **not served over the wire in Phase 7**
  (review, 2026-10-31).
- `tau config check` and `set` use the loader too (a failed source is a check error with §6's
  message); the hard-coded `_EXTENSIONS` goes away.

### 9. Startup order; `reload`

The whole start is D18 §5's shared start. It resolves the session flags first, then changes the
process's working directory to the session's (`chdir`, review, 2026-10-31), so the steps below
run in it: `.tau/extensions/`, the project search and relative paths inside extensions all
agree. It assembles the prompt (D20) for a new session after step 3. The loader's part:

1. `load_config` (trust, D13).
2. `load_extensions(loaded, explicit=..., discover=...)`; the startup then adds `session_name`.
3. `select_model`.
4. Open or create the session (D18).
5. `Driver(extensions=...)`.

Steps 1-2 run before anything is written. The loader does not use `driver.reload` (there is no
driver yet); an in-session toggle would compute a new list with `load_extensions` and call it.
None is built in Phase 7: `tau ext enable`/`disable` while a server runs changes the next
start only.

### 10. tau-search

It does not load unchanged (it imports `tau_agent_core` and has no `name`), so with §6 an
installed copy stops `tau` until ported or disabled; the port is after 0.16.0 (plan).

## Files and formats

`config.json` gains the top-level `load`; `trusted.json` gains `extensions_sha256`,
`extensions` (path to sha256) and `system_sha256` per project. Neither has shipped, so neither
has a version (D12/D13/D17); old records are re-asked. Session files are unchanged.

## Alternatives

| option | why not |
|---|---|
| Keep 0.15's authorization store (authorize/revoke, version pins) | It gates code the user already installed with their own authority; the project is covered by trust. Subtract. |
| On/off keyed by extension `name`, or `extensions.<name>.enabled`, or a list | A name is known only after import, so a crashing source could never be turned off; `extensions.<name>` is per branch, loading is not; lists replace across layers (D13). |
| Refuse two sources with one key | You could neither override a package with your own copy nor `-e` its file. |
| First-wins command names with pins (0.15); namespacing tools too | Load order would decide what `/note` does. A tool's name is model-facing and recorded, so a rename changes prompts and replay. |
| A failed discovered source is a notice (0.15 #58) | A guard that fails to import runs the session unguarded (Open 2). |
| `-e MODULE`; `module:attr`; re-reading project files at import; `--no-extensions` turning off built-ins | A fourth form; a second rule beside C3's; the race D13 closed for `config.json`; built-ins are τ's behaviour, switched in `load`. |

## Open

1. **Discovered sources are on by default.** *Recommend yes*: installing or placing them is
   the consent, and `tau ext disable` is the off switch.
2. **A failure stops the start**, even for a discovered source (§6, reversing 0.15 #58).
   *Recommend yes*; the message gives the one command that gets past it.
3. **Nearest-wins for one key across rows** (§2). *Recommend yes*, shown as `shadowed`.
4. **Phase 7 exit, "tau-search loads through the entry point".** *Recommend* a fixture package
   in `tests/` with tau-search's exact entry-point shape, installed into the test venv, as the
   gate; the real port is the plan's, after 0.16.0.
5. **Several drivers in one process share extension instances.** *Recommend:* one driver per
   process for 0.16 (D21 §1); if a process ever hosts several, `extensions` may be a function
   returning fresh instances.

**Needed from siblings:** D18: a session is opened only after `load_extensions` returns, and
the startup adds `session_name`. D21: `tau serve` loads at server start with the same flags,
and `Form.extension` is in the schema. D20: `.tau/SYSTEM.md` is a pinned member (§4). Phase 8
decides each standard extension's default, and whether the base tools become a built-in.

**Not verified, inferred:** that `importlib.metadata.entry_points` returns two distributions'
entry points of one name as two (§2); that a source finder keeps relative imports sensible.

## Glossary additions and changes

- New: **source**, **key**, **built-in**, **drop-in**, **shadowed**, **`load`**,
  **`load_extensions`**, **`list_extensions`**, **`ExtensionSource`**, **`ExtensionLoadError`**,
  **qualified command name**, **`tau ext`**, **`-e`**, **`--no-extensions`**,
  **`extensions_sha256`**, **`system_sha256`**.
- Changed: **extension**, **command** (names unique per extension), **Form** (`extension`),
  **trusted directory** (`.tau/extensions/`, `.tau/SYSTEM.md`), **UserConfig** (`save_load`),
  **tau config check / set**, **commandline**, **session_name** (added by the startup, `always`).

## Build (rough lines, tests excluded)

| item | size |
|---|---|
| `tau._extensions` (sources, order, shadowing, C3 rule, checks, records, messages) | ~260 |
| `tau._project_code` (hash `.tau/extensions/` and `SYSTEM.md`, report and diff, the finder); `_trust` records the hashes | ~130 (+~25 in `_trust.py`) |
| `load` in the config layers; `UserConfig.save_load`; CLI (`tau ext list/enable/disable`, `-e`, `--no-extensions`, `config check/set` through the loader) | ~230 |
| Namespacing (`assemble_commands`, `Commands.run`, `Form.extension`, `commandline`); name rule; `chat.py` switched | ~75, −30 |
| Tests: each source and the C3 rule; order and shadowing; `load` merge; trust re-ask on a changed or added file, `SYSTEM.md` included, and import from the approved bytes; every §6 message; `ext list` rows and `--json`; `/a:note`, ambiguous `/note`, completion; the entry-point fixture package | ~450 |
