"""Config layers: kind conflicts, key exclusivity, secrets, and cascades (D13 review)."""

import json
from pathlib import Path

import pytest

from tau_next import ConfigError, ConfigProblem, check_config, read_models

URL = "http://localhost:8100/v1"
OLD_KEY = "backend"  # rules: allow(backend): τ 0.15's key, which D13 rejects by name


def write(path: Path, data: object) -> Path:
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def errors(problems: tuple[ConfigProblem, ...]) -> list[ConfigProblem]:
    return [p for p in problems if p.severity == "error"]


def only_error(problems: tuple[ConfigProblem, ...]) -> ConfigProblem:
    found = errors(problems)
    assert len(found) == 1, found
    return found[0]


RENAMED = "`backend` is now `provider`"  # rules: allow(backend): 0.15's key, rejected by name
MODEL = {"model": "m", "base_url": URL}


def test_a_later_layer_giving_models_to_a_model_is_an_error(tmp_path: Path) -> None:
    user = write(tmp_path / "model.json", {"models": {"x": MODEL}})
    project = write(tmp_path / "group.json", {"models": {"x": {**MODEL, "models": {}}}})
    problem = only_error(check_config(user, project))
    assert problem.path == "models.x"
    assert problem.message == f"'x' is a model in {user} and a group in {project}"
    with pytest.raises(ConfigError, match="'x' is a model"):
        read_models(user, project)


def test_a_project_tunes_a_user_group_without_saying_models(tmp_path: Path) -> None:
    user = write(
        tmp_path / "u.json",
        {"models": {"free_tokens": {**MODEL, "models": {"gpt": {"model": "gpt"}}}}},
    )
    project = write(tmp_path / "p.json", {"models": {"free_tokens": {"api_key_env": "MINE"}}})
    catalog = read_models(user, project)
    assert catalog.names() == ("free_tokens:gpt",)
    assert catalog.resolve("free_tokens:gpt").api_key_env == "MINE"
    origin = {o.key: o for o in catalog.explain("free_tokens:gpt")}["api_key_env"]
    assert (origin.source, origin.group) == (str(project), "free_tokens")


def test_a_nested_kind_conflict_names_the_full_path(tmp_path: Path) -> None:
    user = write(tmp_path / "u.json", {"models": {"g": {"models": {"x": MODEL}}}})
    cli = {"models": {"g": {"models": {"x": {"models": {}}}}}}
    assert only_error(check_config(user, cli=cli)).path == "models.g.models.x"


def test_a_null_lets_a_later_layer_redefine_the_kind(tmp_path: Path) -> None:
    user = write(tmp_path / "u.json", {"models": {"x": MODEL}})
    cli = {"models": {"x": None}}
    assert check_config(user, cli=cli) == ()
    project = write(tmp_path / "p.json", {"models": {"x": None}})
    cli2 = {"models": {"x": {"models": {"y": MODEL}}}}
    assert read_models(user, project, cli2).names() == ("x:y",)


def test_a_nearer_api_key_env_beats_an_inherited_literal_key(tmp_path: Path) -> None:
    user = write(
        tmp_path / "u.json",
        {"models": {"g": {"base_url": URL, "api_key": "sk-1", "models": {"m": {"model": "x"}}}}},
    )
    cli = {"models": {"g": {"models": {"m": {"api_key_env": "NAMED"}}}}}
    config = read_models(user, cli=cli).resolve("g:m")
    assert config.api_key_env == "NAMED"
    assert config.api_key({"NAMED": "from-env"}) == "from-env"


def test_a_nearer_literal_key_beats_an_inherited_api_key_env(tmp_path: Path) -> None:
    user = write(
        tmp_path / "u.json",
        {"models": {"g": {"api_key_env": "NAMED", "models": {"m": MODEL}}}},
    )
    project = write(tmp_path / "p.json", {"models": {"g": {"models": {"m": {"api_key": "sk-2"}}}}})
    catalog = read_models(user, project)
    assert catalog.resolve("g:m").api_key_env is None
    assert catalog.resolve("g:m").api_key({}) == "sk-2"
    assert {o.key for o in catalog.explain("g:m")} >= {"api_key"}
    assert "api_key_env" not in {o.key for o in catalog.explain("g:m")}


def test_a_later_layer_switching_a_models_key_kind(tmp_path: Path) -> None:
    user = write(tmp_path / "u.json", {"models": {"m": {**MODEL, "api_key": "sk-1"}}})
    cli = {"models": {"m": {"api_key_env": "NAMED"}}}
    config = read_models(user, cli=cli).resolve("m")
    assert (config.api_key_env, config.literal_api_key) == ("NAMED", None)


def test_one_object_setting_both_key_spellings_is_an_error(tmp_path: Path) -> None:
    user = write(
        tmp_path / "u.json", {"models": {"m": {**MODEL, "api_key": "k", "api_key_env": "E"}}}
    )
    assert "sets both api_key and api_key_env" in only_error(check_config(user)).message


@pytest.mark.parametrize("pasted", ["sk-proj-abc123SECRET", "has space", "9starts_with_digit", ""])
def test_api_key_env_must_be_a_variable_name_and_never_echoes_the_value(
    tmp_path: Path, pasted: str
) -> None:
    user = write(tmp_path / "u.json", {"models": {"x": {**MODEL, "api_key_env": pasted}}})
    problems = check_config(user)
    problem = only_error(problems)
    assert pasted not in str(problem) or pasted == ""
    if pasted:
        assert "is not a variable name" in problem.message
        assert "'x'" in problem.message
    with pytest.raises(ConfigError) as raised:
        read_models(user)
    assert pasted not in str(raised.value) or pasted == ""


