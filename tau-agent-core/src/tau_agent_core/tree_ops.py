"""τ-agent-core tree operations: the five durable session-tree mutations.

``conversation_tree`` READS the session tree and ``tree_surgery`` decides what a
new branch WOULD look like; this module is the half that writes. Every function
here takes a ``SessionLog`` as its first argument, performs one mutation on it,
and returns what the caller needs to re-render — nothing else. No frontend
object, no ``AgentSession``, no display surface.

They lived on ``TauBackend`` until the capability/flow split, which is why
``tree_surgery``'s own module docstring still says the durable half is "in the
frontend's backend object": one head owned the only way to elide a span, branch
from a selection, or paste a subtree, so no other head could offer those
gestures at all and the RPC wire carried none of them. The head still owns the
modals and the re-render; it no longer owns the mutation.

Two of them cost a model call and three do not, and the split is in the
signatures rather than behind a flag: :func:`navigate` is synchronous because
moving a cursor is an append, and :func:`summarize_and_navigate` is a coroutine
returning the summarizer's usage for the caller to bank, because it spends
tokens outside the agent loop.

Reference: TREE-BROWSER-AS-EDITOR.md §6 (plan then commit), §7 (copy entries);
NODE-ADDRESSABLE-AGENTS.md I1 (ancestry is fixed at append), W3 (elide).
"""

from __future__ import annotations

from typing import Any, Sequence

from tau_llm.docs import agent_facing

from tau_agent_core.compaction import estimate_span_tokens
from tau_agent_core.conversation_tree import ConversationTree, is_system_message
from tau_agent_core.session_log import SessionLog, agent_spec_in_force
from tau_agent_core.tree_surgery import (
    branch_refusal_reason,
    copy_of,
    paste_refusal_reason,
    plan_branch,
    plan_paste,
)

__all__ = [
    "commit_branch",
    "elide_span",
    "navigate",
    "paste_subtree",
    "summarize_and_navigate",
]


@agent_facing(topic="sessions")
def navigate(session: SessionLog, target_id: str | None) -> list[dict]:
    """Move ``session``'s cursor to ``target_id`` and return the new context.

    Appends a ``navigate`` entry — zero LLM calls. The abandoned branch drops out
    of context via the ``parentId`` walk but stays on disk, append-only and still
    browsable. A ``target_id`` that is already the cursor is a no-op that still
    returns the context, so a caller need not check first.

    Typed to the ``SessionLog`` Protocol rather than a concrete store: this
    touches only ``cursor``, ``entries()`` and ``append_navigate``, all three of
    which are on the Protocol, so an in-memory, file or database-backed log works
    here unchanged.

    Args:
        session: The session log to move.
        target_id: The entry to move the cursor onto, or ``None`` for pre-root —
            the next append then starts a branch above every existing entry.

    Returns:
        ``ConversationTree.context_for(cursor)`` — the flat message list a head
        swaps into its transcript and re-renders.
    """
    if target_id == session.cursor:
        return ConversationTree(session.entries(), session.cursor).context_for()
    session.append_navigate(target_id)
    return ConversationTree(session.entries(), session.cursor).context_for()


