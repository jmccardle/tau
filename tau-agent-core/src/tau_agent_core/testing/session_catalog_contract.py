"""The ``SessionCatalog`` conformance suite — the construction algebra, executable.

Subclass and supply ``make_catalog()``. Every catalog (the file store, the JMFTS
store, any future database-backed one) must satisfy all of it, because the TUI and
headless never name a concrete store: ``app.py``'s ``action_new_chat`` /
``on_chat_selected`` and ``headless.py``'s ``_select_session`` call ``create`` /
``list`` / ``load`` / ``fork`` / ``resolve_ref`` through the ABC and expect the same
answers whichever one was injected.

The keystone here is :meth:`test_every_listed_ref_loads`. A ``SessionInfo.ref`` is
documented as "the storage-agnostic handle a catalog's ``load()`` accepts back"
(session_catalog.py:14-18) — and until this suite existed, nothing checked that a
store honoured it. The consequence was live: two TUI tests asserted against
``session.path`` and ``rglob("*.jsonl")``, which is the file store's ref *spelling*
rather than the contract, so they broke the moment a config selected a different
store. Those are frontend bugs, but the reason they could hide is that no suite
pinned the ref round-trip on the store side.

The second thing pinned here is *durability across catalog instances*
(:meth:`reopen`). A store whose refs only resolve within the process that minted
them satisfies every single-instance test and still cannot resume a session on the
next run — the one thing a session store exists to do.

Notes for implementers of this suite:

* ``cwd``/``other_cwd`` are fixtures, not constants, so a store needing per-run
  isolation (JMFTS shares one server across runs) overrides them with scoped values.
* ``unknown_ref``/``missing_ref_error`` exist because "names nothing loadable" is
  spelled differently per store (``FileNotFoundError`` on a missing path,
  ``JmftsError`` on a missing document). The *contract* is that it raises rather
  than returning ``None`` or an empty session; the type is the store's to declare.

Reference: session_catalog.py (the ABC + ``ConversationSession`` + ``SessionInfo``);
tau_agent_core.testing.session_log_contract (the sibling suite for the entry algebra).
"""

from __future__ import annotations

from typing import Any

import pytest

from tau_agent_core.session_catalog import ConversationSession, SessionCatalog

MODEL = "contract-model"
BACKEND = "contract-backend"


def _msg(role: str, text: str) -> dict[str, Any]:
    """A message in the wire shape stores actually receive (content *blocks*)."""
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _texts(messages: list[dict[str, Any]]) -> list[str]:
    """Flatten a message fold to text, so assertions read as a transcript."""
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out.extend(b["text"] for b in content if isinstance(b, dict) and "text" in b)
    return out


