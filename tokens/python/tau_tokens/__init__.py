"""Local tokenizers for τ: the wheel `ffwf-tau-tokens`, which `pip install 'ffwf-tau[tokens]'` adds.

Bytes in, counts out: nothing here reads a file, opens a connection or reads a clock. The caller
(τ's `tokens` extension) brings the bytes of a `tokenizer.json`, a chat template's text and a
request's messages, and gets token ids, counts and rendered text.

- `Tokenizer`: `Tokenizer.tiktoken("o200k_base")` or `Tokenizer.from_json(data)`; `encode`, `count`.
- `ChatTemplate`: a model's chat template; `render(messages, tools)` is the text the model is given.
- `parse_identity`: `tiktoken:NAME`, `hf:OWNER/REPO[@REVISION]`, `file:/abs/tokenizer.json`.
- `catalogue`, `candidates`, `verdict`: which tokenizers a model might use, and whether a
  server's reported counts agree with one.

A count here is the count the reference implementations give for the same files (`tokenizers`,
`tiktoken`, `transformers`' `apply_chat_template`); whether a server bills it is for the server's
reports to confirm.
"""

from importlib.metadata import version

from tau_tokens._core import (
    ChatTemplate,
    IdentityError,
    TemplateError,
    Tokenizer,
    TokenizerError,
    candidates,
    catalogue,
    parse_identity,
    verdict,
)

__version__ = version("ffwf-tau-tokens")
"""The wheel's version; τ's `tokens` extension requires it to equal `tau.__version__`."""

__all__ = [
    "ChatTemplate",
    "IdentityError",
    "TemplateError",
    "Tokenizer",
    "TokenizerError",
    "__version__",
    "candidates",
    "catalogue",
    "parse_identity",
    "verdict",
]
