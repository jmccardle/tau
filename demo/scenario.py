"""The fixtures and the scripts both versions are raced on. Deterministic: no clock, no randomness.

Race A: one prompt, CALLS_A sequential `read` calls (one per model step, each on a different file
of the fixture), then an answer.
Race B: TURNS_B user turns, each two sequential `read` steps and then an answer; the last answer
begins with FINISH_B, the text the reopen race waits to see on screen. After them the script holds
one more answer, LIVE_B, for the paced take: a message typed into the reopened session.
The same scripts serve both versions and both takes (instant and paced).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FILES = 300
CALLS_A = 100
TURNS_B = 200
STEPS_PER_TURN = 3  # two reads and an answer
FINISH_A = "RACE-A-DONE"
FINISH_B = f"Turn {TURNS_B} of {TURNS_B}"  # opens the last answer: short and at a line start, so a narrow pane never wraps it
FINISH_LIVE = "RACE-B-LIVE-DONE"
PROMPT_A = "Read every file under files/ and say when you are done."
PROMPT_B = "next turn"
PROMPT_LIVE = "tell me a little more"


def fixture_text(i: int) -> str:
    return "".join(
        f"file {i:03d} line {n:02d}: the quick brown fox {(i * 31 + n * 7) % 997} jumps over the lazy dog\n"
        for n in range(1, 13)
    )


def write_fixture(root: Path) -> Path:
    files = root / "files"
    files.mkdir(parents=True, exist_ok=True)
    for i in range(FILES):
        (files / f"f{i:03d}.txt").write_text(fixture_text(i), encoding="utf-8")
    return root


def read_call(i: int) -> dict[str, Any]:
    return {"name": "read", "arguments": {"path": f"files/f{i % FILES:03d}.txt"}}


def script_a() -> dict[str, Any]:
    responses: list[dict[str, Any]] = [
        {"content": None, "tool_calls": [read_call(i)]} for i in range(CALLS_A)
    ]
    responses.append({"content": f"I read {CALLS_A} files, one at a time. {FINISH_A}."})
    return {"responses": responses}


def answer_b(turn: int) -> str:
    text = (
        f"Turn {turn + 1} of {TURNS_B}: I read two files and compared them.\n\n"
        f"- `files/f{(2 * turn) % FILES:03d}.txt` is a dozen lines of fox.\n"
        f"- `files/f{(2 * turn + 1) % FILES:03d}.txt` is the same shape with other numbers.\n\n"
        "Nothing needs changing."
    )
    return text


LIVE_B = (
    "Happy to. Each file holds a dozen lines in one shape: the file's number, the line's number, "
    "and a figure that changes from file to file, wrapped in the same sentence about a fox.\n\n"
    "1. The first number tells you which file you are in.\n"
    "2. The second tells you where in the file you are.\n"
    "3. The figure is the only thing that varies.\n\n"
    f"That is all there is to it.\n\n{FINISH_LIVE}"
)


def script_b() -> dict[str, Any]:
    responses: list[dict[str, Any]] = []
    for t in range(TURNS_B):
        responses.append({"content": None, "tool_calls": [read_call(2 * t)]})
        responses.append({"content": None, "tool_calls": [read_call(2 * t + 1)]})
        responses.append({"content": answer_b(t)})
    responses.append({"content": LIVE_B})
    return {"responses": responses}


def script_hello() -> dict[str, Any]:
    """One answer, no tools: what startup plus a single round trip costs, to subtract from race A."""
    return {"responses": [{"content": "hello"}]}


def prepare(work: Path) -> Path:
    """Write the fixture directory and the scripts under `work`; return the fixture directory."""
    fixture = write_fixture(work / "fixture")
    scripts = {
        "script-a.json": script_a(),
        "script-b.json": script_b(),
        "script-hello.json": script_hello(),
    }
    for name, script in scripts.items():
        (work / name).write_text(json.dumps(script, indent=1), encoding="utf-8")
    return fixture
