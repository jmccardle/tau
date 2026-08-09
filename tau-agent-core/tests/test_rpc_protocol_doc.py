"""K3: `docs/RPC-PROTOCOL.md` is generated, and a drift check enforces it.

Reference: docs/REMOTE-CONTROL.md §4[8] K3, §1.

    The document is machine-readable and is the same artifact the generated
    reference is built from.

`docs/RPC-PROTOCOL.md` is written by `scripts/generate_rpc_protocol_doc.py`,
which is `tau_agent_core.rpc.protocol_doc.render()` plus a file write. This
test calls `render()` directly (no subprocess) and asserts the checked-in
file already equals it — so a hand-edit of the doc, or a table/schema change
nobody re-ran the generator for, fails the suite instead of the doc quietly
lying about what the wire does (Fail Early: no "close enough" comparison,
no normalization that would mask a real diff).
"""

from __future__ import annotations

from pathlib import Path

from tau_agent_core.rpc import dialect, protocol_doc
from tau_agent_core.rpc.commands import COMMAND_TABLE
from tau_agent_core.rpc.protocol_doc import render

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = REPO_ROOT / "docs" / "RPC-PROTOCOL.md"


def _section(rendered: str, heading: str) -> str:
    start = rendered.index(f"\n## {heading}\n")
    end = rendered.index("\n## ", start + 1)
    return rendered[start:end]


def _acknowledgement_verbs() -> set[str]:
    """Every verb whose response only ACKNOWLEDGES, derived from the table.

    `accepted` in a result schema is what that means on this wire — it is
    `submit`/`prompt`'s `{accepted, submission_id, …}` and `compact`'s
    `{accepted, compaction_id}`, and nothing that answers once has it.
    """
    return {
        name
        for name, entry in COMMAND_TABLE.items()
        if entry.result_schema is not None and "accepted" in entry.result_schema["properties"]
    }


def test_repo_root_is_where_we_think_it_is():
    """Guards the other test's premise: if this ever points at the wrong
    directory, that test would silently stop checking anything real."""
    assert (REPO_ROOT / "docs").is_dir()
    assert (REPO_ROOT / "scripts" / "generate_rpc_protocol_doc.py").is_file()


def test_checked_in_doc_matches_the_generator():
    assert DOC_PATH.is_file(), (
        f"{DOC_PATH} does not exist. Run "
        "`venv/bin/python scripts/generate_rpc_protocol_doc.py` and commit it."
    )
    on_disk = DOC_PATH.read_text()
    generated = render()
    assert on_disk == generated, (
        "docs/RPC-PROTOCOL.md has drifted from tau_agent_core.rpc.protocol_doc"
        ".render() (K3, docs/REMOTE-CONTROL.md §4[8]). Someone hand-edited the "
        "doc, or changed COMMAND_TABLE/WireEvent without regenerating it. Run "
        "`venv/bin/python scripts/generate_rpc_protocol_doc.py` and commit the "
        "result — never hand-edit docs/RPC-PROTOCOL.md."
    )


def test_render_is_deterministic():
    assert render() == render()


def test_the_response_envelope_names_exactly_the_dual_completion_verbs():
    """The envelope section ends with a GLOBAL claim — "No other verb in this
    table has more than one completion" — and a global claim about a table
    that grows is the failure mode this round was told to go looking for. It
    has already happened twice here: "set_model (Tier B, not yet wired)"
    survived set_model shipping, and "These two are the whole list" survived
    `compact` becoming the third.

    So the list is derived from the table (a result schema with `accepted` in
    it IS the acknowledgement shape) and asserted in BOTH directions: every
    dual-completion verb is named in the envelope, and no other verb is —
    the second half being what makes a stale name fail rather than linger.
    """
    rendered = render()
    envelope = _section(rendered, "Response envelope")
    dual = _acknowledgement_verbs()

    assert set(protocol_doc._DUAL_COMPLETION_VERBS) == dual

    for name in sorted(dual):
        assert f"`{name}`" in envelope, (
            f"{name} answers only with an acknowledgement, and the response "
            "envelope never mentions it — a host reading this section would "
            "wait forever for a completion it was not told about"
        )

    others = {n for n, e in COMMAND_TABLE.items() if e.declined_because is None} - dual
    for name in sorted(others):
        assert f"`{name}`" not in envelope, (
            f"the envelope names {name}, which answers exactly once — either "
            "it gained a second completion nobody documented, or the section "
            "is describing a verb that no longer works that way"
        )


