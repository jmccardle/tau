"""No committed provider fixture holds anything that looks like a key, an auth header or an
organization id, and no local server's address: the recorder refuses to write such a file, and
this holds every file under `tests/fixtures/providers` to the same rule with its own list, so a
weakened recorder cannot let one through. A test of the list itself shows each shape is caught."""

import re
from pathlib import Path

import pytest
from replay import ROOT

SHAPES = {
    "an OpenAI-style secret key": re.compile(rb"sk-[A-Za-z0-9_\-]{20,}"),
    "an Anthropic key": re.compile(rb"sk-ant-[A-Za-z0-9_\-]{8,}"),
    "an OpenRouter key": re.compile(rb"sk-or-[A-Za-z0-9_\-]{8,}"),
    "a Google API key": re.compile(rb"AIza[0-9A-Za-z_\-]{30,}"),
    "a bearer token": re.compile(rb"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    "an auth header": re.compile(
        rb"(?i)[\"'](authorization|x-api-key|x-goog-api-key|api-key|cookie|set-cookie)[\"']\s*:"
    ),
    "an organization or project header": re.compile(rb"(?i)openai-(organization|project)"),
    "an OpenAI organization id": re.compile(rb"\borg-[A-Za-z0-9]{20,}"),
    "a key in a URL": re.compile(rb"[?&](key|api_key|access_token|token)=[^&\s\"]{8,}"),
    "a private IPv4 address": re.compile(
        rb"\b(10\.\d{1,3}|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"
    ),
}


def found(data: bytes) -> list[str]:
    """What in `data` looks like a secret, by `SHAPES`' names."""
    return [name for name, shape in SHAPES.items() if shape.search(data)]


def fixture_files() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*") if p.is_file()) if ROOT.is_dir() else []


def test_no_fixture_file_holds_anything_shaped_like_a_key() -> None:
    files = fixture_files()
    if not files:
        pytest.skip(f"no fixtures under {ROOT}")
    leaks = {str(p.relative_to(ROOT)): found(p.read_bytes()) for p in files}
    assert {k: v for k, v in leaks.items() if v} == {}


@pytest.mark.parametrize(
    ("sample", "shape"),
    [
        (b'"x": "sk-proj-' + b"a" * 40 + b'"', "an OpenAI-style secret key"),
        (b"sk-ant-api03-abcdefghijkl", "an Anthropic key"),
        (b"sk-or-v1-abcdefghijkl", "an OpenRouter key"),
        (b"AIzaSyA" + b"b" * 32, "a Google API key"),
        (b"Authorization: Bearer abcdefghijklmnop", "a bearer token"),
        (b'{"x-api-key": "k"}', "an auth header"),
        (b'{"openai-organization": "o"}', "an organization or project header"),
        (b"org-" + b"c" * 24, "an OpenAI organization id"),
        (b"https://h/v1?key=abcdefghij", "a key in a URL"),
        (b"http://192.168.1.100:8100/v1", "a private IPv4 address"),
    ],
)
def test_each_shape_is_caught(sample: bytes, shape: str) -> None:
    assert shape in found(sample)


def test_what_a_fixture_legitimately_holds_is_not_caught() -> None:
    clean = (
        b'{"model": "qwen3.8-27b", "url": "http://llamacpp.invalid/v1/chat/completions", '
        b'"headers": {"content-type": "application/json"}, "id": "chatcmpl-6xZFW4MFJ4eRBDZ", '
        b'"system_fingerprint": "b489-8ca4060", "tokens": [151644, 8948]}'
    )
    assert found(clean) == []
