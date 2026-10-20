# A portable `.tau/` beside the install

Built 2026-10-06. When a bundle's launcher names its install directory and that
directory has a `.tau/` in it, that `.tau/` is τ's entire home: config, sessions,
extensions, themes, settings, the context file, the serve log and exports.

## 1. What was wrong

τ had five independent ways of finding its home, and each of them computed
`~/.tau` itself:
- `tau_coding_agent.config.TAU_DIR`, which is `Path.home() / ".tau"` at import
  time. Sessions, themes, exports, the REPL history and the serve log all hang
  off it.
- `tau_agent_core.settings` in two places: the `extension_dirs` default and the
  global `settings.json`.
- `tau_agent_core.sdk` in two places: `_GLOBAL_EXTENSIONS_DIR` and `_AGENT_DIR`,
  both the literal string `"~/.tau…"`.

That gives a bundle on a flash drive nowhere to put its state except the host's
`$HOME`. A rule applied in only one of those five places would have left the
other four writing to the host. That is the "fix that reached one head" shape
(feature-doc), here reaching one module.

## 2. The rule (`tau_agent_core.tau_home.resolve_tau_home`)

- `$TAU_PORTABLE_BASE` unset or empty: the home is `~/.tau`.
- Set, and `<base>/.tau/` is a directory: the home is that directory, resolved.
- Set, and there is no `.tau/` there: the home is `~/.tau`. This is not a
  fallback. It is how a .deb, or an AppImage with nothing beside it, behaves:
  the launcher always names its base, and the directory's presence is the switch.
- Set to something that is not a directory: `TauHomeError`, because the launcher
  that set it is broken.

All five sites now call this one function, so every head (the TUI, `-p`, rpc,
repl, serve) gets the same answer.

## 3. Two deviations from the literal spec, and why

The spec read "check `{dirname(sys.argv[0])}/.tau/config.json`".

**The base comes from an environment variable, not `sys.argv[0]`.** Under
`python -I -m tau_coding_agent.cli`, `sys.argv[0]` is the module's file inside
`site-packages`. Inside an AppImage it is a path in the read-only squashfs
mount, while the directory a user can write to is `dirname($APPIMAGE)`. Only the
launcher knows the install directory, so it passes it explicitly:
- `bin/tau` sets `TAU_PORTABLE_BASE="${TAU_PORTABLE_BASE:-<tree root>}"`.
- `tau.cmd` does the same with `if not defined`.
- The AppImage's `AppRun` sets it to `dirname($APPIMAGE)` before calling
  `bin/tau`.

The base is the tree root (`tau/`), not `bin/`, so the drive reads
`tau/{bin,python,.tau}`.

**The switch is the directory, not `config.json`.** The portable archive ships an
empty `.tau/`, so it is portable before any config exists. If the check were
for `config.json`, the first run would look in `~/.tau`, find nothing, and tell
the user to create a config there, on the host.

## 4. Where each artifact lands

| Artifact | `.tau/` shipped | Home |
|---|---|---|
| tar.gz / zip from `build_runtime.py --portable-home` | empty | `tau/.tau/` |
| AppImage | none in the image | `.tau/` next to the `.AppImage` file if present, else `~/.tau` |
| .deb (`/opt/tau`) | none | `~/.tau` |
| VSIX (tau-code) | must not ship one | `~/.tau`; an extension directory is replaced on every update |

## 5. What is absent

- **`tau --mode rpc`'s default session base is still `<tmp>/.tau-<uid>/sessions`**
  (D-6, `session_store.rpc_default_base`), which is on the host. Portable mode
  doesn't move it, because that would turn D-6's "ephemeral by design" into
  durable state on the drive without anyone choosing that. A host that wants
  portable rpc sessions passes `--session-dir`. This is the one known way
  portable τ still writes to the host.
- **Session directories are keyed by the host's absolute cwd**
  (`sessions/--media-USBSTICK-proj--/`). Plugging the drive into another machine
  under a different mount path makes `--continue` look in a different key.
  Nothing here normalizes it.
- **Child processes inherit `$TAU_PORTABLE_BASE`.** A `tau` the agent starts
  through its own bash tool shares the portable home. That is consistent, but
  worth knowing.
- **There's no `tau --home` flag.** `TAU_PORTABLE_BASE` names a *base* to
  check, which is a deliberately smaller promise. §6 adds `TAU_HOME`, which
  names the home itself.

## 6. `TAU_HOME`: naming the home outright (2026-10-19, #48)

**What was wrong.** People and parent processes that wanted a throwaway home
set `TAU_PORTABLE_BASE`, because it was the only variable there was. A typo
or a missing `mkdir` then meant the real `~/.tau`, with no message, as §2
says it should. Config, sessions and `extension-authorizations.json` landed
in the real home. Making `TAU_PORTABLE_BASE` strict is not an option: the
launcher sets it on every run, with or without a `.tau` beside it.

**The rule.** `resolve_tau_home` now checks `$TAU_HOME` first:
- Set and non-empty: the home is that directory (`~` expanded, resolved). It
  wins over `TAU_PORTABLE_BASE` and `~/.tau`.
- Set to something that is not an existing directory: `TauHomeError`,
  `TAU_HOME=<x> is not a directory; create it or unset the variable`. Setting
  the variable is an unambiguous request, so a missing directory is refused,
  not replaced. τ never creates it.
- `TAU_PORTABLE_BASE` keeps exactly the meaning §2 gives it.

**Every head says it in one line.** `tau_coding_agent.cli` prints
`tau: error: <message>` and exits 2 (§8 has how, since 2026-10-19). That covers the TUI,
`-p`, rpc, repl, `serve` and `ext`, for both `tau` and
`python -m tau_coding_agent.cli`. A bad `TAU_PORTABLE_BASE` takes the same
path; it used to print a traceback and exit 1.

