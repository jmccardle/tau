"""`Permissions` rules about `fetch` (D24 sections 7 and 9): `fetch` is the tool's whole name, and
`fetch:GLOB` matches the host of the URL, as the tool reads it. `search` has the bare name only."""

import json
from pathlib import Path

import pytest
from fakes import FAKE_MODEL, FakeProvider, FakeTool, Reply
from harness import Harness, say, scenario

from tau import ConfigError, Outcome, Part, Permissions, Stop, UserConfig, model_config

REFUSED = "the user refused this call"


def visit(*urls: str) -> Reply:
    return Reply(
        [Part.Call(f"c{i}", "fetch", json.dumps({"url": u})) for i, u in enumerate(urls)],
        Stop.Tools,
    )


async def start(tmp_path: Path, urls: list[str], **rules: list[str]) -> tuple[Harness, FakeTool]:
    tool = FakeTool("fetch", "page")
    models = {FAKE_MODEL: {"model": FAKE_MODEL, "provider": "fake"}}
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([visit(*urls), say("ok")]),
        tools=[tool],
        extensions=[Permissions(tmp_path)],
        config=model_config(FAKE_MODEL, {"permissions": rules}),
        user_config=UserConfig(
            tmp_path / "tau-home", cli={"models": models, "extensions": {"permissions": rules}}
        ),
    )
    await h.send("go")
    await h.settle()
    return h, tool


def outcomes(h: Harness) -> list[Outcome]:
    body = next(b for b in reversed(h.bodies()) if hasattr(b, "outcomes"))
    return [o for _, o, _ in body.outcomes]  # pyright: ignore[reportAttributeAccessIssue]


@scenario
async def test_a_deny_rule_for_a_host_pattern_refuses_hosts_under_it(tmp_path: Path) -> None:
    urls = ["https://x.evil.com/p", "https://evil.com/", "https://fine.org/"]
    h, tool = await start(tmp_path, urls, deny=["fetch:*.evil.com"])
    assert [c["url"] for c in tool.calls] == urls[1:], "evil.com itself is not under *.evil.com"
    first, *_ = outcomes(h)
    assert first == Outcome.Err(
        "permissions refused this call: it matches the deny rule 'fetch:*.evil.com'"
    )
    await h.close()


@scenario
async def test_the_host_is_matched_whatever_its_case_port_path_and_scheme(tmp_path: Path) -> None:
    urls = ["HTTP://Docs.Python.ORG:8080/a/b?c=d#e", "https://docs.python.org/"]
    h, tool = await start(tmp_path, urls, ask=["fetch"], allow=["fetch:docs.python.org"])
    assert (await h.view()).questions == ()
    assert [c["url"] for c in tool.calls] == urls
    await h.close()


@scenario
async def test_what_a_url_says_before_its_host_is_not_its_host(tmp_path: Path) -> None:
    """`docs.python.org@evil.com` is a visit to evil.com; `docs.python.org.evil.com` too."""
    urls = [
        "https://docs.python.org@evil.com/",
        "https://docs.python.org.evil.com/",
        "https://evil.com/docs.python.org",
    ]
    h, tool = await start(tmp_path, urls, ask=["fetch"], allow=["fetch:docs.python.org"])
    assert tool.calls == []
    assert [q.text for q in (await h.view()).questions] == [
        f"fetch: {u}\n(ask rule: fetch)" for u in urls
    ]
    await h.close()


@scenario
async def test_an_address_in_an_old_spelling_is_the_address(tmp_path: Path) -> None:
    urls = ["http://2130706433/", "http://127.1:8080/", "http://0x7f.0.0.1/x"]
    h, tool = await start(tmp_path, urls, deny=["fetch:127.0.0.1"])
    assert tool.calls == []
    assert [o.error.rsplit("rule ", 1)[1] for o in outcomes(h) if isinstance(o, Outcome.Err)] == [
        "'fetch:127.0.0.1'"
    ] * 3
    await h.close()


@scenario
async def test_a_url_that_is_no_http_url_meets_only_the_bare_rule(tmp_path: Path) -> None:
    urls = ["file:///etc/passwd", "not a url"]
    h, tool = await start(tmp_path, urls, deny=["fetch:*"])
    assert len(tool.calls) == 2, "no host to match; the tool itself refuses such a URL"
    await h.close()
    h, tool = await start(tmp_path / "again", urls, ask=["fetch"])
    assert tool.calls == []
    assert len((await h.view()).questions) == 2
    await h.close()


@scenario
async def test_always_saves_the_rule_for_the_host_and_nothing_wider(tmp_path: Path) -> None:
    h, tool = await start(tmp_path, ["https://docs.python.org/3/library/re.html"], ask=["fetch"])
    await h.respond("always")
    await h.settle()
    assert len(tool.calls) == 1
    saved = tmp_path / "tau-home" / "config.json"
    assert json.loads(saved.read_text(encoding="utf-8")) == {
        "extensions": {"permissions": {"allow": ["fetch:docs.python.org"]}}
    }
    await h.close()


@scenario
async def test_always_on_a_malformed_url_saves_nothing(tmp_path: Path) -> None:
    h, tool = await start(tmp_path, ["not a url"], ask=["fetch"])
    await h.respond("always")
    await h.settle()
    assert len(tool.calls) == 1
    assert not (tmp_path / "tau-home" / "config.json").exists()
    assert any("no rule was saved" in n for n in h.notices())
    await h.close()


@scenario
async def test_no_refuses_the_call(tmp_path: Path) -> None:
    h, tool = await start(tmp_path, ["https://x.org/"], ask=["fetch:x.org"])
    await h.respond("no")
    await h.settle()
    assert tool.calls == []
    assert outcomes(h) == [Outcome.Err(REFUSED)]
    await h.close()


async def idle(tmp_path: Path) -> Harness:
    """A session that has not been sent anything."""
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([]),
        tools=[FakeTool("fetch", "page")],
        extensions=[Permissions(tmp_path)],
    )
    await h.settle()
    return h


@scenario
async def test_rules_for_fetch_and_search_name_tools_that_are_not_loaded(tmp_path: Path) -> None:
    """`search` is off by default: a config that denies it is no misspelling."""
    h = await idle(tmp_path)
    await h.configure("permissions", {"deny": ["search", "fetch:*.example.com"]})
    await h.settle()
    with pytest.raises(ConfigError, match=r"names the tool 'fecth'"):
        await h.configure("permissions", {"deny": ["fecth"]})
    await h.close()


@scenario
async def test_an_address_pattern_of_ipv6_is_a_host(tmp_path: Path) -> None:
    """An IPv6 address has colons too, but more than one: it is a host, not a host and a port."""
    h = await idle(tmp_path)
    await h.configure("permissions", {"deny": ["fetch:::1", "fetch:fe80::*"]})
    await h.close()


@pytest.mark.parametrize(
    ("rule", "said"),
    [
        ("fetch:", "has an empty pattern after the colon"),
        ("fetch:[z-a]", "has an invalid pattern"),
        ("fetch:http://127.0.0.1:8300", "a fetch pattern is a host only"),
        ("fetch:127.0.0.1:8300", "no scheme, port or path"),
        ("fetch:127.0.0.1/*", "a fetch pattern is a host only"),
    ],
)
@scenario
async def test_a_bad_host_pattern_is_refused_where_it_is_configured(
    tmp_path: Path, rule: str, said: str
) -> None:
    h = await idle(tmp_path)
    with pytest.raises(ConfigError, match=said):
        await h.configure("permissions", {"deny": [rule]})
    await h.close()