class SessionCatalogContractTests:
    """Conformance tests for the SessionCatalog construction algebra.

    Subclasses implement :meth:`make_catalog`; everything else has a default that is
    correct for a RAM-only store and is overridden by stores that persist.
    """

    # ------------------------------------------------------------------ knobs

    def make_catalog(self) -> SessionCatalog:
        raise NotImplementedError("contract subclass must provide make_catalog()")

    def reopen(self, catalog: SessionCatalog) -> SessionCatalog | None:
        """A **fresh** catalog over the same storage, or ``None`` if there is none.

        Override in any store that persists — this is what distinguishes "the
        session is in a dict I am still holding" from "the session is on disk / in
        the server and the next process can find it". A RAM-only catalog honestly
        has no such thing and returns ``None``, so the durability tests skip rather
        than passing vacuously.
        """
        return None

    def unknown_ref(self) -> str:
        """A well-formed ref that names nothing.

        The default is a plausible-looking id. Override where a store's refs have
        structure (a path, an integer document id) and a random string would fail
        the wrong way — the test must prove "nothing there", not "unparseable".
        """
        return "0f5a1c4e-0000-4000-8000-000000000000"

    #: What :meth:`SessionCatalog.load` raises for :meth:`unknown_ref`. Tightened by
    #: subclasses; the contract itself only requires that it raises at all.
    missing_ref_error: type[BaseException] = Exception

    @pytest.fixture
    def cwd(self) -> str:
        return "/contract/a"

    @pytest.fixture
    def other_cwd(self) -> str:
        return "/contract/b"

    @pytest.fixture
    def catalog(self) -> SessionCatalog:
        return self.make_catalog()

    # ------------------------------------------------------------- structural

    def test_is_a_session_catalog(self, catalog):
        """The ABC, not a duck: ``most_recent``/``resolve_ref`` come from it."""
        assert isinstance(catalog, SessionCatalog)

    def test_created_session_satisfies_conversation_session(self, catalog, cwd):
        session = catalog.create(cwd, MODEL, BACKEND, system_prompt="sys")
        assert isinstance(session, ConversationSession)

    # ---------------------------------------------------------- create / load

    def test_create_then_load_round_trips(self, catalog, cwd):
        created = catalog.create(cwd, MODEL, BACKEND, system_prompt="sys", name="Title")
        created.append_message(_msg("user", "hi"))

        loaded = catalog.load(catalog.list(cwd)[0].ref)
        assert loaded.id == created.id
        assert loaded.model == MODEL
        assert loaded.backend == BACKEND
        assert _texts(loaded.messages) == ["sys", "hi"]

    def test_load_unknown_ref_raises(self, catalog):
        """Fail-Early: never ``None``, never a blank session standing in for one."""
        with pytest.raises(self.missing_ref_error):
            catalog.load(self.unknown_ref())

    def test_every_listed_ref_loads(self, catalog, cwd):
        """The keystone: a ref handed out by ``list()`` is accepted by ``load()``.

        ``SessionInfo.ref`` is the *only* handle a frontend gets from a picker row,
        so a store that hands back something ``load()`` won't take has no usable
        listing at all.
        """
        made = [catalog.create(cwd, MODEL, BACKEND, name=f"s{n}") for n in range(3)]
        for m in made:
            m.append_message(_msg("user", "x"))

        infos = catalog.list(cwd)
        assert {i.id for i in infos} == {m.id for m in made}
        for info in infos:
            assert catalog.load(info.ref).id == info.id

    # ------------------------------------------------------------ durability

    def test_appends_survive_a_reopen(self, catalog, cwd):
        """What "persisted" means: another catalog instance sees the writes."""
        session = catalog.create(cwd, MODEL, BACKEND, system_prompt="sys")
        session.append_message(_msg("user", "before"))
        ref = catalog.list(cwd)[0].ref

        reopened = self.reopen(catalog)
        if reopened is None:
            pytest.skip("catalog has no durable form")
        assert _texts(reopened.load(ref).messages) == ["sys", "before"]

    def test_a_listed_ref_outlives_the_catalog_that_minted_it(self, catalog, cwd):
        """Refs are storage handles, not process-local tokens.

        A ref that only resolves inside the instance that produced it passes every
        other test here and still cannot resume a session on the next run.
        """
        created = catalog.create(cwd, MODEL, BACKEND)
        created.append_message(_msg("user", "hi"))
        ref = catalog.list(cwd)[0].ref

        reopened = self.reopen(catalog)
        if reopened is None:
            pytest.skip("catalog has no durable form")
        assert reopened.load(ref).id == created.id

    # -------------------------------------------------------------- ephemeral

    def test_ephemeral_session_is_never_reachable_through_the_catalog(self, catalog, cwd):
        """``--no-session`` means *no session*: nothing was written to find.

        Asserted against the listing, not against ``load(session.id)``. An id is
        **not** a ref — under the JMFTS store a ref is an integer document id while
        a session's id is a τ uuid, so ``load(ephemeral.id)`` would be exercising
        that store's ref *parser* (it raises ``ValueError`` before ever asking the
        server) rather than the property in question. What a catalog actually owes
        here is that an ephemeral session never becomes reachable — that no listing
        names it and no "continue where I left off" finds it.
        """
        ephemeral = catalog.create_ephemeral(cwd, MODEL, BACKEND, system_prompt="sys")
        ephemeral.append_message(_msg("user", "hello"))
        ephemeral.append_session_info("renamed")

        assert catalog.list(cwd) == []
        assert catalog.most_recent(cwd) is None

    def test_ephemeral_session_is_otherwise_fully_usable(self, catalog, cwd):
        """Unpersisted, not degraded — the agent loop runs against it unchanged."""
        ephemeral = catalog.create_ephemeral(cwd, MODEL, BACKEND, system_prompt="sys")
        ephemeral.append_message(_msg("user", "hello"))
        ephemeral.append_message(_msg("assistant", "hi"))
        assert isinstance(ephemeral, ConversationSession)
        assert _texts(ephemeral.messages) == ["sys", "hello", "hi"]
        assert ephemeral.model == MODEL

    # ------------------------------------------------------------------ list

    def test_list_scopes_by_cwd(self, catalog, cwd, other_cwd):
        here = catalog.create(cwd, MODEL, BACKEND)
        there = catalog.create(other_cwd, MODEL, BACKEND)

        assert [i.id for i in catalog.list(cwd)] == [here.id]
        assert [i.id for i in catalog.list(other_cwd)] == [there.id]

    def test_list_of_none_spans_every_cwd(self, catalog, cwd, other_cwd):
        here = catalog.create(cwd, MODEL, BACKEND)
        there = catalog.create(other_cwd, MODEL, BACKEND)

        assert {here.id, there.id} <= {i.id for i in catalog.list(None)}

    def test_list_of_an_unused_scope_is_empty(self, catalog, cwd, other_cwd):
        catalog.create(other_cwd, MODEL, BACKEND)
        assert catalog.list(cwd) == []

    def test_list_is_newest_first(self, catalog, cwd):
        """Asserted as an ordering invariant, not an expected pair of ids.

        Two sessions created microseconds apart may share a ``modified`` stamp at
        test speed; "sorted descending" is what ``most_recent`` and the picker
        actually rely on, and it is true whether or not they tie.
        """
        for n in range(3):
            catalog.create(cwd, MODEL, BACKEND).append_message(_msg("user", f"m{n}"))

        modified = [i.modified for i in catalog.list(cwd)]
        assert modified == sorted(modified, reverse=True)

    def test_list_metadata_describes_the_conversation(self, catalog, cwd):
        """The picker renders from these fields alone; ``system`` is not a message."""
        session = catalog.create(cwd, MODEL, BACKEND, system_prompt="sys", name="My Session")
        session.append_message(_msg("user", "first question"))
        session.append_message(_msg("assistant", "an answer"))
        session.append_message(_msg("user", "last question"))

        (info,) = catalog.list(cwd)
        assert info.id == session.id
        assert info.cwd == cwd
        assert info.name == "My Session"
        assert info.message_count == 3
        assert info.first_message == "first question"
        assert info.last_message == "last question"
        assert info.modified >= info.created

    def test_an_unnamed_session_lists_with_no_name(self, catalog, cwd):
        catalog.create(cwd, MODEL, BACKEND)
        assert catalog.list(cwd)[0].name is None

    # ------------------------------------------------------------------ fork

    def test_fork_carries_the_history_under_a_new_id(self, catalog, cwd):
        source = catalog.create(cwd, MODEL, BACKEND, system_prompt="sys")
        source.append_message(_msg("user", "original"))

        forked = catalog.fork(source, cwd)
        assert forked.id != source.id
        assert _texts(forked.messages) == ["sys", "original"]

    def test_fork_leaves_the_source_untouched(self, catalog, cwd):
        """The point of forking: explore without editing what you branched from."""
        source = catalog.create(cwd, MODEL, BACKEND, system_prompt="sys")
        source.append_message(_msg("user", "original"))

        forked = catalog.fork(source, cwd)
        forked.append_message(_msg("user", "branch"))

        assert "branch" not in _texts(source.messages)
        # ...and not in the *stored* source either, which is what the next run reads.
        source_ref = next(i.ref for i in catalog.list(cwd) if i.id == source.id)
        assert _texts(catalog.load(source_ref).messages) == ["sys", "original"]

    def test_fork_is_listed_and_loadable_in_its_own_right(self, catalog, cwd):
        source = catalog.create(cwd, MODEL, BACKEND)
        source.append_message(_msg("user", "original"))
        forked = catalog.fork(source, cwd)

        by_id = {i.id: i for i in catalog.list(cwd)}
        assert forked.id in by_id
        assert catalog.load(by_id[forked.id].ref).id == forked.id
        # Lineage is recorded, not inferred: the tree browser draws the fork under
        # its source from this field alone, and an unset one orphans the branch.
        assert by_id[forked.id].parent == source.id
        assert by_id[source.id].parent is None

    # ---------------------------------- most_recent (shared base-class method)

    def test_most_recent_loads_the_head_of_the_listing(self, catalog, cwd):
        """``most_recent`` is defined as ``load(list(cwd)[0].ref)`` — pin that.

        Comparing against the listing rather than a remembered session keeps this
        honest under ties, while still proving the two agree.
        """
        for n in range(3):
            catalog.create(cwd, MODEL, BACKEND).append_message(_msg("user", f"m{n}"))

        result = catalog.most_recent(cwd)
        assert result is not None
        assert result.id == catalog.list(cwd)[0].id

    def test_most_recent_of_an_unused_scope_is_none(self, catalog, cwd, other_cwd):
        catalog.create(other_cwd, MODEL, BACKEND)
        assert catalog.most_recent(cwd) is None

    # ----------------------------------- resolve_ref (shared base-class method)

    def test_resolve_ref_by_exact_id(self, catalog, cwd):
        session = catalog.create(cwd, MODEL, BACKEND)
        assert catalog.resolve_ref(session.id, cwd=cwd).id == session.id

    def test_resolve_ref_by_unique_id_prefix(self, catalog, cwd):
        session = catalog.create(cwd, MODEL, BACKEND)
        assert catalog.resolve_ref(session.id[:8], cwd=cwd).id == session.id

    def test_resolve_ref_ambiguous_prefix_raises(self, catalog, cwd):
        """Fail-Early: two candidates means ask, never pick one.

        The empty string is the one prefix every id is guaranteed to share no matter
        how a store mints ids, so this is deterministic without forging collisions.
        """
        catalog.create(cwd, MODEL, BACKEND)
        catalog.create(cwd, MODEL, BACKEND)
        with pytest.raises(LookupError, match="matches multiple sessions"):
            catalog.resolve_ref("", cwd=cwd)

    def test_resolve_ref_no_match_raises(self, catalog, cwd):
        catalog.create(cwd, MODEL, BACKEND)
        with pytest.raises(LookupError, match="no session matches"):
            catalog.resolve_ref("zzzzzzzz", cwd=cwd)

    def test_resolve_ref_is_scoped_by_cwd(self, catalog, cwd, other_cwd):
        session = catalog.create(other_cwd, MODEL, BACKEND)
        with pytest.raises(LookupError, match="no session matches"):
            catalog.resolve_ref(session.id, cwd=cwd)
        assert catalog.resolve_ref(session.id, cwd=None).id == session.id
