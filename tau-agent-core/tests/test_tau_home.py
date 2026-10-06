"""resolve_tau_home: the portable ``.tau/`` rule every head shares (docs/PORTABLE-HOME.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tau_agent_core.settings import Settings
from tau_agent_core.tau_home import PORTABLE_BASE_ENV, TauHomeError, resolve_tau_home


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
