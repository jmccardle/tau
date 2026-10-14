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
- **There's no `tau --home` flag**, and no `TAU_HOME` that names the home
  directly. The env var names a *base* to check, which is a deliberately
  smaller promise.
