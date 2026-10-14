#!/usr/bin/env python3
"""Build a τ runtime: a relocatable CPython with τ installed, for one OS, CPU and interpreter.

Reference: experiments/distribution/RESULTS.md (the experiment record this was built in).

    python scripts/build_runtime.py --target linux-x64 --out OUT
    python scripts/build_runtime.py --target win32-arm64 --variant headless --out OUT --archive
    python scripts/build_runtime.py --target linux-x64 --variant standalone --portable-home --archive --out OUT

The output is one directory, ``OUT/tau/``::

    tau/bin/tau            launcher (tau.cmd on Windows); tau-window for --variant standalone
    tau/python/            python-build-standalone, pruned, τ installed into its site-packages
    tau/terminal/          Rio and its pinned config (--variant standalone only)
    tau/.tau/              present only with --portable-home (docs/PORTABLE-HOME.md)
    tau/manifest.json      what was built, read back off disk

Everything a wrapper needs (AppImage, deb/rpm, dmg, installer, VSIX) is in that
tree; no wrapper rebuilds Python. Every target builds on any host: the
interpreter and every wheel are prebuilt, so nothing compiles. The checks that
execute the tree (the GIL gate among them) run only when the target is the
host's own, and the build says so when it could not run them.

Requires ``uv`` (on PATH or $UV). Inherits the lessons of tau-code's
``packages/runtime/scripts/build-payload.mjs``, which this replaces.
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGES = ("tau-llm", "tau-agent-core", "tau-coding-agent", "tau-jmfts", "tau-meta")
PBS_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/{tag}/{asset}"
DEFAULT_PYTHON = "3.14.8"
DEFAULT_PBS_TAG = "20261003"
RIO_VERSION = "0.5.28"
RIO_URL = "https://github.com/raphamorim/rio/releases/download/v{v}/{asset}"


@dataclass(frozen=True)
class Target:
    """One platform a runtime can be built for.

    Attributes:
        triple: python-build-standalone's target triple.
        windows: Windows layout (``python.exe``, ``Lib/``, ``DLLs/``) vs POSIX.
        platform: uv's ``--python-platform``; it also fixes the glibc floor.
        rio_asset: Rio's release asset for ``--variant standalone``; None if unsupported.
    """

    triple: str
    windows: bool
    platform: str
    rio_asset: str | None


TARGETS: dict[str, Target] = {
    "linux-x64": Target(
        "x86_64-unknown-linux-gnu", False, "x86_64-manylinux_2_28", "rioterm_{v}_amd64_x11.deb"
    ),
    "linux-arm64": Target(
        "aarch64-unknown-linux-gnu", False, "aarch64-manylinux_2_28", "rioterm_{v}_arm64_x11.deb"
    ),
    "alpine-x64": Target("x86_64-unknown-linux-musl", False, "x86_64-unknown-linux-musl", None),
    "alpine-arm64": Target("aarch64-unknown-linux-musl", False, "aarch64-unknown-linux-musl", None),
    "darwin-x64": Target("x86_64-apple-darwin", False, "x86_64-apple-darwin", None),
    "darwin-arm64": Target("aarch64-apple-darwin", False, "aarch64-apple-darwin", None),
    "win32-x64": Target(
        "x86_64-pc-windows-msvc", True, "x86_64-pc-windows-msvc", "rio-portable-x86_64.exe"
    ),
    "win32-arm64": Target(
        "aarch64-pc-windows-msvc", True, "aarch64-pc-windows-msvc", "rio-portable-aarch64.exe"
    ),
}

VARIANTS: dict[str, list[str]] = {
    "full": [
        "ffwf-tau=={v}",
        "ffwf-tau-coding-agent[tui,repl,serve]=={v}",
    ],
    "headless": [
        "ffwf-tau-coding-agent[repl,serve]=={v}",
        "ffwf-tau-llm[anthropic,google]=={v}",
        "ffwf-tau-agent-core[images]=={v}",
    ],
}
VARIANTS["standalone"] = VARIANTS["full"]

LAZY_MODULES = ("anthropic", "google.genai", "PIL.Image", "websockets", "prompt_toolkit")
ENTRY_MODULES = {
    "full": (
        "tau_coding_agent.cli",
        "tau_coding_agent.headless",
        "tau_coding_agent.rpc_mode",
        "tau_coding_agent.repl",
        "tau_coding_agent.serve.cli",
        "tau_coding_agent.app",
    ),
    "headless": (
        "tau_coding_agent.cli",
        "tau_coding_agent.headless",
        "tau_coding_agent.rpc_mode",
        "tau_coding_agent.repl",
        "tau_coding_agent.serve.cli",
    ),
}
ENTRY_MODULES["standalone"] = ENTRY_MODULES["full"]
HEADLESS_ABSENT = ("textual",)

PRUNE_DIRS = {"test", "tests", "idlelib", "turtledemo", "lib2to3", "tkinter", "__pycache__"}
TCL_PATTERN = re.compile(r"^(lib)?(tcl|tk|itcl|tdbc|thread|sqlite3\.)", re.IGNORECASE)
TEST_EXTENSION = re.compile(r"^(_test|_ctypes_test|xxlimited|_xxtestfuzz|xxsubtype)")
BINARY = re.compile(r"(\.(dll|pyd|exe|dylib)$)|(\.so($|\.))", re.IGNORECASE)

RIO_CONFIG = """\
# τ's pinned Rio config (RIO_CONFIG_HOME points here). Keys verified in
# rio-backend/src/config/mod.rs at v{v}.
confirm-before-quit = false
env-vars = ["COLORTERM=truecolor"]
"""


class BuildError(Exception):
    """A build step failed; the message says which and why."""


def say(message: str) -> None:
    """Print a progress line to stderr."""
    print(message, file=sys.stderr, flush=True)


def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run ``cmd``; raise BuildError with its output if it fails."""
    done = subprocess.run(cmd, capture_output=True, text=True, **kwargs)  # type: ignore[call-overload]
    if done.returncode != 0:
        raise BuildError(f"{' '.join(cmd)} exited {done.returncode}:\n{done.stdout}{done.stderr}")
    return done  # type: ignore[no-any-return]


