Experiment record (2026-10-05), with a follow-up on 2026-10-06 (§11). τ 0.12.0 as a
self-contained OS application: a relocatable python-build-standalone (PBS) tree, wrapped per
platform. Built and measured on Linux x86_64. Windows, macOS and linux-arm64 trees are
cross-built and checked structurally.

§§1–10 are the first pass, kept as measured. §11 is what John's answers changed:
- **free-threaded by default**, with a gate that fails the build if the GIL comes back on;
- **one runtime builder in this repo**, `scripts/build_runtime.py`, which tau-code is to consume;
- **three variants**;
- **the portable `.tau/` home**.

Where §§1–10 say "recommended" and §11 disagrees, §11 is current. `build_portable.sh` is gone;
`scripts/build_runtime.py` replaces it, and git history keeps the old script.

Evidence tags: **ran** = executed here, numbers are from that run; **read** = from a file, an API
response or a release listing; **inferred** = reasoning or prior knowledge, not checked here;
**assumed** = taken as given.

## 1. The question

John's ask: ship τ as an OS binary (deb, rpm, Windows installer, dmg, AppImage, portable zip/tar)
carrying its own Python, so τ can (a) stop depending on the user's interpreter, (b) choose its
interpreter — 3.14t now, 3.15 at release — (c) take on C++/Rust code, and (d) possibly bring a
pinned terminal and act as a standalone application.

**Short answer (ran):** yes. The PBS tree that tau-code already ships inside a VSIX can be an OS
application too. Here it was built on 3.14.8, 3.14.8t, 3.15.0rc3 and 3.15.0rc3t. It runs from any
directory with a clean environment, and on six stock distro images without Python down to glibc 2.28.
Its startup matches the dev venv. Wrapping it as an AppImage or a .deb takes about 40 lines of shell.

## 2. The patterns John already uses (read; a subagent surveyed the three repos)

| | McRogueFace | gobboclippy | tau-code runtime VSIX |
|---|---|---|---|
| Interpreter source | CPython 3.14.2 built from a git submodule; python.org embeddable package on Windows; emscripten build for WASM | Build host's `libpython` (find_package Development.Embed); python.org embeddable package on Windows | **PBS `install_only_stripped`, CPython 3.11.16, release 20260901** |
| How it finds Python | `PyConfig_InitIsolatedConfig`, `home = <exe>/lib/Python`, `$ORIGIN/lib` rpath plus `LD_LIBRARY_PATH` in `run.sh` | Isolated PyConfig; `SDL_GetBasePath()`; stdlib in `lib/python3XX.zip`; the binary re-execs itself as `python3` (argv[0] switch) | `bin/tau` script: `python/bin/python3.11 -I -m tau_coding_agent.cli` |
| How deps are installed | n/a (engine) | ensurepip wheel shipped | `pip install --target … --platform <tags> --only-binary=:all:` run on the build host, cross-target |
| Trimming | stdlib minus test/tkinter/idlelib, `stdlib_modules.yaml` light/full | `StripTree.cmake`, zip stdlib | drops tests, idlelib, tkinter, Tcl/Tk, `include/`, `.a`, `ensurepip/_bundled`, unused `libpython.so`; one interpreter in `bin/` (vsce copies symlinks) |
| Formats | tar.gz, zip, WASM zip | tar.gz, zip, **dmg** (`hdiutil`, ad-hoc `codesign`) | 9 per-target VSIXes (linux x64/arm64/armhf, alpine x64/arm64, darwin x64/arm64, win32 x64/arm64) |
| CI | Gitea: build and test only | GitHub matrix on ubuntu-22.04 (glibc floor), windows, macos-arm64; Forgejo debian:12 plus a mingw cross build smoke-tested under wine; `SHA256SUMS` and a GitHub release on `v*` | GitHub `release.yml`: every target built on ubuntu-latest (cross-download, nothing compiled); only linux-x64 is run-tested |
| Signing | none | macOS ad-hoc only; notarization written up but not done | none |
| Sizes | ~16 MB Linux tar.gz | 8–35 MB | runtime VSIX 36.0 MB (106 MB unpacked) |

What the three have in common (read):
- Everything resolves relative to the executable.
- Startup is isolated (`-I` or an isolated PyConfig).
- Stdlib trimming is the same in all three.
- Each runs the **packaged copy** in CI with the environment cleared.
- None builds a system installer (deb, rpm, msi, nsis, AppImage).
- None ships free-threaded Python. McRogueFace has a `3.14t` CMake option, used for testing only.
- No real signing anywhere.

The tau-code runtime is the direct ancestor of this experiment. The new parts are: uv instead of
`pip --target`, a newer and free-threaded interpreter, a pinned glibc floor, full bytecode, and OS
wrappers.

## 3. Proof of concept (Linux x86_64)

### 3.1 Scripts (all in this directory, no host paths; inputs come from env/args)

| Script | Does |
|---|---|
| `build_portable.sh <out> [gil\|freethreaded]` | `uv build` the five wheels from the tree → fetch the PBS `install_only_stripped` tarball → `uv pip install --python <bundled> --python-platform x86_64-manylinux_2_28 --only-binary :all: ffwf-tau==<ver>` → delete non-relocatable console scripts → trim → `compileall --invalidation-mode unchecked-hash` → write `bin/tau` → `tau-<ver>-linux-x86_64[-ft].tar.gz` |
| `smoke_portable.sh <tarball>` | Unpacks under a path containing a space, runs with `env -i PATH=/usr/bin:/bin HOME=<empty>`: `--version`, `--help`, interpreter identity, every heavy import (and whether the GIL came back), and `tau -p` against an unreachable model |
| `smoke_tui.sh <dir>` | Starts the TUI in a pty (`script`) for 8 s, clean env; counts the bytes rendered, the DECRQM 2026 query and the kitty-keyboard push |
| `distro_matrix.sh <dir> [images]` | Bind-mounts the bundle read-only into stock containers and runs `tau --version` plus heavy imports |
| `build_linux_packages.sh <tarball> <out>` | AppImage (appimagetool) and .deb (`dpkg-deb --build` of a staged `/opt/tau` + `/usr/bin/tau` symlink + .desktop + icon) |
| `stub_openai.py <port>` | A streaming OpenAI-compatible endpoint that answers "pong", used for the end-to-end `tau -p` |
| `time_start.sh <N> cmd…` | Median wall time of N runs after one warm-up |
| `try_pyinstaller.sh` | Freezes the same wheels with PyInstaller (onedir and onefile) for comparison |

The launcher is the whole relocation story:

```sh
#!/bin/sh
here="$(dirname "$(readlink -f "$0")")"
exec "$here/../python/bin/python3" -I -m tau_coding_agent.cli "$@"
```

Typical invocation: `UV=/path/to/uv experiments/distribution/build_portable.sh "$OUT" freethreaded`.

### 3.2 Runs (ran)

