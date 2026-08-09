"""``Settings`` — loading and layering ``~/.tau/settings.json`` and a
project-local ``{cwd}/.tau/settings.json`` (``tau_agent_core.settings``).

Was half of ``test_phase5_subphase2.py`` (87 tests, shared with session-op
tests now in ``test_session_operations.py`` — see that file's docstring for
why the split). This half was 25 of those tests, all one-assert-per-field or
one-scenario-per-test over the same two calls (``Settings.load()`` and
``Settings.load(cwd=...)``). Consolidated to 9 test functions (16 cases,
counting parametrize expansion).

``Settings`` doesn't import from ``session_manager``, so this file
contributes nothing to ``session_manager.py`` coverage — that gate is
measured on ``test_session_operations.py`` alone.

SANDBOXING: every test here patches ``pathlib.Path.home`` (via
``monkeypatch.setattr(Path, "home", ...)``) to a directory under ``tmp_path``
*before* calling ``Settings.load()``, so none of them can read the developer's
real ``~/.tau/settings.json`` — the same class of bug the repo owner already
fixed elsewhere (see CLAUDE.md's ``KNOWN-DEFECTS.md`` reference for the
sibling case in JMFTS). This was true of the tests carried over from the old
file too; verified during this split rather than found broken.

Reference: docs/PHASE-5-SUBPHASE-2.md (the original spec); settings.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tau_agent_core.settings import Settings


def _settings(tmp_path, monkeypatch, *, home=None, project=None, cwd=True):
    """Configure a fake ``~/.tau/settings.json`` (home) and project-local
    ``{cwd}/.tau/settings.json`` (project), then return Settings.load().

    `home`/`project` of None means "no settings file at that layer" — the
    directory is never even created, so ``Settings.load`` sees ``exists() is
    False`` exactly as it would on a machine that never configured tau.
    `cwd=False` calls ``Settings.load()`` with no cwd at all (global-only
    lookup).
    """
    home_dir = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home_dir)
    if home is not None:
        tau_dir = home_dir / ".tau"
        tau_dir.mkdir(parents=True, exist_ok=True)
        (tau_dir / "settings.json").write_text(json.dumps(home))

    if not cwd:
        return Settings.load()

    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    if project is not None:
        tau_dir = project_dir / ".tau"
        tau_dir.mkdir(parents=True, exist_ok=True)
        (tau_dir / "settings.json").write_text(json.dumps(project))
    return Settings.load(cwd=str(project_dir))


@pytest.mark.parametrize(
    "cwd_present",
    [False, True],
    ids=["no-cwd-argument", "cwd-given-but-no-project-settings-file"],
)
def test_defaults_are_used_when_no_settings_file_exists_anywhere(
    tmp_path, monkeypatch, cwd_present
):
    """Covers: no global file, no project file, and (parametrized) whether a
    cwd was even passed — all three previously separate tests reach the same
    ``Settings()`` defaults, so compare the whole object rather than field by
    field (that also exercises extension_dirs' Path.home()-derived default
    under the same patched home)."""
    settings = _settings(tmp_path, monkeypatch, cwd=cwd_present)
    assert settings == Settings()


@pytest.mark.parametrize(
    "home,project,expect_default_model",
    [
        pytest.param({}, None, "gpt-4o", id="empty-global-json-still-yields-defaults"),
        pytest.param(
            {"default_model": "claude-3"}, {}, "claude-3", id="empty-project-json-inherits-global"
        ),
    ],
)
def test_an_empty_json_object_falls_through_to_the_layer_below(
    tmp_path, monkeypatch, home, project, expect_default_model
):
    settings = _settings(tmp_path, monkeypatch, home=home, project=project)
    assert settings.default_model == expect_default_model


def test_load_reads_every_field_kind_from_settings_json(tmp_path, monkeypatch):
    """One test standing in for what used to be four (bool/int/float/null)
    plus the "multiple fields" and "global settings" tests — they were all
    the same call with different literals."""
    data = {
        "default_model": "claude-3",
        "temperature": 0.9,
        "max_retries": 5,
        "compaction_enabled": False,
        "context_margin": 3000,
        "tool_execution_mode": "sequential",
        "thinking_level": "high",
        "reasoning_level": "low",
        "max_tokens": 8192,
        "custom_system_prompt": None,
    }
    settings = _settings(tmp_path, monkeypatch, home=data)
    for field_name, expected in data.items():
        assert getattr(settings, field_name) == expected
    assert isinstance(settings.max_retries, int)
    assert isinstance(settings.temperature, float)


def test_unknown_fields_in_settings_json_are_silently_ignored(tmp_path, monkeypatch):
    settings = _settings(
        tmp_path, monkeypatch, home={"default_model": "gpt-4", "unknown_field": "x"}
    )
    assert settings.default_model == "gpt-4"
    assert not hasattr(settings, "unknown_field")


def test_invalid_json_raises_rather_than_falling_back_to_defaults(tmp_path, monkeypatch):
    """Fail-Early: a corrupt settings.json must raise, not silently produce a
    default Settings object — the standing repo rule against fallbacks."""
    home_dir = tmp_path / "home" / ".tau"
    home_dir.mkdir(parents=True)
    (home_dir / "settings.json").write_text("{invalid json}")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    with pytest.raises(json.JSONDecodeError):
        Settings.load()


@pytest.mark.parametrize(
    "home,project,expected",
    [
        pytest.param(
            {"default_model": "gpt-3.5-turbo"},
            {"default_model": "gpt-4o"},
            {"default_model": "gpt-4o"},
            id="project-overrides-global",
        ),
        pytest.param(
            {"default_model": "gpt-3.5"},
            {"temperature": 0.3},
            {"default_model": "gpt-3.5", "temperature": 0.3},
            id="project-adds-a-field-global-lacks",
        ),
        pytest.param(
            {"default_model": "gpt-3.5", "temperature": 0.5},
            {"default_model": "gpt-4o"},
            {"default_model": "gpt-4o", "temperature": 0.5},
            id="project-overrides-one-field-preserves-another",
        ),
        pytest.param(
            {"default_model": "claude-3"},
            None,
            {"default_model": "claude-3"},
            id="global-only-no-project-file",
        ),
        pytest.param(
            None,
            {"default_model": "claude-3"},
            {"default_model": "claude-3"},
            id="project-only-no-global-file",
        ),
    ],
)
def test_project_local_settings_layer_over_global(tmp_path, monkeypatch, home, project, expected):
    """The precedence chain (defaults -> global -> project), parametrized
    across the matrix of override/add/partial/absent — six near-duplicate
    tests collapsed into one."""
    settings = _settings(tmp_path, monkeypatch, home=home, project=project)
    for field_name, value in expected.items():
        assert getattr(settings, field_name) == value


@pytest.mark.parametrize(
    "home,project,expected",
    [
        pytest.param(
            {"api_keys": {"openai": "sk-global"}}, None, {"openai": "sk-global"}, id="global-only"
        ),
        pytest.param(
            {"api_keys": {"openai": "sk-global"}},
            {"api_keys": {"anthropic": "sk-project"}},
            {"openai": "sk-global", "anthropic": "sk-project"},
            id="merged-across-layers-not-replaced",
        ),
    ],
)
def test_api_keys_merge_across_layers(tmp_path, monkeypatch, home, project, expected):
    """api_keys is dict-merged (updated), unlike scalar fields which are
    replaced outright — a distinct code path from the override matrix above."""
    settings = _settings(tmp_path, monkeypatch, home=home, project=project)
    assert settings.api_keys == expected


def test_extension_dirs_are_appended_across_layers_not_replaced(tmp_path, monkeypatch):
    """extension_dirs is list-appended, and the built-in default entry
    (derived from Path.home()) is never dropped by either layer's merge."""
    settings = _settings(
        tmp_path,
        monkeypatch,
        home={"extension_dirs": ["/global/ext"]},
        project={"extension_dirs": ["/project/ext"]},
    )
    assert settings.extension_dirs[-2:] == ["/global/ext", "/project/ext"]
    assert any(".tau" in d for d in settings.extension_dirs[:-2])  # built-in default survives


def test_custom_system_prompt_is_loaded_from_global_settings(tmp_path, monkeypatch):
    settings = _settings(
        tmp_path, monkeypatch, home={"custom_system_prompt": "You are a helpful coding assistant."}
    )
    assert settings.custom_system_prompt == "You are a helpful coding assistant."
