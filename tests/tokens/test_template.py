"""`tau_tokens.ChatTemplate` against Jinja2 as `transformers` sets it up, offline: every
template of tests/fixtures/tokens/templates on every request of conversations.json, byte for
byte. The messages go in as the Python lists a caller has, so this is the binding's path too."""

from datetime import datetime
from typing import Any

import pytest
from tokens_golden import FIXTURES, load

from tau_tokens import ChatTemplate, TemplateError

USER = [{"role": "user", "content": "hi"}]


def rendered(template: ChatTemplate, request: dict[str, Any], now: datetime) -> str | None:
    """The rendering, or None when the template refuses the request."""
    try:
        return template.render(
            request["messages"],
            request["tools"],
            add_generation_prompt=request["add_generation_prompt"],
            extra=request["extra"],
            now=now,
        )
    except TemplateError:
        return None


@pytest.mark.parametrize("name", sorted(p.stem for p in (FIXTURES / "templates").glob("*.jinja")))
def test_a_template_renders_as_jinja2_renders_it(
    name: str, requests: list[dict[str, Any]], now: datetime
) -> None:
    golden = load("templates.json")
    source = (FIXTURES / "templates" / f"{name}.jinja").read_text(encoding="utf-8")
    template = ChatTemplate(source, golden["tokens"])
    assert len(requests) >= 20
    for request in requests:
        expected = golden["templates"][name][request["name"]]
        assert rendered(template, request, now) == expected.get("rendered"), request["name"]


def test_there_are_six_templates_and_one_refusal(requests: list[dict[str, Any]]) -> None:
    golden = load("templates.json")["templates"]
    assert sorted(golden) == ["control", "edges", "json", "printing", "strings", "whitespace"]
    refused = [
        (t, r) for t, cases in golden.items() for r, case in cases.items() if "error" in case
    ]
    assert refused == [("control", "unknown-role")], "the reference refuses one request"


def test_a_refusal_carries_the_templates_words() -> None:
    template = ChatTemplate("{{ raise_exception('Only user and assistant roles are supported') }}")
    with pytest.raises(
        TemplateError, match="Only user and assistant roles are supported"
    ) as raised:
        template.render(USER)
    assert isinstance(raised.value, ValueError)


def test_a_template_that_reads_the_clock_needs_now() -> None:
    template = ChatTemplate("{{ strftime_now('%d %b %Y, %A') }}")
    with pytest.raises(TemplateError, match="pass `now`"):
        template.render(USER)
    assert template.render(USER, now=datetime(2024, 7, 26, 23, 59, 59)) == "26 Jul 2024, Friday"
    with pytest.raises(TemplateError, match="%U"):
        ChatTemplate("{{ strftime_now('%U') }}").render(USER, now=datetime(2024, 7, 26))


def test_what_is_not_a_template_or_a_request_raises() -> None:
    with pytest.raises(TemplateError, match="syntax error"):
        ChatTemplate("{% if %}")
    template = ChatTemplate("{{ messages | length }}")
    with pytest.raises(TemplateError, match="each message must be an object"):
        template.render(["hi"])  # pyright: ignore[reportArgumentType]
    with pytest.raises(TemplateError, match="argument of its own"):
        template.render(USER, extra={"tools": []})
    with pytest.raises(ValueError, match="not JSON compliant"):
        template.render([{"role": "user", "content": float("nan")}])
    with pytest.raises(TypeError, match="not JSON serializable"):
        template.render([{"role": "user", "content": object()}])
    with pytest.raises(TemplateError, match="too large to print"):
        template.render([{"role": "user", "content": 10**30}])


def test_a_config_gives_the_template_its_tokens_and_the_tool_template() -> None:
    config = b"""{
        "bos_token": {"content": "<s>", "__type": "AddedToken"}, "eos_token": "</s>",
        "pad_token": null, "model_max_length": 1e30,
        "chat_template": [
            {
             "name": "default",
             "template": "{{ bos_token }}{{ messages[0].content }}{{ eos_token }}[{{ pad_token }}]"
            },
            {"name": "tool_use", "template": "{{ tools | length }} tools"}
        ]
    }"""
    template = ChatTemplate.from_config(config)
    assert template.render(USER) == "<s>hi</s>[]"
    assert template.render(USER, [{"type": "function"}]) == "1 tools"
    assert ChatTemplate.from_config(config, "{{ eos_token }}!").render(USER, []) == "</s>!"
    with pytest.raises(TemplateError, match="no chat_template"):
        ChatTemplate.from_config(b'{"bos_token": "<s>"}')
    with pytest.raises(TemplateError, match=r"tokenizer_config\.json"):
        ChatTemplate.from_config(b"not json")


def test_tools_are_none_when_there_are_none_and_the_generation_prompt_is_on() -> None:
    template = ChatTemplate(
        "{{ tools is none }} {{ documents is none }} {{ add_generation_prompt }}"
    )
    assert template.render(USER) == "True True True"
    assert template.render(USER, [], add_generation_prompt=False) == "False True False"


def test_reasoning_is_given_under_the_names_templates_read() -> None:
    """transformers hands a message's reasoning to the template as `reasoning_content` and as
    `thinking`, whichever of the two (or `reasoning`) the caller used."""
    template = ChatTemplate(
        "{{ messages[0].reasoning_content }}|{{ messages[0].thinking }}|{{ messages[0] | list }}"
    )
    for name in ["reasoning_content", "thinking", "reasoning"]:
        message = {"role": "assistant", "content": "x", name: "why"}
        keys = ["thinking", *[k for k in message if k != "thinking"]]
        keys += [] if name == "reasoning_content" else ["reasoning_content"]
        assert template.render([message]) == f"why|why|{keys}"
