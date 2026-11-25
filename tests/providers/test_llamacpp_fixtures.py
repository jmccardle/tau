"""What the recorded llama.cpp fixtures (`tests/fixtures/providers/llamacpp`, Qwen3.8-27B with
its multimodal projector) say about the server and about τ's reading of it, beyond the replay
every family gets (`test_provider_fixtures.py`): the stop reasons, parallel calls, reasoning,
the usage fields, an image in a tool result, the two refusals, the non-streamed wire, and the
count calls of D26 section 8, each equal to the bill that followed it."""

import json
from pathlib import Path
from typing import Any, cast

import pytest
from harness import scenario
from replay import ROOT, cases, read_json, replayed

from tau import Basis, Body, Cursor, Part, RequestCount, Subject

FAMILY = ROOT / "llamacpp"
pytestmark = pytest.mark.skipif(not cases("llamacpp"), reason="no llama.cpp fixtures")


def chat_requests(case_dir: Path) -> list[int]:
    """The indexes of the case's chat-completion exchanges."""
    return [
        int(p.name[:2])
        for p in sorted(case_dir.glob("*.request.json"))
        if read_json(p)["url"].endswith("/chat/completions")
    ]


def reported_input(case_dir: Path, k: int) -> int:
    """`usage.prompt_tokens` of exchange `k`'s response, streamed or not."""
    body = (case_dir / f"{k:02}.response.body").read_text()
    if body.lstrip().startswith("{"):
        return json.loads(body)["usage"]["prompt_tokens"]
    usages = [
        json.loads(line[6:])["usage"]
        for line in body.splitlines()
        if line.startswith("data: {") and '"usage"' in line
    ]
    return [u for u in usages if u][-1]["prompt_tokens"]


@scenario
async def test_three_turns_read_as_calls_then_answers_with_parallel_calls_and_thinking(
    tmp_path: Path,
) -> None:
    for name in ("turns", "turns-2", "turns-3"):
        async with replayed(FAMILY / name, tmp_path) as run:
            stops = [r["stop"] for r in run.replies]
            assert stops == ["Tools", "End"] * 3, name
            replies = [n.body for n in run.walk if isinstance(n.body, Body.Assistant)]
            calls = [[p for p in r.parts if isinstance(p, Part.Call)] for r in replies]
            assert [len(c) for c in calls] == [1, 0, 2, 0, 1, 0], f"{name}: turn 2 is parallel"
            assert {c.tool for c in calls[2]} == {"lookup_part"}
            assert all(any(isinstance(p, Part.Thinking) for p in r.parts) for r in replies)
            usages = [cast("dict[str, int]", r["usage"]) for r in run.replies]
            assert all({"input", "output", "cached"} <= set(u) for u in usages), name
            results = [n.body for n in run.walk if isinstance(n.body, Body.Results)]
            assert "no such part: Z-999" in str(results[-1].outcomes), "the error result"
            assert run.metas[0] is not None
            assert run.metas[0]["server"]["build_info"], "the drift markers from /props"


@scenario
async def test_a_reply_cut_at_max_tokens_is_a_length_stop(tmp_path: Path) -> None:
    async with replayed(FAMILY / "length", tmp_path) as run:
        (only,) = run.replies
        assert (only["stop"], only["finish_reason"]) == ("Length", "length")
        usage = cast("dict[str, int]", only["usage"])
        assert usage["output"] == run.case["model"]["max_tokens"]


@scenario
async def test_a_tool_results_image_follows_its_tool_message_and_is_read(tmp_path: Path) -> None:
    async with replayed(FAMILY / "tool-image", tmp_path) as run:
        second = run.sent[-1]
        assert isinstance(second, dict)
        messages: list[dict[str, Any]] = second["messages"]
        assert [m["role"] for m in messages][-2:] == ["tool", "user"]
        items = messages[-1]["content"]
        assert items[1]["image_url"]["url"].startswith("data:image/png;base64,")
        answer = next(
            p.text
            for n in run.walk
            if isinstance(n.body, Body.Assistant)
            for p in n.body.parts
            if isinstance(p, Part.Text) and p.text.strip()
        )
        assert "blue" in answer.lower(), "the model saw the swatch"


@pytest.mark.parametrize(
    ("case", "said"),
    [
        ("error-bad-request", "Expected 'messages' to be an array"),
        ("error-overflow", "exceeds the available context size"),
    ],
)
def test_a_refused_request_is_a_sample_error_with_the_servers_words(
    case: str, said: str, tmp_path: Path
) -> None:
    @scenario
    async def check() -> None:
        async with replayed(FAMILY / case, tmp_path) as run:
            assert run.replies == []
            assert run.error is not None
            assert run.error["status"] == 400
            assert said in str(run.error["message"])

    check()


def test_a_non_streamed_reply_carries_its_reasoning_calls_and_usage_in_one_body() -> None:
    text = read_json(FAMILY / "nonstream-text" / "00.response.body")
    message = text["choices"][0]["message"]
    assert message["content"]
    assert message["reasoning_content"]
    assert set(text["usage"]) >= {"prompt_tokens", "completion_tokens", "prompt_tokens_details"}
    assert "timings" in text
    tools = read_json(FAMILY / "nonstream-tools" / "00.response.body")
    choice = tools["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"part": "A-113"}


def test_the_count_calls_equal_the_bills_that_follow_them() -> None:
    """D26 section 8 on the wire: `/apply-template` then `/tokenize` before each chat request,
    and the count is exactly the `prompt_tokens` the server then reports."""
    case_dir = FAMILY / "counted"
    urls = [read_json(p)["url"] for p in sorted(case_dir.glob("*.request.json"))]
    pairs: list[tuple[int, int]] = []
    pending: int | None = None
    for k, url in enumerate(urls):
        if url.endswith("/tokenize"):
            assert urls[k - 1].endswith("/apply-template")
            pending = len(read_json(case_dir / f"{k:02}.response.body")["tokens"])
        elif url.endswith("/chat/completions"):
            assert pending is not None, f"request {k} was not counted first"
            pairs.append((pending, reported_input(case_dir, k)))
            pending = None
    assert len(pairs) == len(chat_requests(case_dir)) == 6
    assert all(count == bill for count, bill in pairs), pairs


class _Probe:
    """Counts the walk before every sample, after `token_calls`."""

    name = "count_probe"
    order = {"pre_sample": -10}  # noqa: RUF012 - read by the driver, never changed

    def __init__(self) -> None:
        self.counted: list[RequestCount] = []

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> None:
        self.counted.append(await cursor.count())


@scenario
async def test_through_tau_each_request_count_is_rendered_and_equals_its_bill(
    tmp_path: Path,
) -> None:
    """The recorded `counted` case replayed with `token_calls` (when `always`): before each
    sample the walk's total is the server's count, `rendered`, and it equals the `input` the
    reply then reports."""
    probe = _Probe()
    async with replayed(FAMILY / "counted", tmp_path, [probe]) as run:
        assert run.sent == run.recorded
        totals = [c.total for c in probe.counted]
        inputs = [cast("dict[str, int]", r["usage"])["input"] for r in run.replies]
        assert [t.basis for t in totals] == [Basis.RENDERED] * len(inputs)
        assert [t.tokens for t in totals] == inputs
