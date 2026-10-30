"""Reading model config from layered files (D13): groups, merging, lookup, problems."""

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from tau_next import ConfigError, ConfigProblem, check_config, read_models

URL = "http://localhost:8100/v1"
OLD_KEY = "backend"  # rules: allow(backend): τ 0.15's key, which D13 rejects by name

TWO_ACCOUNTS = {
    "models": {
        "free_tokens": {
            "base_url": URL,
            "api_key_env": "MY_SUBSCRIPTION_KEY",
            "models": {"gpt-6-sol": {"model": "gpt-6-sol"}},
        },
        "pay_by_usage": {
            "base_url": URL,
            "api_key_env": "MY_BILLING_KEY",
            "models": {"gpt-6-sol": {"model": "gpt-6-sol"}},
        },
    }
}


def write(path: Path, data: object) -> Path:
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def errors(problems: tuple[ConfigProblem, ...]) -> list[ConfigProblem]:
    return [p for p in problems if p.severity == "error"]


def only_error(problems: tuple[ConfigProblem, ...]) -> ConfigProblem:
    found = errors(problems)
    assert len(found) == 1, found
    return found[0]


def with_url(tree: Mapping[str, object]) -> dict[str, object]:
    return {"base_url": URL, **tree}


def test_a_flat_file_resolves(tmp_path: Path) -> None:
    user = write(
        tmp_path / "c.json",
        {"default_model": "qwen", "models": {"qwen": {"model": "q", "base_url": URL}}},
    )
    catalog = read_models(user)
    assert catalog.names() == ("qwen",)
    assert catalog.default_model == "qwen"
    config = catalog.resolve("qwen")
    assert (config.model, config.base_url, config.provider) == ("q", URL, "openai")
    assert check_config(user) == ()


def test_groups_pass_keys_down_and_nest(tmp_path: Path) -> None:
    user = write(
        tmp_path / "c.json",
        {
            "models": {
                "home": {
                    "base_url": URL,
                    "api_key_env": "HOME_KEY",
                    "models": {
                        "big": {"model": "b"},
                        "lab": {"api_key_env": "LAB_KEY", "models": {"small": {"model": "s"}}},
                    },
                }
            }
        },
    )
    catalog = read_models(user)
    assert catalog.names() == ("home:big", "home:lab:small")
    assert catalog.resolve("home:big").api_key_env == "HOME_KEY"
    assert catalog.resolve("home:lab:small").api_key_env == "LAB_KEY"
    assert catalog.resolve("home:lab:small").base_url == URL


def test_the_same_bare_name_in_two_groups(tmp_path: Path) -> None:
    user = write(tmp_path / "c.json", TWO_ACCOUNTS)
    catalog = read_models(user)
    assert catalog.find("gpt-6-sol") == ("free_tokens:gpt-6-sol", "pay_by_usage:gpt-6-sol")
    assert catalog.resolve("free_tokens:gpt-6-sol").api_key_env == "MY_SUBSCRIPTION_KEY"
    assert catalog.resolve("pay_by_usage:gpt-6-sol").api_key_env == "MY_BILLING_KEY"


def test_lookup_by_qualified_name_bare_name_and_miss(tmp_path: Path) -> None:
    catalog = read_models(write(tmp_path / "c.json", TWO_ACCOUNTS))
    assert catalog.find("free_tokens:gpt-6-sol") == ("free_tokens:gpt-6-sol",)
    assert catalog.find("nope") == ()
    with pytest.raises(ConfigError, match="model 'nope' is not in your config"):
        catalog.resolve("nope")
    with pytest.raises(ConfigError, match="model 'gpt-6-sol' is not in your config"):
        catalog.resolve("gpt-6-sol")  # a bare name is a search term, not a name


def test_a_bare_name_with_one_match(tmp_path: Path) -> None:
    user = write(
        tmp_path / "c.json", {"models": {"g": {**with_url({}), "models": {"m": {"model": "x"}}}}}
    )
    assert read_models(user).find("m") == ("g:m",)