def test_a_pasted_key_in_a_group_is_reported_once(tmp_path: Path) -> None:
    secret = "sk-proj-abc123SECRET"
    user = write(
        tmp_path / "u.json",
        {"models": {"g": {"api_key_env": secret, "models": {"a": MODEL, "b": MODEL}}}},
    )
    problem = only_error(check_config(user))
    assert problem.path == "models.g.api_key_env"
    assert secret not in str(problem)


def test_an_invalid_group_value_is_not_repeated_as_missing_on_each_model(tmp_path: Path) -> None:
    user = write(
        tmp_path / "u.json",
        {"models": {"g": {"base_url": 5, "models": {"a": {"model": "x"}, "b": {"model": "y"}}}}},
    )
    problem = only_error(check_config(user))
    assert problem.path == "models.g.base_url"


def test_a_bad_group_provider_is_reported_once(tmp_path: Path) -> None:
    user = write(
        tmp_path / "u.json",
        {"models": {"g": {**MODEL, "provider": "acme", "models": {"a": {}, "b": {}}}}},
    )
    assert only_error(check_config(user)).path == "models.g.provider"


def test_default_model_is_checked_even_when_other_things_are_wrong(tmp_path: Path) -> None:
    user = write(
        tmp_path / "u.json",
        {"default_model": "ghost", "models": {"a": {"modle": "x"}}},
    )
    messages = [p.message for p in errors(check_config(user))]
    assert any("default_model 'ghost' is not in your config" in m for m in messages)
    assert any("unknown key" in m for m in messages)


def test_default_model_may_name_a_model_that_has_other_errors(tmp_path: Path) -> None:
    user = write(tmp_path / "u.json", {"default_model": "a", "models": {"a": {}}})
    assert not any(p.path == "default_model" for p in check_config(user))


def test_an_ambiguous_default_model_lists_the_candidates(tmp_path: Path) -> None:
    user = write(
        tmp_path / "u.json",
        {
            "default_model": "m",
            "models": {"g": {"models": {"m": MODEL}}, "h": {"models": {"m": MODEL}}},
        },
    )
    message = only_error(check_config(user)).message
    assert "ambiguous" in message
    assert "'g:m'" in message
    assert "'h:m'" in message
    ok = write(tmp_path / "ok.json", {**json.loads(user.read_text()), "default_model": "g:m"})
    assert read_models(ok).default_model == "g:m"


def test_null_on_an_unknown_key_is_still_unknown(tmp_path: Path) -> None:
    user = write(tmp_path / "u.json", {"models": {"x": {**MODEL, "modle": None}}})
    problem = only_error(check_config(user))
    assert problem.path == "models.x.modle"
    assert "did you mean 'model'?" in problem.message


def test_null_on_the_renamed_key_still_gets_its_message(tmp_path: Path) -> None:
    user = write(tmp_path / "u.json", {"models": {"x": {**MODEL, OLD_KEY: None}}})
    assert only_error(check_config(user)).message == RENAMED


def test_null_on_an_unknown_top_level_key_is_still_unknown(tmp_path: Path) -> None:
    user = write(tmp_path / "u.json", {"modles": None, "models": {}})
    assert "did you mean 'models'?" in only_error(check_config(user)).message


def test_models_null_on_a_group_drops_its_inherited_models(tmp_path: Path) -> None:
    user = write(
        tmp_path / "u.json",
        {"models": {"g": {"models": {"m": MODEL}}, "h": {"models": {"n": MODEL}}}},
    )
    cli = {"models": {"g": {"models": None}}}
    assert check_config(user, cli=cli) == ()
    assert read_models(user, cli=cli).names() == ("h:n",)


def test_a_duplicate_key_reports_the_path_of_its_object(tmp_path: Path) -> None:
    a = json.dumps({"model": "m", "base_url": URL})
    text = '{"models": {"a": ' + a + ', "g": {"models": {}, "models": {}}}}'
    problem = only_error(check_config(write(tmp_path / "u.json", text)))
    assert problem.path == "models.g"
    assert "duplicate key 'models'" in problem.message


def test_empty_names_are_errors(tmp_path: Path) -> None:
    user = write(tmp_path / "u.json", {"models": {"": MODEL, "g": {"models": {"": MODEL}}}})
    paths = [p.path for p in errors(check_config(user))]
    assert paths == ["models.", "models.g.models."]


def test_a_bare_name_matches_top_level_and_grouped_models(tmp_path: Path) -> None:
    user = write(tmp_path / "u.json", {"models": {"y": MODEL, "g": {"models": {"y": MODEL}}}})
    catalog = read_models(user)
    assert catalog.find("y") == ("y", "g:y")
    assert catalog.find("g:y") == ("g:y",)
    assert catalog.find("h:y") == ()


def test_the_default_provider_shows_in_explain_as_the_default(tmp_path: Path) -> None:
    user = write(tmp_path / "u.json", {"models": {"m": MODEL}})
    origins = {o.key: o for o in read_models(user).explain("m")}
    assert (origins["provider"].source, origins["provider"].group) == ("default", None)
    explicit = write(tmp_path / "e.json", {"models": {"m": {**MODEL, "provider": "openai"}}})
    assert {o.key: o for o in read_models(explicit).explain("m")}["provider"].source == str(
        explicit
    )


def test_a_missing_key_names_the_layer_that_introduced_the_model(tmp_path: Path) -> None:
    user = write(tmp_path / "u.json", {"models": {"m": {"base_url": URL}}})
    cli = {"models": {"m": {"api_key_env": "K"}}}
    problem = only_error(check_config(user, cli=cli))
    assert problem.source == str(user)
    assert str(user) in problem.message
