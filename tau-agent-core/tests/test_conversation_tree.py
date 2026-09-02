"""Tests for the pure ConversationTree (step 1a, SESSION-TREE-IMPLEMENTATION §2.7).

The centerpiece is the *fold parity* battery: for a range of synthetic entry
trees (linear, branched, single-compaction, multiply-compacted) the port
``ConversationTree.context_for`` must produce the same message list as the frozen
System-A oracle ``SessionManager._build_active_path`` + ``get_active_messages``.
System B entries are camelCase (``parentId``/``firstKeptId``); the oracle reads
snake_case, so the entries are translated before feeding the oracle.
"""

from __future__ import annotations

from typing import Any

import pytest

from tau_agent_core.conversation_tree import ConversationTree, TreeNode, is_system_message
from tau_agent_core.session_manager import SessionManager

# --- synthetic entry builders (System B / camelCase shape) -----------------


def _msg(entry_id: str, parent: str | None, role: str, text: str) -> dict[str, Any]:
    return {
        "id": entry_id,
        "type": "message",
        "parentId": parent,
        "timestamp": f"2026-07-03T00:00:{int(entry_id[-2:]):02d}Z",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _compaction(
    entry_id: str, parent: str | None, first_kept_id: str, summary: str
) -> dict[str, Any]:
    return {
        "id": entry_id,
        "type": "compaction",
        "parentId": parent,
        "timestamp": f"2026-07-03T00:00:{int(entry_id[-2:]):02d}Z",
        "summary": summary,
        "firstKeptId": first_kept_id,
        "tokensBefore": 0,
    }


def _branch_summary(
    entry_id: str, parent: str | None, from_id: str, summary: str
) -> dict[str, Any]:
    return {
        "id": entry_id,
        "type": "branch_summary",
        "parentId": parent,
        "timestamp": f"2026-07-03T00:00:{int(entry_id[-2:]):02d}Z",
        "summary": summary,
        "fromId": from_id,
    }


def _to_snake(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate camelCase System-B entries into System-A's snake_case shape so
    the same tree can feed the ``SessionManager`` oracle."""
    out: list[dict[str, Any]] = []
    for entry in entries:
        clone = dict(entry)
        clone["parent_id"] = clone.pop("parentId", None)
        if "firstKeptId" in clone:
            clone["first_kept_id"] = clone.pop("firstKeptId")
        out.append(clone)
    return out


def _oracle_messages(entries: list[dict[str, Any]], leaf: str | None) -> list[dict[str, Any]]:
    """Frozen System-A behavior: feed the (translated) entries to an in-memory
    SessionManager and read ``get_active_messages`` at ``leaf``."""
    mgr = SessionManager.in_memory()
    mgr._memory_store = _to_snake(entries)
    mgr._active_entry_id = leaf
    return mgr.get_active_messages()


def _expected_from_oracle(entries: list[dict[str, Any]], leaf: str | None) -> list[dict[str, Any]]:
    """The oracle's fold plus τ's ONE deliberate divergence from it.

    A splice drops everything on the path before ``firstKeptId``, and in τ — unlike
    pi and unlike the System-A oracle — the system prompt is an ENTRY on that path
    (``Session._init_state``), so the oracle's fold silently loses it.
    ``ConversationTree`` carries system messages across the splice and emits them
    first (``_active_path_entries``).

    Stating the divergence as a transformation OF the oracle rather than deleting
    the compaction trees from the battery keeps the rest of the fold pinned: every
    other message, and their order, must still match System A exactly.
    """
    oracle = _oracle_messages(entries, leaf)
    path = ConversationTree(entries, cursor=leaf).path()
    carried = [e["message"] for e in path if is_system_message(e)]
    missing = [m for m in carried if m not in oracle]
    return [*missing, *oracle]


# --- the synthetic trees ----------------------------------------------------


def _linear() -> list[dict[str, Any]]:
    return [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "hello"),
        _msg("e03", "e02", "assistant", "hi"),
        _msg("e04", "e03", "user", "more"),
        _msg("e05", "e04", "assistant", "ok"),
    ]


def _branched() -> list[dict[str, Any]]:
    # e03 has two children: branch A (e04a→e05a), branch B (e04b).
    return [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "hello"),
        _msg("e03", "e02", "assistant", "hi"),
        _msg("e04", "e03", "user", "path A"),
        _msg("e05", "e04", "assistant", "ansA"),
        _msg("e06", "e03", "user", "path B"),
        _msg("e07", "e06", "assistant", "ansB"),
    ]


def _single_compaction() -> list[dict[str, Any]]:
    # Compaction c08 splices in before e05 (firstKeptId=e05); its parent is e04's
    # former parent (e03). Tip continues at e07 (child of e06→e05).
    return [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "u1"),
        _msg("e03", "e02", "assistant", "a1"),
        _msg("e04", "e03", "user", "u2"),
        _compaction("e08", "e03", "e05", "SUMMARY-1"),
        _msg("e05", "e08", "assistant", "a2"),
        _msg("e06", "e05", "user", "u3"),
        _msg("e07", "e06", "assistant", "a3"),
    ]


def _multi_compaction() -> list[dict[str, Any]]:
    # Two compactions in the path. The LAST (c10, firstKeptId=e07) must win.
    return [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "u1"),
        _msg("e03", "e02", "assistant", "a1"),
        _compaction("e08", "e03", "e04", "SUMMARY-1"),
        _msg("e04", "e08", "user", "u2"),
        _msg("e05", "e04", "assistant", "a2"),
        _msg("e06", "e05", "user", "u3"),
        _compaction("e10", "e06", "e07", "SUMMARY-2"),
        _msg("e07", "e10", "assistant", "a3"),
        _msg("e09", "e07", "user", "u4"),
    ]


ALL_TREES = {
    "linear": _linear,
    "branched": _branched,
    "single_compaction": _single_compaction,
    "multi_compaction": _multi_compaction,
}


def _all_leaves(entries: list[dict[str, Any]]) -> list[str]:
    return [e["id"] for e in entries]


# --- fold parity: the regression net for the port --------------------------


@pytest.mark.parametrize("tree_name", list(ALL_TREES))
def test_context_for_matches_system_a_oracle(tree_name: str) -> None:
    entries = ALL_TREES[tree_name]()
    for leaf in _all_leaves(entries):
        tree = ConversationTree(entries, cursor=leaf)
        assert tree.context_for() == _expected_from_oracle(entries, leaf), (
            f"{tree_name} @ leaf={leaf}"
        )


@pytest.mark.parametrize("tree_name", list(ALL_TREES))
def test_context_for_explicit_leaf_matches_oracle(tree_name: str) -> None:
    entries = ALL_TREES[tree_name]()
    tree = ConversationTree(entries, cursor=None)
    for leaf in _all_leaves(entries):
        assert tree.context_for(leaf) == _expected_from_oracle(entries, leaf)


def test_context_for_none_cursor_falls_back_to_root_like_oracle() -> None:
    entries = _linear()
    tree = ConversationTree(entries, cursor=None)
    assert tree.context_for() == _oracle_messages(entries, None)


def test_the_only_divergence_from_the_oracle_is_the_carried_system_message() -> None:
    """Pins what :func:`_expected_from_oracle` is allowed to add.

    A helper that "adjusts" the oracle can hide any regression it is written wide
    enough to absorb. This asserts the adjustment is EMPTY wherever no splice drops
    a system message — every leaf of the two uncompacted trees, and the leaves of a
    compacted tree that sit above its anchor — so the battery still compares the
    port against System A byte for byte on those, and the carry is the single
    difference on the rest.
    """
    for tree_name in ("linear", "branched"):
        entries = ALL_TREES[tree_name]()
        for leaf in _all_leaves(entries):
            assert _expected_from_oracle(entries, leaf) == _oracle_messages(entries, leaf)

    entries = _single_compaction()
    for leaf in ("e01", "e02", "e03"):  # above the compaction: nothing is spliced
        assert _expected_from_oracle(entries, leaf) == _oracle_messages(entries, leaf)
    # e07 is below it, and the difference is exactly one message: the system prompt.
    assert len(_expected_from_oracle(entries, "e07")) == len(_oracle_messages(entries, "e07")) + 1


def test_context_for_empty_tree() -> None:
    assert ConversationTree([], cursor=None).context_for() == []


# --- the compaction splice, verified concretely -----------------------------


def test_single_compaction_drops_pre_boundary_and_keeps_summary() -> None:
    entries = _single_compaction()
    tree = ConversationTree(entries, cursor="e07")
    msgs = tree.context_for()
    # u1(e02) + a1(e03) precede the boundary → dropped. sys(e01) precedes it too and
    # is CARRIED, ahead of the summary, because a system message that survives has to
    # be first for the provider and because the fold is what the model is sent.
    assert msgs[0] == {"role": "system", "content": [{"type": "text", "text": "sys"}]}
    assert msgs[1] == {
        "role": "user",
        "content": [{"type": "text", "text": "[[Compaction summary: SUMMARY-1]]"}],
    }
    texts = [m["content"][0]["text"] for m in msgs[2:]]
    assert texts == ["a2", "u3", "a3"]


def test_multi_compaction_anchors_on_last() -> None:
    entries = _multi_compaction()
    tree = ConversationTree(entries, cursor="e09")
    msgs = tree.context_for()
    # SUMMARY-2 wins; SUMMARY-1 and its kept region are gone. The system prompt is
    # carried by whichever anchor wins, exactly once — not once per anchor.
    assert [m["content"][0]["text"] for m in msgs] == [
        "sys",
        "[[Compaction summary: SUMMARY-2]]",
        "a3",
        "u4",
    ]


# --- the system prompt survives a splice ------------------------------------
#
# τ stores the system prompt as an entry (``Session._init_state``) where pi keeps
# it in the request frame, so τ's splice — and only τ's — could drop it. It did:
# every compaction and every elide removed it from the fold, and the model kept
# seeing a system prompt only because ``AgentLoop._call_llm`` re-inserted the
# CONFIG's when the context did not start with one. These pin the fix.


def _elide(entry_id: str, parent: str | None, first_kept_id: str | None) -> dict[str, Any]:
    return {
        "id": entry_id,
        "type": "elide",
        "parentId": parent,
        "timestamp": f"2026-07-03T00:00:{int(entry_id[-2:]):02d}Z",
        "firstKeptId": first_kept_id,
    }


def test_elide_carries_the_system_prompt() -> None:
    entries = [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "u1"),
        _msg("e03", "e02", "assistant", "a1"),
        _msg("e04", "e03", "user", "u2"),
        _elide("e05", "e04", "e04"),
    ]
    msgs = ConversationTree(entries, cursor="e05").context_for()
    # An elide renders nothing of its own, so this is the whole context: the system
    # prompt and the kept region. u1/a1 are hidden.
    assert [(m["role"], m["content"][0]["text"]) for m in msgs] == [
        ("system", "sys"),
        ("user", "u2"),
    ]


def test_a_system_message_below_the_boundary_is_not_duplicated() -> None:
    """The carry takes only what the splice DROPS.

    A system entry inside the kept region is emitted once, by the kept region, in
    its own position — carrying it as well would send the provider two system
    messages and put the second one somewhere it never was.
    """
    entries = [
        _msg("e01", None, "user", "u1"),
        _msg("e02", "e01", "system", "sys"),
        _msg("e03", "e02", "assistant", "a1"),
        _elide("e04", "e03", "e02"),
    ]
    msgs = ConversationTree(entries, cursor="e04").context_for()
    assert [(m["role"], m["content"][0]["text"]) for m in msgs] == [
        ("system", "sys"),
        ("assistant", "a1"),
    ]


def test_a_custom_message_is_not_carried() -> None:
    """``customMessage`` is conversation, not frame (:func:`is_system_message`)."""
    entries = [
        _msg("e01", None, "system", "sys"),
        {
            "id": "e02",
            "type": "customMessage",
            "parentId": "e01",
            "timestamp": "2026-07-03T00:00:02Z",
            "customType": "reminder",
            "message": {"role": "custom", "content": [{"type": "text", "text": "note"}]},
        },
        _msg("e03", "e02", "user", "u1"),
        _elide("e04", "e03", "e03"),
    ]
    msgs = ConversationTree(entries, cursor="e04").context_for()
    assert [m["content"][0]["text"] for m in msgs] == ["sys", "u1"]


# --- branch_summary is an INLINE node, NOT a splice anchor (Decision 5, §5) --
#
# The 1b test here asserted branch_summary spliced *like* compaction (the §2.4
# unification). That was verified WRONG against pi (Decision 5, fix 2): pi's
# buildSessionContext anchors the drop-prefix splice on ``compaction`` alone
# (session-manager.ts:367); branch_summary is emitted inline via
# createBranchSummaryMessage (:390-397). These tests lock the pi-correct topology.


def test_branch_summary_is_inline_not_a_splice_yields_A_B_S() -> None:
    # A real summarized branch: root A → point B, with an abandoned child C.
    # branchWithSummary parents the summary S at the branch point B (fix 1), so the
    # active path is A → B → S and C drops out purely via the parentId walk — NOT a
    # splice. pi gives context [A, B, S]; the old unified splice wrongly gave [S, B].
    entries = [
        _msg("e01", None, "system", "rootA"),
        _msg("e02", "e01", "user", "pointB"),
        _msg("e03", "e02", "assistant", "abandonedC"),  # sibling of the summary
        _branch_summary("e04", "e02", "e02", "SUMMARY-S"),  # parented at B (fix 1)
    ]
    msgs = ConversationTree(entries, cursor="e04").context_for()
    assert msgs == [
        {"role": "system", "content": [{"type": "text", "text": "rootA"}]},
        {"role": "user", "content": [{"type": "text", "text": "pointB"}]},
        {"role": "user", "content": [{"type": "text", "text": "[[Branch summary: SUMMARY-S]]"}]},
    ]


def test_mixed_compaction_and_branch_summary_path() -> None:
    # Both kinds on one path: compaction drops the pre-boundary prefix; the later
    # branch_summary renders inline (no prefix drop). Matches pi buildSessionContext
    # (compaction is the sole anchor; the post-anchor branch_summary is appendMessage'd).
    entries = [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "u1"),
        _msg("e03", "e02", "assistant", "a1"),
        _compaction("e04", "e03", "e05", "COMP"),  # firstKeptId=e05
        _msg("e05", "e04", "assistant", "a2"),
        _msg("e06", "e05", "user", "u3"),
        _branch_summary("e07", "e06", "e05", "BR"),  # inline, after the compaction
        _msg("e08", "e07", "assistant", "a4"),
    ]
    msgs = ConversationTree(entries, cursor="e08").context_for()
    texts = [m["content"][0]["text"] for m in msgs]
    assert texts == [
        "sys",  # carried across the compaction; u1/a1 were not
        "[[Compaction summary: COMP]]",  # prefix (u1/a1) dropped by the compaction
        "a2",
        "u3",
        "[[Branch summary: BR]]",  # inline — drops nothing
        "a4",
    ]


# --- navigate / path --------------------------------------------------------


def test_navigate_moves_cursor_and_changes_context() -> None:
    entries = _branched()
    tree = ConversationTree(entries, cursor="e05")  # branch A tip
    assert [m["content"][0]["text"] for m in tree.context_for()] == [
        "sys",
        "hello",
        "hi",
        "path A",
        "ansA",
    ]
    tree.navigate("e07")  # branch B tip
    assert [m["content"][0]["text"] for m in tree.context_for()] == [
        "sys",
        "hello",
        "hi",
        "path B",
        "ansB",
    ]


def test_navigate_to_none_is_pre_root() -> None:
    tree = ConversationTree(_linear(), cursor="e05")
    tree.navigate(None)
    assert tree.cursor is None


def test_navigate_unknown_raises() -> None:
    tree = ConversationTree(_linear(), cursor="e05")
    with pytest.raises(KeyError):
        tree.navigate("nope")


def test_path_returns_root_to_leaf_chain() -> None:
    entries = _branched()
    tree = ConversationTree(entries, cursor="e07")
    assert [e["id"] for e in tree.path()] == ["e01", "e02", "e03", "e06", "e07"]
    # explicit leaf overrides the cursor
    assert [e["id"] for e in tree.path("e05")] == ["e01", "e02", "e03", "e04", "e05"]


def test_path_cycle_guard_stops() -> None:
    entries = [
        {"id": "a", "type": "message", "parentId": "b", "message": {"role": "user", "content": ""}},
        {"id": "b", "type": "message", "parentId": "a", "message": {"role": "user", "content": ""}},
    ]
    tree = ConversationTree(entries, cursor="a")
    ids = [e["id"] for e in tree.path()]
    assert set(ids) == {"a", "b"} and len(ids) == 2


# --- tree() -----------------------------------------------------------------


def test_tree_structure_and_leaf_marker() -> None:
    entries = _branched()
    roots = ConversationTree(entries, cursor="e07").tree()
    assert len(roots) == 1
    root = roots[0]
    assert root.id == "e01" and root.parent_id is None
    # e03 fans out to e04 and e06 (sorted by timestamp, oldest first)
    e03 = root.children[0].children[0]
    assert e03.id == "e03"
    assert [c.id for c in e03.children] == ["e04", "e06"]
    # the cursor (e07) is the only leaf-marked node
    leaves = _collect_leaf_ids(roots)
    assert leaves == {"e07"}


def test_tree_node_previews_and_roles() -> None:
    entries = _single_compaction()
    roots = ConversationTree(entries, cursor="e07").tree()
    by_id: dict[str, TreeNode] = {}
    _index(roots, by_id)
    assert by_id["e02"].role == "user" and by_id["e02"].preview == "u1"
    # TREE-BROWSER-AS-EDITOR.md §4.2: the compaction row states the span it folds
    # BEFORE its summary. Here ``firstKeptId=e05`` is a DESCENDANT of the anchor
    # (``SessionManager.apply_compaction``'s re-parented shape), so the fold keeps
    # nothing from before e08 and the count is the parent context e01..e03 LESS the
    # system message, which the fold carries rather than folds — a row that counted
    # it would disagree with the context it describes.
    assert by_id["e08"].kind == "compaction"
    assert by_id["e08"].preview == "folds 2 entries, resumes at e05 — SUMMARY-1"
    assert by_id["e08"].role is None


def _preview(entries: list[dict[str, Any]], entry_id: str, cursor: str) -> str:
    by_id: dict[str, TreeNode] = {}
    _index(ConversationTree(entries, cursor=cursor).tree(), by_id)
    return by_id[entry_id].preview


def test_compaction_preview_states_the_span_it_folds() -> None:
    """§4.2: the tip-append shape — ``firstKeptId`` is an ANCESTOR of the anchor.

    ``session_store.Session.append_compaction`` appends at the leaf, so the kept
    region precedes the anchor. The row must state the span (e02 and e03 drop out;
    e04 onward is kept) and the summary, in that order.
    """
    entries = [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "u1"),
        _msg("e03", "e02", "assistant", "a1"),
        _msg("e04", "e03", "user", "u2"),
        _compaction("e05", "e04", "e04", "SUMMARY-1\nsecond line"),
        _msg("e06", "e05", "assistant", "a2"),
    ]
    # e02 and e03 fold away, e04 is the resume point; e01 is carried, not folded, so
    # it is not counted. Only the summary's FIRST line survives, and it lands after
    # the span rather than displacing it.
    assert _preview(entries, "e05", "e06") == "folds 2 entries, resumes at e04 — SUMMARY-1"


def test_compaction_preview_singular_entry() -> None:
    # Three entries, not two: the system message is carried across the splice and no
    # longer counts toward the span, so a fixture that folds only it now reads
    # "folds 0 entries" and says nothing about the singular noun this test is for.
    entries = [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "u1"),
        _msg("e03", "e02", "assistant", "a1"),
        _compaction("e04", "e03", "e03", "S"),
    ]
    assert _preview(entries, "e04", "e04") == "folds 1 entry, resumes at e03 — S"


def test_compaction_preview_reports_an_unreachable_resume_point() -> None:
    """§4.2's honest-reporting case, inherited from the elide row.

    ``firstKeptId`` names an entry on a SIBLING branch — neither an ancestor of the
    anchor nor a descendant of it. ``_active_path_entries``' forward scan never finds
    it, so the fold keeps nothing before the anchor and the resume point is
    unfollowable. The row must say that rather than print a count next to a
    meaningless id, and the summary must still be there.
    """
    entries = [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "branch A"),
        _msg("e03", "e01", "user", "branch B"),
        _compaction("e04", "e03", "e02", "SUMMARY-1"),
    ]
    assert _preview(entries, "e04", "e04") == (
        "compaction → e02: resume point is not on this path "
        "(folds everything) — SUMMARY-1"
    )


def test_compaction_preview_without_a_summary_is_just_the_span() -> None:
    """A compaction whose ``summary`` is empty renders no trailing em dash."""
    entries = [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "u1"),
        _msg("e03", "e02", "assistant", "a1"),
        _compaction("e04", "e03", "e03", ""),
    ]
    assert _preview(entries, "e04", "e04") == "folds 1 entry, resumes at e03"


def test_elide_preview_is_unchanged_by_the_shared_arithmetic() -> None:
    """The §4.2 refactor gives ``compaction`` elide's arithmetic; it must not change
    elide's own row. An elide has no summary, so it degrades to the span phrase."""
    entries = [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "u1"),
        _msg("e03", "e02", "assistant", "a1"),
        _msg("e04", "e03", "user", "u2"),
        {
            "id": "e05",
            "type": "elide",
            "parentId": "e04",
            "timestamp": "2026-07-03T00:00:05Z",
            "firstKeptId": "e04",
        },
    ]
    # Two hidden entries, u1 and a1. The fixture gained u2 when the system message
    # stopped counting toward the span, so the plural row this test is about is
    # still the row being asserted.
    assert _preview(entries, "e05", "e05") == "hides 2 entries, resumes at e04"

    entries[4]["firstKeptId"] = None
    assert _preview(entries, "e05", "e05") == (
        "elide → None: resume point is not on this path (hides everything)"
    )


def test_branch_summary_preview_is_untouched() -> None:
    """§4.3 needs no change HERE: ``branch_summary`` is not a splice anchor, so it
    has no span, and the sibling relation §4.3 wants shown is a two-row relation a
    one-line preview cannot express (it is `render_label` work in the TUI)."""
    entries = [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "abandoned"),
        _branch_summary("e03", "e01", "e02", "what that branch tried"),
    ]
    assert _preview(entries, "e03", "e03") == "what that branch tried"


