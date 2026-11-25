"""What an image costs as a count (D26 section 7.3, D30 section 9): the rule of the model's
provider, with the provider's published resize, and the model's `image_tokens` over it.

The figures are the providers' published rules, read 2026-11-24:
- Anthropic (platform.claude.com, "Vision" and "Coordinates and bounding boxes"): one token for each
  28-pixel patch; a larger image is scaled down to the tier's long edge and visual-token limit
  (1,568 px and 1,568 tokens; 2,576 px and 4,784 tokens on Claude 4.7 and later). Not published,
  measured with `count_tokens` on 2026-11-25 (`MEASURED`): an image in a tool result bills 21
  tokens more than its patches (`ANTHROPIC_IMAGE_OVERHEAD`), the first of a result with text.
- Gemini (aistudio.google.com, "Media resolution" and "Tokens"): Gemini 4 and 3 at most 1,120 per
  image at the default and `high` resolution; earlier models 258 per 768-pixel tile, and 258 for
  an image at most 384 pixels a side.
- OpenAI ("Images and vision"): `85 + 170` per 512-pixel tile after the fit within 2048 and the
  shorter side to 768 (GPT-4o, GPT-4.1); other models tile or patch differently, so the
  `openai` provider's figure is an estimate.
"""

import json
import math
from pathlib import Path

import pytest
from fakes import FakeProvider, Reply
from harness import Harness, scenario
from pngs import blank

from tau import (
    Basis,
    Body,
    ConfigError,
    Cursor,
    ImageLimits,
    ImageRule,
    Outcome,
    Part,
    ReadTool,
    RequestCount,
    Stop,
    image_rule,
    read_models,
)

SIZES = [(1, 1), (1024, 768), (1920, 1080), (3840, 2160), (8000, 100), (100, 8000), (8000, 8000)]

# Each provider's figure for each of SIZES, in that order.
EXPECTED = {
    "anthropic": [22, 1057, 2712, 4805, 1165, 1165, 4805],
    "gemini": [1120, 1120, 1548, 3870, 2838, 2838, 31218],
    "openai": [255, 1036, 2691, 10764, 1144, 1144, 81796],
}


@pytest.mark.parametrize("provider", sorted(EXPECTED))
def test_each_provider_counts_the_published_sizes(provider: str) -> None:
    rule = ImageRule(provider)
    assert [rule.tokens(w, h) for w, h in SIZES] == EXPECTED[provider]


def test_a_4k_screenshot_is_capped_by_the_providers_that_publish_a_cap() -> None:
    """The bug of TC-05: 3840x2160 was 10,764 for every model, before any resize."""
    assert ImageRule("anthropic").tokens(3840, 2160) == 4784 + 21
    assert ImageRule("gemini").tokens(3840, 2160) == 3870
    assert ImageRule("openai").tokens(3840, 2160) == 10764, "no cap holds for every server"


def _claude_resized(width: int, height: int, max_edge: int, max_tokens: int) -> tuple[int, int]:
    """Anthropic's reference implementation of its resize ("How Claude resizes and pads images"),
    as published."""

    def fits(w: int, h: int) -> bool:
        return (
            math.ceil(w / 28) * 28 <= max_edge
            and math.ceil(h / 28) * 28 <= max_edge
            and math.ceil(w / 28) * math.ceil(h / 28) <= max_tokens
        )

    if fits(width, height):
        return width, height
    if height > width:
        h, w = _claude_resized(height, width, max_edge, max_tokens)
        return w, h
    aspect = width / height
    lo, hi = 1, width
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if fits(mid, max(round(mid / aspect), 1)):
            lo = mid
        else:
            hi = mid
    return lo, max(round(lo / aspect), 1)


TIERS = [(1568, 1568), (2576, 4784)]
MORE = [(1075, 1520), (2000, 1500), (1092, 1092), (200, 200), (7999, 3), (3, 7999), (2577, 1)]


@pytest.mark.parametrize(("width", "height"), SIZES + MORE)
def test_the_claude_ceiling_is_never_under_either_tiers_real_cost(width: int, height: int) -> None:
    most = ImageRule("anthropic").tokens(width, height)
    for edge, budget in TIERS:
        w, h = _claude_resized(width, height, edge, budget)
        assert math.ceil(w / 28) * math.ceil(h / 28) + 21 <= most, (edge, w, h)


MEASURED = {
    # Messages `count_tokens`, 2026-11-25, on claude-haiku-4-5, claude-sonnet-4-6 (standard tier),
    # claude-opus-4-7 and claude-opus-5-5 (high tier): what one PNG of (width, height) added to a
    # `tool_result` whose content was the text "Swatch S-1, as an image:" (tau's form: the text,
    # then the images). The same on all four models where the tiers agree.
    (1, 1): 22,
    (64, 64): 30,  # also the recorded tests/fixtures/providers/anthropic/tool-image: 810 vs 780
    (1000, 1000): 1317,
}
MEASURED_HIGH_TIER = {(3840, 2160): 4805}  # opus-5-5; the standard tier billed 1,581 (1,560 + 21)
# Each image after the first in one result billed its patches + 13 (64x64: 22), and an image in a
# user message its patches + 3 (64x64: 12: a raw request of 30 tokens vs 18 without the image,
# recorded before D31 replaced that fixture with tau's own request).


@pytest.mark.parametrize(("size", "cost"), [*MEASURED.items(), *MEASURED_HIGH_TIER.items()])
def test_the_claude_ceiling_is_never_under_a_measured_cost(
    size: tuple[int, int], cost: int
) -> None:
    """The first image of a result is billed exactly the ceiling; later ones 8 less."""
    assert ImageRule("anthropic").tokens(*size) == cost


