"""Tokenizer identities, the catalogue, and fingerprinting (D26 §7.4), through `tau_tokens`."""

import os
from importlib.metadata import requires

import pytest

import tau
import tau_tokens
from tau_tokens import IdentityError, Tokenizer, candidates, catalogue, parse_identity, verdict

REPLIES = [
    "Paris.",
    "The file holds three pangrams: the quick brown fox, the five dozen liquor jugs, and the "
    "daft zebras. None of them is longer than a line, and none repeats a word of another. " * 8,
    "def f(x: int) -> str:\n    return f'{x:>08.3f}'\n",
    "D'accord — « très bien ». こんにちは、世界！",
]


def test_an_identity_has_three_forms() -> None:
    assert parse_identity("tiktoken:o200k_base") == ("tiktoken", "o200k_base", None)
    assert parse_identity("hf:Qwen/Qwen3-8B") == ("hf", "Qwen/Qwen3-8B", None)
    assert parse_identity("hf:Qwen/Qwen3-8B@b968826d") == ("hf", "Qwen/Qwen3-8B", "b968826d")
    # A file is a path absolute on this machine: `/models/...` here, `D:\models\...` on Windows.
    file = os.path.abspath("/models/qwen/tokenizer.json")
    assert parse_identity(f"file:{file}") == ("file", file, None)


@pytest.mark.parametrize(
    "text",
    [
        "none",
        "",
        "o200k_base",
        "hf:Qwen3-8B",
        "hf:../../etc",
        "hf:a/b@refs/pr/1",
        "file:tokenizer.json",
    ],
)
def test_anything_else_is_an_identity_error_naming_the_forms(text: str) -> None:
    with pytest.raises(
        IdentityError, match=r"tiktoken:NAME|hf:OWNER/REPO|file:/abs|revision after @"
    ) as raised:
        parse_identity(text)
    assert isinstance(raised.value, ValueError)


def test_the_catalogue_lists_identities_with_sizes_and_patterns() -> None:
    listed = catalogue()
    assert len(listed) == 11
    for tokenizer in listed:
        assert set(tokenizer) == {"id", "vocab", "served", "models"}
        kind, name, revision = parse_identity(tokenizer["id"])
        assert kind == "tiktoken" or (kind == "hf" and revision is not None and len(revision) == 40)
        if kind == "tiktoken":
            assert Tokenizer.tiktoken(name).vocab_size == tokenizer["vocab"]
    assert listed[0]["id"] == "tiktoken:o200k_base"


def test_a_model_id_ranks_candidates_and_a_vocabulary_size_filters_them() -> None:
    assert [i for i, _ in candidates("gpt-4o-mini")][:2] == [
        "tiktoken:o200k_base",
        "tiktoken:cl100k_base",
    ]
    assert candidates("gpt-4-turbo")[0][0] == "tiktoken:cl100k_base"
    assert candidates("local/Qwen3-8B-Q4_K_M.gguf")[0][0].startswith("hf:Qwen/Qwen3-8B@")
    assert all(prior == 0 for _, prior in candidates("gpt-6")), "an unknown model has no prior"
    assert len(candidates("gpt-6")) == len(catalogue())
    # llama.cpp reports the model's own size (151,936 for Qwen3), not the tokenizer's 151,669.
    assert [i.split("@")[0] for i, _ in candidates("anything", 151936)] == ["hf:Qwen/Qwen3-8B"]
    assert candidates("qwen3", 128256)[0][0].startswith("hf:unsloth/Llama-3.2"), "the size wins"
    assert candidates("qwen3", 12345) == []


def test_a_constant_residual_confirms_and_a_moving_one_drops() -> None:
    assert verdict([]) == "candidate"
    assert verdict([("reply", 12, 9), ("reply", 243, 240)]) == "candidate"
    assert verdict([("reply", 12, 9), ("reply", 243, 240), ("reply", 53, 50)]) == "confirmed"
    assert verdict([("reply", 12, 9), ("reply", 43, 40), ("reply", 53, 50)]) == "candidate"
    assert verdict([("reply", 12, 9), ("reply", 250, 240)]) == "dropped"
    assert verdict([("user", 12, 9), ("user+results", 250, 240), ("user", 53, 50)]) == "candidate"


def test_a_servers_counts_pick_its_tokenizer_within_three_replies() -> None:
    """A server that counts with o200k_base and adds 3 tokens around each reply: o200k_base is
    confirmed by the third reply, and cl100k_base is dropped by the second."""
    server = Tokenizer.tiktoken("o200k_base")
    reported = [server.count(text) + 3 for text in REPLIES]
    seen: dict[str, list[str]] = {"o200k_base": [], "cl100k_base": []}
    for name, verdicts in seen.items():
        candidate = Tokenizer.tiktoken(name)
        pairs: list[tuple[str, int, int]] = []
        for text, bill in zip(REPLIES, reported, strict=True):
            pairs.append(("reply", bill, candidate.count(text)))
            verdicts.append(verdict(pairs))
    assert seen["o200k_base"][:3] == ["candidate", "candidate", "confirmed"]
    assert seen["cl100k_base"][1] == "dropped"


def test_the_two_wheels_have_one_version_and_this_one_depends_on_nothing() -> None:
    assert tau_tokens.__version__ == tau.__version__
    assert not requires("ffwf-tau-tokens")
    assert sorted(tau_tokens.__all__) == sorted(
        n for n in dir(tau_tokens) if n in tau_tokens.__all__
    )