def test_project_tunes_the_users_model_by_qualified_name(tmp_path: Path) -> None:
    user = write(tmp_path / "user.json", TWO_ACCOUNTS)
    project = write(
        tmp_path / "project.json",
        {"models": {"pay_by_usage": {"models": {"gpt-6-sol": {"model": "gpt-6-terra"}}}}},
    )
    catalog = read_models(user, project)
    assert catalog.resolve("pay_by_usage:gpt-6-sol").model == "gpt-6-terra"
    assert catalog.resolve("free_tokens:gpt-6-sol").model == "gpt-6-sol"
    assert catalog.resolve("pay_by_usage:gpt-6-sol").api_key_env == "MY_BILLING_KEY"


def test_cli_beats_project_beats_user(tmp_path: Path) -> None:
    def layer(name: str, url: str) -> Path:
        return write(tmp_path / name, {"models": {"m": {"model": "x", "base_url": url}}})

    user, project = layer("u.json", "http://user/v1"), layer("p.json", "http://project/v1")
    assert read_models(user, project).resolve("m").base_url == "http://project/v1"
    cli = {"models": {"m": {"base_url": "http://cli/v1"}}}
    assert read_models(user, project, cli).resolve("m").base_url == "http://cli/v1"
    assert read_models(user, None, cli).resolve("m").base_url == "http://cli/v1"


def test_cli_alone_can_define_a_model() -> None:
    cli = {"models": {"m": {"model": "x", "base_url": URL}}, "default_model": "m"}
    assert read_models(cli=cli).resolve("m").model == "x"


def test_null_drops_an_inherited_key(tmp_path: Path) -> None:
    user = write(
        tmp_path / "u.json",
        {"models": {"g": {"base_url": URL, "api_key_env": "K", "models": {"m": {"model": "x"}}}}},
    )
    project = write(tmp_path / "p.json", {"models": {"g": {"api_key_env": None, "models": {}}}})
    assert read_models(user).resolve("g:m").api_key_env == "K"
    assert read_models(user, project).resolve("g:m").api_key_env is None
    # and a null on the model itself drops the group's value for that model only
    cli = {"models": {"g": {"models": {"m": {"api_key_env": None}, "n": {"model": "y"}}}}}
    catalog = read_models(user, cli=cli)
    assert catalog.resolve("g:m").api_key_env is None
    assert catalog.resolve("g:n").api_key_env == "K"


def test_null_drops_a_whole_model(tmp_path: Path) -> None:
    user = write(tmp_path / "u.json", {"models": {"a": {"model": "x", "base_url": URL}}})
    project = write(tmp_path / "p.json", {"models": {"a": None}})
    assert read_models(user, project).names() == ()


def test_scalars_replace_and_objects_merge(tmp_path: Path) -> None:
    user = write(tmp_path / "u.json", {"models": {"a": {"model": "x", "base_url": URL}}})
    project = write(
        tmp_path / "p.json", {"models": {"a": {"model": "y"}, "b": {"model": "z", "base_url": URL}}}
    )
    catalog = read_models(user, project)
    assert catalog.names() == ("a", "b")
    assert (catalog.resolve("a").model, catalog.resolve("a").base_url) == ("y", URL)


def test_explain_names_the_layer_and_group_of_each_value(tmp_path: Path) -> None:
    user = write(
        tmp_path / "u.json",
        {"models": {"g": {"base_url": URL, "api_key_env": "K", "models": {"m": {"model": "x"}}}}},
    )
    project = write(tmp_path / "p.json", {"models": {"g": {"models": {"m": {"model": "y"}}}}})
    cli = {"models": {"g": {"api_key_env": "CLI_KEY", "models": {}}}}
    origins = {o.key: o for o in read_models(user, project, cli).explain("g:m")}
    assert (origins["model"].source, origins["model"].group) == (str(project), None)
    assert (origins["base_url"].source, origins["base_url"].group) == (str(user), "g")
    assert (origins["api_key_env"].source, origins["api_key_env"].group) == ("cli", "g")