**Relocated with a clean env, both flavors.** Unpacked under `<tmp>/some where/else/`, with
`env -i PATH=/usr/bin:/bin HOME=<empty>`:
- `tau 0.12.0`; `--help` OK.
- `sys.version` reports 3.14.8, with `sys.prefix` inside the bundle.
- The host's `/usr/bin/python3` (3.11) is on PATH and goes unused.

**Free-threaded holds (3.14.8t and 3.15.0rc3t).** Importing `tau_coding_agent.app`, `pydantic_core`,
`jiter`, `PIL`, `cryptography`'s Rust binding, `textual`, `anthropic` and `google.genai` with
`-W error::RuntimeWarning` reports `sys._is_gil_enabled() == False`. Every compiled dependency
resolved to a `cp314t` (or `cp315t`/`abi3t`) wheel. No source builds happened.

**End-to-end `tau -p`** against `stub_openai.py`: prints `pong`, exit 0, on GIL, FT and the venv.

**Fail-early path:** `tau -p` against `127.0.0.1:9` exits 1 with `RuntimeError: Streaming error from
model 'stub' … ConnectError`. It also prints the full traceback, which is the same behaviour as the
venv and is not specific to the bundle. **Bit sucky** for an end user of a binary: we should show the
one line without the stack.

**TUI under a pty:** keeps running until killed (exit 124 from `timeout`) and renders about 36.9 KB
including `τ`. It sends one `ESC[?2026$p` (the synchronized-output query) and one `ESC[>…u` (kitty
keyboard push). This held on GIL, FT, 3.15t and the PyInstaller build.

**Stock distro containers** (bundle bind-mounted read-only, the images have no python3):

| image | glibc | result |
|---|---|---|
| debian:10 | 2.28 | `tau 0.12.0`, heavy imports OK (GIL and FT) |
| rockylinux:8 | 2.28 | OK (GIL and FT) |
| ubuntu:20.04 | 2.31 | OK |
| fedora:42 | 2.41 | OK |
| archlinux:latest | 2.44 | OK (GIL and FT) |
| ubuntu:18.04 | 2.27 | **fails as designed**: `GLIBC_2.28 not found (required by …cryptography…/_rust.abi3.so)` |

**`.deb` installed in debian:10:** `dpkg -i` puts `/usr/bin/tau` → `/opt/tau/bin/tau`, and
`tau --version` works with no `python3` on the system.

**AppImage:** runs via FUSE and with `APPIMAGE_EXTRACT_AND_RUN=1`.

### 3.3 Three defects the PoC found and fixed

1. **The glibc floor follows the build host unless pinned.** The first bundle, built on a glibc 2.36
   host, pulled `cryptography-…-cp311-abi3-manylinux_2_34`, so the floor became 2.34.
   `--python-platform x86_64-manylinux_2_28` brought the highest symbol requirement down to
   `GLIBC_2.28` (objdump over every `.so`). The distro matrix confirms it.
