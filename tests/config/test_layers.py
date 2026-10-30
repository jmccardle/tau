"""Layered model config (D13): groups, merging, lookup, problems.

Merging is read through `load_config` (user file, trusted project file, overrides);
problems through `check_config` and `tau config check`, which report every problem.
"""

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from world import PROVIDERS, URL, World, tau, trust

from tau_next import (
    ConfigError,
    ConfigProblem,
    ModelCatalog,
    ModelConfig,
    check_config,
    load_config,
    read_models,
)

OLD_KEY = "backend"  # rules: allow(backend): τ 0.15's key, which D13 rejects by name
MODEL = {"model": "m", "base_url": URL}
ACCOUNTS = {
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


def load(
    world: World,
    user: object | None = None,
    project: object | None = None,
    cli: Mapping[str, object] | None = None,
) -> ModelCatalog:
    """The catalog a frontend gets: the user's file, a trusted project's, then `cli`."""
    if user is not None:
        world.user_config(user)
    root = world.home
    if project is not None:
        root = world.home / "proj"
        (root / ".tau").mkdir(parents=True, exist_ok=True)
        write(root / ".tau" / "config.json", project)
        trust(world, root)
    return load_config(root, world.environ, ask=None, cli=cli).catalog


def write(path: Path, data: object) -> Path:
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def errors(problems: tuple[ConfigProblem, ...]) -> list[ConfigProblem]:
    return [p for p in problems if p.severity == "error"]


def only_error(problems: tuple[ConfigProblem, ...]) -> ConfigProblem:
    found = errors(problems)
    assert len(found) == 1, found
    return found[0]


def check(
    tmp_path: Path, data: object, environ: dict[str, str] | None = None
) -> tuple[ConfigProblem, ...]:
    return check_config(write(tmp_path / "c.json", data), environ=environ, providers=PROVIDERS)


# -- reading and lookup


def test_a_flat_file_resolves_and_is_sound(world: World, tmp_path: Path) -> None:
    config = {"default_model": "qwen", "models": {"qwen": {"model": "q", "base_url": URL}}}
    catalog = load(world, config)
    assert catalog.names() == ("qwen",)
    assert catalog.default_model == "qwen"
    resolved = catalog.resolve("qwen")
    assert (resolved.model, resolved.base_url, resolved.provider) == ("q", URL, "openai")
    assert check(tmp_path, config) == ()


def test_no_config_at_all_is_an_empty_catalog(world: World) -> None:
    catalog = load_config(world.home, world.environ, ask=None).catalog
    assert catalog.names() == ()
    assert catalog.default_model is None


def test_groups_pass_keys_down_and_nest(world: World) -> None:
    catalog = load(
        world,
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
    assert catalog.names() == ("home:big", "home:lab:small")
    assert catalog.resolve("home:big").api_key_env == "HOME_KEY"
    assert catalog.resolve("home:lab:small").api_key_env == "LAB_KEY"
    assert catalog.resolve("home:lab:small").base_url == URL


def test_qualified_names_tell_apart_one_bare_name_in_two_groups(world: World) -> None:
    catalog = load(world, ACCOUNTS)
    assert catalog.find("gpt-6-sol") == ("free_tokens:gpt-6-sol", "pay_by_usage:gpt-6-sol")
    assert catalog.find("free_tokens:gpt-6-sol") == ("free_tokens:gpt-6-sol",)
    assert catalog.find("nope") == ()
    assert catalog.resolve("pay_by_usage:gpt-6-sol").api_key_env == "MY_BILLING_KEY"
    with pytest.raises(ConfigError, match="model 'gpt-6-sol' is not in your config"):
        catalog.resolve("gpt-6-sol")  # a bare name is a search term, not a name


def test_a_bare_name_matches_top_level_and_grouped_models(world: World) -> None:
    catalog = load(world, {"models": {"y": MODEL, "g": {"models": {"y": MODEL}}}})
    assert catalog.find("y") == ("y", "g:y")
    assert catalog.find("h:y") == ()


def test_an_ambiguous_bare_name_fails_a_script_listing_both(world: World) -> None:
    world.user_config(ACCOUNTS)
    result = tau(world, "config", "check", "--show", "gpt-6-sol")
    assert result.code == 1
    assert "free_tokens:gpt-6-sol" in result.err
    assert "pay_by_usage:gpt-6-sol" in result.err


# -- merging


def test_cli_beats_project_beats_user(world: World) -> None:
    user = {"models": {"m": {"model": "x", "base_url": "http://user/v1"}}}
    project = {"models": {"m": {"base_url": "http://project/v1"}}}
    cli = {"models": {"m": {"base_url": "http://cli/v1"}}}
    assert load(world, user).resolve("m").base_url == "http://user/v1"
    assert load(world, user, project).resolve("m").base_url == "http://project/v1"
    assert load(world, user, project, cli).resolve("m").base_url == "http://cli/v1"


def test_the_project_tunes_the_users_model_and_group_without_replacing_them(
    world: World,
) -> None:
    project = {
        "models": {
            "pay_by_usage": {"models": {"gpt-6-sol": {"model": "gpt-6-terra"}}},
            "free_tokens": {"api_key_env": "MINE"},
        }
    }
    catalog = load(world, ACCOUNTS, project)
    assert catalog.resolve("pay_by_usage:gpt-6-sol").model == "gpt-6-terra"
    assert catalog.resolve("pay_by_usage:gpt-6-sol").api_key_env == "MY_BILLING_KEY"
    assert catalog.resolve("free_tokens:gpt-6-sol").api_key_env == "MINE"
    assert catalog.names() == ("free_tokens:gpt-6-sol", "pay_by_usage:gpt-6-sol")


def test_objects_merge_and_scalars_replace(world: World) -> None:
    user = {"models": {"a": {"model": "x", "base_url": URL}}}
    project = {"models": {"a": {"model": "y"}, "b": {"model": "z", "base_url": URL}}}
    catalog = load(world, user, project)
    assert catalog.names() == ("a", "b")
    assert (catalog.resolve("a").model, catalog.resolve("a").base_url) == ("y", URL)


def test_null_drops_an_inherited_key_a_model_or_a_groups_models(world: World) -> None:
    user = {
        "models": {
            "g": {"base_url": URL, "api_key_env": "K", "models": {"m": {"model": "x"}}},
            "h": {"models": {"n": MODEL}},
            "a": MODEL,
        }
    }
    project = {"models": {"g": {"api_key_env": None}, "h": {"models": None}, "a": None}}
    assert load(world, user).resolve("g:m").api_key_env == "K"
    catalog = load(world, user, project)
    assert catalog.resolve("g:m").api_key_env is None
    assert catalog.names() == ("g:m",)
    # a null on the model itself drops the group's value for that model only
    cli = {"models": {"g": {"models": {"m": {"api_key_env": None}, "n": {"model": "y"}}}}}
    both = load_config(world.home, world.environ, ask=None, cli=cli).catalog
    assert both.resolve("g:m").api_key_env is None
    assert both.resolve("g:n").api_key_env == "K"


def test_the_first_layer_decides_model_or_group_and_a_later_models_on_a_model_is_an_error(
    world: World, tmp_path: Path
) -> None:
    user = write(tmp_path / "model.json", {"models": {"x": MODEL}})
    project = write(tmp_path / "group.json", {"models": {"x": {**MODEL, "models": {}}}})
    problem = only_error(check_config(user, project, providers=PROVIDERS))
    assert problem.path == "models.x"
    assert str(user) in problem.message
    assert str(project) in problem.message
    nested = only_error(
        check_config(
            write(tmp_path / "u.json", {"models": {"g": {"models": {"x": MODEL}}}}),
            cli={"models": {"g": {"models": {"x": {"models": {}}}}}},
            providers=PROVIDERS,
        )
    )
    assert nested.path == "models.g.models.x"
    # a null in between lets a later layer redefine the kind
    cli = {"models": {"x": {"models": {"y": MODEL}}}}
    catalog = load(world, {"models": {"x": MODEL}}, {"models": {"x": None}}, cli)
    assert catalog.names() == ("x:y",)


def test_the_nearer_api_key_or_api_key_env_wins(world: World) -> None:
    group = {"base_url": URL, "api_key": "sk-1", "models": {"m": {"model": "x"}}}
    named = {"models": {"g": {"models": {"m": {"api_key_env": "NAMED"}}}}}
    config = load(world, {"models": {"g": group}}, named).resolve("g:m")
    assert config.api_key_env == "NAMED"
    assert config.api_key({"NAMED": "from-env"}) == "from-env"
    literal = {"models": {"g": {"models": {"m": {"api_key": "sk-2"}}}}}
    env_group = {"models": {"g": {"api_key_env": "NAMED", "models": {"m": MODEL}}}}
    config = load(world, env_group, literal).resolve("g:m")
    assert config.api_key_env is None
    assert config.api_key({}) == "sk-2"


def test_one_object_setting_both_key_spellings_is_an_error(tmp_path: Path) -> None:
    problem = only_error(
        check(tmp_path, {"models": {"m": {**MODEL, "api_key": "k", "api_key_env": "E"}}})
    )
    assert "api_key" in problem.message
    assert "api_key_env" in problem.message


def test_explain_names_the_layer_and_innermost_group_of_each_value(world: World) -> None:
    user = {
        "models": {
            "a": {
                "base_url": URL,
                "models": {"b": {"base_url": URL + "x", "models": {"m": {"model": "x"}}}},
            }
        }
    }
    project = {"models": {"a": {"models": {"b": {"models": {"m": {"model": "y"}}}}}}}
    cli = {"models": {"a": {"api_key_env": "CLI_KEY"}}}
    world.user_config(user)
    root = world.project(config=project)
    trust(world, root)
    catalog = load_config(root, world.environ, ask=None, cli=cli).catalog
    origins = {o.key: o for o in catalog.explain("a:b:m")}
    assert (origins["model"].source, origins["model"].group) == (
        str(root / ".tau" / "config.json"),
        None,
    )
    assert (origins["base_url"].source, origins["base_url"].group) == (
        str(world.tau / "config.json"),
        "a:b",
    )
    assert (origins["api_key_env"].source, origins["api_key_env"].group) == ("cli", "a")
    assert (origins["provider"].source, origins["provider"].group) == ("default", None)


# -- problems


def test_invalid_json_has_file_line_and_column_and_does_not_hide_another_layer(
    world: World, tmp_path: Path
) -> None:
    user = write(tmp_path / "c.json", '{\n  "models": {\n    "a": oops\n  }\n}\n')
    problem = only_error(check_config(user, providers=PROVIDERS))
    assert (problem.source, problem.line, problem.column) == (str(user), 3, 10)
    other = write(tmp_path / "p.json", {"models": {"a:b": MODEL}})
    assert len(errors(check_config(user, other, providers=PROVIDERS))) == 2
    world.user_config('{\n  "models": {\n    "a": oops\n  }\n}\n')
    result = tau(world, "config", "check")
    assert result.code == 1
    assert result.out.startswith(f"{world.tau / 'config.json'}:3:10: error: ")
    with pytest.raises(ConfigError, match=r"config\.json:3:10"):
        load_config(world.home, world.environ, ask=None)


def test_an_unreadable_file_is_a_problem(tmp_path: Path) -> None:
    problem = only_error(check_config(tmp_path / "missing.json", providers=PROVIDERS))
    assert "cannot read" in problem.message
    assert problem.source == str(tmp_path / "missing.json")


def test_a_duplicate_key_reports_the_path_of_its_object(tmp_path: Path) -> None:
    text = '{"models": {"a": {"model": "x", "model": "y", "base_url": "' + URL + '"}}}'
    problem = only_error(check(tmp_path, text))
    assert "duplicate key 'model'" in problem.message
    assert problem.path == "models.a"


def test_unknown_keys_have_a_path_and_a_suggestion_even_when_null(tmp_path: Path) -> None:
    config = {
        "modles": None,
        "models": {
            "a": {"modle": "x", "zzzzzz": 1, "model": "m", "base_url": URL},
            "g": {"base_ur": URL, "models": {}},
        },
    }
    found = {p.path: p.message for p in errors(check(tmp_path, config))}
    assert "did you mean 'models'?" in found["modles"]
    assert "did you mean 'model'?" in found["models.a.modle"]
    assert "did you mean 'base_url'?" in found["models.g.base_ur"]
    assert "known keys are" in found["models.a.zzzzzz"]


def test_the_renamed_provider_key_gets_its_own_message(tmp_path: Path) -> None:
    for value in ("openai", None):
        problem = only_error(check(tmp_path, {"models": {"a": {**MODEL, OLD_KEY: value}}}))
        assert problem.message == "`backend` is now `provider`"  # rules: allow(backend): D13
        assert problem.path == f"models.a.{OLD_KEY}"


def test_names_must_be_non_empty_plain_and_printable(tmp_path: Path) -> None:
    config = {"models": {"a:b": MODEL, "": MODEL, "g\x07": MODEL, "h": {"models": {"": MODEL}}}}
    problems = errors(check(tmp_path, config))
    assert sorted(p.path for p in problems) == sorted(
        ["models.a:b", "models.", "models.g\x07", "models.h.models."]
    )
    assert "must not contain ':'" in next(p.message for p in problems if p.path == "models.a:b")
    assert not any("\x07" in p.message for p in problems)


def test_missing_values_after_merge_name_the_layer_that_introduced_the_model(
    tmp_path: Path,
) -> None:
    user = write(tmp_path / "u.json", {"models": {"m": {"base_url": URL}}})
    problem = only_error(
        check_config(user, cli={"models": {"m": {"api_key_env": "K"}}}, providers=PROVIDERS)
    )
    assert problem.source == str(user)
    assert "missing 'model'" in problem.message
    assert str(user) in problem.message
    bare = check(tmp_path, {"models": {"a": {}}})
    assert [p.message.split(" is ")[-1] for p in bare] == ["missing 'model'"]
    group = {"models": {"g": {"model": "m", "base_url": URL, "models": {"a": {}}}}}
    assert check(tmp_path, group) == ()  # a group can supply them


def test_wrong_types_are_problems_at_their_path(tmp_path: Path) -> None:
    config = {
        "models": {
            "a": {"model": 3, "base_url": URL},
            "b": 7,
            "c": {"model": "m", "base_url": [URL]},
        },
        "default_model": 3,
    }
    found = {p.path: p.message for p in errors(check(tmp_path, config))}
    assert set(found) == {"models.a.model", "models.b", "models.c.base_url", "default_model"}
    assert "must be a string" in found["models.a.model"]
    assert "must be an object" in found["models.b"]
    assert "must be a JSON object" in only_error(check(tmp_path, "[]")).message
    assert "'models' must be an object" in only_error(check(tmp_path, {"models": []})).message


@pytest.mark.parametrize("url", ["localhost", "http://[x/v1", "ftp://h/v1"])
def test_a_bad_base_url_is_a_problem_at_its_key_and_an_error_not_a_crash(
    world: World, url: str
) -> None:
    cli = {"models": {"a": {"model": "m", "base_url": url}}}
    assert [p.path for p in check_config(cli=cli, providers=PROVIDERS)] == ["models.a.base_url"]
    with pytest.raises(ConfigError, match="base_url"):
        load_config(world.home, world.environ, ask=None, cli=cli)
    with pytest.raises(ConfigError, match="base_url"):
        ModelConfig("m", url)


def test_an_unknown_provider_is_a_problem_once_even_in_a_group(tmp_path: Path) -> None:
    config = {"models": {"g": {**MODEL, "provider": "acme", "models": {"a": {}, "b": {}}}}}
    problem = only_error(check(tmp_path, config))
    assert problem.path == "models.g.provider"
    assert "unknown provider 'acme'" in problem.message
    ok = {"models": {"a": {**MODEL, "provider": "openai"}}}
    assert check(tmp_path, ok) == ()


def test_an_unknown_provider_names_the_providers_the_driver_was_given(tmp_path: Path) -> None:
    problem = only_error(check(tmp_path, {"models": {"a": {**MODEL, "provider": "acme"}}}))
    assert (
        "unknown provider 'acme'; known providers are ['gemini', 'mock', 'openai']"
        in problem.message
    )
    only = check_config(
        write(tmp_path / "d.json", {"models": {"a": {**MODEL, "provider": "openai"}}}),
        providers={"mock": PROVIDERS["mock"]},
    )
    assert "known providers are ['mock']" in only_error(only).message


def test_max_tokens_and_options_merge_by_layer_and_down_through_groups(world: World) -> None:
    user = {
        "models": {
            "g": {
                "provider": "mock",
                "max_tokens": 8,
                "options": {"delay_ms": 5, "reply": "hi"},
                "models": {"m": {"model": "m"}, "n": {"model": "n", "options": {"delay_ms": 9}}},
            }
        }
    }
    cli = {"models": {"g": {"models": {"m": {"max_tokens": 4, "options": {"reply": None}}}}}}
    catalog = load(world, user, cli=cli)
    m, n = catalog.resolve("g:m"), catalog.resolve("g:n")
    assert (m.max_tokens, m.options) == (4, {"delay_ms": 5})
    assert (n.max_tokens, n.options) == (8, {"delay_ms": 9, "reply": "hi"})
    assert m.base_url is None
    assert {o.key: o.source for o in catalog.explain("g:m")}["max_tokens"] == "cli"


def test_options_that_are_not_an_object_are_a_problem_even_under_a_group_of_objects(
    tmp_path: Path,
) -> None:
    config = {
        "models": {
            "g": {
                "provider": "mock",
                "options": {"delay_ms": 1},
                "models": {"m": {"model": "m", "options": 5}},
            }
        }
    }
    assert only_error(check(tmp_path, config)).path == "models.g.models.m.options"
    with pytest.raises(ConfigError, match="'options' must be an object"):
        read_models(cli=config)


def test_a_resolved_models_options_cannot_be_changed_through_the_catalog(world: World) -> None:
    config = {"models": {"m": {"model": "m", "provider": "mock", "options": {"a": {"b": [1]}}}}}
    catalog = load(world, config)
    options = catalog.resolve("m").options
    with pytest.raises(TypeError):
        options["a"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        options["a"]["b"] = 1  # type: ignore[index]
    assert options == catalog.resolve("m").options == {"a": {"b": (1,)}}
    given = {"x": [1]}
    model = ModelConfig("m", options=given)
    given["x"].append(2)
    assert model.options == {"x": (1,)}


def test_bad_max_tokens_and_options_are_problems_at_their_key(tmp_path: Path) -> None:
    bad = [("max_tokens", 0), ("max_tokens", "9"), ("max_tokens", True), ("options", [])]
    for key, value in bad:
        config = {"models": {"a": {"model": "a", "provider": "mock", key: value}}}
        assert only_error(check(tmp_path, config)).path == f"models.a.{key}"


def test_an_invalid_group_value_is_not_repeated_as_missing_on_each_model(tmp_path: Path) -> None:
    config = {
        "models": {"g": {"base_url": 5, "models": {"a": {"model": "x"}, "b": {"model": "y"}}}}
    }
    assert only_error(check(tmp_path, config)).path == "models.g.base_url"


def test_default_model_must_exist_and_be_unambiguous(tmp_path: Path) -> None:
    missing = {"default_model": "ghost", "models": {"a": {"modle": "x"}}}
    messages = [p.message for p in errors(check(tmp_path, missing))]
    assert any("default_model 'ghost' is not in your config" in m for m in messages)
    assert any("unknown key" in m for m in messages)  # still checked beside other errors
    two = {
        "default_model": "m",
        "models": {"g": {"models": {"m": MODEL}}, "h": {"models": {"m": MODEL}}},
    }
    message = only_error(check(tmp_path, two)).message
    assert "'g:m'" in message
    assert "'h:m'" in message
    assert check(tmp_path, {**two, "default_model": "g:m"}) == ()


def test_the_checker_reports_every_problem_and_loading_fails_early_listing_them(
    world: World,
) -> None:
    config = {"models": {"a": {"modle": "x", "model": "m", "base_url": URL}, "b:c": {}}}
    world.user_config(config)
    with pytest.raises(ConfigError) as raised:
        load_config(world.home, world.environ, ask=None)
    assert "modle" in str(raised.value)
    assert "b:c" in str(raised.value)
    result = tau(world, "config", "check")
    assert result.code == 1
    assert "modle" in result.out
    assert "b:c" in result.out


# -- keys


@pytest.mark.parametrize("pasted", ["sk-proj-abc123SECRET", "has space"])
def test_api_key_env_must_be_a_variable_name_and_never_echoes_the_value(
    world: World, tmp_path: Path, pasted: str
) -> None:
    config = {"models": {"g": {**MODEL, "api_key_env": pasted, "models": {"a": {}, "b": {}}}}}
    problem = only_error(check(tmp_path, config))  # reported once, at the group
    assert problem.path == "models.g.api_key_env"
    assert pasted not in str(problem)
    world.user_config(config)
    cli = tau(world, "config", "check")
    assert cli.code == 1
    assert "api_key_env" in cli.out
    with pytest.raises(ConfigError) as raised:
        load_config(world.home, world.environ, ask=None)
    for text in (cli.both, str(raised.value)):
        assert pasted not in text


def test_a_literal_key_works_but_warns_and_is_never_printed(world: World) -> None:
    secret = "sk-very-secret"
    config = {"models": {"a": {**MODEL, "api_key": secret}}}
    world.user_config(config)
    catalog = load_config(world.home, world.environ, ask=None).catalog
    (problem,) = check_config(world.tau / "config.json", providers=PROVIDERS)
    assert problem.severity == "warning"
    assert catalog.warnings == (problem,)
    resolved = catalog.resolve("a")
    assert resolved.api_key({}) == secret
    assert resolved.api_key_env is None
    shown = [str(problem), repr(resolved), repr(catalog.explain("a")), repr(catalog)]
    shown += [tau(world, "config", "check").both, tau(world, "config", "check", "--show", "a").both]
    assert not any(secret in text for text in shown)


def test_an_unset_key_variable_is_a_warning_only_given_an_environ(tmp_path: Path) -> None:
    config = {"models": {"a": {**MODEL, "api_key_env": "SOME_KEY"}}}
    assert check(tmp_path, config) == ()
    (problem,) = check(tmp_path, config, environ={})
    assert problem.severity == "warning"
    assert "SOME_KEY" in problem.message
    assert check(tmp_path, config, environ={"SOME_KEY": "x"}) == ()


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_a_number_json_cannot_hold_is_a_config_problem(tmp_path: Path, number: str) -> None:
    text = '{"models": {"m": {"model": "x", "options": {"extra_body": {"t": ' + number + "}}}}}"
    problem = only_error(check(tmp_path, text))
    assert "invalid JSON" in problem.message
