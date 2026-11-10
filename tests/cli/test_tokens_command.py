"""`tau tokens show|reset` (D26 section 6.3), and what `tau -p` leaves under `$TAU_HOME` with
the `tokens` built-in on (the default) and off."""

import hashlib
from pathlib import Path

from world import World, tau

from tau import Author, Body, CalibrationStore, Outcome, calibration_features

URL = "http://localhost:8100/v1"
CONFIG = {
    "default_model": "qwen",
    "models": {"qwen": {"model": "qwen3", "provider": "openai", "base_url": URL}},
}
NAME = "openai/" + hashlib.sha256(f"{URL}\nqwen3".encode()).hexdigest()[:16] + ".json"
INSTALL = "pip install 'ffwf-tau[tokens]'"


def learn(world: World, spans: int) -> Path:
    """A record for the model `qwen` that has seen `spans` proved spans of tool results."""
    store = CalibrationStore(world.tau / "calibration")
    for i in range(spans):
        text = "r" * (400 + 100 * i)
        body = Body.Results([("c", Outcome.Ok(text), Author.Tools())])
        markers = {"model": "qwen3", "build_info": "b7965"}
        store.observe(
            NAME, ("openai", URL, "qwen3"), markers, (calibration_features(body), 100 + 25 * i), "t"
        )
    return world.tau / "calibration" / NAME


def test_show_with_no_records_says_where_it_looked(world: World) -> None:
    world.user_config(CONFIG)
    result = tau(world, "tokens", "show")
    assert (result.code, result.err) == (0, "")
    assert result.out == f"no calibration records under {world.tau / 'calibration'}\n"
    assert not (world.tau / "calibration").exists(), "looking writes nothing"
    result = tau(world, "tokens", "show", "qwen")
    assert result.code == 0
    assert result.out.startswith("no calibration record for qwen (")


def test_show_lists_every_record_and_one_model_in_words(world: World) -> None:
    world.user_config(CONFIG)
    path = learn(world, 12)
    listed = tau(world, "tokens", "show")
    assert listed.code == 0
    (line,) = listed.out.splitlines()
    assert line.startswith(f"{NAME}: heuristic ±")
    assert line.endswith(f"from 12 replies (no tokenizer: {INSTALL}) [openai qwen3 at {URL}]")
    shown = tau(world, "tokens", "show", "qwen")
    assert shown.code == 0
    lines = shown.out.splitlines()
    assert lines[0] == f"qwen (openai, qwen3 at {URL})"
    assert lines[1] == f"  file: {path}"
    assert lines[2] == "  learned from: 12 proved spans; last reply observed t"
    assert lines[3].startswith("  counting: heuristic ±")
    assert lines[4].startswith(
        "  tokens per node: system 0, user 0, assistant 0, calls 0, results "
    )
    assert lines[5] == "  drift markers: build_info=b7965, model=qwen3"


def test_a_record_still_learning_says_how_far_along_it_is(world: World) -> None:
    world.user_config(CONFIG)
    learn(world, 3)
    shown = tau(world, "tokens", "show", "qwen")
    assert (
        "  counting: learning from 3 replies (3 of 10 proved spans since it last changed); "
        "byte ceilings until then" in shown.out
    )


def test_reset_deletes_one_models_record(world: World) -> None:
    world.user_config(CONFIG)
    path = learn(world, 12)
    result = tau(world, "tokens", "reset", "qwen")
    assert (result.code, result.out) == (0, f"removed {path}\n")
    assert not path.exists()
    again = tau(world, "tokens", "reset", "qwen")
    assert (again.code, again.out) == (0, "no calibration record for qwen\n")


def test_a_model_the_config_does_not_have_is_an_error(world: World) -> None:
    world.user_config(CONFIG)
    result = tau(world, "tokens", "show", "nope")
    assert result.code == 1
    assert "nope" in result.err


def test_a_damaged_record_is_reported_and_reset_removes_it(world: World) -> None:
    world.user_config(CONFIG)
    path = learn(world, 2)
    path.write_text("not json")
    shown = tau(world, "tokens", "show", "qwen")
    assert shown.code == 1
    assert "is not a calibration record this build reads" in shown.err
    assert "`tau tokens reset qwen` removes it" in shown.err
    listed = tau(world, "tokens", "show")
    assert listed.code == 0
    assert "is not a calibration record" in listed.out
    assert tau(world, "tokens", "reset", "qwen").code == 0
    assert not path.exists()


def test_tokens_without_a_command_is_a_usage_error(world: World) -> None:
    world.user_config(CONFIG)
    try:
        code = tau(world, "tokens").code
    except SystemExit as stopped:  # argparse: a command is required
        code = stopped.code
    assert code == 2


MOCK = {
    "default_model": "m",
    "models": {"m": {"model": "m", "provider": "mock", "options": {"delay_ms": 0}}},
}


def test_a_run_with_tokens_on_keeps_a_record_and_one_with_it_off_writes_none(
    world: World,
) -> None:
    """D26 T3: `load: {tokens: false}` and a session with replies leaves no `calibration`."""
    world.user_config({**MOCK, "load": {"tokens": False}})
    assert tau(world, "-p", "hello").out == "mock: hello\n"
    assert not (world.tau / "calibration").exists()
    world.user_config(MOCK)
    assert tau(world, "-p", "hello").out == "mock: hello\n"
    records = list((world.tau / "calibration").rglob("*.json"))
    assert len(records) == 1, "one record, the mock model's"
    assert records[0].parent.name == "mock"
    listed = tau(world, "tokens", "show")
    assert "learning from 1 reply (0 of 10 proved spans" in listed.out