@agent_facing(topic="sessions")
async def summarize_and_navigate(
    session: SessionLog,
    target_id: str,
    model: Any,
    *,
    api_key: str | None = None,
    custom_instructions: str | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """Summarize the subtree at ``target_id`` and splice the summary onto the path.

    The summarizing arm of what used to be ``navigate_tree(summarize=True)``.
    Extracts the abandoned subtree's text (``ConversationTree.subtree_text``),
    summarizes it (``session_manager.summarize_branch``, which raises on a failed
    or empty summary rather than returning one), and appends a ``branch_summary``
    entry parented at the branch point (Decision 5, fix 1).

    Separate from :func:`navigate` rather than a flag on it, because the two
    differ in what they cost: this one makes a completion call, so it is a
    coroutine and it hands back the tokens it spent. The caller banks them —
    ``AgentSession.record_side_usage`` on the live path — because this function
    holds no session object to bank them against.

    Args:
        session: The session log to write the summary into.
        target_id: The branch point. The subtree BELOW it is what gets summarized,
            and the ``branch_summary`` entry is parented at it.
        model: The model config the summarizer runs against.
        api_key: The key for that model's provider, when it needs one.
        custom_instructions: Extra guidance for the summarizer's SYSTEM prompt
            (the tree browser's mode 3).

    Returns:
        A pair: the re-rendered context (``ConversationTree.context_for``) and the
        summarizer's usage, for the caller to add to its side ledger.

    Raises:
        ValueError: ``target_id`` names no entry (checked first, before the model
            call — ``subtree_text`` answers ``""`` for an unknown id, so without
            this the operation would spend a completion summarizing nothing and
            append the result), or the summarizer returned nothing usable — the
            second raised by ``session_manager.summarize_branch``, not fabricated
            into an empty summary here.
    """
    from tau_agent_core.session_manager import summarize_branch

    entries = session.entries()
    if target_id not in {e["id"] for e in entries}:
        raise ValueError(f"summarize target {target_id!r} not found")
    branch_text = ConversationTree(entries, session.cursor).subtree_text(target_id)
    summary, usage = await summarize_branch(
        branch_text,
        model,
        api_key=api_key,
        custom_instructions=custom_instructions,
    )
    session.append_branch_summary(summary, target_id)
    return ConversationTree(session.entries(), session.cursor).context_for(), usage


@agent_facing(topic="sessions")
def elide_span(session: SessionLog, anchor_id: str, first_kept_id: str) -> list[dict]:
    """Fold a span out of ``session``'s context and return the new context.

    ``elide`` is the summary-less generalization of the compaction anchor (W3,
    NODE-ADDRESSABLE-AGENTS.md). **Synchronous**, unlike
    :func:`summarize_and_navigate`: there is no summary, therefore no model call
    and nothing to await. An ``async def`` with no ``await`` would advertise an
    I/O boundary this operation does not have.

    Two ids, because an elide is not a branch point. ``anchor_id`` is where the
    fold jumps FROM — the elide entry is appended as its child, so the anchor
    becomes the end of the kept region and the new tip. ``first_kept_id`` is where
    it jumps TO: ``ConversationTree._active_path_entries`` emits the anchor, then
    the anchor's ancestors from ``firstKeptId`` onward. Everything on that path
    BEFORE ``firstKeptId`` is the elided span.

    **``first_kept_id`` must therefore be the anchor itself or one of its
    ancestors, never a descendant.** That direction is not a style choice, it is
    what the fold's forward scan over ``path[:anchor_idx]`` can reach: a boundary
    the scan never finds leaves ``found`` False forever, so the fold emits the
    anchor and NOTHING else — an empty context, silently, with no error. Hence the
    check here, before either append: ``append_elide``'s own Fail-Early only proves
    the id names *an* entry, not that it names a reachable one, and the unreachable
    case is the more damaging of the two.

    Refusing a no-op elide is the other check. An elide whose span is empty
    (``first_kept_id`` already the first entry the fold keeps) persists a node that
    changes nothing about the context it was created to change — the
    silent-no-op anti-pattern, indistinguishable to the user from a successful
    fold. The core's ``append_elide`` deliberately permits it (an anchor on a
    root-level entry is a pinned contract case); this operation, where someone just
    asked for a span to disappear, does not.

    Nothing is erased: the navigate/elide pair are appends like any other, and
    every entry the fold now skips is still in ``entries()`` (Decision 7 / T5).

    Args:
        session: The session log to fold.
        anchor_id: The entry the fold jumps from, which becomes the new tip.
        first_kept_id: The entry the fold resumes at. The anchor itself, or one of
            its ancestors.

    Returns:
        ``ConversationTree.context_for(cursor)`` — the flat message list a head
        swaps into its transcript and re-renders, exactly as :func:`navigate` does.

    Raises:
        ValueError: An unknown anchor or resume point, a resume point that is not
            on the anchor's path, or a span that would hide nothing. All three are
            checked before the first append, so a refusal leaves the log
            byte-identical.
    """
    entries = session.entries()
    known = {e["id"] for e in entries}
    if anchor_id not in known:
        raise ValueError(f"elide anchor {anchor_id!r} not found")
    if first_kept_id not in known:
        raise ValueError(f"elide resume point {first_kept_id!r} not found")

    tree = ConversationTree(entries, session.cursor)
    path_ids = [e["id"] for e in tree.path(anchor_id)]
    if first_kept_id not in path_ids:
        raise ValueError(
            f"elide resume point {first_kept_id!r} is not on the path to anchor "
            f"{anchor_id!r} — it must be the anchor itself or one of its ancestors. "
            "The fold scans only the anchor's ancestors for the boundary, so a "
            "resume point it cannot reach would drop the ENTIRE context, silently."
        )

    kept = set(path_ids[path_ids.index(first_kept_id) :])
    hidden = [
        e
        for e in tree.context_entries(anchor_id)
        if e["id"] not in kept and not is_system_message(e)
    ]
    if not hidden:
        raise ValueError(
            f"elide from anchor {anchor_id!r} resuming at {first_kept_id!r} would hide "
            "nothing — the resume point is already the first entry the fold keeps"
        )

    if session.cursor != anchor_id:
        session.append_navigate(anchor_id)
    session.append_elide(
        first_kept_id,
        covered_entries=len(hidden),
        covered_tokens=estimate_span_tokens(hidden),
        agent_spec_id=agent_spec_in_force(entries, anchor_id),
    )

    return ConversationTree(session.entries(), session.cursor).context_for()


@agent_facing(topic="sessions")
def commit_branch(session: SessionLog, ids: Sequence[str], *, drop_context: bool) -> list[dict]:
    """Build a branch out of the marked entries and continue on it.

    The durable half of TREE-BROWSER-AS-EDITOR.md §6. ``tree_surgery`` decides what
    the branch IS — which marks are kept in place, which are minted as copies,
    whether an elide follows — and this performs it, in the order §6.3 fixes:

    1. move the leaf to the plan's attach point (the last kept mark);
    2. mint each copy with ``append_at``, parented at the previous one;
    3. move the leaf onto the last minted entry;
    4. append the elide, when the caller asked to keep only the selection.

    **Step 2 is invisible until step 3 lands.** ``append_at`` does not move the
    leaf, so a mint that fails partway leaves orphan entries hanging off the attach
    point and the cursor exactly where it was — the commit is atomic from the
    cursor's point of view, which is the property §6.3 is built around and the
    reason the copies are not appended one gesture at a time.

    Nothing is re-parented and nothing is erased. I1 holds because every entry's
    ``parentId`` is still written once, at append (§6.1's argument for why a plan
    exists at all rather than a sequence of edits).

    Args:
        session: The session log to write to.
        ids: The marked entry ids, in any order — ``tree_surgery`` puts them into
            tree order.
        drop_context: Whether the branch keeps only the selection. ``True`` appends
            an elide resuming at the root-most mark, so the context becomes the
            system prompt plus the branch. ``False`` leaves everything above the
            attach point in context.

    Returns:
        ``ConversationTree.context_for(cursor)`` — the new flat message list, the
        same re-render seam :func:`elide_span` and :func:`navigate` use.

    Raises:
        ValueError: The selection is empty, names an unknown entry, contains an
            entry no branch can carry, or composes a path that is not
            turn-complete. Checked before the first append, so a refusal leaves the
            log byte-identical.
    """
    entries = session.entries()
    tree = ConversationTree(entries, session.cursor)
    refusal = branch_refusal_reason(tree, ids, drop_context=drop_context)
    if refusal is not None:
        raise ValueError(f"cannot branch from this selection: {refusal}")
    plan = plan_branch(tree, ids, drop_context=drop_context)

    if session.cursor != plan.attach:
        session.append_navigate(plan.attach)

    parent = plan.attach
    for source_id in plan.copies:
        kind, payload = copy_of(tree.entry(source_id))
        parent = session.append_at(parent, kind, payload)
    if plan.copies:
        session.append_navigate(parent)

    if plan.elide_from is not None:
        after = session.entries()
        grown = ConversationTree(after, session.cursor)
        path_ids = [e["id"] for e in grown.path(session.cursor)]
        kept = set(path_ids[path_ids.index(plan.elide_from) :])
        hidden = [
            e
            for e in grown.context_entries(session.cursor)
            if e["id"] not in kept and not is_system_message(e)
        ]
        session.append_elide(
            plan.elide_from,
            covered_entries=len(hidden),
            covered_tokens=estimate_span_tokens(hidden),
            agent_spec_id=agent_spec_in_force(after, str(session.cursor)),
        )

    return ConversationTree(session.entries(), session.cursor).context_for()


@agent_facing(topic="sessions")
def paste_subtree(session: SessionLog, source_id: str, target_id: str) -> list[str]:
    """Re-create the subtree at ``source_id`` under ``target_id``.

    The durable half of TREE-BROWSER-AS-EDITOR.md §7. Every copied entry is a new
    entry carrying ``copiedFrom``, minted with ``append_at`` so the paste never
    moves the leaf: a paste edits the TREE, and what the model sees changes only
    when someone navigates onto the copy. That split is why this returns ids rather
    than a message list — nothing about the current context changed.

    Parents are minted before children (``plan_paste`` orders them that way), and a
    source→new id map re-hangs each child under its copied parent, so the copy keeps
    the original's shape including its forks.

    Args:
        session: The session log to write to.
        source_id: The copied node — the root of the subtree.
        target_id: The entry the copy hangs from.

    Returns:
        The ids minted, in the order they were appended. The first is the copy of
        ``source_id`` itself.

    Raises:
        ValueError: An unknown id, a source whose kind cannot be copied, a target
            inside the source's own subtree, or a copied tool result whose call is
            on neither the target's path nor the copied run. Checked before the
            first append.
    """
    tree = ConversationTree(session.entries(), session.cursor)
    plan = plan_paste(tree, source_id, target_id)
    refusal = paste_refusal_reason(tree, plan)
    if refusal is not None:
        raise ValueError(f"cannot paste here: {refusal}")

    minted: dict[str, str] = {}
    for mint in plan.mints:
        parent = plan.target if mint.parent_source_id is None else minted[mint.parent_source_id]
        minted[mint.source_id] = session.append_at(parent, mint.kind, mint.payload)
    return [minted[mint.source_id] for mint in plan.mints]