def tree_size(path: Path) -> int:
    """Bytes of regular files under ``path``, symlinks not followed."""
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def fetch(url: str, into: Path) -> Path:
    """Download ``url`` to ``into`` unless cached; atomic via a ``.partial`` rename."""
    if into.exists():
        return into
    into.parent.mkdir(parents=True, exist_ok=True)
    partial = into.with_name(into.name + ".partial")
    say(f"  fetch   {url}")
    with urllib.request.urlopen(url) as response, open(partial, "wb") as out:
        shutil.copyfileobj(response, out)
    partial.rename(into)
    return into


def host_target() -> str | None:
    """The TARGETS key for this machine, or None if it is not one."""
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower()
    )
    if arch is None:
        return None
    system = platform.system()
    if system == "Windows":
        return f"win32-{arch}"
    if system == "Darwin":
        return f"darwin-{arch}"
    if system == "Linux":
        musl = any(Path("/lib").glob("ld-musl-*.so.1"))
        return f"{'alpine' if musl else 'linux'}-{arch}"
    return None


def tau_version() -> str:
    """τ's version, from the same literal the CLI's ``--version`` reads."""
    init = REPO / "tau-coding-agent" / "src" / "tau_coding_agent" / "__init__.py"
    match = re.search(r'^__version__ = "([^"]+)"', init.read_text(), re.MULTILINE)
    if match is None:
        raise BuildError(f"no __version__ in {init}")
    return match.group(1)


