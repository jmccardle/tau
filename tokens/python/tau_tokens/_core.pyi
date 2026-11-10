"""Type stubs for the native module built from crates/tau-tokens-py.

Hand-written. `python -m mypy.stubtest tau_tokens._core` (run by scripts/check.sh) fails when
this file and the built module disagree, and `scripts/stub_docs.py --check` when a docstring
here is not the built module's (the Rust `///` is the source; `scripts/stub_docs.py` copies it).
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, final

__all__ = [
    "ChatTemplate",
    "IdentityError",
    "TemplateError",
    "Tokenizer",
    "TokenizerError",
    "candidates",
    "catalogue",
    "parse_identity",
    "verdict",
]

class TokenizerError(ValueError):
    """Bytes that are not a `tokenizer.json`, a name that is not an embedded encoding, or text a tokenizer could not encode."""

class TemplateError(ValueError):
    """A chat template that does not parse, cannot render the request, reads a clock it was not given, or refused the request (`raise_exception`); or a request that is not what a template is given."""

class IdentityError(ValueError):
    """A tokenizer identity that is none of `tiktoken:NAME`, `hf:OWNER/REPO[@REVISION]` and `file:/abs/tokenizer.json`."""

@final
class Tokenizer:
    """A tokenizer: an embedded `OpenAI` encoding, or a Hugging Face `tokenizer.json`.

    `count(text)` is always `len(encode(text))`. Nothing is added around the text (no BOS).
    A Hugging Face tokenizer finds its special tokens in the text, as a server does when it
    encodes a rendered request; a tiktoken encoding reads them as ordinary text. One tokenizer
    may be used from several threads at once.
    """

    @staticmethod
    def tiktoken(name: str) -> Tokenizer:
        """One of the embedded encodings: `o200k_base` or `cl100k_base`. Any other name is a
        `TokenizerError`.
        """
    @staticmethod
    def from_json(data: bytes) -> Tokenizer:
        """A Hugging Face tokenizer from the bytes of its `tokenizer.json`. Truncation and padding
        the file asks for are turned off. Bytes that are not such a file are a `TokenizerError`.
        """
    def encode(self, text: str) -> list[int]:
        """The token ids of `text`."""
    def count(self, text: str) -> int:
        """How many tokens `text` is. A Hugging Face tokenizer remembers the count of every piece
        of text between two special tokens, so counting a conversation again after a message
        was added tokenizes only that message.
        """
    @property
    def vocab_size(self) -> int:
        """How many token ids the tokenizer has, added tokens included."""
    def work(self) -> tuple[int, int]:
        """The work `count` has done so far, as `(tokenized, reused)`: pieces of text it tokenized,
        and pieces whose count it remembered.
        """

@final
class ChatTemplate:
    """A model's chat template: what turns a request's messages and tools into the text the model
    is given, rendered as `transformers`' `apply_chat_template` renders it.

    `ChatTemplate(source, tokens)` takes the template's text and the special tokens it may name
    (`{"bos_token": "<s>", ...}`). A template that does not parse is a `TemplateError`.
    """

    def __new__(cls, source: str, tokens: Mapping[str, str] | None = None) -> ChatTemplate: ...
    @staticmethod
    def from_config(config: bytes, source: str | None = None) -> ChatTemplate:
        """A template from the bytes of a `tokenizer_config.json`: its `chat_template` and its
        special tokens. `source`, when given, is the template instead (a repository's
        `chat_template.jinja`, or the server's own), and the file gives only the tokens.
        """
    def render(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        add_generation_prompt: bool = True,
        extra: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> str:
        """The text the model is given for a request.

        `messages` and `tools` are lists of dicts as the chat API takes them (a tool call's
        `arguments` as the server passes them to the template: most parse them into a dict).
        `add_generation_prompt` ends the text where the reply begins. `extra` is more variables
        for the template (`enable_thinking`, `reasoning_effort`). `now` is the
        `datetime.datetime` that `strftime_now` prints; a template that reads the clock without
        one is a `TemplateError`, as is a template that fails on the request or refuses it.
        """

def parse_identity(text: str) -> tuple[Literal["tiktoken", "hf", "file"], str, str | None]:
    """Read a tokenizer identity: `("tiktoken", name, None)`, `("hf", "OWNER/REPO", revision)` with
    `revision` `None` when the identity names none, or `("file", path, None)`. Anything else is
    an `IdentityError` that says which forms there are.
    """

def catalogue() -> list[dict[str, Any]]:
    """The tokenizer catalogue: one dict per base tokenizer, with its `id` (an identity, pinned to
    a commit when it is a Hugging Face repository), `vocab` (how many token ids it has),
    `served` (other vocabulary sizes a server reports for it) and `models` (patterns over a
    lowercased model id, `*` for any run of characters).
    """

def candidates(model: str, n_vocab: int | None = None) -> list[tuple[str, int]]:
    """The catalogue's candidates for a model, best first, as `(id, prior)`. With `n_vocab` (the
    vocabulary size the server reports) only tokenizers of that size are kept, allowing for
    padding. `prior` is how specific the catalogue's match of the model id is; 0 means the id
    says nothing for that tokenizer.
    """

def verdict(
    pairs: Sequence[tuple[str, int, int]],
) -> Literal["candidate", "confirmed", "dropped"]:
    """What a server's counts say about a candidate tokenizer: `"candidate"`, `"confirmed"` or
    `"dropped"`. Each pair is `(group, reported, counted)`: the tokens the server reported for
    some text and the tokens the candidate counts in it; `group` names what must stay the same
    for their difference to be a constant (a reply, or a mix of roles). Confirmed: at least
    three pairs of one group with one difference, one of them of 200 tokens or more. Dropped:
    two pairs of one group with different differences.
    """