**What it does not replace.** τ reads two layers, and `TAU_HOME` moves only
the first:
- *The τ home, the user layer:* `config.json`, `settings.json`, sessions,
  `extensions/`, `skills/`, `prompts/`, themes, logs, the global context
  file, `extension-authorizations.json`.
- *The project layer, `<cwd>/.tau/`:* `.tau/SYSTEM.md`, project `prompts/`
  and `skills/`. It still layers on top of whichever home is in force.

`--mode rpc`'s `<tmp>/.tau-<uid>/sessions` default (§5) is not the home and
does not move either.

**For testers and embedders.** `mkdir` a directory, write its `config.json`,
and set `TAU_HOME` to it. Don't use `TAU_PORTABLE_BASE` for this. The test
suite never sees either variable (§7).

## 7. The test suite can't see a tester's home (2026-10-19, #54)

**What was wrong.** Subprocess tests wrote a config under a fake `$HOME` and
passed the rest of the parent's environment through. When the tester's shell
had `TAU_PORTABLE_BASE` set, which the 0.14.0 test recipe asked for, the child
resolved the portable home over the fake one and 37 tests failed. `TAU_HOME`
would have done the same. In-process tests were exposed too: `config.TAU_DIR`
resolves when the module is first imported.

**Two guards.**
- The repo-root `conftest.py` removes every name in
  `tau_home.HOME_ENV_VARS` from `os.environ` when pytest imports it. That is
  before any package's `tests/conftest.py` or test module, so before
  `TAU_DIR` resolves. A session-scoped fixture would run after collection,
  which is too late. Child processes inherit the cleaned environment.
- `tau_agent_core.testing.child_env(home, **overrides)` builds a subprocess
  environment: the parent's, minus `HOME_ENV_VARS`, with `HOME` set. Every
  test that starts a τ child uses it, so a child is isolated even when the
  helper's caller runs outside this suite (a fork's own tests, Tectum's).

`HOME_ENV_VARS` lives beside `resolve_tau_home`. A third home variable is
added there, in one place, and both guards pick it up.

## 8. The home resolves at use, never at import (2026-10-19)

**What was wrong.** `tau_coding_agent.config.TAU_DIR` and `CONFIG_PATH` were
computed when the module was imported, and §6 made `cli.py` turn a
`TauHomeError` there into `SystemExit(2)`. So with a bad `TAU_HOME`,
`tau --help` (the text that documents `TAU_HOME`) and `tau --version` exited 2,
and any program importing `tau_coding_agent.cli` to reach `build_parser` was
killed by the import.

**Now.** Neither constant exists. Every reader calls `resolve_tau_home()`
when it needs the home, and nothing caches the answer, so a long-lived
process (the serve daemon, an embedding program) sees a change to `TAU_HOME`
or `HOME` at its next read: `config.default_config_path()`, the file session
store's default base, the serve log, the REPL history, themes and exports.
`cli.main` resolves once right after argument parsing, inside the handler
that prints `tau: error:`. `tau ext` and `tau serve` run through the same
handler, and they parse before they touch the home. So `--help` and
`--version` work whatever the home is, and a real command still refuses a
bad one in one line with exit 2.

**For tests and embedders.** There is nothing left to monkeypatch. Set
`TAU_HOME` to an existing directory; `testing.sandbox.sandbox_tau_home` does
exactly that.

## 9. A relative `TAU_HOME` is pinned for children (2026-10-19)

**What was wrong.** `TAU_HOME=scratch tau` resolved `scratch` against the cwd,
but left the variable relative. A child τ started from another directory (the
bash tool, a delegate, `examples/20_delegate.py`) resolved a different home,
or exited 2 because there was none.

**Now.** `cli.main` calls `tau_home.pin_home_variables()` first. It rewrites a
relative `TAU_HOME` or `TAU_PORTABLE_BASE` in `os.environ` as an absolute path,
so every child inherits the home the head chose. It checks nothing and can't
fail; a bad value is still refused at resolution, after `--help` has had its
chance.

**Why rewrite rather than refuse.** A relative path typed in a shell has one
obvious meaning, the directory it was typed in. Refusing it would make the
user restate something τ can work out, which is an error without a decision
behind it.

**Not covered.** A program that embeds `tau_agent_core` without going through
`cli.main` gets no pinning: `resolve_tau_home` resolves a relative value
against the cwd of each call. Such a program should call
`pin_home_variables()` itself before it starts children or changes directory.

## 10. Messages name the home in force (2026-10-19, #62)

Errors and notices used to say `~/.tau/config.json` (or `~/.tau/sessions`)
whatever the home was, so under `TAU_HOME` or a portable home they sent the
user to the wrong file. Now:
- **A runtime message in a head or in core** names the resolved file:
  `config.default_config_path()` in `tau_coding_agent`,
  `resolve_tau_home() / "config.json"` in `tau_agent_core`. Example:
  `/srv/scratch/config.json "session_store" must be a JSON object`.
- **Static text** (`--help`, RPC and serve capability descriptions, which are
  built once) says `<τ home>/config.json`, and `--help`'s environment block
  says what `<τ home>` is.
- **`tau_llm` messages** say `τ's config.json`. `tau_llm` sits below
  `tau_agent_core` and must not import it, so it cannot know the path.

The ext_kit state and ledger directories, `examples/20_delegate.py`'s pricing
config, and `scripts/reasoning_wire_probe.py` resolve the home the same way.
`scripts/gemini_capability_probe.py` keeps `~/.tau/gemini-key`, because it
promises to run without τ installed. The two `experiments/` runs that read
`~/.tau/config.json` are records of runs already made, so they are left alone.
