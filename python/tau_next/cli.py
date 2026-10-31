"""The `tau` command: `tau config check [--show MODEL]` and `tau trust [DIR]` (D13).

`main` takes its streams, environment, and working directory as arguments so a test
drives it with real files and real streams. Exit status: 0 success, 1 a problem
(printed as one message on stderr, never a traceback), 2 a usage error, 130 interrupted.
Everything printed from config data goes through `safe_text`.
"""

import argparse
import asyncio
import json
import os
import shlex
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from tau_next._config import ConfigError
from tau_next._config_layers import LayerText, ModelCatalog, check_config
from tau_next._dotenv import DotenvError
from tau_next._options import check_options
from tau_next._startup import load_config, prompts, select_model
from tau_next._trust import (
    TAU_DIR,
    TrustError,
    UntrustedProject,
    create_project,
    ensure_trusted,
    find_project,
    project_environment,
    refuse_special,
    safe_block,
    safe_text,
    tau_home,
    user_config_path,
)
from tau_next.protocols import Provider
from tau_next.providers import builtin_providers
from tau_next.step_limit import StepLimit

_USAGE_ERROR = 2
_INTERRUPTED = 130
_UNTRUSTED = " [untrusted project: not used until `tau trust`]"


@dataclass(frozen=True)
class _Run:
    stdin: TextIO
    stdout: TextIO
    environ: Mapping[str, str]
    cwd: Path


def _parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    parser = argparse.ArgumentParser(prog="tau", description="τ next (Trusty).")
    sub = parser.add_subparsers(dest="command")
    config = sub.add_parser("config", help="inspect configuration")
    config_sub = config.add_subparsers(dest="config_command", metavar="{check}")
    check = config_sub.add_parser("check", help="report every problem in the config files")
    check.add_argument("--show", metavar="MODEL", help="print MODEL's resolved values and sources")
    trust = sub.add_parser("trust", help="approve a project directory's .tau/")
    trust.add_argument(
        "dir", nargs="?", type=Path, help="a directory in the project (default: the current)"
    )
    return parser, config


@dataclass
class _Findings:
    """What `config check` prints, and whether any of it stops startup."""

    lines: list[str] = field(default_factory=lambda: [])
    failed: bool = False

    def error(self, where: Path, message: str) -> None:
        self.lines.append(f"{where}: error: {message}")
        self.failed = True


def _project_findings(
    root: Path, home: Path, environ: Mapping[str, str], found: _Findings
) -> tuple[LayerText | None, Mapping[str, str], bool]:
    """The project's config layer, the environment to check keys against, and whether the
    project is trusted. Everything that would make startup refuse is added to `found`."""
    try:
        project = ensure_trusted(root, home, ask=None)
    except UntrustedProject as e:
        command = f"tau trust {shlex.quote(str(root))}"
        found.error(root / TAU_DIR, f"project is not trusted; run `{command}`")
        return e.layer, environ, False
    except TrustError as e:  # a refused file, or TAU_HOME or trusted.json writable by others
        found.error(e.where or root / TAU_DIR, str(e))
        return None, environ, False
    try:
        return project.layer(), project_environment(project, environ), True
    except DotenvError as e:  # approved, so it parsed; only a changed parser gets here
        found.error(root / ".env", str(e))
        return project.layer(), environ, True


def _check(run: _Run, show: str | None) -> int:
    home = tau_home(run.environ)
    root = find_project(run.cwd, run.environ).root
    found = _Findings()
    layer: LayerText | None = None
    environ = run.environ
    trusted = True
    if root is not None:
        layer, environ, trusted = _project_findings(root, home, run.environ, found)
    providers = builtin_providers(environ)
    try:
        problems = check_config(user_config_path(home), layer, environ=environ, providers=providers)
    finally:  # a check opens no connection; closing is the builder's duty all the same
        asyncio.run(_close(providers))
    for p in problems:
        untrusted = not trusted and layer is not None and p.source == layer.source
        found.lines.append(str(p) + (_UNTRUSTED if untrusted else ""))
    for line in found.lines:
        run.stdout.write(safe_text(line) + "\n")
    if found.failed or any(p.severity == "error" for p in problems):
        return 1
    loaded = load_config(run.cwd, run.environ, ask=None)
    # The options of the extensions this command knows; a project's own are checked where
    # they are loaded (`check_options`).
    wrong = check_options(loaded.catalog, (StepLimit(),))
    for p in wrong:
        run.stdout.write(safe_text(str(p)) + "\n")
    if wrong:
        return 1
    if show is not None:
        name = select_model(loaded.catalog, show, pick=prompts(run.stdin, run.stdout).pick)
        _show(loaded.catalog, name, run.stdout)
    return 0


async def _close(providers: Mapping[str, Provider]) -> None:
    for provider in providers.values():
        await provider.aclose()


def _show(catalog: ModelCatalog, name: str, out: TextIO) -> None:
    config = catalog.resolve(name)
    values = {
        "model": config.model,
        "base_url": config.base_url,
        "max_tokens": config.max_tokens,
        "options": json.dumps(config.options, default=dict),
        "provider": config.provider,
        "api_key_env": config.api_key_env,
        "api_key": "(set)" if config.literal_api_key is not None else None,
    }
    out.write(f"{safe_text(name)}\n")
    for origin in catalog.explain(name):
        group = f", group {origin.group}" if origin.group else ""
        value = values.get(origin.key)
        out.write(safe_text(f"  {origin.key:<12} {value}  [{origin.source}{group}]") + "\n")


def _trust(run: _Run, given: Path | None) -> int:
    home = tau_home(run.environ)
    start = (run.cwd / given).resolve() if given is not None else run.cwd.resolve()
    refuse_special(start, run.environ)
    if not start.is_dir():
        raise TrustError(f"{start} is not a directory")
    search = find_project(start, run.environ)
    root = search.root
    created = root is None
    if root is None:
        # A `.tau` passed over further up does not matter; one at `start` is in the way.
        if search.skipped is not None and os.path.lexists(start / TAU_DIR):
            raise TrustError(f"no project: {search.skipped}")
        create_project(start)
        root = start
    asked = False

    def show_report(report: str) -> bool:
        nonlocal asked
        asked = True
        run.stdout.write(safe_block(report) + "\n")
        return True

    try:
        ensure_trusted(root, home, ask=show_report)
    except BaseException:
        if created:  # leave no empty `.tau/` that startup would then find and refuse
            shutil.rmtree(root / TAU_DIR, ignore_errors=True)
        raise
    run.stdout.write(safe_block(f"trusted {root}" if asked else f"already trusted: {root}") + "\n")
    return 0


def _fail(err: TextIO, message: str) -> int:
    err.write("tau: " + safe_block(message) + "\n")
    return 1


def main(  # noqa: PLR0913 - the process, injected so a test can supply real ones
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> int:
    """Run `tau` with `argv` (default `sys.argv[1:]`); the other arguments default to
    the process's streams, environment, and working directory. Returns the exit status."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    run = _Run(
        sys.stdin if stdin is None else stdin,
        out,
        os.environ if environ is None else environ,
        Path.cwd() if cwd is None else cwd,
    )
    parser, config = _parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if args.command is None:
        parser.print_help(err)
        return _USAGE_ERROR
    if args.command == "config" and args.config_command is None:
        config.print_help(err)
        return _USAGE_ERROR
    try:
        if args.command == "trust":
            return _trust(run, args.dir)
        return _check(run, args.show)
    except (ConfigError, TrustError, DotenvError) as e:
        return _fail(err, str(e))
    except KeyboardInterrupt:
        err.write("\ntau: interrupted\n")
        return _INTERRUPTED