def test_the_error_table_covers_exactly_the_codes_dialect_exports():
    """Round-3 finding 2 of the Tier B review, as the property.

    `COMMAND_NOT_SUPPORTED` (-32001) has been reachable from an ordinary
    `submit {"text": "/tree", "expand_commands": true}` since Tier C, and was
    absent from the generated reference's error table the whole time. The
    round that ADDED a fourth implementation-defined code pinned that one row
    (`test_the_generated_protocol_doc_states_the_bound_and_the_refusal`)
    rather than the set, so nothing in the suite could fail while a code
    existed on the wire and nowhere in the document.

    Derived from `dialect`'s own module namespace, not from a list kept
    beside it: a new `-32xxx` constant is documented before it can be
    shipped, and a row for a constant that was deleted fails too.
    """
    # JSON-RPC 2.0 reserves -32768..-32000 for the protocol: the five standard
    # codes and the -32000..-32099 implementation-defined band both live there,
    # so one range is the whole vocabulary.
    exported = {
        value
        for name, value in vars(dialect).items()
        if not name.startswith("_")
        and type(value) is int
        and -32768 <= value <= -32000
    }
    assert exported, "no error-code constants found on dialect — this test stopped testing"

    documented = {code for code, _, _ in protocol_doc._ERROR_ORDER}
    assert documented == exported, (
        "the generated reference's error table and dialect's error codes "
        f"disagree. Undocumented but reachable: {sorted(exported - documented)}. "
        f"Documented but no longer exported: {sorted(documented - exported)}. "
        "A host cannot write a correct error branch for a code it cannot look "
        "up; add the row to protocol_doc._ERROR_ORDER and regenerate."
    )


def test_the_error_table_renders_every_code_it_carries():
    """The set-equality above is on the list; this is on the OUTPUT.

    A row present in `_ERROR_ORDER` and lost by the renderer would satisfy
    the previous test and still leave the host without the code.
    """
    errors = _section(render(), "Error codes")
    for code, name, _ in protocol_doc._ERROR_ORDER:
        assert str(code) in errors, f"{name} ({code}) never reaches the rendered table"
        assert name in errors, f"{name}'s NAME never reaches the rendered table"


def test_compacts_notes_enumerate_every_required_compaction_end_key():
    """Round-3 finding 3 of the Tier B review, as the property.

    `compact`'s `notes` are the one place the reference tells a host what a
    `compaction_end` carries, and they are prose — so when finding 5 added
    `cancelled` to `COMPACTION_END_PARAMS_SCHEMA`'s `required` and appended a
    separate paragraph instead of amending the enumeration, the list a second
    implementor would build a parser from silently omitted a field present on
    every single notification.

    This is the third round running that the top finding was a hand-written
    claim falsified by a sibling's addition. The schema is the authority; the
    prose is asserted against it, so the NEXT required key cannot be added
    without this sentence learning about it.
    """
    from tau_agent_core.rpc import commands

    notes = COMMAND_TABLE["compact"].notes or ""
    required = commands.COMPACTION_END_PARAMS_SCHEMA["required"]
    assert "cancelled" in required, (
        "the field this test was written for is gone from the schema — if "
        "that is deliberate, the notes need re-reading, not this assertion "
        "deleting"
    )
    missing = [key for key in required if key not in notes]
    assert not missing, (
        f"compact's notes describe what compaction_end carries and never "
        f"mention {missing}, which COMPACTION_END_PARAMS_SCHEMA marks REQUIRED "
        "on every notification. A host building a parser from the reference "
        "would omit it."
    )
