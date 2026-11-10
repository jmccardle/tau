"""The `tau_tokens` tests (the wheel `ffwf-tau-tokens`, D26 §7): what they share.

Every test under this directory is marked `tokenizers` and imports `tau_tokens` at the top of its
file, so with the wheel absent it is a collection error, never a skip. The one way to run the
suite without the wheel is to say so: `-m "not tokenizers"` leaves this directory out.

The reference values are tests/fixtures/tokens/, made by scripts/tokens_goldens.py from
`transformers`, `tokenizers` and `tiktoken`. The real tokenizer files are not checked in:
`real_files` gives their directory, or skips with the command that fetches them; with
`TAU_TOKENS_FILES=require` in the environment (CI) their absence fails instead.
"""

import os
from datetime import datetime
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest
from tokens_golden import FETCH, FILES, load


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Leave this directory out only when the run says `-m "not tokenizers"` and the wheel is
    absent; in every other case a missing wheel is the import error it should be."""
    asked = "not tokenizers" in str(config.getoption("markexpr", ""))
    if asked and find_spec("tau_tokens") is None:
        return True
    return None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if Path(__file__).parent in item.path.parents:
            item.add_marker(pytest.mark.tokenizers)


@pytest.fixture(scope="session")
def goldens() -> Any:
    return load("goldens.json")


@pytest.fixture(scope="session")
def texts() -> dict[str, str]:
    return load("texts.json")


@pytest.fixture(scope="session")
def requests() -> list[dict[str, Any]]:
    """The requests of conversations.json as `ChatTemplate.render` takes them: `messages`,
    `tools` (or None), `add_generation_prompt`, `extra` (or None), and `name`."""
    file = load("conversations.json")
    return [
        {
            "name": c["name"],
            "messages": c["messages"],
            "tools": file["tools"][c["tools"]] if "tools" in c else None,
            "add_generation_prompt": c.get("generate", True),
            "extra": c.get("extra"),
        }
        for c in file["conversations"]
    ]


@pytest.fixture(scope="session")
def now(goldens: Any) -> datetime:
    """The time the references were rendered at."""
    return datetime(*goldens["now"])


@pytest.fixture(scope="session")
def real_files() -> dict[str, Path]:
    """Artifact name -> the directory of its fetched files, every pinned file checked to be
    there. Absent files skip the test with the command that fetches them, or fail it when
    `TAU_TOKENS_FILES=require`."""
    out: dict[str, Path] = {}
    missing: list[str] = []
    for artifact in load("artifacts.json")["artifacts"]:
        directory = FILES / artifact["repo"] / artifact["revision"]
        missing += [
            f"{artifact['repo']} {n}" for n in artifact["files"] if not (directory / n).exists()
        ]
        out[artifact["name"]] = directory
    if missing and os.environ.get("TAU_TOKENS_FILES") == "require":
        pytest.fail(f"{FETCH} (missing: {', '.join(missing[:3])}, ...)")
    if missing:
        pytest.skip(FETCH)
    return out
