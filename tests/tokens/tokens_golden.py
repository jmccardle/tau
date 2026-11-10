"""Where the `tau_tokens` tests' reference values are, and how they are kept (see conftest.py)."""

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "tokens"
FILES = ROOT / "target" / "tokens-files"
FETCH = "needs the tokenizer files: scripts/tokens_goldens.py fetch"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def ids_hash(ids: list[int]) -> str:
    """How goldens.json keeps a list of ids: sha256 of the decimal ids joined by commas."""
    return hashlib.sha256(",".join(map(str, ids)).encode()).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
