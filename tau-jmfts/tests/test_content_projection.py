"""``_content_for``: what of an entry becomes searchable text (Sec2.1).

Offline by design — no marker, no server. ``_content_for`` is a pure function of
``(kind, payload)``, and the one thing that needs a store around it (that the write
path actually feeds the projection into the document it POSTs) is covered with a
recording stand-in for the single client method ``_append`` calls.

The subject is TREE-BROWSER-AS-EDITOR.md §7.3: an ``elide`` used to project to the
empty string, so the node that records "history was folded here" was the one node a
JMFTS query could never find. Eliding is not deletion — it is engineering the
conversation to proceed in a new direction — so the marker has to be searchable.
"""

from __future__ import annotations

from typing import Any

from tau_jmfts.store import JmftsSessionLog, _content_for

# TREE-BROWSER-AS-EDITOR.md §8/§11.3: the splice appenders now require the anchor's
# provenance as keyword-only arguments with no defaults. These tests are about
# something else, so they name plausible values once here.
_ELIDE_PROV = {"covered_entries": 1, "covered_tokens": 50, "agent_spec_id": None}


class RecordingClient:
    """Stand-in for the ONE :class:`JmftsClient` method ``_append`` / ``create`` use.

    Not a JMFTS emulator (see ``test_strategy_store.FakeJmftsClient`` for the larger
    one): this suite only ever creates documents and reads back what was sent.
    """

    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []
        self._next_id = 1

    def create_document(self, **kwargs: Any) -> dict[str, Any]:
        doc = {"id": self._next_id, **kwargs}
        self._next_id += 1
        self.docs.append(doc)
        return doc


# --- the projection itself ---------------------------------------------------


def test_message_and_summary_projections_are_unchanged() -> None:
    assert (
        _content_for("message", {"message": {"role": "user", "content": "hi"}}) == "hi"
    )
    assert _content_for("compaction", {"summary": "S", "firstKeptId": "7"}) == "S"
    assert _content_for("branch_summary", {"summary": "S", "fromId": "7"}) == "S"
    assert _content_for("navigate", {"targetId": "7"}) == ""


def test_elide_projects_searchable_text_naming_its_resume_point() -> None:
    """§7.3: non-empty, and it carries the ``firstKeptId`` so the fold is followable."""
    content = _content_for("elide", {"firstKeptId": "412"})
    assert content == "elide: history folded here, context resumes at entry 412"


def test_elide_projection_states_a_missing_resume_point() -> None:
    """Both appenders reject an anchor naming no entry, so a payload with no
    ``firstKeptId`` is a corrupt or hand-written log the importer is being used to
    inspect. Say so — do not fabricate an id and do not block the import."""
    assert _content_for("elide", {}) == "elide: history folded here, no resume point recorded"


def test_elide_projection_invents_no_count() -> None:
    """Fail-Early: the browser row says "hides N entries", but N needs the TREE
    (``ConversationTree._splice_anchor_preview`` walks ``parentId``), which
    ``_content_for`` is not given. A fabricated N would read as a recorded
    measurement forever, so the projection must contain no digits but the id."""
    content = _content_for("elide", {"firstKeptId": "412"})
    assert "".join(c for c in content if c.isdigit()) == "412"


# --- the write path uses it --------------------------------------------------


def test_appended_elide_document_carries_the_searchable_content() -> None:
    client = RecordingClient()
    log = JmftsSessionLog.create(client, cwd=".", model="m", backend="b")  # type: ignore[arg-type]
    anchor = log.append_message({"role": "user", "content": "keep me"})
    elide_id = log.append_elide(anchor, **_ELIDE_PROV)

    doc = next(d for d in client.docs if d["id"] == int(elide_id))
    assert doc["usetype"] == "tau:elide"
    assert doc["content"] == f"elide: history folded here, context resumes at entry {anchor}"
