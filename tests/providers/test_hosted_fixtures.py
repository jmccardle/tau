"""What the recorded hosted fixtures (Anthropic Claude Haiku 4.5, Gemini 3.5 Flash-Lite, Mistral
Small 2603 through the OpenAI-compatible provider) say about τ's reading of each, beyond the
replay every family gets (`test_provider_fixtures.py`): one call, then two in parallel, then
one that errors; a tool result's image read; a user's image read (small, 4K, and with a
tool's); a length stop; a malformed body refused with the
server's words; and Anthropic's cache proving the spans of a conversation whose prompt is
cacheable, and none across the cache miss after the gap."""

import json
from pathlib import Path

import pytest
from harness import scenario
from replay import ROOT, cases, replayed

from tau import Body, Part

PNG_BASE64 = "iVBORw0KGgo"  # what every PNG's base64 begins with
HOSTED = [f for f in ("anthropic", "gemini", "mistral") if cases(f)]
pytestmark = pytest.mark.skipif(not HOSTED, reason="no hosted fixtures")


def replies(run: object) -> list[Body.Assistant]:
    walk = getattr(run, "walk")  # noqa: B009 - a Run
    return [n.body for n in walk if isinstance(n.body, Body.Assistant)]


@pytest.mark.parametrize("family", HOSTED)
def test_three_turns_are_one_call_two_parallel_calls_and_an_error(
    family: str, tmp_path: Path
) -> None:
    @scenario
    async def check() -> None:
        for name in ("turns", "turns-2", "turns-3"):
            async with replayed(ROOT / family / name, tmp_path) as run:
                calls = [sum(isinstance(p, Part.Call) for p in r.parts) for r in replies(run)]
                assert calls[:6] == [1, 0, 2, 0, 1, 0], f"{family}/{name}"
                results = [n.body for n in run.walk if isinstance(n.body, Body.Results)]
                assert "no such part: Z-999" in str(results[-1].outcomes)
                assert all(r["usage"] is not None for r in run.replies)

    check()


@pytest.mark.parametrize("family", HOSTED)
def test_a_tool_results_image_is_read_and_a_reply_is_cut_at_max_tokens(
    family: str, tmp_path: Path
) -> None:
    @scenario
    async def check() -> None:
        async with replayed(ROOT / family / "tool-image", tmp_path) as run:
            said = " ".join(
                p.text for r in replies(run) for p in r.parts if isinstance(p, Part.Text)
            )
            assert "blue" in said.lower(), f"{family}: the model saw the swatch"
        async with replayed(ROOT / family / "length", tmp_path) as run:
            assert [r["stop"] for r in run.replies] == ["Length"]

    check()


def said_by(run: object) -> str:
    return " ".join(p.text for r in replies(run) for p in r.parts if isinstance(p, Part.Text))


@pytest.mark.parametrize("family", HOSTED)
@pytest.mark.parametrize(("case", "colour"), [("user-image", "red"), ("user-image-large", "green")])
def test_a_users_image_is_sent_after_its_text_and_read(
    family: str, case: str, colour: str, tmp_path: Path
) -> None:
    @scenario
    async def check() -> None:
        async with replayed(ROOT / family / case, tmp_path) as run:
            (images,) = run.case["turn_images"]
            assert len(images) == 1
            (user,) = (n.body for n in run.walk if isinstance(n.body, Body.User))
            assert len(user.images) == 1
            assert colour in said_by(run).lower(), f"{family}/{case}: the model saw the image"

    check()


@pytest.mark.parametrize("family", HOSTED)
def test_a_users_image_and_a_tools_image_are_in_one_request(family: str, tmp_path: Path) -> None:
    @scenario
    async def check() -> None:
        async with replayed(ROOT / family / "user-and-tool-image", tmp_path) as run:
            posts = [s for s in run.sent if s is not None]  # not a probe's GET
            assert len(posts) == 2
            assert json.dumps(posts[-1]).count(PNG_BASE64) == 2, "both images, in the last request"
            if family != "mistral":  # Mistral Small answered "white" for both: not asserted
                said = said_by(run).lower()
                assert "red" in said
                assert "blue" in said

    check()


@pytest.mark.parametrize("family", HOSTED)
def test_a_malformed_body_is_a_sample_error_with_the_servers_status(
    family: str, tmp_path: Path
) -> None:
    @scenario
    async def check() -> None:
        async with replayed(ROOT / family / "error-bad-request", tmp_path) as run:
            assert run.replies == []
            assert run.error is not None
            assert run.error["status"] in {400, 422}
            said = str(run.error["message"]).lower()
            assert "messages" in said or "contents" in said, "names the field it refused"

    check()


@pytest.mark.skipif(not cases("anthropic"), reason="no Anthropic fixtures")
def test_anthropic_proves_spans_by_its_cache_only_when_the_prompt_is_cacheable(
    tmp_path: Path,
) -> None:
    """Under Haiku 4.5's minimum cacheable prompt nothing is cached and no span is proved
    (`turns`); over it (`cached`) each request after a tool call reads all the previous one
    cached, which proves the span, while a new user turn drops the previous turn's thinking,
    so the read stops short and proves nothing; after the gap the cache is cold."""

    @scenario
    async def check() -> None:
        async with replayed(ROOT / "anthropic" / "turns", tmp_path) as run:
            assert all(r["usage"]["cached"] == 0 for r in run.replies)  # type: ignore[index]
            assert run.spans == ()
        async with replayed(ROOT / "anthropic" / "cached", tmp_path) as run:
            assert [s.proof for s in run.spans] == ["cache"] * 3
            assert run.replies[-1]["usage"]["cached"] == 0  # type: ignore[index]

    check()
