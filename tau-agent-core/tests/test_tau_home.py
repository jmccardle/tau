"""resolve_tau_home: the home rule every head shares (docs/PORTABLE-HOME.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tau_agent_core.resources import resource_locations
from tau_agent_core.sdk import load_project_context_files
from tau_agent_core.settings import Settings
from tau_agent_core.tau_home import (
    HOME_ENV,
    PORTABLE_BASE_ENV,
    TauHomeError,
    pin_home_variables,
    resolve_tau_home,
)


def test_unset_is_the_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_tau_home({}) == tmp_path / ".tau"


def test_empty_value_is_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_tau_home({PORTABLE_BASE_ENV: ""}) == tmp_path / ".tau"


def test_portable_dir_wins_over_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    drive = tmp_path / "drive" / "tau"
    (drive / ".tau").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert resolve_tau_home({PORTABLE_BASE_ENV: str(drive)}) == (drive / ".tau").resolve()


def test_base_without_dot_tau_is_an_ordinary_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    install = tmp_path / "opt" / "tau"
    install.mkdir(parents=True)
    assert resolve_tau_home({PORTABLE_BASE_ENV: str(install)}) == tmp_path / "home" / ".tau"


def test_base_that_is_not_a_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(TauHomeError, match=PORTABLE_BASE_ENV):
        resolve_tau_home({PORTABLE_BASE_ENV: str(tmp_path / "missing")})


def test_settings_read_the_portable_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drive = tmp_path / "drive"
    (drive / ".tau").mkdir(parents=True)
    (drive / ".tau" / "settings.json").write_text('{"default_model": "from-the-drive"}')
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(PORTABLE_BASE_ENV, str(drive))
    settings = Settings.load()
    assert settings.default_model == "from-the-drive"
    assert settings.extension_dirs == [str((drive / ".tau").resolve() / "extensions")]


def test_tau_home_wins_over_portable_base_and_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "drive"
    (drive / ".tau").mkdir(parents=True)
    explicit = tmp_path / "throwaway"
    explicit.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    env = {HOME_ENV: str(explicit), PORTABLE_BASE_ENV: str(drive)}
    assert resolve_tau_home(env) == explicit.resolve()


def test_empty_tau_home_is_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_tau_home({HOME_ENV: ""}) == tmp_path / ".tau"


def test_missing_tau_home_raises_even_with_a_portable_home(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    (drive / ".tau").mkdir(parents=True)
    missing = tmp_path / "typo"
    env = {HOME_ENV: str(missing), PORTABLE_BASE_ENV: str(drive)}
    with pytest.raises(TauHomeError) as excinfo:
        resolve_tau_home(env)
    assert str(excinfo.value) == (
        f"TAU_HOME={missing} is not a directory; create it or unset the variable"
    )


def test_tau_home_that_is_a_file_raises(tmp_path: Path) -> None:
    afile = tmp_path / "file"
    afile.write_text("")
    with pytest.raises(TauHomeError, match=HOME_ENV):
        resolve_tau_home({HOME_ENV: str(afile)})


def test_project_layer_still_reads_under_tau_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "throwaway"
    explicit.mkdir()
    (explicit / "AGENTS.md").write_text("from the home")
    project = tmp_path / "proj"
    (project / ".tau" / "prompts").mkdir(parents=True)
    (project / ".tau" / "SYSTEM.md").write_text("from the project")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(HOME_ENV, str(explicit))
    locations = resource_locations("prompts", cwd=str(project), project=True)
    assert locations.user == explicit.resolve() / "prompts"
    assert locations.project == (project / ".tau" / "prompts").resolve()
    contents = [f.content for f in load_project_context_files(cwd=project)]
    assert contents == ["from the home", "from the project"]


def test_pin_makes_relative_home_variables_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    env = {HOME_ENV: "scratch", PORTABLE_BASE_ENV: "drive/tau", "OTHER": "rel"}
    pin_home_variables(env)
    assert env == {
        HOME_ENV: str(tmp_path / "scratch"),
        PORTABLE_BASE_ENV: str(tmp_path / "drive" / "tau"),
        "OTHER": "rel",
    }


def test_pin_leaves_absolute_empty_and_unset_values_alone(tmp_path: Path) -> None:
    env = {HOME_ENV: str(tmp_path), PORTABLE_BASE_ENV: ""}
    pin_home_variables(env)
    assert env == {HOME_ENV: str(tmp_path), PORTABLE_BASE_ENV: ""}


def test_pin_resolves_like_resolve_tau_home_through_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``lnk/../h`` pins to the same directory ``resolve_tau_home`` would pick."""
    (tmp_path / "real" / "inner").mkdir(parents=True)
    (tmp_path / "real" / "h").mkdir()
    (tmp_path / "lnk").symlink_to(tmp_path / "real" / "inner")
    monkeypatch.chdir(tmp_path)
    env = {HOME_ENV: "lnk/../h"}
    expected = resolve_tau_home(env)
    pin_home_variables(env)
    assert env[HOME_ENV] == str(expected) == str(tmp_path / "real" / "h")
