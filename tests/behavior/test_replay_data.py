"""Replay data through the driver (D15): the ordering rule ignores it, a reload keeps it, and
`replay_for` hands a provider only its own."""

from pathlib import Path

from fakes import FakeProvider, Reply
from harness import Harness, scenario

from tau_next import Body, Part, Replay, Stop, TextDelta, ThinkingDelta, replay_for

ANTHROPIC = Replay("anthropic", '{"signature":"s1=="}')
GEMINI = Replay("gemini", '{"thoughtSignature":"g1"}')


@scenario
async def test_a_final_part_may_carry_replay_its_deltas_lacked(tmp_path: Path) -> None:
    script = [
        ThinkingDelta("hm"),
        Part.Thinking("hm", ANTHROPIC),
        TextDelta("hi"),
        Part.Text("hi", GEMINI),
    ]
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply(script)]))
    await h.send("go")
    await h.settle()
    h.driver.check()
    reply = next(b for b in h.bodies() if isinstance(b, Body.Assistant))
    assert reply.parts == [Part.Thinking("hm", ANTHROPIC), Part.Text("hi", GEMINI)]
    await h.close()


@scenario
async def test_the_ordering_rule_still_compares_text_when_the_part_has_replay(
    tmp_path: Path,
) -> None:
    script = [TextDelta("hi"), Part.Text("other", GEMINI)]
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply(script)]))
    await h.send("go")
    await h.settle()
    replies = [b for b in h.bodies() if isinstance(b, Body.Assistant)]
    assert [(r.stop, r.parts) for r in replies] == [(Stop.Error, [Part.Text("hi")])]
    await h.close()


@scenario
async def test_a_thinking_part_with_no_text_but_replay_is_saved_and_reloaded(
    tmp_path: Path,
) -> None:
    redacted = Part.Thinking("", Replay("anthropic", '{"redacted":"zz=="}'))
    script = [redacted, Part.Text("done")]
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply(script)]))
    await h.send("go")
    await h.settle()
    await h.crash()
    await h.settle()
    reply = next(b for b in h.bodies() if isinstance(b, Body.Assistant))
    assert reply.parts == [redacted, Part.Text("done")]
    await h.close()


def test_replay_for_returns_the_data_to_its_own_provider() -> None:
    assert replay_for(Part.Thinking("hm", ANTHROPIC), "anthropic") == {"signature": "s1=="}
    assert replay_for(Part.Text("t", GEMINI), "gemini") == {"thoughtSignature": "g1"}
    call = Part.Call("c", "tool", "{}", GEMINI)
    assert replay_for(call, "gemini") == {"thoughtSignature": "g1"}


def test_replay_for_returns_nothing_for_another_providers_tag() -> None:
    assert replay_for(Part.Thinking("hm", ANTHROPIC), "gemini") is None
    assert replay_for(Part.Text("t", GEMINI), "anthropic") is None
    assert replay_for(Part.Call("c", "tool", "{}", GEMINI), "openai") is None


def test_replay_for_returns_nothing_for_a_part_without_replay() -> None:
    for part in (Part.Text("t"), Part.Thinking("t"), Part.Call("c", "tool", "{}")):
        assert replay_for(part, "anthropic") is None