2. **A source build silently breaks the floor.** The first 3.15.0rc3 build ran without
   `--only-binary`, and uv built `pydantic-core` from its sdist (it got hold of a Rust toolchain
   without one on PATH, inferred via maturin's bootstrap). The result was tagged `linux_x86_64` and
   needed `GLIBC_2.34`, while the platform pin reported success. With `--only-binary :all:` (now the
   default, `ALLOW_SOURCE=1` to override) the same build resolved a `cp315-manylinux_2_17` wheel. We
   didn't determine why the first resolution preferred the sdist; the fail-early flag makes that
   question moot.
3. **PBS's stdlib is only partly precompiled.** After a root `dpkg -i`, the first `tau --version`
   wrote 67 `.pyc` files into `/opt/tau` (asyncio and others), and `dpkg -r` then left the
   directories behind. On a read-only mount (an AppImage) those modules are recompiled in memory on
   every launch. A `compileall --invalidation-mode unchecked-hash` pass over the whole tree fixed it:
   **0** `.pyc` written after install. AppImage `--version` went from 679 ms to 532–544 ms, with the
   second number measured under heavier host load.

### 3.4 Sizes (ran)

| Artifact | Compressed | Unpacked |
|---|---|---|
| Bundle, untrimmed, 3.14.8 | 71.3 MB tar.gz | 243 MB |
| Bundle, trimmed, site-packages bytecode only | 54.2 MB | 186 MB |
| **Bundle, trimmed + full unchecked-hash bytecode, 3.14.8** | **58.2 MB** | 197 MB on disk / 173 MB apparent, 10,185 files |
| same, 3.14.8t | 58.3 MB | 197 MB |
| same, 3.15.0rc3 / 3.15.0rc3t | 59.8 / 60.0 MB | — |
| AppImage (squashfs, appimagetool default) 3.14.8 / 3.14.8t | 53.5 / 53.6 MB | — |
| .deb (`-Zxz`) 3.14.8 / 3.14.8t | 40.3 / 40.4 MB | Installed-Size 190 MB |
| PyInstaller onedir / onefile (same wheels, 3.14.8) | 44.7 MB tgz / 43 MB single file | 92 MB |
| `uv tool install` (alternative, no package) | — | 89 MB tool venv + 111 MB managed Python |
| tau-code runtime VSIX (read, 3.11) | 36.0 MB | 106 MB |

Where the bytes go (ran, unpacked, untrimmed GIL):
- statically linked `bin/python3.14`: 32 MB
- unused `libpython3.14.so`: 33 MB (trimmed)
- bytecode: about 51 MB
- `anthropic`: 23 MB
- `google`: 17 MB
- `cryptography`: 16 MB (pulled in by google-auth)
- `pillow` + libs: 21 MB
- `pygments`: 10 MB
- `textual`: 7.6 MB
- `pip`: 6.5 MB (trimmed)

The two cloud SDKs plus cryptography are roughly 56 MB of 197. They come from the `ffwf-tau`
metapackage's `[anthropic,google]` extras. A "local-models" bundle without them would be about 140 MB
unpacked (inferred).

### 3.5 Startup (ran; medians; the host was shared and loaded, loadavg 6–12 on 8 cores in later runs)

First, quieter run (n=15, warm cache):

| | `python -I -c pass` | `tau --version` | `import tau_coding_agent.app` | app + anthropic + google.genai |
|---|---|---|---|---|
| Bundle 3.14.8 | 12 ms | 291 ms | 550 ms | 1923 ms |
| Bundle 3.14.8t | 13 ms | 348 ms | 550 ms | 2038 ms |
| Dev venv 3.11.2 (editable) | 12 ms | 306 ms | 579 ms | 1632 ms |

- End-to-end `tau -p` to the stub, three interleaved warm runs: bundle 449–520 ms, venv 499–550 ms.
  The first run after a fresh extract was 888 ms bundle vs 578 ms venv. That is page-cache cold
  start, not interpreter cost.
- `-X importtime`: `tau_coding_agent` takes 300 ms cumulative in the bundle vs 353 ms in the venv.
  `tau_agent_core.events` dominates both at about 235 ms.
- Later runs under load moved every row up to 1.5–3× in both directions, so treat ±30% as noise.
  The conclusion holds: **no startup regression from the bundle; FT costs nothing measurable at
  startup.**
- AppImage (FUSE + squashfs): `--version` 532–544 ms vs 457 ms for the plain tree measured at the
  same moment.
- PyInstaller at the same moment: onedir 689 ms, onefile 1282 ms (unpacks to /tmp on every launch).

## 4. Format / tool matrix

The portable tree is the single product. Every format below wraps it, and none rebuilds Python.

| Format | Tool | Built here? | How | CI runner |
|---|---|---|---|---|
| tar.gz (Linux, macOS) / zip (Windows) | tar / `Compress-Archive` | **ran** (Linux) | `build_portable.sh` | each OS |
| AppImage | appimagetool (continuous, 2026-10-04; repo license NOASSERTION, upstream MIT, inferred) | **ran** | AppDir = tree + `AppRun` + `.desktop` (Terminal=true) + icon | ubuntu-22.04 / -24.04-arm |
| .deb | `dpkg-deb --build` | **ran** | `/opt/tau` + `/usr/bin/tau` symlink, `Depends: libc6 (>= 2.28)` | ubuntu |
| .deb + .rpm + .apk + archlinux | **nfpm** (MIT, v2.47.0, read) | documented | one `nfpm.yaml`: contents `/opt/tau` (tree), symlink `/usr/bin/tau`; `nfpm pkg -p deb\|rpm\|archlinux`. No root and no rpmbuild needed (inferred). Replaces the dpkg-deb step. | ubuntu |
| .dmg (macOS) | `hdiutil create -format UDZO` (gobboclippy's pattern) or create-dmg (MIT) | documented | `Tau.app/Contents/{MacOS/tau → launcher, Resources/tau/…}`. A terminal app can't open its own window without a terminal (§6), so the .app launches `Terminal.app`/bundled terminal running `bin/tau`, and the .dmg also ships the bare tree for `/usr/local/bin`. | macos-14 (arm64), macos-13 (x86_64); PBS has no universal2, so two builds |
| .pkg (macOS, puts `tau` on PATH) | `pkgbuild`/`productbuild` | documented | install tree to `/usr/local/lib/tau`, symlink `/usr/local/bin/tau` | macos |
| Windows installer | **Inno Setup** (custom permissive, 7.1.0) or **WiX** v7 (MSI) | documented | install tree to `%LOCALAPPDATA%\Programs\tau` (per-user, no UAC), add `bin` to user PATH, Start-menu entry opening Windows Terminal. WiX if MSI/Intune is wanted (check WiX's current maintenance-fee terms before adopting, inferred). NSIS (zlib) works too. | windows-latest, windows-11-arm |
| Windows launcher | small compiled `tau.exe` | documented | A `.cmd` launcher shows "Terminate batch job (Y/N)?" on Ctrl+C and mangles quoting (inferred), and Ctrl+C is τ's steering key. Use a tiny compiled trampoline: about 30 lines of C, or a Rust stub. PyApp (§5) is one. | windows |
| VSIX | vsce (existing tau-code flow) | existing | consume the **same** tree instead of a separate pip-`--target` build | ubuntu |

**Signing** (inferred unless noted):
- **macOS:** every Mach-O in the tree (`python3`, each `.so`, pillow's dylibs) needs Developer ID
  signing with hardened runtime, then `notarytool submit --wait` and `stapler staple` on the
  dmg/pkg. rcodesign (apple-codesign) can do this from Linux. Keep binaries **unstripped** after
  signing (tau-code's note, read).
- **Extensions on macOS:** anything that installs wheels *into* the signed tree breaks the
  signature. User extension deps must go to a user site-dir outside the bundle, and the hardened
  runtime then needs `com.apple.security.cs.disable-library-validation` to load them.
- **Windows:** Authenticode-sign `tau.exe`, `python.exe` and the installer (Azure Trusted Signing is
  the cheap route). PBS's `python.exe` is unsigned; python.org's is PSF-signed.
- **Linux:** no signing needed. Optionally attach a GPG or Sigstore signature to `SHA256SUMS`.

## 5. Alternatives (brief, with evidence)

| Option | Evidence | Verdict |
|---|---|---|
| **PBS tree + thin wrappers (this PoC)** | ran: all of §3 | **Recommended.** It is relocatable, keeps a real interpreter so `tau --extension x.py` can import anything and extension deps can be installed, lets us choose GIL, FT or 3.15 by changing one env var, and is the pattern tau-code already ships. There is no bootloader for antivirus to flag. Cost: ~58 MB, and we own the wrappers. |
| **PyApp** (ofek/pyapp v0.29.0, 2025-10-15, read; Apache-2.0/MIT dual, inferred) | not built: no cargo on this host | A Rust single-exe that, at first run, unpacks or downloads PBS and installs the project into a user data dir; it has `self update`/`self remove` (inferred). A strong **Windows launcher** candidate and a fine "single file" option. It is less transparent than a tree and depends on a first-run step. Its last release is a year old (read). |
| **PyInstaller** 6.22.3 (GPL-2.0 with bootloader exception, inferred; API says NOASSERTION, read) | ran: works with `--collect-data/--collect-submodules` for textual/anthropic/google.genai; onedir 92 MB / 44.7 MB tgz, 689 ms `--version`; onefile 43 MB, 1282 ms | Smaller, but **freezing removes the interpreter**: no `python` for extensions to pip-install into, and hidden-import upkeep for every new dynamic import. Its bootloader is the classic Windows Defender false-positive source (inferred). Doesn't fit a *programmable* agent. |
| **Nuitka** (repo license API reports **AGPL-3.0**, read; historically Apache-2.0, so check before any use) | not built | Compiles Python to C. It would freeze τ the same way PyInstaller does, so the same extension problem applies, plus long builds and Defender heuristics (inferred). For speed, targeted Rust/C++ extensions (§8) beat whole-program compilation. |
| **Briefcase** (BeeWare v0.4.5, BSD-3, read) | not built | Uses PBS-derived support packages and produces msi/dmg/AppImage/Flatpak/deb/rpm (inferred). It is GUI-app-shaped and its Linux "system" packages use the distro Python (inferred), which defeats our goal of choosing the interpreter. Worth borrowing ideas from rather than adopting. |
| **conda-pack** 0.9.2 (BSD-3, read) | not built | Relocatable conda env. It brings the conda toolchain and a channel, and conda-forge FT availability lags PyPI's (inferred). No advantage over PBS + uv. |
| **pex / shiv / zipapp** | inferred | Each needs a host Python of the right version, which is exactly the dependency we're removing. Useful only for "already has 3.14". |
| **`uv tool install`** + bootstrap script | ran: cold install with empty caches (downloads PBS + wheels) 8.8–10.7 s; `tau 0.12.0` works on 3.14 and 3.14t (`--python 3.14t`). **`uv tool install ffwf-tau` fails**: "No executables are provided by package `ffwf-tau`", hint: use `ffwf-tau-coding-agent`. | A zero-infrastructure **second channel**: `curl -LsSf https://astral.sh/uv/install.sh \| sh && uv tool install --python 3.14t ffwf-tau-coding-agent --with ffwf-tau`. **That sucks** for the name people type. Fix: give `ffwf-tau` its own `[project.scripts] tau = …` (open decision §10). |

## 6. Terminal bundling (read via repos/APIs; prototypes ran)

| Terminal | License (verified) | OS | Latest | Linux x86_64 prebuilt (size) | `--version` here | kitty gfx | sync 2026 | Pinned launch | Embeddable |
|---|---|---|---|---|---|---|---|---|---|
| **WezTerm** | **MIT** (`LICENSE.md`; the API says NOASSERTION) | L/M/W | stable 20240203 (stale); nightly 2026-10-05 | AppImage 48 MB, tar.xz 30 MB, deb/rpm | ran: `wezterm 20240203-110809-5046fc22` | yes (partial) | yes | `wezterm --config-file tau.lua start --class tau -- tau` | no (mux server only) |
| **Rio** | **MIT** (API) | L/M/W | v0.5.28, 2026-09-17 | deb 11 MB (34 MB unpacked, 23 MB binary) | ran: `rioterm 0.5.28` | yes (strings in binary; sixel, iTerm2 too) | yes (inferred) | `rio -e tau` with a pinned config dir (`RIO_CONFIG_HOME`, inferred) | **`librio.a` + header + RioKit.xcframework** in the release (read) |
| **Ghostty** | **MIT** (API) | L/M (no Windows app) | v1.3.1 tag; rolling `tip` | **none** (macOS dmg; Linux source only) | — | yes | yes | `ghostty --config-default-files=false --config-file=… -e tau` (inferred) | **libghostty-vt** (C/Zig, also wasm): "usable today… API in flux… not tagged" (read) |
| Alacritty | **Apache-2.0** (`alacritty/Cargo.toml` and `alacritty_terminal/Cargo.toml`: `license = "Apache-2.0"`, read). **Not MIT, not dual.** | L/M/W | v0.17.0 | **none** (msi, portable exe, dmg only) | — | no | yes | `alacritty --config-file … -e tau` | `alacritty_terminal` crate |
| Kitty | **GPL-3.0** (API) | L/M | v0.49.2 | txz 44 MB (160 MB unpacked) | ran: `kitty 0.49.2` | defines it | yes | `kitty --config … tau` | no |
| foot | MIT (LICENSE, Codeberg) | Linux/Wayland | 1.28.0 | distro only | — | no (sixel) | yes | `foot -c … tau` | no |
| Contour | Apache-2.0 (API) | L/M/W | 0.7.0.8982 | deb 4.4 MB | ran: **fails** on Debian 12 (needs `libyaml-cpp.so.0.8`, GLIBC_2.38, Qt 6.10) | sixel | yes | — | no |
| Windows Terminal | MIT (API) | Windows | 1.25.2733.0 | n/a (zip 12 MB) | — | no (sixel) | unknown | `wt.exe new-tab -- tau`; settings only via portable mode | no |

MIT: WezTerm, Rio, Ghostty, foot, Windows Terminal. Alacritty is Apache-2.0, which is permissive but
needs a NOTICE. Kitty is GPL-3.0; shipping it beside τ is aggregation and legally possible, but it
brings source-offer obligations into every installer, so avoid it.

**What Textual does with the terminal** (read, textual 8.2.7 in the dev venv):
- **Synchronized output:** actively queried. `drivers/linux_driver.py:315-324` writes `ESC[?2026$p`
  (skipped on Apple_Terminal). The reply is parsed at `_xterm_parser.py:24`, which turns on
  `SYNC_START/END` framing (`_ansi_sequences.py:436-437`, `app.py:4582`). Confirmed in the pty
  capture (ran).
- **Kitty keyboard:** not detected, pushed unconditionally (`linux_driver.py:285-292`; opt out with
  `TEXTUAL_DISABLE_KITTY_KEY`, `constants.py:116`). Confirmed in the capture (ran). A terminal
  without it falls back to legacy keys, so **chords like Ctrl+Enter/Shift+Enter are only reliable on
  a kitty-protocol terminal** (inferred).
- **Truecolor:** Textual defers to Rich, which reads `COLORTERM in ("truecolor","24bit")`
  (`rich/console.py:805-807`). There is no query, so a launcher must export `COLORTERM=truecolor`.
- **Images:** nothing native. It needs `textual-image` (kitty graphics/sixel) or `rich-pixels`
  (half-blocks). A guaranteed kitty-graphics terminal is what would make inline images (pillow is
  already bundled) worth building.

**What a pinned terminal buys:**
- a known-good font with the glyphs τ uses
- truecolor without guessing
- sync output (no tearing during streaming)
- the kitty keyboard protocol (unambiguous chords)
- kitty graphics (images)
- one config to support instead of N terminals

**What it costs:** 11 MB compressed for Rio (34 MB unpacked) or 30–48 MB for WezTerm per platform,
plus a second upstream to track and sign. The GUI-launch path needs a display; `--version` is all we
could prove headlessly.

Sketch of the WezTerm config (not run, needs a display):

```lua
-- share/tau/wezterm.lua, shipped in the tree
local wezterm = require 'wezterm'
return {
  font = wezterm.font 'JetBrains Mono', -- bundled, OFL
  enable_kitty_keyboard = true,
  enable_kitty_graphics = true,
  set_environment_variables = { COLORTERM = 'truecolor' },
  window_close_confirmation = 'NeverPrompt',
  automatically_reload_config = false,
}
```

Launch: `wezterm --config-file "$TAU_HOME/share/tau/wezterm.lua" start --class tau -- "$TAU_HOME/bin/tau"`.
Ship it as a second desktop entry, "τ (window)", alongside `Terminal=true` "τ". The terminal is an
**optional variant** (`tau-<ver>-<os>-<arch>-app`), never a requirement: `tau` must keep working in
any terminal and over SSH.

**Terminal recommendation:**
- **Rio** for a launched window today: MIT, all three OSes, small, active, kitty graphics, and the
  only one shipping an embeddable static library.
- **WezTerm** if feature depth matters more than release freshness. Its last stable is from 2024, so
  we'd pin a nightly.
- **libghostty** is the long-term "τ is the application" path: embed the VT in a native shell.
  Revisit once it is tagged.

## 7. Recommended architecture

```
                 ┌── wheels (py3-none-any) ──► PyPI (unchanged, Trusted Publishing)
 source @ tag ──►│
                 └── runtime tree per (os, arch, flavor) = PBS + same wheels + bin/tau
                        ├─ tar.gz / zip            (portable)
                        ├─ AppImage                (Linux)
                        ├─ deb / rpm / apk / arch  (nfpm)
                        ├─ dmg / pkg               (macOS; signed + notarized)
                        ├─ Inno/WiX installer      (Windows; Authenticode)
                        └─ VSIX payload            (tau-code consumes the same tree)
```

1. **One builder**, `build_portable.sh` grown into `scripts/build_runtime.py`, used by both τ and
   tau-code. tau-code's `build-payload.mjs` is then replaced by downloading τ's release asset.
   - Today they disagree on both Python and method: 3.11 via `pip --target` vs 3.14 via uv.
2. **Build the runtime from the `build` job's wheels artifact**, so the bundle and PyPI ship
   byte-identical τ. This is the same "publish consumes only the build artifact" rule publish.yml
   already enforces.
3. **Default interpreter: 3.14 with the GIL.** Ship **3.14t as a parallel `-ft` artifact** until τ
   has a measured reason (CPU-parallel Python work) to prefer it. τ is asyncio-bound today. Add 3.15
   the day it is final; 3.15rc3 and rc3t already build and pass the smoke tests here.
4. **Build flags that are not optional:**
   - `--only-binary :all:`
   - `--python-platform <arch>-manylinux_2_28` (the glibc floor; the matching macOS and Windows
     platform pins too)
   - full `unchecked-hash` bytecode
   - `-I` in the launcher
5. **Extension deps live outside the tree:** a user site-dir under `~/.tau/`, so the installed tree
   stays read-only and signed.

### CI sketch (GitHub Actions; not run)

```yaml
  runtime:
    needs: [build]                       # consumes tau-dists (the wheels)
    strategy:
      fail-fast: true
      matrix:
        include:
          - {os: ubuntu-22.04,      target: x86_64-unknown-linux-gnu,  plat: x86_64-manylinux_2_28}
          - {os: ubuntu-24.04-arm,  target: aarch64-unknown-linux-gnu, plat: aarch64-manylinux_2_28}
          - {os: macos-14,          target: aarch64-apple-darwin,      plat: aarch64-apple-darwin}
          - {os: macos-13,          target: x86_64-apple-darwin,       plat: x86_64-apple-darwin}
          - {os: windows-latest,    target: x86_64-pc-windows-msvc,    plat: x86_64-pc-windows-msvc}
          - {os: windows-11-arm,    target: aarch64-pc-windows-msvc,   plat: aarch64-pc-windows-msvc}
        flavor: [gil, freethreaded]
    steps:
      - download tau-dists; install uv (astral-sh/setup-uv)
      - build runtime tree (build_runtime.py --wheels dist/ --target … --flavor …)
      - smoke: move tree, clean env, --version, heavy imports + GIL check, tau -p vs stub, TUI pty
      - package: Linux → AppImage + nfpm(deb,rpm); macOS → codesign, dmg, notarytool, staple;
                 Windows → signtool, Inno/WiX
      - upload-artifact
  distro-matrix:  # Linux only: debian:10, rockylinux:8, ubuntu:20.04, fedora, arch — run the tree read-only
  release:        # SHA256SUMS, gh release upload --verify-tag; PyPI jobs unchanged
```

That is 12 runtime jobs. They run in parallel and nothing is compiled except our own launcher, so
they should be about the cost of today's `test` matrix (inferred). Secrets: Apple Developer ID cert +
notary API key, Windows signing identity. PyPI stays tokenless.

## 8. Native acceleration (C++/Rust) in this model

- **Build:** Rust via PyO3 + maturin (Apache-2.0, v1.15.0, read), using `maturin-action`. C++ via
  nanobind or pybind11 + scikit-build-core, using `cibuildwheel`. Put it in a new distribution
  (e.g. `ffwf-tau-accel`) that the core depends on.
  - Per CLAUDE.md's Fail Early, it should be a **hard dependency with wheels for every supported
    target**, not an optional speedup with a silent pure-Python fallback.
- **ABI:**
  - `abi3` (stable ABI, one wheel per platform for cp311+) does **not** cover free-threaded builds
    on 3.14. A 3.14t wheel must be per-version `cp314t` (inferred).
  - 3.15 adds the free-threaded stable ABI, `abi3t`. **Observed here:** the 3.15.0rc3t bundle
    resolved `cryptography-…-cp315-abi3t-manylinux_2_28` (ran).
  - So the wheel set is `cp311-abi3` + `cp314t` (+ `cp315-abi3t` once 3.15 ships) per platform.
  - PyO3 supports free-threading (`#[pymodule(gil_used = false)]`); without that declaration, the
    interpreter re-enables the GIL on import with a RuntimeWarning (inferred). That warning is what
    `smoke_portable.sh` turns into an error.
- **Impact on the release matrix (docs/RELEASING.md):**
  - Today it is one pure build on 3.11 serving every version, plus a 3.11–3.14 test matrix.
  - With a native package, the **PyPI** side grows to roughly
    `{linux x86_64, aarch64} × {manylinux_2_28, musllinux} + {macOS x86_64, arm64} + {win x64, arm64}`
    × `{abi3, cp314t}`, about 16 wheels per release.
  - The test matrix then also needs a `3.14t` row.
  - The **bundle** side barely changes: each runtime job needs exactly one wheel, for its own
    interpreter. It can build it in the same job, or take it from the wheel matrix.
  - The sdist needs a Rust or C++ toolchain to build, and `--only-binary` in the bundle build turns
    a missing wheel into a loud failure rather than a host-glibc build (§3.3, defect 2).
- **Where native code would pay** (inferred, unmeasured): `tau_agent_core.events` import (235 ms of
  the 300 ms startup is ours, not the interpreter's), large-session tree/JSONL parsing, diffing
  (`_generate_diff`), tokenization. Measure first with `-X importtime` and py-spy (in the dev venv).

## 9. Risks

- **Size:** about 58 MB per artifact × 12 targets per release; GitHub release assets are free but
  slow for users on bad links. Mitigation: a no-cloud-SDK variant (about −56 MB unpacked), zstd
  instead of gzip.
- **PBS cadence:** we inherit its release cadence and its CVE response for OpenSSL, sqlite and
  libexpat. Pin `PBS_TAG` and bump it deliberately; Renovate-style automation is possible.
- **glibc 2.28 floor** drops Ubuntu 18.04 and CentOS 7 (ran: 18.04 fails). Going lower means
  `manylinux_2_17` wheels, which pillow's recent wheels may not provide (read: pillow ships
  2_27/2_28). A musl/Alpine tree is a separate target (PBS has `-musl`).
- **macOS notarization and library validation** versus user-installed extension deps (§4). This is
  the one real design constraint the bundle imposes on τ's extension model.
- **Windows:** launcher quality (Ctrl+C is τ's steer key), Defender reputation for a new unsigned
  exe, and `%LOCALAPPDATA%` vs Program Files.
- **Two ways to get τ** (PyPI and bundle) means two bug surfaces. `tau --version` should report the
  interpreter and the channel (bundle/pip) so reports can be triaged (inferred; not implemented).
- **Free-threaded:** the GIL stays off only while every compiled dependency is FT-ready. One new dep
  without `cp314t` wheels quietly turns 3.14t into 3.14-with-overhead. The smoke check
  (`-W error::RuntimeWarning` + `sys._is_gil_enabled()`) must gate the `-ft` artifact.
- **Terminal variant:** a second upstream to sign, track and support. Keep it optional.

## 10. Open decisions for John (as asked on 2026-10-05; answers in §11.0)

1. Unify with tau-code's runtime build? **Answered:** yes, at least partially (§11.2).
2. Default interpreter? **Answered:** free-threaded, 3.14t and 3.15t (§11.1).
3. Formats for the first release. Still open.
4. Bundle contents, full vs lighter? **Answered:** three variants (§11.3).
5. Terminal variant? **Answered:** Rio (§11.3c).
6. `ffwf-tau` should be `uv tool install`-able. Still open.
7. Native code. Still open.

## 11. Follow-up (2026-10-06): John's answers, built

### 11.0 The answers this section implements

1. Standalone builds are free-threaded by default (3.14t, 3.15t). Extensions may bring the GIL
   back; the base framework must not, so the build fails if it does.
2. Upstream tau-code's runtime-building techniques into one builder here, and describe the
   tau-code change without editing tau-code.
3. Three variants: (a) full; (b) headless (CLI, `-p`, rpc, with no Textual); (c) standalone on Rio.
4. A portable install checks a `.tau/` beside it, resolved in `tau_agent_core`, so τ runs from a
   flash drive.

### 11.1 The GIL gate (ran)

`scripts/build_runtime.py` defaults to `--flavor freethreaded`. On a native build, `verify()`
does the following in the finished tree, with `-I -B`:
1. imports τ's entry modules for the variant (cli, headless, rpc_mode, repl, serve.cli, and `app`
   when the TUI is present);
2. imports the lazily-loaded SDKs (anthropic, google.genai, PIL.Image, websockets, prompt_toolkit);
3. imports **every compiled extension module in site-packages**, derived from the `.so`/`.pyd`
   files rather than from a list;
4. then reads `sys._is_gil_enabled()`.

If any import fails, or the GIL is on, the build exits 1 and nothing is archived.

- **Positive (ran):** 3.14.8t and 3.15.0rc3t, all variants: "imported 26 modules … GIL off after
  every import".
- **Negative (ran):** `gil_gate_negative.sh` compiles `gilneeder.c`, a module with no
  `Py_MOD_GIL_NOT_USED` declaration, into a built tree and re-runs `build_runtime.py --verify-only`.
  The result is exit 1: "free-threaded runtime has the GIL ON after importing τ and its default
  dependencies". Remove the module and it is exit 0 again.
- **The gate caught a real dead file on its first run (ran):** `PIL._imagingtk` failed to import,
  because tkinter is pruned. It is now pruned with it, the same "a file that exists and fails"
  rule tau-code wrote for `_tkinter`.
- **Cross targets can't execute.** For them the gate is structural: every extension filename in a
  free-threaded tree must carry a `t` ABI tag, `cp314t`/`cpython-314t`. Only a native runner can
  catch a `cp314t` wheel that still forgets `Py_MOD_GIL_NOT_USED`, so CI must build each
  free-threaded target on its own OS and CPU, or at least re-run `--verify-only` there.

### 11.2 One runtime builder: `scripts/build_runtime.py`

**Interface:**

```
python scripts/build_runtime.py --target {linux-x64,linux-arm64,alpine-x64,alpine-arm64,
    darwin-x64,darwin-arm64,win32-x64,win32-arm64} [--variant full|headless|standalone]
    [--flavor freethreaded|gil] [--python 3.14.8|3.15.0rc3] [--pbs-tag 20261003]
    [--wheels DIR] --out OUT [--portable-home] [--no-bytecode] [--archive] [--verify-only] [--list]
```

The output is `OUT/tau/{bin, python, terminal?, .tau?, manifest.json, LICENSE}`, plus
`tau-<ver>-<target>-<variant>-py<XY>[t].tar.gz`, or `.zip` for Windows. The script is stdlib-only
and runs on the host's Python 3.11. It needs `uv`.

**What it took from tau-code's `build-payload.mjs` (read, all 1000 lines):**
- the VS Code target names, kept identical so that `--target` passes straight through;
- the `manifest.json` shape, `apiVersion: 1`, with the same `target`, `interpreter`, `shim`, `args`,
  `pythonVersion`, `tauVersion`, `pbsRelease` and `builtAt` keys. τ adds `variant`,
  `freeThreaded`, `wheelPlatform`, `requirements`, `portableHome`, `bytecode`, `terminal` and
  `checks`;
- reading the τ version back off the installed dist-info;
- deleting by **referrer evidence**, not by name. The Tcl sweep and the libpython drop both
  byte-scan every binary for the library's name first. On win32-x64 that kept
  `DLLs/sqlite3.dll` because `_sqlite3.cp314t-win_amd64.pyd` names it, which is the exact
  regression tau-code paid for (ran);
- `_tkinter` deleted along with Tcl;
- `bin/` collapsed to one interpreter, so vsce doesn't triple it through symlinks;
- `ensurepip/_bundled`, `include/`, `share/`, `libs/` and `*.a` dropped;
- pip **kept**, because `python -m pip` is how an extension's dependencies get installed;
- `--only-binary :all:` always;
- a launcher that follows symlinks, and a `.cmd` launcher on Windows;
- structural checks for cross targets, with an honest "structural only" line in the output.

**What it does differently, and why:**
- **uv, with a host interpreter of the same version and flavor, instead of `pip --target
  --python-version 311`.** pip's `--python-version` can't ask for free-threaded wheels. uv given
  a free-threaded *host* interpreter plus `--python-platform x86_64-pc-windows-msvc` resolves
  `_pydantic_core.cp314t-win_amd64.pyd` and `jiter.cp314t-win_amd64.pyd` (ran). The builder
  fetches that host PBS itself.
- **Bytecode on by default**, compiled `unchecked-hash` with `-s <root> -p <tau>`. That strips the
  build machine's path from `co_filename`, which was tau-code's reason for `--no-compile`, while
  read-only installs (AppImage, `/opt` as root) still write nothing (§3.3 #3). It costs 46 MB
  unpacked and 16 MB in the tar.gz (headless: 173.7 vs 127.3 MB, 55.9 vs 39.8 MB, ran). A VSIX
  lives in a writable directory, so tau-code should pass `--no-bytecode`.
- **The glibc floor is explicit:** `manylinux_2_28` through uv's `--python-platform`, where
  tau-code listed manylinux tags.
- **linux-armhf is dropped:** uv has no armv7 `--python-platform` (read: `uv pip install --help`).
  tau-code ships it today, so that is an open decision (§11.6).

**Cross builds from this Linux host (ran):**

| target | variant | archive | unpacked | note |
|---|---|---|---|---|
| win32-x64 | full | 58.4 MB zip | 158.3 MB | `sqlite3.dll` kept by referrer |
| win32-x64 | standalone | 68.7 MB zip | 181.8 MB | `rio.exe` (portable build) + `tau-window.cmd` |
| darwin-arm64 | headless | 54.8 MB | 164.1 MB | structural only |
| linux-arm64 | full | 53.6 MB | 173.3 MB | structural only |

**What tau-code would delete or change to consume it (read: tau-code `packages/runtime/`; not
edited):**

1. **Delete** nearly all of `packages/runtime/scripts/build-payload.mjs`: `TARGETS`, `PRUNE_DIRS`,
   `TCL_PATTERN`, `referrers`/`namedBy`/`prune`/`dropTcl`/`dropUnreferencedLibpython`/`collapseBin`,
   `download`, `installedVersion`, `tauSpec`/`withoutExtras`/`requirements`, `writeShim`, and the
   prune half of `build`. About 750 of the 1000 lines.
2. **Keep** `verify()`, `generatedProtocol()` and `freePort()`, about 120 lines, as a post-step.
   They say `hello` to the payload's `tau serve` with tau-code's own generated protocol version.
   That check is client-specific, so it belongs to the client, and τ's builder doesn't do it. Its
   result goes into a separate `protocolVersion` field, or a sidecar file. τ's manifest has no
   such key.
3. **Replace** the build step with one of these:
   - **(A)** download τ's release asset `tau-<ver>-<target>-headless-py314t.tar.gz`, check it
     against `SHA256SUMS`, and unpack it to `runtime/`. This needs the CI in §7 to publish runtime
     archives.
   - **(B)** for an unreleased τ, run `python <tau checkout>/scripts/build_runtime.py --target
     <t> --variant headless --no-bytecode --wheels <dir> --out <tmp>`, then move `<tmp>/tau` to
     `runtime/`. This replaces today's `TAU_WHEELS`/`TAU_SPEC` path.

   Recommended: A for releases, B for release candidates.
4. **`package.json`:** `tauSpec` becomes `{ "tau": "0.12.0", "variant": "headless" }`. The
   requirement list moves into τ's `VARIANTS["headless"]`, which is tau-code's current spec plus
   `[repl]` (prompt_toolkit, about 3 MB). If that 3 MB matters, add a `serve` variant to the
   builder, which is a one-line table entry.
5. **Never pass `--portable-home`.** A VS Code extension directory is replaced on every update,
   so a `.tau/` inside it would delete the user's sessions at the next upgrade.
6. **`src/extension.ts` needs no change.** It reads `interpreter`, `shim`, `args`,
   `pythonVersion`, `tauVersion` and `target` from the manifest (read: `extension.ts:47-56`), all
   relative paths with the same names. The interpreter becomes `python/bin/python3.14t`, which it
   gets from the manifest rather than from a literal.
7. **`.github/workflows/release.yml`:** the matrix comes from
   `python scripts/build_runtime.py --list`, or from τ's release, instead of
   `build-payload.mjs --list`. The cache key hashes τ's version rather than `build-payload.mjs`.
   linux-armhf leaves the matrix unless decision §11.6 says otherwise.
8. **`docs/ARCHITECTURE.md` §14:** the runtime's Python version (3.11.16 → 3.14.8t) and its
   provenance (τ's builder) change. Sizes go up: headless `--no-bytecode` zips to 45.2 MB decimal (43.1 MiB), vs the
   36.0 MB VSIX today, because the 3.14t interpreter is larger and prompt_toolkit is added
   (ran: `zip -9` of the tree).

### 11.3 The three variants (ran; linux-x64, 3.14.8t, bytecode on)

| variant | requirements | unpacked | tar.gz | AppImage | .deb |
|---|---|---|---|---|---|
| (a) full | `ffwf-tau` + `coding-agent[tui,repl,serve]` | 180.9 MB | 58.1 MB | 53.6 MB | 40.0 MB |
| (b) headless | `coding-agent[repl,serve]` + `llm[anthropic,google]` + `agent-core[images]` | 173.7 MB | 55.9 MB | 51.6 MB | 38.6 MB |
| (c) standalone | full + Rio 0.5.28 (x11 build) | 203.0 MB | 67.9 MB | 62.7 MB | 46.7 MB |
| full, `--flavor gil` | as (a) | 180.4 MB | 57.9 MB | — | — |
| full, 3.15.0rc3t | as (a) | 186.1 MB | 59.7 MB | — | — |

MB here is MiB. The .deb and AppImage byte counts are in the build logs.

**(b) Headless saves only 7 MB (4%).** Textual and its share of rich are small next to the
interpreter (~48 MB after pruning), the bytecode (46 MB) and the cloud SDKs plus cryptography
(~56 MB). Its value is the missing *dependency*, not the size:
- `find_spec("textual")` is None in the tree, and the builder fails if Textual is ever pulled in.
- Bare `tau` answers "the interactive TUI needs the 'tui' extra (textual is missing) …", exit 2.

**`tau --mode repl` and Textual (read + ran).** CLAUDE.md's "shares the TUI's path" is about the
*prompt* path, not imports. `repl.py` imports rich at module scope and prompt_toolkit lazily, and
pyproject's `[repl]` extra says outright that "the whole point of this head is that it needs no
Textual". So the headless variant includes repl, with no change and no fallback:
- In the headless tree, `import tau_coding_agent.cli, tau_coding_agent.repl` loads no `textual*`
  module.
- `tau --mode repl` in a pty, typing `ping` against the stub, prints `pong`, exit 0.
- `tau -p` prints `pong`.
- `tau --mode rpc` with a JSON-RPC `prompt` produces `agent_end` on stdout.

`smoke_headless.sh` runs all four.

**Finding: the full variant had no repl before (read).** The `ffwf-tau` metapackage pulls
`coding-agent[tui]` only, so the first pass's bundle (§3), and `pip install ffwf-tau`, have no
prompt_toolkit, and `tau --mode repl` fails there. The builder's full variant adds
`[repl,serve]`. Whether `ffwf-tau` on PyPI should too is a one-line `tau-meta/pyproject.toml`
decision (§11.6).

**(c) Standalone on Rio:**
- `terminal/rio` is unpacked from `rioterm_0.5.28_amd64_x11.deb` by the builder's own `ar`
  reader, so it needs no dpkg. Windows uses `rio-portable-x86_64.exe`.
- `terminal/config/config.toml` pins `confirm-before-quit = false` and
  `env-vars = ["COLORTERM=truecolor"]`. Both keys were read from `rio-backend/src/config/mod.rs`.
- `bin/tau-window` runs
  `RIO_CONFIG_HOME=<tree>/terminal/config rio --app-id tau --title-placeholder tau -e <tree>/bin/tau "$@"`.

Checked without a display (ran, `smoke_standalone.sh`):
- `rioterm 0.5.28` runs from the tree under `env -i`.
- `RIO_CONFIG_HOME` is honoured: `--write-config` lands in the pointed-at directory.
- The launcher parses.
- **Rio needs `GLIBC_2.35`** (objdump), so the standalone .deb declares `libc6 (>= 2.35)`.
  debian:10 refuses it (ran), and the variant's floor is Ubuntu 22.04 / Debian 12.

Desktop integration:
- The .deb ships `/usr/bin/tau-window`, with a desktop entry `Exec=tau-window`, `Terminal=false`.
- The standalone AppImage's `AppRun` opens Rio only when started with no arguments and no tty
  (from a file manager or launcher); from a shell it is the CLI.

**Not run:** an actual Rio window. There is no Xvfb here, and the host display is John's desktop.

**Not wired:** macOS standalone. Rio ships only a `.dmg`, which needs `hdiutil` on a macOS runner.

### 11.4 Portable install (built in `tau_agent_core`; ran end to end)

**Where `~/.tau` was resolved before (grep, read): five places, none shared.**
- `tau_coding_agent/config.py:25`, `TAU_DIR = Path.home() / ".tau"`. Sessions, themes, exports,
  the REPL history and the serve log all hang off it.
- `tau_agent_core/settings.py:42,70`: the extension_dirs default and the global `settings.json`.
- `tau_agent_core/sdk.py:243,762`: `_GLOBAL_EXTENSIONS_DIR` and `_AGENT_DIR`, the global
  `AGENTS.md`.

All five now call `tau_agent_core.tau_home.resolve_tau_home()`. Your reading is right: when the
portable `.tau/` exists it is the **whole** home. **One exception:** `--mode rpc`'s default
session base is `<tmp>/.tau-<uid>/sessions` (D-6, a deliberate ephemeral default), still on the
host. Moving it would turn "ephemeral" into durable state on the drive without anyone deciding
that, so it is recorded and left (`docs/PORTABLE-HOME.md` §5). A host that wants portable rpc
sessions passes `--session-dir`.

**Deviations from the literal spec (§3 of the design record):**
- The launcher passes `TAU_PORTABLE_BASE=<tree root>` (bin/tau, tau.cmd), or
  `dirname($APPIMAGE)` (AppRun). It doesn't use `sys.argv[0]`, which is `site-packages/…/cli.py`
  under `-m` and a squashfs path inside an AppImage.
- The switch is `.tau/` existing, not `.tau/config.json`. The shipped `.tau/` is empty, and a
  `config.json` check would send the first run to look on the host.

Unit tests:
- `tau-agent-core/tests/test_tau_home.py`, 6 tests. Unset, empty, portable wins, a base without
  `.tau` is an ordinary install, a non-directory raises, and `Settings.load()` reads the drive.
- `tau-coding-agent/tests/test_portable_home.py`, 2 tests. These run a fresh interpreter, because
  `TAU_DIR` is fixed at import time.

**End to end (ran, `smoke_portable_home.sh`)** on the full archive, unpacked to
`<work>/media/USBSTICK/`, with `env -i`, an empty HOME, an empty TMPDIR and an empty cwd:

```
== drive layout: bin LICENSE manifest.json python .tau
== tau -p, config only on the drive
pong
== the session landed on the drive:
<drive>/tau/.tau/sessions/--tmp-tmp.y4lJOr6ri6-cwd--/2026-10-06T05-04-05-565Z_….jsonl
<drive>/tau/.tau/config.json
== TUI for 6 s in a pty
== files written outside the drive (HOME, TMPDIR, cwd); expected none:
== [end of list]
== same bundle, .tau/ removed: an ordinary install
pong
<work>/home/.tau/sessions/--tmp-…-cwd--/….jsonl
== AppImage on a drive with .tau/ beside it
pong
<work>/media/STICK2/.tau/sessions/--tmp-…-cwd--/….jsonl
== files written outside the drive; expected none:
<work>/tmp/.mount_tau.ApHlFBIj
```

That last line is the AppImage *runtime's* FUSE mountpoint directory. It is empty and left behind
in TMPDIR; it isn't τ's.

**.deb (ran, debian:10 container):**
- `/opt/tau` ships no `.tau/`.
- `tau -p` with a config in `/root/.tau` writes its session to
  `/root/.tau/sessions/--tmp--/….jsonl`.
- The runtime writes zero `.pyc` after install.

**Known limit:** session directories are keyed by the host's absolute cwd, so a drive mounted at
another path on another machine sees different `--continue` keys (`docs/PORTABLE-HOME.md` §5).

### 11.5 Things that suck, found on the way (ran)

- `tau --mode rpc > file` → `ValueError: Pipe transport is only for pipes, sockets and character
  devices` with a traceback. **Bit sucky:** a host that logs the wire to a file can't, without
  `| cat`. This is pre-existing (`rpc_mode`'s asyncio pipe transport), not caused by the bundle.
- In the headless bundle, bare `tau` says "pip install 'ffwf-tau-coding-agent[tui]'". **Bit
  sucky:** there is no pip workflow for a bundle; it should say "use the full or standalone
  build". That needs the launcher to tell τ what channel it is (the §9 "channel" item).
- In a `--portable-home` tree, `~/.tau/config.json` is ignored *by design*. A user who unpacks the
  tar.gz into `~/opt` and expects their existing config gets "no default_model". The error names
  `~/.tau/config.json`, which is the wrong path in that case. **Bit sucky:** the error should name
  the home it actually resolved. `config.py` builds the message; a one-line fix, not made here.

### 11.6 Still open

1. **linux-armhf:** uv can't target armv7. Either drop it from tau-code's nine targets or keep
   one `pip --target` path for it.
2. **`ffwf-tau` on PyPI:** add `[repl,serve]` to the metapackage, and a `tau` script so that
   `uv tool install ffwf-tau` works (§5).
3. **The rpc session default in portable mode** (§11.4): keep the tmp default, or move it to
   `<portable>/.tau/rpc-sessions`.
4. **Formats for release 1, and signing** (§10.3).
5. **A `serve` variant for tau-code** (headless minus prompt_toolkit), if 3 MB matters.

## Reproduce

```bash
# needs: uv (UV=... or on PATH), curl, tar, docker (distro matrix), appimagetool (AppImage), gcc (gate negative test)
OUT="$SCRATCH/rt"
for v in full headless standalone; do
  python3 scripts/build_runtime.py --target linux-x64 --variant $v --portable-home --archive --out "$OUT/$v"
done
python3 scripts/build_runtime.py --target linux-x64 --python 3.15.0rc3 --archive --out "$OUT/full-315t"
python3 scripts/build_runtime.py --target win32-x64 --variant standalone --portable-home --archive --out "$OUT/win"
experiments/distribution/smoke_portable.sh "$OUT/full/tau-0.12.0-linux-x64-full-py314t.tar.gz"
experiments/distribution/smoke_tui.sh "$OUT/full/tau"
experiments/distribution/smoke_headless.sh "$OUT/headless"
experiments/distribution/smoke_standalone.sh "$OUT/standalone"
experiments/distribution/distro_matrix.sh "$OUT/full" debian:10 rockylinux:8 ubuntu:18.04 archlinux:latest
UV=$UV experiments/distribution/gil_gate_negative.sh "$OUT/full" "$PBS_314T/python"
APPIMAGETOOL=$APPIMAGETOOL experiments/distribution/build_linux_packages.sh \
    "$OUT/full/tau-0.12.0-linux-x64-full-py314t.tar.gz" "$OUT/pkg"
experiments/distribution/smoke_portable_home.sh "$OUT/full/tau-0.12.0-linux-x64-full-py314t.tar.gz" \
    "$OUT/pkg/tau-0.12.0-linux-x64-full-py314t.AppImage"
```