def test_the_published_examples_of_the_claude_tiers() -> None:
    """The Vision page's table: 1920x1080 is 1,560 on the standard tier and 2,691 on the high one;
    3840x2160 is 1,560 and 4,784. The ceiling is the high tier's, since it holds for both."""
    assert _claude_resized(1920, 1080, 1568, 1568) == (1456, 819)
    assert ImageRule("anthropic").tokens(1920, 1080) == 2691 + 21
    assert _claude_resized(3840, 2160, 2576, 4784) == (2576, 1449)


def test_gemini_never_counts_less_than_the_gemini_3_figure_or_the_tiles() -> None:
    rule = ImageRule("gemini")
    assert rule.tokens(384, 384) == 1120, "one 258-token image on 2.5, at most 1,120 on 3 and 4"
    assert rule.tokens(385, 384) == 1120, "one 768-pixel tile"
    assert rule.tokens(1537, 769) == 258 * 3 * 2


def test_which_rules_are_bounds() -> None:
    assert ImageRule("anthropic").bound
    assert ImageRule("gemini").bound
    assert not ImageRule("openai").bound, "one adapter, many servers: an estimate"
    assert not ImageRule("someone-elses").bound
    assert ImageRule("openai", 1024).bound, "a model's image_tokens is its server's cap"


def test_a_models_image_tokens_caps_its_providers_rule() -> None:
    assert ImageRule("openai", 1024).tokens(1, 1) == 1024, "the cap is the most, for any size"
    assert ImageRule("openai", 1024).tokens(3840, 2160) == 1024
    assert ImageRule("anthropic", 1568).tokens(3840, 2160) == 1568 + 21, "a standard-tier Claude"
    assert ImageRule("anthropic", 1568).tokens(28, 28) == 1 + 21, "under the cap: the patches"
    assert ImageRule("gemini", 5000).tokens(3840, 2160) == 3870


def test_image_tokens_is_a_model_key() -> None:
    models = {
        "local": {"model": "qwen", "image_tokens": 1024},
        "claude": {"model": "claude", "provider": "anthropic"},
    }
    catalog = read_models(cli={"models": models})
    assert image_rule(catalog.resolve("local")) == ImageRule("openai", 1024)
    assert image_rule(catalog.resolve("claude")) == ImageRule("anthropic")
    with pytest.raises(ConfigError, match="image_tokens"):
        read_models(cli={"models": {"bad": {"model": "x", "image_tokens": 0}}}).resolve("bad")


# Through the Harness: the count of a node holding a 4K screenshot, per provider.

SHOT = blank(3840, 2160)
LIMITS = ImageLimits(10_000_000, 8000)


class _Probe:
    """Counts the walk when told."""

    name = "probe"

    def __init__(self) -> None:
        self.counted: list[RequestCount] = []
        self.commands = (self.probe,)

    async def probe(self, cursor: Cursor) -> str:
        """Count this walk."""
        self.counted.append(await cursor.count())
        return "counted"


async def _count_shot(tmp_path: Path, provider: str, **model: object) -> tuple[int, RequestCount]:
    """The `read` of a 3840x2160 screenshot on a model of `provider`: the upper of its results
    node less its bytes, and the whole request count."""
    (tmp_path / "shot.png").write_bytes(SHOT)
    probe = _Probe()
    call = Reply([Part.Call("c1", "read", json.dumps({"path": "shot.png"}))], Stop.Tools)
    fake = FakeProvider([call, Reply([Part.Text("seen")])], image_limits=LIMITS, name=provider)
    config = {"model": "m", "provider": provider, "images": True, **model}
    h = await Harness.create(
        tmp_path,
        provider=fake,
        catalog=read_models(cli={"models": {"fake": config}}),
        tools=[ReadTool(tmp_path)],
        extensions=[probe],
    )
    await h.send("look")
    await h.settle()
    await h.run("probe")
    results = next(n for n in h.nodes() if isinstance(n.body, Body.Results))
    assert isinstance(results.body, Body.Results)
    ((call_id, outcome, _),) = results.body.outcomes
    assert isinstance(outcome, Outcome.Ok)
    text = len(call_id.encode()) + len(outcome.value.encode())
    await h.close()
    count = probe.counted[-1]
    node = count.nodes[results.id]
    assert node.basis is Basis.CEILING
    return node.upper - text - 16, count


@scenario
async def test_a_4k_screenshot_on_claude_counts_at_most_the_high_tiers_cap(tmp_path: Path) -> None:
    image, count = await _count_shot(tmp_path, "anthropic")
    assert image == 4784 + 21
    assert count.why is None


@scenario
async def test_a_4k_screenshot_on_gemini_counts_its_tiles(tmp_path: Path) -> None:
    image, count = await _count_shot(tmp_path, "gemini")
    assert image == 3870
    assert count.why is None


@scenario
async def test_on_an_openai_compatible_server_the_estimate_says_so(tmp_path: Path) -> None:
    image, count = await _count_shot(tmp_path, "openai")
    assert image == 10764
    assert count.why is not None
    assert "estimated" in count.why
    assert "image_tokens" in count.why


@scenario
async def test_a_models_image_tokens_makes_the_ceiling_its_cap(tmp_path: Path) -> None:
    image, count = await _count_shot(tmp_path, "openai", image_tokens=1024)
    assert image == 1024
    assert count.why is None
