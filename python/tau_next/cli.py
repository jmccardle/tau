"""The `tau` command: `tau config check [--show MODEL]` and `tau trust [DIR]` (D13).

`main` takes its streams, environment, and working directory as arguments so a test
drives it with real files and real streams. Exit status: 0 success, 1 a problem
(printed as one message on stderr, never a traceback), 2 a usage error.
"""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from tau_next.config import ConfigError
from tau_next.config_layers import ConfigProblem, LayerText, ModelCatalog, check_config
from tau_next.dotenv import DotenvError
from tau_next.startup import load_config, prompts, select_model, user_config_path
from tau_next.trust import (
    TrustError,
    approve_project,
    ensure_trusted,
    find_project,
    is_trusted,
    project_config_path,
    project_environment,
    tau_home,
    trust_report,
)

_USAGE_ERROR = 2
_UNTRUSTED = " [untrusted project: not used until `tau trust`]"


@dataclass(frozen=True)
class _Run:
    stdin: TextIO
    stdout: TextIO
    environ: Mapping[str, str]
    cwd: Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tau", description="τ next (Trusty).")
    sub = parser.add_subparsers(dest="command")
    config = sub.add_parser("config", help="inspect configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    check = config_sub.add_parser("check", help="report every problem in the config files")
    check.add_argument("--show", metavar="MODEL", help="print MODEL's resolved values and sources")
    trust = sub.add_parser("trust", help="approve a project directory's .tau/")
    trust.add_argument("dir", nargs="?", type=Path, help="project directory (default: the current)")
    return parser


def _problem_line(problem: ConfigProblem, note: str) -> str:
    where = problem.source or "config"
    if problem.line is not None:
        where += f":{problem.line}:{problem.column}"
    at = f"{problem.path}: " if problem.path else ""
    return f"{where}: {problem.severity}: {at}{problem.message}{note}"


def _check(run: _Run, show: str | None) -> int:
    home = tau_home(run.environ)
    root = find_project(run.cwd, run.environ)
    config_file = project_config_path(root) if root is not None else None
    layer: Path | LayerText | None = None
    trusted = None
    if root is not None and config_file is not None and config_file.exists():
        if is_trusted(root, home):
            trusted = ensure_trusted(root, home, ask=None)
            layer = trusted.layer()
        else:
            layer = config_file
    problems = check_config(
        user_config_path(home), layer, environ=project_environment(trusted, run.environ)
    )
    for p in problems:
        untrusted = trusted is None and config_file is not None and p.source == str(config_file)
        run.stdout.write(_problem_line(p, _UNTRUSTED if untrusted else "") + "\n")
    if any(p.severity == "error" for p in problems):
        return 1
    if show is not None:
        loaded = load_config(run.cwd, run.environ, ask=None)
        name = select_model(loaded.catalog, show, pick=prompts(run.stdin, run.stdout).pick)
        _show(loaded.catalog, name, run.stdout)
    return 0


def _show(catalog: ModelCatalog, name: str, out: TextIO) -> None:
    config = catalog.resolve(name)
    values = {
        "model": config.model,
        "base_url": config.base_url,
        "provider": config.provider,
        "api_key_env": config.api_key_env,
        "api_key": "(set)" if config.literal_api_key is not None else None,
    }
    out.write(f"{name}\n")
    for origin in catalog.explain(name):
        group = f", group {origin.group}" if origin.group else ""
        out.write(f"  {origin.key:<12} {values.get(origin.key)}  [{origin.source}{group}]\n")


def _trust(run: _Run, given: Path | None) -> int:
    home = tau_home(run.environ)
    root = given.resolve() if given is not None else find_project(run.cwd, run.environ)
    if root is None:
        raise TrustError(f"no project: no .tau/ found from {run.cwd}")
    report = trust_report(root, home)
    run.stdout.write(report + "\n")
    try:
        approve_project(root, home)
    except TrustError as e:
        if str(e) == report:  # the refusal is the report we just printed
            raise TrustError(f"{root} cannot be trusted; fix the errors above") from e
        raise
    run.stdout.write(f"trusted {root}\n")
    return 0


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
    parser = _parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if args.command is None:
        parser.print_help(out)
        return _USAGE_ERROR
    try:
        if args.command == "trust":
            return _trust(run, args.dir)
        return _check(run, args.show)
    except (ConfigError, TrustError, DotenvError) as e:
        err.write(f"tau: {e}\n")
        return 1
