"""`tau_tokens.Tokenizer` against the reference implementations, offline: the embedded encodings
against `tiktoken`, four small Hugging Face tokenizers against `tokenizers`."""

import json
import subprocess
import sys
import sysconfig
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from tokens_golden import FIXTURES, ids_hash, load

from tau_tokens import Tokenizer, TokenizerError

FREE_THREADED = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
TINY = ["bytelevel", "metaspace", "wordpiece", "split"]


def tiny(name: str) -> Tokenizer:
    return Tokenizer.from_json((FIXTURES / "tiny" / f"{name}.json").read_bytes())


@pytest.mark.parametrize("name", ["o200k_base", "cl100k_base"])
def test_an_embedded_encoding_gives_tiktokens_ids(
    name: str, goldens: Any, texts: dict[str, str]
) -> None:
    tokenizer = Tokenizer.tiktoken(name)
    golden = goldens["tiktoken"][name]
    assert tokenizer.vocab_size == golden["vocab"]
    for key, text in texts.items():
        ids = tokenizer.encode(text)
        assert ids_hash(ids) == golden["texts"][key]["ids"], key
        assert tokenizer.count(text) == len(ids) == golden["texts"][key]["count"], key


@pytest.mark.parametrize("name", TINY)
def test_a_tokenizer_json_gives_the_ids_of_the_tokenizers_library(name: str) -> None:
    tokenizer = tiny(name)
    golden = load("tiny/cases.json")[name]
    assert tokenizer.vocab_size == golden["vocab"]
    for case in golden["cases"]:
        ids = tokenizer.encode(case["text"])
        assert ids_hash(ids) == case["ids"], case["text"]
        assert tokenizer.count(case["text"]) == len(ids) == case["count"], case["text"]


def test_counting_again_tokenizes_only_what_is_new() -> None:
    tokenizer = tiny("bytelevel")
    messages = [
        f"<|im_start|>user\nMessage {n}: the quick brown fox.<|im_end|>\n" for n in range(300)
    ]
    first = tokenizer.count("".join(messages[:-1]))
    tokenized, _ = tokenizer.work()
    assert tokenized == 300  # the text of 299 messages, and the "\n" between two of them
    second = tokenizer.count("".join(messages))
    assert second == len(tokenizer.encode("".join(messages))) > first
    tokenized_after, reused = tokenizer.work()
    assert tokenized_after - tokenized == 1, "only the new message's text was tokenized"
    assert reused >= 598


def test_special_tokens_are_found_by_hf_and_are_text_to_tiktoken() -> None:
    assert len(tiny("bytelevel").encode("<|im_start|>")) == 1
    assert Tokenizer.tiktoken("o200k_base").count("<|endoftext|>") > 1
    assert tiny("bytelevel").encode("") == []
    assert Tokenizer.tiktoken("cl100k_base").count("") == 0


def test_what_is_not_a_tokenizer_raises_tokenizer_error() -> None:
    with pytest.raises(TokenizerError, match="o200k_base, cl100k_base") as raised:
        Tokenizer.tiktoken("p50k_base")
    assert isinstance(raised.value, ValueError)
    for data in [b"", b"not json", b"{}", json.dumps({"model": 3}).encode()]:
        with pytest.raises(TokenizerError, match=r"not a tokenizer\.json"):
            Tokenizer.from_json(data)
    with pytest.raises(TypeError):
        Tokenizer.from_json("a string, not bytes")  # pyright: ignore[reportArgumentType]


def test_a_tokenizer_is_frozen_and_made_only_by_its_two_constructors() -> None:
    tokenizer = tiny("split")
    with pytest.raises(AttributeError):
        tokenizer.vocab_size = 3  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(TypeError):
        Tokenizer()


def test_threads_share_one_tokenizer(texts: dict[str, str]) -> None:
    tokenizer = tiny("bytelevel")
    marked = [f"<|im_start|>{text}<|im_end|>" for text in texts.values()]
    expected = [len(tiny("bytelevel").encode(text)) for text in marked]

    def counts(_: int) -> list[int]:
        return [tokenizer.count(text) for text in marked]

    with ThreadPoolExecutor(max_workers=16) as pool:
        for got in pool.map(counts, range(64)):
            assert got == expected


@pytest.mark.skipif(not FREE_THREADED, reason="GIL build: nothing to re-enable")
def test_import_keeps_the_gil_disabled() -> None:
    code = "import sys, tau_tokens; print(sys._is_gil_enabled())"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "False", done.stderr
