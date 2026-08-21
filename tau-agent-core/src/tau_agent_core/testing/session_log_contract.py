"""The ``SessionLog`` conformance suite — the entry algebra, executable.

Subclass and supply ``make_log()``. Every implementation (``InMemorySessionLog``, the
file ``Session``, and any database-backed store) must satisfy all of it, because
``ConversationTree`` folds them all with the same code: *same entries → same tree*
(SESSION-TREE-IMPLEMENTATION.md §4.5).

Before this suite existed the only cross-implementation check was a single
``isinstance(log, SessionLog)`` — and ``runtime_checkable`` verifies **method names
only**, not signatures or behaviour. A store could satisfy it while chaining parentIds
wrong, and nothing would notice until a conversation tree came back malformed.

Reference: docs/JMFTS-INTEGRATION-PLAN.md §7 ("Testing spine, the keystone").
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import SessionLog, open_branch, resolve_cursor


def _msg(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _texts(messages: list[dict[str, Any]]) -> list[str]:
    """Flatten a context fold to the text of each message, for readable assertions."""
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out.extend(b["text"] for b in content if isinstance(b, dict) and "text" in b)
    return out


class SessionLogContractTests:
    """Conformance tests for the SessionLog entry algebra.

    Subclasses implement :meth:`make_log`.
    """

    def make_log(self) -> SessionLog:
        raise NotImplementedError("contract subclass must provide make_log()")

    def reload(self, log: SessionLog) -> SessionLog | None:
        """Re-open ``log`` from its durable storage, or ``None`` if it has none.

        Override in a store that persists (the file ``Session`` re-reads its JSONL; a
        JMFTS store re-fetches the document subtree). Durability is the property a
        database-backed store is most likely to get wrong — wrong sibling order, a
        cursor rebuilt from the wrong row, a nested payload lost in serialization — and
        it is precisely the reason this suite exists. Leaving it out of the shared
        contract meant a new store could subclass the whole suite, go green, and still
        come back malformed on the first resume.

        ``InMemorySessionLog`` genuinely has no durable form, so it returns ``None`` and
        the reload tests skip — an honest "not applicable", not a silent pass.
        """
        return None

    @pytest.fixture
    def log(self) -> SessionLog:
        return self.make_log()

    # ---------------------------------------------------------------- identity

    def test_satisfies_the_protocol(self, log):
        assert isinstance(log, SessionLog)

    def test_id_is_stable_and_not_a_path(self, log):
        """Identity is the session id — never a filesystem path (§4.2)."""
        assert isinstance(log.id, str) and log.id
        assert log.id == log.id
        assert "/" not in log.id

    def test_a_fresh_log_has_a_self_consistent_cursor(self, log):
        """A store MAY seed initial entries (the file ``Session`` records model/backend
        at creation; ``InMemorySessionLog`` starts blank). That is initialization
        policy, not algebra — what the contract requires is that the cursor agrees with
        the entries either way.
        """
        assert log.cursor == resolve_cursor(log.entries())

    def test_the_first_entry_is_root_level(self, log):
        """``parentId is None`` means ROOT-LEVEL, not "the one and only root".

        A store MAY seed entries (the file ``Session`` records model/backend at
        creation), so what is pinned here is the weaker, actually-true statement: the
        chain bottoms out at a root-level entry, and no ``parentId`` dangles. The
        stronger "exactly one root" reading is FALSE — ``append_navigate(None)`` puts
        the cursor before the root, so the next append is a second root-level entry.
        That is legal and load-bearing; see
        :meth:`test_navigate_to_none_starts_a_new_root_level_branch`.
        """
        log.append_message(_msg("user", "hello"))
        entries = log.entries()
        ids = {e["id"] for e in entries}
        roots = [e for e in entries if e["parentId"] is None]

        assert roots, "the parentId chain must bottom out at a root-level entry"
        assert all(e["parentId"] is None or e["parentId"] in ids for e in entries), (
            "no parentId may dangle: every non-root parent names a real entry"
        )

    def test_navigate_to_none_starts_a_new_root_level_branch(self, log):
        """``navigate(None)`` = "cursor before the root"; the next append is root-level.

        Pinned as a CONTRACT, not left to each store, because the three implementations
        map ``parentId is None`` onto genuinely different things and must still agree:
        the file/in-memory logs store a literal ``None``, while a JMFTS-backed log
        parents the document under the **conversation root document** (the header),
        which is where the single-root property actually lives — the τ-entry "forest"
        is one document tree once the header is counted. Round-tripping that mapping
        (``parent_id == root_doc_id`` ⟺ ``parentId is None``) is exactly what this
        test forces a new store to get right.

        Reachable from the public API (``ctx.navigate(None)`` →
        ``extension_types.py`` → ``log.append_navigate(None)``), so it is a real code
        path, not a theoretical one.
        """
        # NB: this first append is not necessarily root-level itself — the file store
        # seeds a model_change entry at creation, so it may parent onto that. What the
        # navigate below establishes is a genuinely root-level SECOND branch.
        log.append_message(_msg("user", "on the first branch"))

        log.append_navigate(None)
        assert log.cursor is None, "navigate(None) puts the cursor before the root"

        second = log.append_message(_msg("user", "on a second, sibling branch"))

        by_id = {e["id"]: e for e in log.entries()}
        assert by_id[second]["parentId"] is None, "the post-navigate append is root-level"

        # The two branches are siblings, and the context fold follows only the active
        # one — the whole point of the manoeuvre. If a store leaked the first branch
        # into the context here, it would be folding by insertion order rather than by
        # parentId, which is the bug this contract exists to catch.
        context = ConversationTree(log.entries(), log.cursor).context_for(log.cursor)
        assert "on the first branch" not in _texts(context)
        assert "on a second, sibling branch" in _texts(context)

    # ------------------------------------------------------------ parentId chain

    def test_appends_chain_off_the_leaf(self, log):
        parent_before = log.cursor  # may be non-None if the store seeded entries

        a = log.append_message(_msg("user", "one"))
        b = log.append_message(_msg("assistant", "two"))
        c = log.append_message(_msg("user", "three"))

        by_id = {e["id"]: e for e in log.entries()}
        assert by_id[a]["parentId"] == parent_before
        assert by_id[b]["parentId"] == a
        assert by_id[c]["parentId"] == b
        assert log.cursor == c

    def test_every_entry_has_id_type_parentid_timestamp(self, log):
        """The only hard per-entry requirements the tree fold relies on."""
        log.append_message(_msg("user", "x"))
        log.append_custom_entry("note", {"k": "v"})

        for entry in log.entries():
            assert isinstance(entry["id"], str)
            assert isinstance(entry["type"], str)
            assert "parentId" in entry
            assert entry["timestamp"]

    def test_entry_ids_are_unique(self, log):
        ids = [log.append_message(_msg("user", str(i))) for i in range(25)]
        assert len(set(ids)) == 25

    def test_entries_returns_a_deep_copy(self, log):
        """A caller mutating the returned list, an entry, or a NESTED payload must not
        corrupt the log.

        The nested case is the one that mattered and the one the original test missed:
        both stores returned ``[dict(e) for e in entries]`` — a SHALLOW copy, which
        protects the top-level keys and leaves the message payload shared. So
        ``entries()[0]["message"]["content"] = …`` silently rewrote the live log (and,
        on a file store, diverged it from the on-disk JSONL, which is never rewritten).
        ``ctx.entries()`` documents itself as returning a read-only copy; this is the
        test that makes that true.
        """
        log.append_message(_msg("user", "original"))
        before = len(log.entries())

        entries = log.entries()
        entries.append({"type": "bogus", "id": "zz", "parentId": None})
        entries[0]["type"] = "tampered"
        for entry in entries:
            if entry.get("type") == "message":
                entry["message"]["content"][0]["text"] = "TAMPERED"

        assert len(log.entries()) == before
        assert all(e["type"] != "tampered" for e in log.entries())
        assert "TAMPERED" not in _texts(ConversationTree(log.entries(), log.cursor).context_for())

    def test_entries_preserves_append_order(self, log):
        """``resolve_cursor`` reads ``entries[-1]``, so order is load-bearing. A store
        that returns rows ``ORDER BY id`` (or unordered) would resolve the wrong cursor.
        """
        ids = [log.append_message(_msg("user", str(i))) for i in range(6)]
        listed = [e["id"] for e in log.entries()]

        assert [i for i in listed if i in set(ids)] == ids

    # ------------------------------------------------------------------ cursor

    def test_navigate_moves_the_cursor_to_its_target(self, log):
        a = log.append_message(_msg("user", "one"))
        log.append_message(_msg("assistant", "two"))

        log.append_navigate(a)

        assert log.cursor == a

    def test_navigate_to_unknown_target_raises(self, log):
        """Fail-Early: a dangling cursor would silently truncate the context fold."""
        log.append_message(_msg("user", "one"))
        with pytest.raises(ValueError):
            log.append_navigate("does-not-exist")

    def test_appending_after_navigate_branches(self, log):
        """navigate + append IS the branch mechanism — no separate branch entry kind."""
        a = log.append_message(_msg("user", "one"))
        b = log.append_message(_msg("assistant", "original"))
        log.append_navigate(a)
        c = log.append_message(_msg("assistant", "alternative"))

        by_id = {e["id"]: e for e in log.entries()}
        assert by_id[b]["parentId"] == a
        assert by_id[c]["parentId"] == a  # sibling of b, not its child
        assert log.cursor == c

    def test_resolve_cursor_agrees_with_the_live_cursor(self, log):
        """Reload-invariance: the cursor rebuilt from entries == the in-memory cursor."""
        log.append_message(_msg("user", "one"))
        a = log.append_message(_msg("assistant", "two"))
        log.append_message(_msg("user", "three"))
        log.append_navigate(a)

        assert resolve_cursor(log.entries()) == log.cursor == a

    # -------------------------------------------------------------- context fold

    def test_context_follows_the_active_path_only(self, log):
        """The abandoned branch stays on disk but drops out of model input."""
        a = log.append_message(_msg("user", "question"))
        log.append_message(_msg("assistant", "abandoned answer"))
        log.append_navigate(a)
        log.append_message(_msg("assistant", "kept answer"))

        context = ConversationTree(log.entries(), log.cursor).context_for()

        assert _texts(context) == ["question", "kept answer"]

    def test_custom_message_reaches_the_context(self, log):
        log.append_message(_msg("user", "hi"))
        log.append_custom_message({"role": "custom", "content": "injected"}, "myext")

        context = ConversationTree(log.entries(), log.cursor).context_for()

        assert len(context) == 2

    def test_custom_entry_is_durable_but_never_model_input(self, log):
        """Tree-as-backplane state: on the path, readable, excluded from the fold."""
        log.append_message(_msg("user", "hi"))
        log.append_custom_entry("bookmark", {"note": "remember"})
        log.append_message(_msg("assistant", "yo"))

        context = ConversationTree(log.entries(), log.cursor).context_for()

        assert _texts(context) == ["hi", "yo"]
        assert any(e["type"] == "customEntry" for e in log.entries())

    def test_unknown_entry_kinds_are_walked_through_not_crashed_on(self, log):
        """Foreign nodes (e.g. a JMFTS RAPTOR summary) must cost nothing and break nothing."""
        log.append_message(_msg("user", "hi"))
        log.append_custom_entry("jmfts:document", {"docId": "42"})
        log.append_message(_msg("assistant", "yo"))

        tree = ConversationTree(log.entries(), log.cursor)

        assert _texts(tree.context_for()) == ["hi", "yo"]
        assert len(tree.tree()) >= 1  # renders without raising

    # ------------------------------------------------------------- compaction

    def test_compaction_splices_the_context(self, log):
        log.append_message(_msg("user", "old one"))
        log.append_message(_msg("assistant", "old two"))
        keep = log.append_message(_msg("user", "recent"))
        log.append_compaction("SUMMARY", keep, 1234)

        context = ConversationTree(log.entries(), log.cursor).context_for()
        texts = _texts(context)

        assert any("SUMMARY" in t for t in texts)
        assert "recent" in texts
        assert "old one" not in texts  # spliced out

    def test_compaction_is_append_only(self, log):
        """Nothing is ever rewritten: the pre-compaction entries survive verbatim."""
        a = log.append_message(_msg("user", "old"))
        keep = log.append_message(_msg("user", "recent"))
        before = log.entries()

        log.append_compaction("SUMMARY", keep, 10)
        after = log.entries()

        assert after[: len(before)] == before
        assert any(e["id"] == a for e in after)

    def test_compaction_from_unknown_first_kept_id_raises(self, log):
        """Fail-Early, and the most damaging of the three unknown-id cases.

        ``navigate`` and ``branch_summary`` both raise on an unknown id; ``compaction``
        did not — it validated nothing. The fold walks the active path looking for the
        anchor and only starts keeping entries once it finds it, so an anchor that
        matches nothing is never found and the ENTIRE kept region drops out of the
        context. Not a crash, not a warning: the conversation silently loses its recent
        history and the model is handed ``[summary] + whatever_came_after``.

        The suite previously asserted the two cases that already raised and omitted the
        one that did not — matching the implementation instead of the algebra.
        """
        log.append_message(_msg("user", "one"))
        log.append_message(_msg("user", "recent"))

        with pytest.raises(ValueError):
            log.append_compaction("SUMMARY", "does-not-exist", 10)

    def test_last_compaction_wins(self, log):
        log.append_message(_msg("user", "one"))
        k1 = log.append_message(_msg("user", "two"))
        log.append_compaction("FIRST", k1, 10)
        k2 = log.append_message(_msg("user", "three"))
        log.append_compaction("SECOND", k2, 20)

        texts = _texts(ConversationTree(log.entries(), log.cursor).context_for())

        assert any("SECOND" in t for t in texts)
        assert not any("FIRST" in t for t in texts)

    # ----------------------------------------------------------- elide (W3/T3)
    #
    # NODE-ADDRESSABLE-AGENTS.md W3: ``elide`` generalizes the compaction anchor
    # into a summary-less splice — the SAME fold step in
    # ``ConversationTree._active_path_entries``, minus anything to render. Decision
    # 2 (tree SHAPE, not a per-node flag) and Decision 7 (``entries()`` stays TOTAL —
    # elide hides a span from a fold, never from the log) are what these tests pin.

    def test_elide_splices_the_context_with_no_summary(self, log):
        """T3 -- an anchor with no summary. The excluded span disappears from
        ``context_for`` exactly as it does for ``compaction``, but NO placeholder
        message takes its place: elide contributes literally nothing, which is
        the whole point of generalizing the anchor instead of special-casing it."""
        log.append_message(_msg("user", "old one"))
        log.append_message(_msg("assistant", "old two"))
        keep = log.append_message(_msg("user", "recent"))
        log.append_elide(keep)

        texts = _texts(ConversationTree(log.entries(), log.cursor).context_for())

        assert texts == ["recent"], "the anchor renders nothing; only the kept region remains"
        assert "old one" not in texts and "old two" not in texts  # spliced out

    def test_elide_whose_boundary_is_the_root_elides_nothing(self, log):
        """T3 -- an anchor whose boundary IS a root-level entry (``parentId is
        None``): the degenerate case where the excluded span is EMPTY. Nothing
        precedes the boundary in the leaf→root walk, so the boundary is found on
        the walk's very first entry and everything from the root through the
        anchor survives the fold -- only the anchor's own (nonexistent) message
        is missing. Pins that the boundary search does not need a non-empty
        prefix to work correctly.
        """
        log.append_navigate(None)  # cursor before any root -- next append is root-level
        root = log.append_message(_msg("user", "root message"))
        log.append_message(_msg("assistant", "middle"))
        log.append_elide(root)

        texts = _texts(ConversationTree(log.entries(), log.cursor).context_for())

        assert texts == ["root message", "middle"]

    def test_elide_from_unknown_first_kept_id_raises(self, log):
        """Fail-Early on an unknown anchor, exactly as ``compaction`` requires --
        the same forward-scan hazard (an anchor matching nothing is never found,
        so the whole kept region silently drops from the fold) applies unchanged."""
        log.append_message(_msg("user", "one"))
        log.append_message(_msg("user", "recent"))

        with pytest.raises(ValueError):
            log.append_elide("does-not-exist")

    def test_branch_rooted_inside_an_elided_span_is_unaffected(self, log):
        """T3 -- a branch rooted INSIDE a later elided span. ``b`` sits in the
        region the PRIMARY's own elide (appended after the branch already exists)
        will hide from ITS OWN ``context_for`` -- but the branch's ancestor chain
        stops at ``b`` and never reaches the elide entry (it was appended on a
        different path), so the branch's context is completely untouched. This is
        I1 (NODE-ADDRESSABLE-AGENTS.md) restated for ``elide`` specifically:
        exclusion lives in tree SHAPE, so a walker that never reaches the anchor
        cannot be affected by it -- no flag was there to forget to check.
        """
        log.append_message(_msg("user", "early"))
        b = log.append_message(_msg("assistant", "branch point"))

        # A branch forked off `b`, BEFORE the primary elides past it.
        branch = open_branch(log, b, label="reviewer")
        branch_leaf = branch.append_message(_msg("user", "branch content"))
        before = ConversationTree(log.entries(), log.cursor).context_for(branch_leaf)

        # The primary continues past `b`, then elides everything up to `keep` --
        # which puts `early` and `b` inside the excluded span from the PRIMARY leaf.
        keep = log.append_message(_msg("user", "kept on primary"))
        log.append_elide(keep)

        primary_texts = _texts(ConversationTree(log.entries(), log.cursor).context_for())
        assert "early" not in primary_texts and "branch point" not in primary_texts

        after = ConversationTree(log.entries(), log.cursor).context_for(branch_leaf)
        assert after == before, "an elide on another path must not perturb the branch's context"
        assert "branch point" in _texts(after) and "branch content" in _texts(after)

    # ------------------------------------------------------ branch lanes (C2/W14)

    def test_append_at_writes_to_an_explicit_parent_without_moving_the_leaf(self, log):
        """``append_at`` is the C2 branch primitive: a SECOND cursor writing to one log.

        The two halves are equally load-bearing. It must parent where it is TOLD (not at
        the leaf), and it must LEAVE THE LEAF ALONE — a store that quietly advanced its
        own leaf here would drag the primary conversation into the sub-agent's lane on
        the very next primary append, which no test of the branch's own context would
        catch.
        """
        anchor = log.append_message(_msg("user", "the branch point"))
        tip = log.append_message(_msg("assistant", "the primary tip"))

        branched = log.append_at(
            anchor, "message", {"message": _msg("user", "in a lane")}, lane="L"
        )

        by_id = {e["id"]: e for e in log.entries()}
        assert by_id[branched]["parentId"] == anchor, "parented where told, not at the leaf"
        assert by_id[branched]["branchOf"] == "L", "carries the lane marker"
        assert log.cursor == tip, "the primary leaf did NOT move"

    def test_a_lane_tagged_entry_landing_last_does_not_capture_the_primary_cursor(self, log):
        """Cursor discipline: a sub-agent's write landing last (a crash mid-branch) must
        not make the next load resume INSIDE the branch — the primary conversation would
        silently continue from a sub-agent's lane, with the wrong context and no error.
        """
        tip = log.append_message(_msg("user", "the primary tip"))
        log.append_at(tip, "message", {"message": _msg("assistant", "landed last")}, lane="L")

        entries = log.entries()
        assert "branchOf" in entries[-1], "precondition: a lane entry really is last"
        assert resolve_cursor(entries) == tip
        assert log.cursor == tip

        reloaded = self.reload(log)
        if reloaded is None:
            pytest.skip("no durable form")
        assert reloaded.cursor == tip, "and the discipline survives a real reload"

    def test_lane_entries_never_reach_the_primary_context(self, log):
        """Isolation is structural: a lane entry is never an ANCESTOR of the primary
        leaf, so the leaf→root walk cannot reach it — even though ``entries()`` has it."""
        anchor = log.append_message(_msg("user", "shared prefix"))
        log.append_message(_msg("assistant", "primary work"))
        log.append_at(anchor, "message", {"message": _msg("user", "LANE ONLY")}, lane="L")

        texts = _texts(ConversationTree(log.entries(), log.cursor).context_for())
        assert "LANE ONLY" not in texts
        assert "shared prefix" in texts and "primary work" in texts

    # --------------------------------------------------------- branch summary

    def test_branch_summary_reparents_to_the_branch_point(self, log):
        """The summary parents at the branch point, so the abandoned subtree becomes a
        sibling and drops out of the fold. Appending off the *current* leaf instead
        would leave the abandoned branch on the active path."""
        a = log.append_message(_msg("user", "question"))
        log.append_message(_msg("assistant", "abandoned"))
        sid = log.append_branch_summary("BRANCH SUMMARY", a)

        by_id = {e["id"]: e for e in log.entries()}
        assert by_id[sid]["parentId"] == a

        texts = _texts(ConversationTree(log.entries(), log.cursor).context_for())
        assert any("BRANCH SUMMARY" in t for t in texts)
        assert "abandoned" not in texts

    def test_branch_summary_from_unknown_id_raises(self, log):
        log.append_message(_msg("user", "x"))
        with pytest.raises(ValueError):
            log.append_branch_summary("s", "nope")

    # ------------------------------------------------------------ round-trip

    def test_a_reloaded_log_yields_the_same_tree(self, log):
        """Reload-invariance, over a log with a BRANCH and a COMPACTION in it — the two
        shapes whose ordering and anchoring a store can get subtly wrong.
        """
        reloaded = self.reload(log)
        if reloaded is None:
            pytest.skip("store has no durable form (in-memory)")

        a = log.append_message(_msg("user", "question"))
        log.append_message(_msg("assistant", "abandoned"))
        log.append_navigate(a)
        keep = log.append_message(_msg("assistant", "kept"))
        log.append_compaction("SUMMARY", keep, 99)

        expected_entries, expected_cursor = log.entries(), log.cursor
        expected_context = ConversationTree(expected_entries, expected_cursor).context_for()

        reloaded = self.reload(log)
        assert reloaded is not None

        assert reloaded.entries() == expected_entries
        assert reloaded.cursor == expected_cursor
        assert (
            ConversationTree(reloaded.entries(), reloaded.cursor).context_for() == expected_context
        )

    def test_same_entries_same_tree(self, log):
        """The invariant the whole design rests on: the fold is a pure function of
        (entries, cursor), so any store producing these entries produces this tree."""
        a = log.append_message(_msg("user", "one"))
        log.append_message(_msg("assistant", "two"))
        log.append_navigate(a)
        log.append_message(_msg("assistant", "alt"))

        entries, cursor = log.entries(), log.cursor
        first = ConversationTree(entries, cursor).context_for()
        second = ConversationTree([dict(e) for e in entries], cursor).context_for()

        assert first == second

    # ------------------------------------------------ node-addressable agents (I1/T2/T5)
    #
    # NODE-ADDRESSABLE-AGENTS.md §2 states I1 and I2 as consequences of the fold's
    # shape (a leaf→root parentId walk that never consults siblings or load order)
    # and never having a mutating write path. T1/T2/T5 below are that document's
    # own "Test obligations" §, made executable per Decision 7 / the doc's framing
    # that a contract belongs in the shared suite, not in a paragraph.

    def test_context_for_a_leaf_is_immutable_under_unrelated_appends(self, log):
        """T1 -- I1 as a conformance test. Per the spec, "the single most valuable
        test in this document": every concurrent reader (BranchView, a second
        agent parked at a node) depends on ``context_for(L)`` never changing once
        ``L`` exists, and no store proved it before this test existed.

        Three shapes of "elsewhere", chosen because each is a distinct way a
        store could fold by something OTHER than the strict parentId walk (e.g.
        insertion order, the whole entry list, a leaf-keyed cache) and still pass
        a less adversarial test:

          1. a lane (``open_branch``) -- a second cursor writing into the SAME log
          2. a plain sibling off an EARLIER node (``navigate`` + append)
          3. a compaction spliced into that OTHER, unrelated path

        ``context_for(leaf)`` is called with the captured leaf id explicitly
        throughout, so moving the log's own cursor via ``navigate`` in step 2
        cannot itself be what keeps the assertion trivially true.
        """
        root = log.cursor  # may be None (blank log) or a seeded entry (file/JMFTS)
        a = log.append_message(_msg("user", "shared question"))
        leaf = log.append_message(_msg("assistant", "the answer at L"))

        before = ConversationTree(log.entries(), log.cursor).context_for(leaf)

        # 1. a lane rooted at `a` -- a concurrent writer, never an ancestor of `leaf`.
        branch = open_branch(log, a, label="reviewer")
        branch.append_message(_msg("user", "lane-only content"))
        assert ConversationTree(log.entries(), log.cursor).context_for(leaf) == before

        # 2. a plain sibling off the root -- shares only the root with `leaf`'s
        #    ancestor chain, so it is "elsewhere" even though it is on the
        #    PRIMARY lane (unlike case 1).
        log.append_navigate(root)
        log.append_message(_msg("user", "an unrelated sibling subtree"))
        assert ConversationTree(log.entries(), log.cursor).context_for(leaf) == before

        # 3. a compaction spliced into that sibling subtree -- exercises the one
        #    fold step that scans more than a single entry (the anchor search),
        #    on a path that is still not an ancestor of `leaf`.
        keep = log.append_message(_msg("user", "kept on the other path"))
        log.append_compaction("SUMMARY ON THE OTHER PATH", keep, 10)
        assert ConversationTree(log.entries(), log.cursor).context_for(leaf) == before

    def test_no_pre_existing_entry_is_mutated_by_any_later_append(self, log):
        """T2 -- the no-mutation property T1 rests on. After any append, every
        PRE-EXISTING entry dict is unchanged: id, parentId, and payload. Cheap,
        and it pins the premise that makes I1 true at all -- if an append could
        rewrite an existing entry's parentId, a leaf's ancestor chain would no
        longer be fixed at the moment the leaf is appended.

        Exercises every append kind that touches parentId semantics
        (``append_branch_summary`` and ``append_navigate`` re-parent or move a
        cursor; ``append_at``/lanes write to an explicit, non-leaf parent), not
        just plain messages -- those are exactly the shapes a store could get
        subtly wrong while still passing a message-only version of this test.
        """
        a = log.append_message(_msg("user", "one"))
        b = log.append_message(_msg("assistant", "two"))
        before = copy.deepcopy(log.entries())

        log.append_message(_msg("user", "three"))
        log.append_custom_entry("note", {"k": "v"})
        log.append_navigate(a)
        log.append_message(_msg("assistant", "branch"))
        log.append_branch_summary("summary", a)
        c = log.append_message(_msg("user", "for compaction"))
        log.append_compaction("SUMMARY", c, 10)
        branch = open_branch(log, b, label="lane")
        branch.append_message(_msg("user", "lane content"))

        after_by_id = {e["id"]: e for e in log.entries()}
        for entry in before:
            assert after_by_id[entry["id"]] == entry, (
                f"pre-existing entry {entry['id']!r} was mutated by a later append"
            )

    def test_agent_spec_survives_reload_and_still_contributes_nothing_to_context(self, log):
        """T4 -- reload-invariance of the ``agent_spec`` provenance node (W2,
        NODE-ADDRESSABLE-AGENTS.md). ``agent_spec`` is a plain ``customEntry`` --
        already proven durable and context-excluded by
        ``test_custom_entry_is_durable_but_never_model_input`` above -- but that
        test used an arbitrary payload. What W2's own "record, never a contract"
        position (Decision 3) rests on is specifically THIS kind, carrying
        THIS shape, never leaking into the fold and surviving a REAL reload --
        not just an in-RAM re-fold, the same bar T1's sibling tests hold every
        other splice/lane mechanism to.
        """
        log.append_message(_msg("user", "hi"))
        spec_id = log.append_custom_entry(
            "agent_spec",
            {
                "model": {"id": "local-llm", "provider": "openai", "context_window": 8192},
                "system_prompt_digest": "deadbeef" * 8,  # a sha256 hex digest, never text
                "tools": ["read", "grep"],
                "extensions": ["ext_kit.steer"],
                "cwd": "/srv/project",
            },
        )
        log.append_message(_msg("assistant", "yo"))

        context = ConversationTree(log.entries(), log.cursor).context_for()
        assert _texts(context) == ["hi", "yo"], "agent_spec must never reach model input"

        reloaded = self.reload(log)
        if reloaded is None:
            pytest.skip("store has no durable form (in-memory)")

        by_id = {e["id"]: e for e in reloaded.entries()}
        assert spec_id in by_id, "agent_spec must survive reload -- it is a durable record"
        assert by_id[spec_id]["customType"] == "agent_spec"
        assert by_id[spec_id]["data"]["tools"] == ["read", "grep"]
        # Absolute prohibition (W2): api_key must NEVER enter the tree, hashed or not.
        assert "api_key" not in by_id[spec_id]["data"]

        reloaded_context = ConversationTree(reloaded.entries(), reloaded.cursor).context_for()
        assert _texts(reloaded_context) == ["hi", "yo"], (
            "agent_spec contributes nothing to context_for even after a reload"
        )

    def test_entries_is_total_across_every_exclusion_operation(self, log):
        """T5 -- Decision 7: entries() is total. Every filtering mechanism --
        ``context_for``, a lane, a branch-summary re-parent, a compaction, an
        elide -- is a FOLD OVER ``entries()``; none of them may be the only way to
        reach an entry. After each exclusion operation the store exposes, every id
        any append ever returned must still be present in ``entries()`` -- the
        audit guarantee that licenses hiding a span from a *view* in the first
        place, and specifically what licenses W3: ``elide`` may hide a span from a
        fold, never from the log.
        """
        minted: list[str] = []

        a = log.append_message(_msg("user", "one"))
        minted.append(a)
        b = log.append_message(_msg("assistant", "two"))
        minted.append(b)

        # 1. open a lane -- lands in entries() but is excluded from context_for.
        branch = open_branch(log, a, label="lane")
        lane_entry = branch.append_message(_msg("user", "lane content"))
        minted.append(lane_entry)

        # 2. re-parent via append_branch_summary -- `b`'s subtree drops out of
        #    context_for (it is no longer an ancestor of the new leaf) but must
        #    not drop out of entries().
        summary_id = log.append_branch_summary("BRANCH SUMMARY", a)
        minted.append(summary_id)

        # 3. compact -- the pre-boundary region is spliced OUT of context_for.
        keep = log.append_message(_msg("user", "kept"))
        minted.append(keep)
        compaction_id = log.append_compaction("SUMMARY", keep, 10)
        minted.append(compaction_id)

        # 4. elide -- W3's summary-less anchor splices out a second span the same
        #    way, and must be just as reachable through entries() afterward.
        keep2 = log.append_message(_msg("user", "kept again"))
        minted.append(keep2)
        elide_id = log.append_elide(keep2)
        minted.append(elide_id)

        present = {e["id"] for e in log.entries()}
        for entry_id in minted:
            assert entry_id in present, (
                f"entry {entry_id!r} is unreachable via entries() after an exclusion "
                "operation -- entries() must stay total, or a fold becomes the ONLY "
                "way to reach an entry (Decision 7)"
            )
