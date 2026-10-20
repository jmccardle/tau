"""The coding agent's home: portable ``.tau/``, ``TAU_HOME``, and a bad home (docs/PORTABLE-HOME.md).

Each case runs in a fresh interpreter, so nothing cached in this one can answer.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tau_agent_core.tau_home import HOME_ENV, PORTABLE_BASE_ENV
from tau_agent_core.testing import child_env


def _tau_dir(home: Path, **env: str) -> str:
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import tau_coding_agent.config as c; print(c.default_config_path().parent)",
        ],
        env=child_env(home, **env),
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_portable_base_with_dot_tau_is_tau_dir(tmp_path: Path) -> None:
    (tmp_path / "drive" / ".tau").mkdir(parents=True)
    env = {PORTABLE_BASE_ENV: str(tmp_path / "drive")}
    assert _tau_dir(tmp_path / "home", **env) == str((tmp_path / "drive" / ".tau").resolve())


def test_no_portable_base_is_home(tmp_path: Path) -> None:
    assert _tau_dir(tmp_path / "home", **{PORTABLE_BASE_ENV: ""}) == str(tmp_path / "home" / ".tau")


def test_tau_home_is_tau_dir(tmp_path: Path) -> None:
    (tmp_path / "drive" / ".tau").mkdir(parents=True)
    (tmp_path / "throwaway").mkdir()
    env = {PORTABLE_BASE_ENV: str(tmp_path / "drive"), HOME_ENV: str(tmp_path / "throwaway")}
    assert _tau_dir(tmp_path / "home", **env) == str((tmp_path / "throwaway").resolve())


def test_cli_refuses_a_missing_tau_home_in_one_line(tmp_path: Path) -> None:
    missing = tmp_path / "typo"
    env = child_env(tmp_path / "home", **{HOME_ENV: str(missing)})
    out = subprocess.run(
        [sys.executable, "-m", "tau_coding_agent.cli", "-p", "hi"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert out.returncode == 2
    assert out.stdout == ""
    assert out.stderr == (
        f"tau: error: TAU_HOME={missing} is not a directory; create it or unset the variable\n"
    )


def _cli(tmp_path: Path, *argv: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tau_coding_agent.cli", *argv],
        env=child_env(tmp_path / "home", **env),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


@pytest.mark.parametrize(
    "argv", [["--help"], ["--version"], ["serve", "--help"], ["ext", "--help"]]
)
def test_help_and_version_work_with_a_bad_tau_home(tmp_path: Path, argv: list[str]) -> None:
    out = _cli(tmp_path, *argv, **{HOME_ENV: str(tmp_path / "typo")})
    assert out.returncode == 0, out.stderr
    assert out.stdout


@pytest.mark.parametrize("argv", [["-p", "hi"], ["--mode", "rpc"], ["ext", "list"], ["serve"]])
def test_a_real_command_refuses_a_bad_tau_home_in_one_line(tmp_path: Path, argv: list[str]) -> None:
    missing = tmp_path / "typo"
    out = _cli(tmp_path, *argv, **{HOME_ENV: str(missing)})
    assert out.returncode == 2
    assert out.stderr == (
        f"tau: error: TAU_HOME={missing} is not a directory; create it or unset the variable\n"
    )


def test_importing_the_cli_with_a_bad_tau_home_does_not_raise(tmp_path: Path) -> None:
    probe = "import tau_coding_agent.cli as c; c.build_parser(); print('ok')"
    out = subprocess.run(
        [sys.executable, "-c", probe],
        env=child_env(tmp_path / "home", **{HOME_ENV: str(tmp_path / "typo")}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == "ok\n"


def test_a_relative_tau_home_reaches_a_child_started_elsewhere(tmp_path: Path) -> None:
    """The head pins a relative ``TAU_HOME``, so a child run from ``/`` gets the same home."""
    (tmp_path / "work" / "scratch").mkdir(parents=True)
    probe = (
        "import os, subprocess, sys\n"
        "from tau_coding_agent import cli\n"
        "assert cli.main(['ext', 'list']) == 0\n"
        "child = 'import tau_coding_agent.config as c; print(c.default_config_path().parent)'\n"
        "out = subprocess.run([sys.executable, '-c', child], cwd='/', capture_output=True,"
        " text=True, check=True)\n"
        "print('CHILD', out.stdout.strip())\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path / "work",
        env=child_env(tmp_path / "home", **{HOME_ENV: "scratch"}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[-1] == f"CHILD {(tmp_path / 'work' / 'scratch').resolve()}"


def test_a_config_error_names_the_tau_home_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tau_coding_agent.headless import CLIError, resolve_extensions_config

    home = tmp_path / "throwaway"
    home.mkdir()
    monkeypatch.setenv(HOME_ENV, str(home))
    with pytest.raises(CLIError) as excinfo:
        resolve_extensions_config({"extensions": 5}, {})
    assert str(excinfo.value).startswith(f'{home.resolve() / "config.json"} "extensions"')
    assert "~/.tau" not in str(excinfo.value)


def test_the_cli_names_the_tau_home_config_file_in_a_store_error(tmp_path: Path) -> None:
    home = tmp_path / "throwaway"
    home.mkdir()
    (home / "config.json").write_text('{"session_store": 5}')
    out = _cli(tmp_path, "-p", "hi", **{HOME_ENV: str(home)})
    assert out.returncode != 0
    assert f'{home.resolve() / "config.json"} "session_store" must be a JSON object' in out.stderr