def test_tree_orphan_is_root() -> None:
    entries = [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "hi"),
        _msg("e09", "missing", "assistant", "orphan"),
    ]
    roots = ConversationTree(entries, cursor="e02").tree()
    assert {r.id for r in roots} == {"e01", "e09"}


def _collect_leaf_ids(nodes: list[TreeNode]) -> set[str]:
    found: set[str] = set()
    for node in nodes:
        if node.is_leaf:
            found.add(node.id)
        found |= _collect_leaf_ids(node.children)
    return found


def _index(nodes: list[TreeNode], out: dict[str, TreeNode]) -> None:
    for node in nodes:
        out[node.id] = node
        _index(node.children, out)


# --- subtree_text -----------------------------------------------------------


def test_subtree_text_collects_descendants() -> None:
    entries = _branched()
    text = ConversationTree(entries, cursor="e07").subtree_text("e04")
    # e04 subtree = e04 → e05 (branch A only; branch B under e06 is excluded)
    assert text == "[user]: path A\n[assistant]: ansA"


def test_subtree_text_includes_summary_nodes() -> None:
    entries = _single_compaction()
    text = ConversationTree(entries, cursor="e07").subtree_text("e08")
    assert text.startswith("[compaction]: SUMMARY-1")
    assert "[assistant]: a2" in text


def test_subtree_text_unknown_id_is_empty() -> None:
    tree = ConversationTree(_linear(), cursor="e05")
    assert tree.subtree_text("does-not-exist") == ""


# --- camelCase field-read guard (the reconciliation, §2.7) ------------------


def test_reads_camelcase_parent_and_first_kept_fields() -> None:
    # If context_for read snake_case, the compaction boundary would misresolve
    # (firstKeptId ignored → whole prefix kept) and the parent chain would break.
    entries = _single_compaction()
    assert "parentId" in entries[1] and "firstKeptId" in entries[4]
    msgs = ConversationTree(entries, cursor="e07").context_for()
    assert msgs[0]["content"][0]["text"] == "sys"  # carried across the splice
    assert msgs[1]["content"][0]["text"] == "[[Compaction summary: SUMMARY-1]]"
    assert [m["content"][0]["text"] for m in msgs[2:]] == ["a2", "u3", "a3"]
