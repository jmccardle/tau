"""The option `extensions.tokens.tokenizers` (D26 section 7.2, John's ruling of 2026-11-24):
`require` with the tokenizer wheel absent, broken or stale stops the start with the command
that repairs it; `auto` with it broken or stale warns once and does not use it; `off` never
imports it. The wheel is reached through `Tokens`' `importer`, so each case here hands it a
module of its own."""

import importlib.metadata
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest
from fakes import FakeProvider, Reply
from harness import Harness, scenario

from tau import Part, Tokens

VERSION = importlib.metadata.version("ffwf-tau")
INSTALL = f"pip install 'ffwf-tau[tokens]=={VERSION}'"


class _Tokenizer:
    def __init__(self, counts: int) -> None:
        self._counts = counts

    def count(self, text: str) -> int:
        return self._counts


def wheel(version: str = VERSION, counts: int = 2) -> ModuleType:
    """A stand-in for `tau_tokens`: its version, and a tokenizer that counts every text as
    `counts` (the real one counts the smoke text as 2)."""
    tokenizer = SimpleNamespace(tiktoken=lambda name: _Tokenizer(counts))
    return cast("ModuleType", SimpleNamespace(__version__=version, Tokenizer=tokenizer))


def importing(module: ModuleType | Exception):
    asked: list[str] = []

    def importer(name: str) -> ModuleType:
        asked.append(name)
        if isinstance(module, Exception):
            raise module
        return module

    return importer, asked


def absent() -> ModuleNotFoundError:
    return ModuleNotFoundError("No module named 'tau_tokens'", name="tau_tokens")


@pytest.mark.parametrize(
    ("module", "said"),
    [
        (absent(), "not installed"),
        (wheel("0.0.1"), f"ffwf-tau-tokens 0.0.1 does not match ffwf-tau {VERSION}"),
        (RuntimeError("bad ELF header"), "does not import (RuntimeError: bad ELF header)"),
        (wheel(counts=3), "counts 'hello world' as 3 o200k_base tokens, not 2"),
        (
            ModuleNotFoundError("No module named 'tau_tokens._core'", name="tau_tokens._core"),
            "does not import",
        ),
    ],
)
def test_require_without_a_usable_wheel_stops_the_start_naming_the_fix(
    tmp_path: Path, module: ModuleType | Exception, said: str
) -> None:
    importer, _ = importing(module)
    with pytest.raises(ImportError, match="tokenizers is require") as caught:
        Tokens(tmp_path, mode="require", importer=importer)
    assert said in str(caught.value)
    assert INSTALL in str(caught.value)


def test_require_and_auto_accept_a_wheel_of_this_version_that_counts(tmp_path: Path) -> None:
    for mode in ("require", "auto"):
        importer, asked = importing(wheel())
        tokens = Tokens(tmp_path, mode=mode, importer=importer)
        assert asked == ["tau_tokens"]
        assert "tokenizers" not in tokens.tokens(None)  # pyright: ignore[reportArgumentType]


def test_off_never_imports_it(tmp_path: Path) -> None:
    importer, asked = importing(RuntimeError("never"))
    Tokens(tmp_path, mode="off", importer=importer)
    assert asked == []


def test_auto_without_the_wheel_says_nothing(tmp_path: Path) -> None:
    importer, _ = importing(absent())
    tokens = Tokens(tmp_path, mode="auto", importer=importer)
    assert tokens.tokens(cast("object", None)) == "no model has been counted or observed yet"  # type: ignore[arg-type]


@scenario
async def test_auto_with_a_stale_wheel_warns_once_and_does_not_use_it(tmp_path: Path) -> None:
    importer, _ = importing(wheel("0.0.1"))
    tokens = Tokens(tmp_path / "home", mode="auto", importer=importer)
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Text("one")]), Reply([Part.Text("two")])]),
        extensions=[tokens],
    )
    for text in ("hello", "again"):
        await h.send(text)
        await h.settle()
    warned = [n for n in h.notices() if "tokenizer wheel is not used" in n]
    assert len(warned) == 1
    assert f"ffwf-tau-tokens 0.0.1 does not match ffwf-tau {VERSION}" in warned[0]
    assert "extensions.tokens.tokenizers=off" in warned[0], "the way past it is named"
    assert "tokenizers: not used" in str(await h.run("tokens"))
    await h.close()