class Builder:
    """One runtime build. ``build()`` is the whole sequence."""

    def __init__(self, args: argparse.Namespace) -> None:
        """Capture the options and resolve everything derived from them."""
        self.name: str = args.target
        if self.name not in TARGETS:
            raise BuildError(f"unknown target {self.name!r}; known: {', '.join(TARGETS)}")
        self.target = TARGETS[self.name]
        self.variant: str = args.variant
        self.ft: bool = args.flavor == "freethreaded"
        self.py: str = args.python
        self.short = ".".join(self.py.split(".")[:2]) + ("t" if self.ft else "")
        self.pbs_tag: str = args.pbs_tag
        self.out = Path(args.out).resolve()
        self.cache = Path(args.cache).resolve() if args.cache else self.out / ".cache"
        self.uv: str = args.uv or os.environ.get("UV") or shutil.which("uv") or ""
        if not self.uv:
            raise BuildError("uv not found; put it on PATH or pass --uv / set $UV")
        self.wheels = Path(args.wheels).resolve() if args.wheels else None
        self.portable_home: bool = args.portable_home
        self.bytecode: bool = args.bytecode
        self.archive: bool = args.archive
        self.version = tau_version()
        self.native = self.name == host_target()
        self.root = self.out / "tau"
        self.python = self.root / "python"
        if self.variant == "standalone" and self.target.rio_asset is None:
            raise BuildError(f"--variant standalone has no Rio build wired for {self.name}")

    def pbs_asset(self, triple: str) -> str:
        """The python-build-standalone asset name for ``triple`` at this version and flavor."""
        flavor = "freethreaded-install_only_stripped" if self.ft else "install_only_stripped"
        return f"cpython-{self.py}+{self.pbs_tag}-{triple}-{flavor}.tar.gz"

    def extract_pbs(self, triple: str, into: Path) -> Path:
        """Fetch and unpack PBS for ``triple`` into ``into``; return its ``python/`` dir."""
        asset = self.pbs_asset(triple)
        tarball = fetch(PBS_URL.format(tag=self.pbs_tag, asset=asset), self.cache / asset)
        into.mkdir(parents=True, exist_ok=True)
        run(["tar", "-xzf", str(tarball), "-C", str(into)])
        python = into / "python"
        if not python.is_dir():
            raise BuildError(f"{asset} did not unpack a python/ directory")
        return python

    def interpreter(self, python: Path, windows: bool) -> Path:
        """The executable inside a PBS ``python/`` tree."""
        exe = python / "python.exe" if windows else python / "bin" / f"python{self.short}"
        if not exe.exists():
            raise BuildError(f"no interpreter at {exe}")
        return exe

    def site_packages(self) -> Path:
        """The target tree's site-packages."""
        if self.target.windows:
            return self.python / "Lib" / "site-packages"
        return self.python / "lib" / f"python{self.short}" / "site-packages"

    def stdlib(self) -> Path:
        """The target tree's stdlib directory."""
        if self.target.windows:
            return self.python / "Lib"
        return self.python / "lib" / f"python{self.short}"

    def host_python(self) -> Path:
        """An interpreter of the same version and flavor that runs here, for resolving and compiling."""
        if self.native:
            return self.interpreter(self.python, self.target.windows)
        host = host_target()
        if host is None:
            raise BuildError("this host is not a known target, so it cannot resolve for another")
        hdir = self.cache / f"host-{host}-{self.py}{'t' if self.ft else ''}"
        python = hdir / "python"
        if not python.is_dir():
            self.extract_pbs(TARGETS[host].triple, hdir)
        return self.interpreter(python, TARGETS[host].windows)

    def build_wheels(self) -> Path:
        """``--wheels`` if given, else build τ's five wheels from this tree with ``uv build``."""
        if self.wheels is not None:
            return self.wheels
        wheels = self.cache / f"wheels-{self.version}"
        if not wheels.is_dir():
            for pkg in PACKAGES:
                run([self.uv, "build", "--wheel", "--out-dir", str(wheels), str(REPO / pkg)])
        return wheels

    def install(self, wheels: Path) -> list[str]:
        """Install the variant's requirements into the target site-packages; return them."""
        reqs = [r.format(v=self.version) for r in VARIANTS[self.variant]]
        site = self.site_packages()
        say(f"  install {' '.join(reqs)}")
        run(
            [
                self.uv,
                "pip",
                "install",
                "--quiet",
                "--python",
                str(self.host_python()),
                "--python-platform",
                self.target.platform,
                "--target",
                str(site),
                "--only-binary",
                ":all:",
                "--no-compile",
                "--find-links",
                str(wheels),
                *reqs,
            ]
        )
        for stray in ("bin", "Scripts"):
            shutil.rmtree(site / stray, ignore_errors=True)
        return reqs

    def referrers(self) -> list[tuple[Path, bytes]]:
        """Every binary in the tree that could name a shared library, read once."""
        return [
            (p, p.read_bytes())
            for p in self.python.rglob("*")
            if p.is_file() and BINARY.search(p.name)
        ]

    @staticmethod
    def named_by(candidates: list[tuple[Path, bytes]], name: str, ignore: set[Path]) -> Path | None:
        """The first candidate outside ``ignore`` whose bytes contain ``name``."""
        needle = name.encode()
        return next((p for p, b in candidates if p not in ignore and needle in b), None)

    def prune(self) -> int:
        """Drop what nothing in τ's closure reaches; return bytes freed."""
        freed = 0

        def remove(path: Path) -> None:
            nonlocal freed
            if path.is_dir() and not path.is_symlink():
                freed += tree_size(path)
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                freed += 0 if path.is_symlink() else path.stat().st_size
                path.unlink()

        for d in sorted(self.python.rglob("*"), key=lambda p: len(p.parts)):
            if d.is_dir() and d.name in PRUNE_DIRS and d.exists():
                remove(d)
        ext_dir = self.python / "DLLs" if self.target.windows else self.stdlib() / "lib-dynload"
        for p in list(ext_dir.iterdir()):
            if p.name.startswith("_tkinter.") or TEST_EXTENSION.match(p.name):
                remove(p)
        for p in (self.site_packages() / "PIL").glob("_imagingtk.*"):
            remove(p)
        remove(self.stdlib() / "ensurepip" / "_bundled")
        for dev in ("include", "share", "libs", "tcl"):
            remove(self.python / dev)
        if not self.target.windows:
            for p in (self.python / "lib").glob("*.a"):
                remove(p)
            for p in self.stdlib().glob("config-*"):
                remove(p)
        lib_dir = self.python / "DLLs" if self.target.windows else self.python / "lib"
        sweep = [p for p in lib_dir.iterdir() if TCL_PATTERN.match(p.name)]
        candidates = self.referrers()
        for p in sweep:
            if p.is_dir():
                remove(p)
                continue
            keeper = self.named_by(candidates, p.name, set(sweep))
            if keeper is None:
                remove(p)
            else:
                say(f"  keep    {p.name} -- {keeper.relative_to(self.python)} names it")
        if not self.target.windows:
            shared = [p for p in (self.python / "lib").glob("libpython*.so*")]
            for p in shared:
                keeper = self.named_by(candidates, p.name, set(shared))
                if keeper is None:
                    remove(p)
                else:
                    say(f"  keep    {p.name} -- {keeper.relative_to(self.python)} names it")
            keep = {f"python{self.short}", "pip", "pip3", f"pip{self.short}"}
            for p in (self.python / "bin").iterdir():
                if p.name not in keep:
                    remove(p)
        return freed

    def compile_bytecode(self) -> None:
        """Precompile everything, unchecked-hash, build paths stripped from co_filename."""
        run(
            [
                str(self.host_python()),
                "-I",
                "-m",
                "compileall",
                "-q",
                "-j0",
                "--invalidation-mode",
                "unchecked-hash",
                "-s",
                str(self.root),
                "-p",
                "<tau>",
                str(self.python / ("Lib" if self.target.windows else "lib")),
            ]
        )

    def write_launchers(self) -> str:
        """Write ``bin/tau`` (or ``tau.cmd``), and ``tau-window`` for standalone; return the shim path."""
        bindir = self.root / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        if self.target.windows:
            (bindir / "tau.cmd").write_text(
                "@echo off\r\n"
                'if not defined TAU_PORTABLE_BASE set "TAU_PORTABLE_BASE=%~dp0.."\r\n'
                '"%~dp0..\\python\\python.exe" -I -m tau_coding_agent.cli %*\r\n'
            )
            if self.variant == "standalone":
                (bindir / "tau-window.cmd").write_text(
                    "@echo off\r\n"
                    'set "RIO_CONFIG_HOME=%~dp0..\\terminal\\config"\r\n'
                    'start "" "%~dp0..\\terminal\\rio.exe" -e "%~dp0tau.cmd" %*\r\n'
                )
            return "bin/tau.cmd"
        resolve = (
            "#!/bin/sh\n"
            "set -e\n"
            'self="$0"\n'
            'while [ -L "$self" ]; do\n'
            '  link=$(readlink "$self")\n'
            '  case "$link" in /*) self="$link" ;; *) self="$(dirname "$self")/$link" ;; esac\n'
            "done\n"
            'root=$(cd "$(dirname "$self")/.." && pwd)\n'
        )
        tau = bindir / "tau"
        tau.write_text(
            resolve
            + 'export TAU_PORTABLE_BASE="${TAU_PORTABLE_BASE:-$root}"\n'
            + f'exec "$root/python/bin/python{self.short}" -I -m tau_coding_agent.cli "$@"\n'
        )
        tau.chmod(0o755)
        if self.variant == "standalone":
            window = bindir / "tau-window"
            window.write_text(
                resolve
                + 'export RIO_CONFIG_HOME="$root/terminal/config"\n'
                + 'exec "$root/terminal/rio" --app-id tau --title-placeholder tau -e "$root/bin/tau" "$@"\n'
            )
            window.chmod(0o755)
        return "bin/tau"

    def add_rio(self) -> str:
        """Unpack Rio into ``terminal/`` with its pinned config; return Rio's version."""
        assert self.target.rio_asset is not None
        asset = self.target.rio_asset.format(v=RIO_VERSION)
        blob = fetch(RIO_URL.format(v=RIO_VERSION, asset=asset), self.cache / asset)
        term = self.root / "terminal"
        (term / "config").mkdir(parents=True, exist_ok=True)
        if asset.endswith(".exe"):
            shutil.copy2(blob, term / "rio.exe")
        else:
            data = _deb_member(blob.read_bytes(), "data.tar")
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                member = next((m for m in tf.getmembers() if m.name.endswith("usr/bin/rio")), None)
                if member is None:
                    raise BuildError(f"{asset} has no usr/bin/rio")
                extracted = tf.extractfile(member)
                assert extracted is not None
                (term / "rio").write_bytes(extracted.read())
            (term / "rio").chmod(0o755)
        (term / "config" / "config.toml").write_text(RIO_CONFIG.format(v=RIO_VERSION))
        return RIO_VERSION

    def verify(self) -> dict[str, object]:
        """Execute the tree (host target only) or check it structurally; raise on any failure."""
        site = self.site_packages()
        if self.variant == "headless":
            present = [m for m in HEADLESS_ABSENT if (site / m).exists()]
            if present:
                raise BuildError(f"headless runtime contains {present}; a requirement pulled it in")
        if not self.native:
            say(f"  verify  structural only: {self.name} cannot run on {host_target()}")
            if self.ft:
                wrong = [
                    p.name
                    for p in site.rglob("*")
                    if BINARY.search(p.name)
                    and re.search(r"cp3\d+-|cpython-3\d+-", p.name)
                    and not re.search(r"cp3\d+t-|cpython-3\d+t-", p.name)
                ]
                if wrong:
                    raise BuildError(
                        f"free-threaded runtime has GIL-only extension modules: {wrong[:5]}"
                    )
            return {"executed": False}
        exe = self.interpreter(self.python, self.target.windows)
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        with tempfile.TemporaryDirectory() as home:
            env["HOME"] = home
            env["USERPROFILE"] = home
            shim = self.root / ("bin/tau.cmd" if self.target.windows else "bin/tau")
            version = run([str(shim), "--version"], env=env).stdout.strip()
            say(f"  verify  {version}")
            modules = [*ENTRY_MODULES[self.variant], *LAZY_MODULES]
            probe = (
                "import importlib, json, pathlib, sys\n"
                "site = pathlib.Path(sys.argv[1])\n"
                "names = list(sys.argv[2:])\n"
                "for p in site.rglob('*'):\n"
                "    if p.suffix in ('.so', '.pyd') and '.libs' not in p.parent.name:\n"
                "        rel = p.relative_to(site).with_suffix('')\n"
                "        names.append('.'.join(rel.parts).split('.cpython-')[0].split('.cp3')[0].split('.abi3')[0])\n"
                "failed = {}\n"
                "for n in sorted(set(names)):\n"
                "    try: importlib.import_module(n)\n"
                "    except Exception as e: failed[n] = repr(e)\n"
                "gil = sys._is_gil_enabled() if hasattr(sys, '_is_gil_enabled') else True\n"
                "print(json.dumps({'imported': len(set(names)) - len(failed), 'failed': failed, 'gil': gil}))\n"
            )
            report = json.loads(
                run([str(exe), "-I", "-B", "-c", probe, str(site), *modules], env=env).stdout
            )
        if report["failed"]:
            raise BuildError(f"modules in the runtime do not import: {report['failed']}")
        say(
            f"  verify  imported {report['imported']} modules (τ entry points, SDKs, every extension module)"
        )
        if self.ft and report["gil"]:
            raise BuildError(
                "free-threaded runtime has the GIL ON after importing τ and its default dependencies; "
                "an extension module without Py_MOD_GIL_NOT_USED re-enabled it"
            )
        if self.ft:
            say("  verify  GIL off after every import")
        return {"executed": True, "version": version, "gilEnabled": report["gil"]}

    def build(self) -> Path:
        """Run the whole sequence; return the archive (with --archive) or the tree."""
        say(
            f"=== {self.name} {self.variant} python {self.short} ({'native' if self.native else 'cross'})"
        )
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.extract_pbs(self.target.triple, self.root)
        raw = tree_size(self.python)
        reqs = self.install(self.build_wheels())
        freed = self.prune()
        rio = self.add_rio() if self.variant == "standalone" else None
        shim = self.write_launchers()
        if self.bytecode:
            self.compile_bytecode()
        if self.portable_home:
            (self.root / ".tau").mkdir()
        shutil.copy2(REPO / "LICENSE", self.root / "LICENSE")
        checks = self.verify()
        site = self.site_packages()
        dist = [p.name for p in site.glob("ffwf_tau_coding_agent-*.dist-info")]
        if len(dist) != 1:
            raise BuildError(f"expected one ffwf_tau_coding_agent dist-info, found {dist}")
        manifest = {
            "apiVersion": 1,
            "target": self.name,
            "triple": self.target.triple,
            "variant": self.variant,
            "interpreter": str(
                self.interpreter(self.python, self.target.windows).relative_to(self.root)
            ),
            "shim": shim,
            "args": ["-I", "-m", "tau_coding_agent.cli"],
            "pythonVersion": self.py,
            "freeThreaded": self.ft,
            "pbsRelease": self.pbs_tag,
            "wheelPlatform": self.target.platform,
            "tauVersion": dist[0].removeprefix("ffwf_tau_coding_agent-").removesuffix(".dist-info"),
            "requirements": reqs,
            "portableHome": self.portable_home,
            "bytecode": self.bytecode,
            "terminal": {"name": "rio", "version": rio} if rio else None,
            "checks": checks,
            "builtAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        }
        (self.root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        say(
            f"  size    {tree_size(self.root) / 2**20:.1f} MB (interpreter {raw / 2**20:.1f} MB, pruned {freed / 2**20:.1f} MB)"
        )
        if not self.archive:
            return self.root
        tag = f"{self.short.replace('.', '')}"
        stem = f"tau-{self.version}-{self.name}-{self.variant}-py{tag}"
        if self.target.windows:
            path = self.out / f"{stem}.zip"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(self.root.rglob("*")):
                    zf.write(p, p.relative_to(self.out))
        else:
            path = self.out / f"{stem}.tar.gz"
            with tarfile.open(path, "w:gz") as tf:
                tf.add(self.root, arcname="tau")
        say(f"  archive {path.name} {path.stat().st_size / 2**20:.1f} MB")
        return path


def _deb_member(deb: bytes, prefix: str) -> bytes:
    """The bytes of the first ``ar`` member of a .deb whose name starts with ``prefix``."""
    if not deb.startswith(b"!<arch>\n"):
        raise BuildError("not a .deb (ar) archive")
    pos = 8
    while pos < len(deb):
        header = deb[pos : pos + 60]
        name = header[:16].decode().strip().rstrip("/")
        size = int(header[48:58].decode().strip())
        body = deb[pos + 60 : pos + 60 + size]
        if name.startswith(prefix):
            return body
        pos += 60 + size + (size % 2)
    raise BuildError(f"no {prefix}* member in the .deb")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--target", default=host_target(), help=f"one of {', '.join(TARGETS)} (default: this host)"
    )
    parser.add_argument("--variant", choices=sorted(VARIANTS), default="full")
    parser.add_argument("--flavor", choices=("freethreaded", "gil"), default="freethreaded")
    parser.add_argument(
        "--python", default=DEFAULT_PYTHON, help="CPython version, e.g. 3.14.8 or 3.15.0rc3"
    )
    parser.add_argument(
        "--pbs-tag", default=DEFAULT_PBS_TAG, help="python-build-standalone release"
    )
    parser.add_argument("--wheels", help="τ wheels to install (default: build them from this tree)")
    parser.add_argument("--out", help="output directory; the tree lands in OUT/tau (required)")
    parser.add_argument("--cache", help="download cache (default: OUT/.cache)")
    parser.add_argument("--uv", help="uv binary (default: $UV, then PATH)")
    parser.add_argument(
        "--portable-home", action="store_true", help="ship an empty .tau/ (docs/PORTABLE-HOME.md)"
    )
    parser.add_argument(
        "--no-bytecode", dest="bytecode", action="store_false", help="skip precompiling"
    )
    parser.add_argument(
        "--archive", action="store_true", help="also write tar.gz (zip for Windows)"
    )
    parser.add_argument(
        "--verify-only", action="store_true", help="re-run the checks on an existing OUT/tau"
    )
    parser.add_argument(
        "--list", action="store_true", help="print the target names as JSON and exit"
    )
    args = parser.parse_args(argv)
    if args.list:
        print(json.dumps(list(TARGETS)))
        return 0
    if args.out is None:
        parser.error("--out is required")
    if args.target is None:
        parser.error("this host is not a known target; pass --target")
    try:
        builder = Builder(args)
        if args.verify_only:
            if not builder.root.is_dir():
                raise BuildError(f"--verify-only: no tree at {builder.root}")
            builder.verify()
        else:
            builder.build()
    except BuildError as exc:
        say(f"build_runtime: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