def test_explain_reports_the_innermost_group(tmp_path: Path) -> None:
    user = write(
        tmp_path / "u.json",
        {
            "models": {
                "a": {
                    "base_url": URL,
                    "models": {"b": {"base_url": URL + "x", "models": {"m": {"model": "x"}}}},
                }
            }
        },
    )
    (origin,) = [o for o in read_models(user).explain("a:b:m") if o.key == "base_url"]
    assert origin.group == "a:b"


def test_invalid_json_has_file_line_and_column(tmp_path: Path) -> None:
    user = write(tmp_path / "c.json", '{\n  "models": {\n    "a": oops\n  }\n}\n')
    problem = only_error(check_config(user))
    assert (problem.source, problem.line, problem.column) == (str(user), 3, 10)
    assert "invalid JSON" in problem.message
    with pytest.raises(ConfigError, match=r"c\.json:3:10"):
        read_models(user)


def test_a_syntax_error_in_one_layer_does_not_hide_another_layers_problem(tmp_path: Path) -> None:
    user = write(tmp_path / "u.json", "{")
    project = write(tmp_path / "p.json", {"models": {"a:b": {"model": "x", "base_url": URL}}})
    assert len(errors(check_config(user, project))) == 2


def test_duplicate_keys_in_one_object(tmp_path: Path) -> None:
    user = write(
        tmp_path / "c.json",
        '{"models": {"a": {"model": "x", "model": "y", "base_url": "' + URL + '"}}}',
    )
    problem = only_error(check_config(user))
    assert "duplicate key 'model'" in problem.message
    assert problem.path == "models.a"


def test_unknown_key_has_path_and_suggestion(tmp_path: Path) -> None:
    user = write(
        tmp_path / "c.json", {"models": {"a": {"modle": "x", "base_url": URL, "model": "m"}}}
    )
    problem = only_error(check_config(user))
    assert problem.path == "models.a.modle"
    assert "did you mean 'model'?" in problem.message


def test_unknown_key_at_the_top_and_in_a_group(tmp_path: Path) -> None:
    user = write(
        tmp_path / "c.json", {"modles": {}, "models": {"g": {"base_ur": URL, "models": {}}}}
    )
    paths = {p.path: p.message for p in errors(check_config(user))}
    assert "did you mean 'models'?" in paths["modles"]
    assert "did you mean 'base_url'?" in paths["models.g.base_ur"]


def test_unknown_key_far_from_any_known_one_lists_them(tmp_path: Path) -> None:
    user = write(
        tmp_path / "c.json", {"models": {"a": {"zzzzzz": 1, "model": "m", "base_url": URL}}}
    )
    assert "known keys are" in only_error(check_config(user)).message


def test_colon_in_a_name(tmp_path: Path) -> None:
    user = write(tmp_path / "c.json", {"models": {"a:b": {"model": "x", "base_url": URL}}})
    problem = only_error(check_config(user))
    assert "must not contain ':'" in problem.message
    assert problem.path == "models.a:b"


def test_missing_model_and_base_url_after_merge(tmp_path: Path) -> None:
    user = write(tmp_path / "c.json", {"models": {"a": {}}})
    messages = [p.message for p in errors(check_config(user))]
    assert messages == [
        f"model 'a' (from {user}) is missing 'model'",
        f"model 'a' (from {user}) is missing 'base_url'",
    ]
    # but a group can supply them
    ok = write(
        tmp_path / "ok.json",
        {"models": {"g": {"model": "m", "base_url": URL, "models": {"a": {}}}}},
    )
    assert check_config(ok) == ()


def test_wrong_types(tmp_path: Path) -> None:
    user = write(
        tmp_path / "c.json",
        {
            "models": {
                "a": {"model": 3, "base_url": URL},
                "b": 7,
                "c": {"model": "m", "base_url": [URL]},
            }
        },
    )
    found = {p.path: p.message for p in errors(check_config(user))}
    assert found["models.a.model"] == "'model' must be a string"
    assert found["models.b"] == "must be an object"
    assert found["models.c.base_url"] == "'base_url' must be a string"


