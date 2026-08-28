"""Contract tests for the ``@agent_facing`` marker and the reference it generates.

Reference: docs/AGENT-DOCS.md

Four things are held here:

1. The marker is a real no-op. Decorating changes neither the object nor its
   behaviour, so nobody has to weigh "is this documented" against "is this fast".
2. griffe reads the marker out of the AST for every import form, without
   importing the decorated module. This is the load-bearing property: it is why
   the build can document ``tau_coding_agent`` (Textual) and the lazy provider
   modules in an environment where neither SDK is installed.
3. The build refuses a malformed marker instead of guessing.
4. The checked-in pages under ``docs/library/reference/`` match a fresh build,
   the same guard ``test_rpc_protocol_doc.py`` puts on ``docs/RPC-PROTOCOL.md``.

Coverage itself is NOT asserted here. ``scripts/check_docs_coverage.py`` is the
gate for that, and it is deliberately not a pytest: a failing docstring is a
task for the author of the change, reported by name and line, not a red suite
for everyone who runs ``pytest``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

import pytest

# Private, and used on purpose: it is the exact set `isinstance` consults for a
# Protocol, so asserting on it names the regression instead of only its symptom.
from typing import _get_protocol_attrs  # type: ignore[attr-defined]

from tau_agent_core.docs_build import (
    PACKAGES,
    TOPICS,
    DocsBuildError,
    UnknownTopicError,
    collect,
    coverage,
    pages,
    render_index,
    render_topic,
    source_paths,
)
from tau_llm.docs import agent_facing

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_DIR = REPO_ROOT / "docs" / "library" / "reference"


# --------------------------------------------------------------------------
# 1. The marker is a no-op.
# --------------------------------------------------------------------------


def test_decorator_returns_the_same_object() -> None:
    def original(x: int) -> int:
        return x + 1

    decorated = agent_facing(topic="tools")(original)

    assert decorated is original
    assert decorated(1) == 2


def test_decorator_leaves_no_runtime_trace() -> None:
    class Plain:
        value = 3

    before = set(vars(Plain))

    @agent_facing(topic="events")
    class Marked:
        value = 3

    assert Marked().value == 3
    assert set(vars(Marked)) == before


def test_marking_a_protocol_does_not_change_isinstance() -> None:
    """The regression that made the marker leave no trace at all.

    An earlier version set an attribute on the decorated object. On a Protocol
    that attribute joins ``__protocol_attrs__``, becoming a member every
    implementation must have -- so ``isinstance`` began rejecting classes that
    satisfy the protocol. ``ConversationSession`` is such a protocol, and
    ``AgentSessionRuntime.fork()`` isinstance-checks against it.
    """

    @agent_facing(topic="sessions")
    @runtime_checkable
    class Marked(Protocol):
        def go(self) -> None: ...

    class Implementation:
        def go(self) -> None: ...

    assert isinstance(Implementation(), Marked)
    assert _get_protocol_attrs(Marked) == {"go"}


# --------------------------------------------------------------------------
# 2. griffe reads the marker statically, for every import form.
# --------------------------------------------------------------------------


IMPORT_FORMS = '''
"""A module the build must read without importing."""

from tau_llm.docs import agent_facing
import tau_llm.docs
from tau_llm import docs as aliased

import a_module_that_does_not_exist_anywhere  # noqa: F401


@agent_facing(topic="tools")
def direct(name: str) -> None:
    """Marked through a direct import.

    Args:
        name: A name.
    """


@tau_llm.docs.agent_facing(topic="tools")
def dotted(name: str) -> None:
    """Marked through the dotted path.

    Args:
        name: A name.
    """


@aliased.agent_facing(topic="tools")
def renamed(name: str) -> None:
    """Marked through a renamed module.

    Args:
        name: A name.
    """


def unmarked(name: str) -> None:
    """Not marked, must not appear."""


class Unmarked:
    @agent_facing(topic="events")
    def buried(self, n: int) -> None:
        """A marked method inside an unmarked class.

        Args:
            n: A number.
        """
'''


@pytest.fixture
def probe_tree(tmp_path: Path) -> list[Path]:
    """Write a module that cannot be imported, and return search paths for it."""
    (tmp_path / "probe_module.py").write_text(IMPORT_FORMS)
    return [tmp_path, *source_paths(REPO_ROOT)]


def test_all_three_import_forms_are_found(probe_tree: list[Path]) -> None:
    objects, _ = collect(["probe_module"], probe_tree)
    assert sorted(o.name for o in objects) == ["buried", "direct", "dotted", "renamed"]


def test_the_probe_module_really_cannot_be_imported(probe_tree: list[Path]) -> None:
    # Guards the point of the previous test. If this module ever became
    # importable, the static claim would still pass while proving nothing.
    sys.path.insert(0, str(probe_tree[0]))
    try:
        with pytest.raises(ModuleNotFoundError):
            __import__("probe_module")
    finally:
        sys.path.remove(str(probe_tree[0]))


def test_unmarked_objects_are_not_published(probe_tree: list[Path]) -> None:
    objects, _ = collect(["probe_module"], probe_tree)
    assert "unmarked" not in {o.name for o in objects}


def test_parameters_are_published_without_a_docstring(tmp_path: Path) -> None:
    (tmp_path / "bare.py").write_text(
        "from tau_llm.docs import agent_facing\n\n"
        "@agent_facing(topic='tools')\n"
        "def bare(path: str, timeout: float = 1.0) -> bool: ...\n"
    )
    objects, _ = collect(["bare"], [tmp_path, *source_paths(REPO_ROOT)])

    (obj,) = objects
    assert obj.has_docstring is False
    assert [(p.name, p.annotation, p.default) for p in obj.params] == [
        ("path", "str", None),
        ("timeout", "float", "1.0"),
    ]
    # The signature is still published; only the meaning is missing.
    assert obj.undocumented_params == ("path", "timeout")
    assert "timeout: float = 1.0" in render_topic(pages(objects)[0])


def test_griffe_reports_docstring_signature_drift(tmp_path: Path) -> None:
    (tmp_path / "drift.py").write_text(
        "from tau_llm.docs import agent_facing\n\n"
        "@agent_facing(topic='tools')\n"
        "def renamed(new_name: str) -> None:\n"
        '    """Drifted.\n\n    Args:\n        old_name: gone.\n    """\n'
    )
    _, warnings = collect(["drift"], [tmp_path, *source_paths(REPO_ROOT)])

    assert any("old_name" in w and "does not appear" in w for w in warnings)


# --------------------------------------------------------------------------
# 3. A malformed marker stops the build.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("@agent_facing(topic='nonsense')", UnknownTopicError),
        ("@agent_facing(since='0.9.5')", DocsBuildError),
        ("@agent_facing('tools')", DocsBuildError),
        ("@agent_facing(topic=SOME_CONSTANT)", DocsBuildError),
    ],
    ids=["unknown-topic", "no-topic", "positional", "non-literal"],
)
def test_a_bad_marker_raises(tmp_path: Path, source: str, expected: type[Exception]) -> None:
    (tmp_path / "bad.py").write_text(
        "from tau_llm.docs import agent_facing\n\n"
        "SOME_CONSTANT = 'tools'\n\n"
        f"{source}\ndef thing() -> None:\n    '''Doc.'''\n"
    )
    with pytest.raises(expected):
        collect(["bad"], [tmp_path, *source_paths(REPO_ROOT)])


def test_an_empty_marked_set_is_not_a_pass(tmp_path: Path) -> None:
    (tmp_path / "nothing.py").write_text("def plain() -> None:\n    '''Doc.'''\n")
    objects, warnings = collect(["nothing"], [tmp_path, *source_paths(REPO_ROOT)])

    report = coverage(objects, warnings)
    assert report.total == 0
    # `fraction` reads 1.0 on an empty set, which is why the CLI checks `total`
    # before reporting a pass. This asserts the trap is still there to check.
    assert report.fraction == 1.0


# --------------------------------------------------------------------------
# 4. The checked-in reference matches a fresh build.
# --------------------------------------------------------------------------


def _fresh_build() -> dict[Path, str]:
    objects, _ = collect(PACKAGES, source_paths(REPO_ROOT))
    built = pages(objects)
    files = {REFERENCE_DIR / f"{page.topic}.md": render_topic(page) for page in built}
    files[REFERENCE_DIR / "index.md"] = render_index(built)
    return files


def test_checked_in_reference_is_current() -> None:
    for path, expected in sorted(_fresh_build().items()):
        assert path.exists(), (
            f"{path.relative_to(REPO_ROOT)} is missing. "
            "Run: venv/bin/python scripts/build_agent_docs.py"
        )
        assert path.read_text() == expected, (
            f"{path.relative_to(REPO_ROOT)} is stale. "
            "Run: venv/bin/python scripts/build_agent_docs.py"
        )


def test_no_stale_reference_pages() -> None:
    on_disk = {p.name for p in REFERENCE_DIR.glob("*.md")}
    assert on_disk == {p.name for p in _fresh_build()}


def test_every_marked_topic_exists(tmp_path: Path) -> None:
    objects, _ = collect(PACKAGES, source_paths(REPO_ROOT))
    assert objects, "no @agent_facing objects found in the headless packages"
    assert {o.topic for o in objects} <= set(TOPICS)


def test_every_page_marks_its_top_level_sections() -> None:
    for path in REFERENCE_DIR.glob("*.md"):
        lines = path.read_text().splitlines()
        for index, line in enumerate(lines):
            if not line.startswith("## "):
                continue
            assert lines[index + 1] == "<!-- agent: yes -->", (
                f"{path.name}:{index + 1}: '{line}' has no audience marker"
            )
