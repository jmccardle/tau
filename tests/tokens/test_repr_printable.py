"""A string a template prints inside a list is `repr`'d: every code point of Unicode must come out
as Python's own `repr` has it (which characters print as themselves is `str.isprintable`, and is
a table made from `unicodedata`: scripts/pyprintable.py)."""

import re
import unicodedata
from pathlib import Path

import pytest

from tau_tokens import ChatTemplate

TABLE = Path(__file__).parents[2] / "crates/tau-tokens/src/pyprintable.rs"
BATCH = 0x4000


def table_version() -> str:
    """The Unicode version the generated table records."""
    found = re.search(r'UNICODE_VERSION: &str = "([^"]+)"', TABLE.read_text(encoding="utf-8"))
    assert found, f"{TABLE} records no Unicode version"
    return found.group(1)


def test_every_code_point_prints_as_python_repr_prints_it() -> None:
    if unicodedata.unidata_version != table_version():
        pytest.skip(
            f"the table is Unicode {table_version()}, this Python is {unicodedata.unidata_version}"
        )
    template = ChatTemplate("{% for m in messages %}{{ [m.content] }}\n{% endfor %}")
    codes = [c for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF]
    for start in range(0, len(codes), BATCH):
        batch = codes[start : start + BATCH]
        got = template.render([{"role": "user", "content": chr(c)} for c in batch])
        want = "".join(f"{[chr(c)]!r}\n" for c in batch)
        if got != want:
            pairs = zip(batch, got.split("\n"), want.split("\n"), strict=False)
            bad = next(c for c, g, w in pairs if g != w)
            pytest.fail(f"U+{bad:04X}: got {chr(bad)!r}, Python prints {[chr(bad)]!r}")


def test_a_combining_mark_and_a_space_separator_print_as_python_prints_them() -> None:
    text = "e" + chr(0x301) + chr(0x3000) + chr(0x378)
    template = ChatTemplate("{{ [messages[0].content] }}")
    assert template.render([{"role": "user", "content": text}]) == repr([text])
    assert repr([text]) == "['e" + chr(0x301) + "\\u3000\\u0378']"
