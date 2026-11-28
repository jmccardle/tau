"""The recorded provider fixtures (D26 §10), replayed: each case gives the usage, stop and
finish reason τ computed when it was recorded, from the same requests; its proved spans cost no
more than their ceilings (the one assumption every ceiling rests on, D26 section 4); and the
Anthropic case proves its spans and none across its cache miss. A family nobody has recorded yet
is skipped, saying how to record it (`replay.RECORD`): until its fixtures exist, nothing here says
anything about that family's real server."""

import json
from pathlib import Path

import pytest
from harness import scenario
from replay import FAMILIES, RECORD, Run, cases, recorded_families, replayed

from tau import Body, ImageRule, Node, Outcome, Part

CEILING_PER_NODE = 16
"""D26 section 4: what a ceiling adds per node sent, on top of one token per byte. Decision 3
of D26: raise it if a fixture's per-message overhead comes close (`test_..._ceilings` prints
the closest)."""


def wire_only(case_dir: Path) -> bool:
    """A raw case the provider cannot read (a response that is not streamed): a fixture of
    the wire alone."""
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    return case["kind"] == "raw" and not case.get("raw", {}).get("adapter", True)


@scenario
async def replays_as_recorded(case_dir: Path, tmp_path: Path) -> None:
    if wire_only(case_dir):
        return
    async with replayed(case_dir, tmp_path) as run:
        assert run.replies == run.expected, f"{case_dir}: usage, stop or finish_reason changed"
        assert run.error == run.expected_error, f"{case_dir}: the error read changed"
        if run.case["kind"] == "turns":
            assert run.sent == run.recorded, f"{case_dir}: the provider sends other requests now"


FAMILIES_ON_DISK = tuple(sorted({*FAMILIES, *recorded_families()}))


@pytest.mark.parametrize("family", FAMILIES_ON_DISK)
def test_a_recorded_case_replays_to_its_golden_replies_and_its_requests(
    family: str, tmp_path: Path
) -> None:
    recorded = cases(family)
    if not recorded:
        pytest.skip(f"no recorded fixtures for {family}: run `{RECORD.get(family, family)}`")
    for case_dir in recorded:
        replays_as_recorded(case_dir, tmp_path)


def ceiling(node: Node, provider: str = "openai") -> int:
    """A node's ceiling as D26 section 4 defines it, computed here from the node alone: its
    UTF-8 bytes (text, reasoning, replay data, call ids, names and arguments, outcomes) plus
    `CEILING_PER_NODE`, and each image's `ImageRule(provider).tokens` (TC-05, D26 section 7.3);
    0 for a node that is not sent."""

    def size(*texts: str) -> int:
        return sum(len(t.encode()) for t in texts)

    def part(p: Part) -> int:
        replay = getattr(p, "replay", None)
        signed = 0 if replay is None else size(replay.data)
        match p:
            case Part.Text(text=text) | Part.Thinking(text=text):
                return size(text) + signed
            case Part.Call(id=id, tool=tool, args_raw=args):
                return size(id, tool, args) + signed
            case _:
                return signed

    def outcome(o: Outcome) -> int:
        match o:
            case Outcome.Ok(value=value):
                pictures = sum(ImageRule(provider).tokens(i.width, i.height) for i in o.images)
                return size(value) + pictures
            case Outcome.Err(error=error):
                return size(error)
            case _:
                return size("Interrupted")

    match node.body:
        case Body.System(text=text) | Body.User(text=text):
            return size(text) + CEILING_PER_NODE
        case Body.Assistant(parts=parts):
            return sum(part(p) for p in parts) + CEILING_PER_NODE
        case Body.Results(outcomes=outcomes):
            return sum(size(call) + outcome(o) for call, o, _ in outcomes) + CEILING_PER_NODE
        case _:
            return 0


async def replay_of(case_dir: Path, tmp_path: Path) -> Run:
    async with replayed(case_dir, tmp_path) as run:
        return run


def turn_cases(family: str) -> list[Path]:
    recorded = [c for c in cases(family) if not wire_only(c)]
    if not recorded:
        pytest.skip(f"no recorded fixtures for {family}: run `{RECORD.get(family, family)}`")
    return recorded


@scenario
async def spans_fit_their_ceilings(case_dir: Path, tmp_path: Path) -> None:
    run = await replay_of(case_dir, tmp_path)
    for span in run.spans:
        most = sum(ceiling(n, run.case["provider"]) for n in span.nodes)
        ids = [n.id for n in span.nodes]
        assert span.cost <= most, (
            f"{case_dir}: the span of nodes {ids} cost {span.cost} tokens, more than its "
            f"ceiling of {most}: the ceiling assumption (one token per byte and "
            f"{CEILING_PER_NODE} per node, D26 section 4) does not hold for this server"
        )


@pytest.mark.parametrize("family", FAMILIES_ON_DISK)
def test_no_proved_span_costs_more_than_its_ceilings(family: str, tmp_path: Path) -> None:
    """D26 T2: on every fixture. It starts to say something the day a family's fixtures land."""
    for case_dir in turn_cases(family):
        spans_fit_their_ceilings(case_dir, tmp_path)


@pytest.mark.parametrize("family", [f for f in FAMILIES_ON_DISK if f != "vllm"])
def test_a_recorded_conversation_proves_spans(family: str, tmp_path: Path) -> None:
    """Each family's way of proving a span works against its real server: Anthropic by its
    cache report, llama.cpp by its slot or the request's prefix, OpenAI and Gemini by the
    prefix. (vLLM reports no cache and its spans are the prefix's, as OpenAI's are.)"""
    proofs = {
        "anthropic": {"cache"},
        "llamacpp": {"slot", "prefix"},
    }

    @scenario
    async def check(case_dir: Path) -> None:
        run = await replay_of(case_dir, tmp_path)
        if run.case["kind"] != "turns" or len(run.replies) < 2:  # one request proves nothing
            return
        if family == "anthropic" and run.case.get("scenario") != "cached":
            return  # under the minimum cacheable prompt nothing is cached: nothing to prove by
        assert run.spans, f"{case_dir}: no span was proved in a recorded conversation"
        assert {s.proof for s in run.spans} <= proofs.get(family, {"prefix", "cache"})

    for case_dir in turn_cases(family):
        check(case_dir)


def test_the_anthropic_cache_miss_turn_proves_no_span(tmp_path: Path) -> None:
    """D26 T2: the turn recorded after the gap read nothing from the cache, so the span before
    it is not proved, while the turns before the gap are."""

    @scenario
    async def check(case_dir: Path) -> None:
        run = await replay_of(case_dir, tmp_path)
        gaps = run.case["gaps"]
        if not any(gaps) or run.case.get("scenario") != "cached":
            return
        last_user = max(n.id for n in run.walk if isinstance(n.body, Body.User))
        covered = {n.id for s in run.spans for n in s.nodes}
        assert run.spans, f"{case_dir}: the turns before the gap prove their spans"
        assert last_user not in covered, f"{case_dir}: the turn after the gap was a cache miss"

    gapped = [c for c in turn_cases("anthropic") if (c / "case.json").is_file()]
    for case_dir in gapped:
        check(case_dir)