def test_wrong_types_at_the_top(tmp_path: Path) -> None:
    assert (
        "'models' must be an object"
        in only_error(check_config(write(tmp_path / "a.json", {"models": []}))).message
    )
    assert (
        "'default_model' must be a string"
        in only_error(check_config(write(tmp_path / "b.json", {"default_model": 3}))).message
    )
    assert (
        "must be a JSON object"
        in only_error(check_config(write(tmp_path / "c.json", "[]"))).message
    )


def test_a_bad_url_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    user = write(tmp_path / "c.json", {"models": {"a": {"model": "m", "base_url": "localhost"}}})
    assert "http" in only_error(check_config(user)).message


def test_unknown_provider(tmp_path: Path) -> None:
    user = write(
        tmp_path / "c.json", {"models": {"a": {"model": "m", "base_url": URL, "provider": "acme"}}}
    )
    assert "unknown provider 'acme'" in only_error(check_config(user)).message
    ok = write(
        tmp_path / "ok.json",
        {"models": {"a": {"model": "m", "base_url": URL, "provider": "openai"}}},
    )
    assert read_models(ok).resolve("a").provider == "openai"


def test_default_model_must_exist(tmp_path: Path) -> None:
    user = write(tmp_path / "c.json", {"default_model": "ghost", "models": {}})
    assert "default_model 'ghost' is not in your config" in only_error(check_config(user)).message


def test_unset_api_key_variable_is_a_warning_given_an_environ(tmp_path: Path) -> None:
    user = write(
        tmp_path / "c.json",
        {"models": {"a": {"model": "m", "base_url": URL, "api_key_env": "SOME_KEY"}}},
    )
    assert check_config(user) == ()
    (problem,) = check_config(user, environ={})
    assert problem.severity == "warning"
    assert "SOME_KEY" in problem.message
    assert check_config(user, environ={"SOME_KEY": "x"}) == ()
    read_models(user)  # warnings never fail a load


def test_a_literal_key_works_but_warns_and_is_never_reported(tmp_path: Path) -> None:
    secret = "sk-very-secret"
    user = write(
        tmp_path / "c.json", {"models": {"a": {"model": "m", "base_url": URL, "api_key": secret}}}
    )
    (problem,) = check_config(user)
    assert problem.severity == "warning"
    catalog = read_models(user)
    assert catalog.warnings == (problem,)
    config = catalog.resolve("a")
    assert config.api_key({}) == secret
    assert config.api_key_env is None
    shown = [str(problem), repr(config), repr(catalog.explain("a")), repr(catalog)]
    assert not any(secret in text for text in shown)


def test_the_renamed_provider_key(tmp_path: Path) -> None:
    user = write(
        tmp_path / "c.json",
        {"models": {"a": {"model": "m", "base_url": URL, OLD_KEY: "openai"}}},
    )
    problem = only_error(check_config(user))
    assert problem.message == "`backend` is now `provider`"  # rules: allow(backend): 0.15's key
    assert problem.path == f"models.a.{OLD_KEY}"


def test_the_checker_collects_every_problem(tmp_path: Path) -> None:
    user = write(
        tmp_path / "c.json",
        {"models": {"a": {"modle": "x"}, "b:c": {}, "d": {"model": 1, "base_url": URL}}},
    )
    assert len(errors(check_config(user))) >= 4


def test_loading_fails_early_listing_every_error(tmp_path: Path) -> None:
    user = write(
        tmp_path / "c.json",
        {"models": {"a": {"modle": "x", "model": "m", "base_url": URL}, "b:c": {}}},
    )
    with pytest.raises(ConfigError) as raised:
        read_models(user)
    text = str(raised.value)
    assert "modle" in text
    assert "b:c" in text


def test_an_unreadable_file_is_a_problem(tmp_path: Path) -> None:
    problem = only_error(check_config(tmp_path / "missing.json"))
    assert "cannot read" in problem.message
    assert problem.source == str(tmp_path / "missing.json")


def test_no_layers_is_an_empty_catalog() -> None:
    catalog = read_models()
    assert catalog.names() == ()
    assert catalog.default_model is None
