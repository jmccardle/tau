"""τ-agent-core tree surgery: the pure plan algebra behind the editing gestures.

``ConversationTree`` reads the session tree; this module decides what a *new*
branch would look like before anything is written. It is side-effect-free and holds
no ``SessionLog``: every function takes a ``ConversationTree`` and returns either a
plan or the reason there is not one. The durable half — ``append_at``,
``append_navigate``, ``append_elide`` — lives in the frontend's backend object
(``TauBackend.commit_branch`` / ``paste_subtree``), for the same reason the elide's
does: the modal accumulates an intent, the caller performs it
(TREE-BROWSER-AS-EDITOR.md §11.1).

**Why a plan exists at all** (§6.1). Take ``m1 → m2 → m3 → m4 → m5`` and a wanted
path ``m1, m4, m5``. The elide cannot express it: ``_active_path_entries`` emits the
anchor and then its ancestors from ``firstKeptId`` onward, so what an elide removes
is always a PREFIX. Keeping ``m1`` while skipping ``m2..m3`` needs a prefix and a
suffix with a gap between them, and the only append-only way to get one is to mint
new entries. Minting them one gesture at a time either re-parents existing nodes —
which breaks I1 (``NODE-ADDRESSABLE-AGENTS.md``: an entry's ancestor chain is fixed
at append, which is what makes "what did this message see" answerable forever) — or
litters a fresh copy of the tail per gesture. A plan mints the divergent tail once,
in order, with correct parents on the first attempt.

The second reason is validation. Copying an assistant message without its
``toolResult`` composes a prefix most providers reject outright, and the plan is the
only place the composed path can be checked while refusing still costs nothing.

**What the plan is** (§6.2): an ordered list, root-most first, of items that are
either *kept* (an existing entry, used in place, identity preserved) or *copied*
(an existing entry's content, minted here as a new entry). :func:`plan_branch`
derives that split from the marked set rather than asking the reader for it — the
largest run of marks that is already a real ancestor chain is kept, and everything
after the first gap is copied, which is §6.3's "reusable prefix" rule read off the
tree instead of typed in.

Reference: TREE-BROWSER-AS-EDITOR.md §6 (plan then commit), §7 (copy entries);
NODE-ADDRESSABLE-AGENTS.md I1 (ancestry is fixed at append).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable

from tau_llm.docs import agent_facing

from tau_agent_core.conversation_tree import (
    ConversationTree,
    TreeNode,
    entries_to_messages,
    is_system_message,
)

#: Entry kinds a copy can be made of. A copy is minted with ``append_at`` under a
#: new parent and must stand on its own there, which rules out the two splice
#: anchors and ``navigate``: all three carry an id (``firstKeptId`` / ``targetId``)
#: naming an entry the copy's new path does not contain, and ``_active_path_entries``
#: reads an unreachable ``firstKeptId`` as "keep nothing", collapsing the context to
#: the anchor alone. ``customEntry`` is backplane state, not conversation, and is
#: excluded for the plainer reason that it contributes no message.
COPYABLE_KINDS = ("message", "customMessage", "branch_summary")


@agent_facing(topic="sessions", since="0.9.7")
@dataclass(frozen=True)
class BranchPlan:
    """A branch worked out from a set of marked nodes, before anything is written.

    Attributes:
        attach: The existing entry the branch grows from — the last kept item. The
            commit moves the cursor here before minting anything.
        keeps: Marked entries used IN PLACE, root-most first. Always at least one:
            the root-most mark is an existing entry and is trivially a chain of one.
            These keep their identity, their ids, and their recorded usage.
        copies: Marked entries to be minted as new entries under ``attach``, in
            order. Empty when the whole selection was already a real ancestor chain
            — §6.3's case A, the shape that mints nothing.
        elide_from: The entry an ``elide`` should resume at once the branch is
            minted, or ``None`` when no elide is wanted or when one would hide
            nothing. Set only for ``drop_context=True``.
        hidden: How many entries that elide would remove from the fold. ``0``
            whenever ``elide_from`` is ``None``.
    """

    attach: str
    keeps: tuple[str, ...]
    copies: tuple[str, ...]
    elide_from: str | None
    hidden: int

    @property
    def mints(self) -> int:
        """How many new entries the commit will append for the copies."""
        return len(self.copies)


@agent_facing(topic="sessions", since="0.9.7")
@dataclass(frozen=True)
class PasteMint:
    """One entry a paste will mint, and where it hangs.

    Attributes:
        source_id: The entry being copied. Written into the new entry as
            ``copiedFrom``.
        parent_source_id: The source id of this mint's parent, or ``None`` when it
            hangs directly from the paste target. Source ids, not new ids, because
            the new ones do not exist until the commit runs — the commit keeps a
            source→new map as it walks the mints in order.
        kind: The entry type to append, always one of :data:`COPYABLE_KINDS`.
        payload: The append payload, ready for ``SessionLog.append_at``, including
            ``copiedFrom``.
    """

    source_id: str
    parent_source_id: str | None
    kind: str
    payload: dict[str, Any]


@agent_facing(topic="sessions", since="0.9.7")
@dataclass(frozen=True)
class PastePlan:
    """A subtree copy worked out against the tree, before anything is written.

    Attributes:
        target: The entry the copied subtree hangs from.
        mints: The entries to append, parents before children.
        skipped: Source entries left out because their kind is not copyable
            (:data:`COPYABLE_KINDS`). Their children re-parent onto the nearest
            copied ancestor, so the copy is shorter than the original rather than
            broken — and the count is reported to the reader rather than swallowed.
    """

    target: str
    mints: tuple[PasteMint, ...]
    skipped: tuple[str, ...]


@agent_facing(topic="sessions", since="0.9.7")
def selection_order(tree: ConversationTree, ids: Iterable[str]) -> tuple[str, ...]:
    """Put marked ids into the order the browser draws them.

    The reader marks nodes in whatever order they find them; a branch built from
    "the order I happened to click" would be a different conversation depending on
    how the reader navigated. Row order is the tree's own order — a preorder walk
    of :meth:`ConversationTree.tree`, whose children are already sorted by
    timestamp — so the branch reads down the screen exactly as it was selected.

    Args:
        tree: The tree the ids belong to.
        ids: The marked entry ids, in any order. Duplicates collapse.

    Returns:
        The same ids, root-most first, in row order.

    Raises:
        ValueError: An id names no entry in this tree. Fail-Early: a plan built
            around a dangling id would mint a branch missing a message the reader
            asked for, and say nothing.
    """
    wanted = set(ids)
    unknown = [entry_id for entry_id in sorted(wanted) if not tree.contains(entry_id)]
    if unknown:
        raise ValueError(f"marked entries not in this tree: {', '.join(unknown)}")
    return tuple(node_id for node_id in _row_order(tree) if node_id in wanted)


@agent_facing(topic="sessions", since="0.9.7")
def tool_group(tree: ConversationTree, entry_id: str) -> frozenset[str]:
    """``entry_id`` together with the entries that cannot be separated from it.

    An assistant message that declares tool calls and the ``toolResult`` entries
    answering them are one unit as far as any provider is concerned: a copy of one
    without the other is a prefix that gets rejected. Rather than let the reader
    build that selection and refuse it at the commit, the browser expands a mark to
    this set — selecting either end selects both, and the reader SEES the group
    light up on the rows.

    The pairing is structural: an assistant's results are looked for among its
    DESCENDANTS, and a result's declaring assistant among its ANCESTORS, which is
    the only relation that means "answered this call on this line". The first match
    per ``tool_call_id`` in row order wins, so an assistant whose call was re-run on
    two branches pulls in one result, not a group spanning both.

    Args:
        tree: The tree ``entry_id`` belongs to.
        entry_id: The entry the reader marked.

    Returns:
        The ids to mark, always including ``entry_id``. A message with no tool calls
        and no ``tool_call_id`` is its own group, so this is the identity for the
        ordinary case.

    Raises:
        KeyError: No entry has that id.
    """
    entry = tree.entry(entry_id)
    message = entry.get("message")
    if not isinstance(message, dict):
        return frozenset({entry_id})

    role = message.get("role")
    if role == "assistant":
        call_ids = {
            str(block["id"])
            for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "toolCall" and "id" in block
        }
        if not call_ids:
            return frozenset({entry_id})
        found: dict[str, str] = {}
        for candidate in _descendants(tree, entry_id):
            call_id = _tool_result_call_id(tree.entry(candidate))
            if call_id in call_ids and call_id not in found:
                found[call_id] = candidate
        return frozenset({entry_id, *found.values()})

    if role == "toolResult":
        call_id = _tool_result_call_id(entry)
        if call_id is None:
            return frozenset({entry_id})
        for ancestor in reversed(tree.path(entry_id)[:-1]):
            declared = ancestor.get("message")
            if not isinstance(declared, dict) or declared.get("role") != "assistant":
                continue
            if any(
                isinstance(block, dict)
                and block.get("type") == "toolCall"
                and str(block.get("id")) == call_id
                for block in declared.get("content", [])
            ):
                # The whole group, from the assistant's side: marking a result must
                # pull in its siblings too, or the assistant arrives with one of its
                # two calls answered.
                return tool_group(tree, str(ancestor["id"]))
        return frozenset({entry_id})

    return frozenset({entry_id})


@agent_facing(topic="sessions", since="0.9.7")
def plan_branch(tree: ConversationTree, ids: Iterable[str], *, drop_context: bool) -> BranchPlan:
    """Work out the branch a marked selection asks for.

    The keep/copy split is read off the tree (§6.3 step 2): the longest run of marks
    starting at the root-most one where each is the real ``parentId`` child of the
    one before is KEPT, and everything after the first gap is COPIED. A selection
    that is already a contiguous ancestor chain therefore mints nothing at all and
    the commit is a cursor move plus, at most, an elide — case A, the only form that
    preserves every id's identity.

    Args:
        tree: The tree the marks belong to.
        ids: The marked entry ids, in any order (ordered by :func:`selection_order`).
        drop_context: Whether the branch should keep only the selection. ``True``
            plans an ``elide`` resuming at the root-most mark, so the context becomes
            the system prompt plus the branch; ``False`` leaves everything above the
            attach point in context.

    Returns:
        The plan. Ask :func:`branch_refusal_reason` first — this function assumes the
        selection is legal and describes what would happen, rather than judging it.

    Raises:
        ValueError: The selection is empty, or an id names no entry.
    """
    ordered = selection_order(tree, ids)
    if not ordered:
        raise ValueError("nothing marked: a branch needs at least one message")

    keep_count = 1
    for previous, current in zip(ordered, ordered[1:]):
        if tree.entry(current).get("parentId") != previous:
            break
        keep_count += 1

    keeps = ordered[:keep_count]
    copies = ordered[keep_count:]
    attach = keeps[-1]

    elide_from: str | None = None
    hidden = 0
    if drop_context:
        # The same arithmetic ``TauBackend.elide_span`` performs, run here so the
        # offer and the commit cannot disagree about whether the elide is a no-op.
        # Measured at the ATTACH point, because that is where the elide's boundary
        # has to be reachable from; the copies minted below it only extend the path.
        root_most = ordered[0]
        path_ids = [e["id"] for e in tree.path(attach)]
        kept_ids = set(path_ids[path_ids.index(root_most) :])
        removed = [
            entry
            for entry in tree.context_entries(attach)
            if entry["id"] not in kept_ids and not is_system_message(entry)
        ]
        if removed:
            elide_from = root_most
            hidden = len(removed)

    return BranchPlan(
        attach=attach,
        keeps=keeps,
        copies=copies,
        elide_from=elide_from,
        hidden=hidden,
    )


@agent_facing(topic="sessions", since="0.9.7")
def branch_refusal_reason(
    tree: ConversationTree, ids: Iterable[str], *, drop_context: bool
) -> str | None:
    """Why the marked selection cannot become a branch, or ``None`` if it can.

    Every refusal the commit would raise, computed while the reader can still see
    the tree they marked. Three of them:

    * a marked node whose kind cannot be copied (:data:`COPYABLE_KINDS`) — a
      ``navigate`` or a splice anchor carries an id pointing at a path the copy
      will not be on;
    * a system message among the COPIES, which would mint a second system prompt in
      the middle of the branch beside the one every fold carries anyway. As the
      root-most mark it is a *keep* and stays legal — that is how "hang these
      messages straight off the system prompt" is said;
    * a composed path that is not turn-complete (:func:`admission_reason`).

    The last should be rare: the browser expands a mark to its :func:`tool_group`,
    so a reader cannot easily select half a tool call. It is still checked, because
    "rare" and "impossible" are different and the log is what pays the difference.

    Args:
        tree: The tree the marks belong to.
        ids: The marked entry ids, in any order.
        drop_context: As :func:`plan_branch` — it changes what the composed path is,
            and therefore what turn-completeness means for it.

    Returns:
        A sentence naming the problem, or ``None``.

    Raises:
        ValueError: The selection is empty, or an id names no entry.
    """
    ordered = selection_order(tree, ids)
    if not ordered:
        raise ValueError("nothing marked: a branch needs at least one message")

    for entry_id in ordered:
        kind = str(tree.entry(entry_id).get("type", ""))
        if kind not in COPYABLE_KINDS:
            return (
                f"{entry_id} is a {kind!r} entry, which a branch cannot carry: it is "
                "structure rather than a message, and it names a position in the tree "
                "the new branch will not be at."
            )

    plan = plan_branch(tree, ordered, drop_context=drop_context)

    # A system message among the COPIES would be minted a second time, in the middle
    # of a conversation, beside the one the fold carries across every splice
    # (``is_system_message``). As a KEEP it is fine and useful — it is the branch's
    # attach point, which is how "hang these messages straight off the system prompt"
    # is said — so this refuses the one position that duplicates it rather than the
    # mark itself.
    duplicated = [entry_id for entry_id in plan.copies if is_system_message(tree.entry(entry_id))]
    if duplicated:
        return (
            f"{duplicated[0]} is the system prompt, and copying it here would put a "
            "second one in the middle of the branch. Every fold carries the system "
            "prompt already; mark it as the FIRST message of the branch to hang the "
            "rest off it, or leave it out."
        )

    return admission_reason(planned_messages(tree, plan))


@agent_facing(topic="sessions", since="0.9.7")
def planned_messages(tree: ConversationTree, plan: BranchPlan) -> list[dict[str, Any]]:
    """The message list the branch would hand the model, if it were committed.

    Built from the same fold the live session uses (:func:`entries_to_messages`), so
    a plan that validates here cannot fail differently once appended. The copies
    contribute their source entries' messages, because that is what a copy IS — the
    same content at a new id.

    Args:
        tree: The tree the plan was made against.
        plan: The plan to project.

    Returns:
        The composed root→leaf message list.
    """
    base = tree.context_entries(plan.attach)
    if plan.elide_from is not None:
        # What the planned elide leaves: the carried system messages plus the run
        # from the resume point onward.
        keeping = False
        kept: list[dict[str, Any]] = []
        for entry in base:
            if entry["id"] == plan.elide_from:
                keeping = True
            if keeping:
                kept.append(entry)
            elif is_system_message(entry):
                kept.append(entry)
        base = kept
    return entries_to_messages([*base, *(tree.entry(entry_id) for entry_id in plan.copies)])


@agent_facing(topic="sessions", since="0.9.7")
def admission_reason(messages: list[dict[str, Any]]) -> str | None:
    """Why this message sequence would be rejected as a turn, or ``None``.

    The plan-level counterpart of
    :meth:`~tau_agent_core.conversation_tree.ConversationTree.fork_admission_reason`,
    which asks the same question of a real path. Two faults, and the first is the one
    a hand-built selection produces:

    * an **orphan result** — a ``toolResult`` whose ``tool_call_id`` no preceding
      assistant message declared. Copying a result without its call, or eliding the
      call out from under it, both land here;
    * a **pending call** — the sequence ends on an assistant message with tool calls
      that nothing answered. This is what ``fork_admission_reason`` rejects, and it
      matters here for the same reason: the branch's tip becomes the cursor, so the
      next turn starts from exactly this prefix.

    Args:
        messages: The composed message list, root→leaf.

    Returns:
        A sentence naming the offending call, or ``None`` when the sequence is
        turn-complete.
    """
    pending: dict[str, str] = {}
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            pending = {
                str(block["id"]): str(block.get("name", "?"))
                for block in message.get("content", [])
                if isinstance(block, dict) and block.get("type") == "toolCall" and "id" in block
            }
        elif role == "toolResult":
            call_id = message.get("tool_call_id")
            if call_id is None:
                continue
            if str(call_id) not in pending:
                return (
                    f"a tool result for call {call_id} has no tool call before it on this "
                    "path — the message that made the call is not in the selection."
                )
            pending.pop(str(call_id))

    if pending:
        names = ", ".join(f"{name}({call_id})" for call_id, name in pending.items())
        return (
            f"the last assistant message still has unanswered tool call(s) {names} — a "
            "conversation cannot end there, and the branch's tip is where the next turn "
            "would start."
        )
    return None


@agent_facing(topic="sessions", since="0.9.7")
def copy_of(entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The ``(kind, payload)`` an ``append_at`` needs to re-mint ``entry`` elsewhere.

    A copied message keeps ``type: "message"`` rather than gaining a kind of its own
    (§7.1): a copy IS an ordinary message on an ordinary path, and every existing
    walker should treat it as one without being taught anything. The provenance is
    one extra field, ``copiedFrom``, which joins the JMFTS store's cross-reference
    fields so a query can dedupe two documents with identical text.

    A ``branch_summary`` copy drops ``fromId``. That field names the branch point the
    summary was written at, which the copy is not at; carrying it over would state a
    relation to a node the copy has no edge to.

    Args:
        entry: The source entry, whose kind must be in :data:`COPYABLE_KINDS`.

    Returns:
        The entry type and the payload to append.

    Raises:
        ValueError: The entry's kind cannot be copied.
    """
    kind = str(entry.get("type", ""))
    if kind not in COPYABLE_KINDS:
        raise ValueError(f"a {kind!r} entry cannot be copied")

    payload: dict[str, Any] = {"copiedFrom": entry["id"]}
    if kind == "branch_summary":
        payload["summary"] = entry.get("summary", "")
        payload["fromId"] = None
        return kind, payload

    payload["message"] = copy.deepcopy(entry.get("message", {}))
    if kind == "customMessage":
        payload["customType"] = entry.get("customType", "")
    return kind, payload


@agent_facing(topic="sessions", since="0.9.7")
def plan_paste(tree: ConversationTree, source_id: str, target_id: str) -> PastePlan:
    """Work out the subtree copy a paste asks for.

    The whole subtree, not one node: a paste re-creates what hangs below the copied
    node, including its forks, because a conversation branch's shape is part of what
    was copied. Parents come before children, so the commit can walk the mints once
    keeping a source→new id map.

    Args:
        tree: The tree both ids belong to.
        source_id: The copied node — the root of the subtree.
        target_id: The entry the copy hangs from.

    Returns:
        The plan.

    Raises:
        ValueError: Either id is unknown, the source's kind cannot be copied, or the
            target is inside the source's own subtree — that last one would put the
            copy and the original on one root→leaf path, where a duplicated
            ``tool_call_id`` stops meaning one call.
    """
    if not tree.contains(source_id):
        raise ValueError(f"copied entry {source_id!r} not found")
    if not tree.contains(target_id):
        raise ValueError(f"paste target {target_id!r} not found")

    subtree = _descendants(tree, source_id)
    if target_id == source_id or target_id in subtree:
        raise ValueError(
            f"cannot paste {source_id!r} into its own subtree: the copy and the original "
            "would share a root→leaf path, so a duplicated tool_call_id would name two "
            "different calls at once."
        )

    source_kind = str(tree.entry(source_id).get("type", ""))
    if source_kind not in COPYABLE_KINDS:
        raise ValueError(
            f"{source_id!r} is a {source_kind!r} entry, which cannot be copied "
            f"(copyable kinds: {', '.join(COPYABLE_KINDS)})"
        )

    mints: list[PasteMint] = []
    skipped: list[str] = []
    # Nearest COPIED ancestor, by source id. A skipped entry maps to whatever its own
    # parent mapped to, which is how its children re-parent onto the nearest copied
    # ancestor instead of vanishing with it.
    attach_of: dict[str, str | None] = {source_id: None}
    for entry_id in [source_id, *subtree]:
        entry = tree.entry(entry_id)
        kind = str(entry.get("type", ""))
        parent_attach = attach_of.get(entry_id)
        if kind not in COPYABLE_KINDS:
            skipped.append(entry_id)
            for child in tree.children_of(entry_id):
                attach_of[child] = parent_attach
            continue
        copied_kind, payload = copy_of(entry)
        mints.append(
            PasteMint(
                source_id=entry_id,
                parent_source_id=parent_attach,
                kind=copied_kind,
                payload=payload,
            )
        )
        for child in tree.children_of(entry_id):
            attach_of[child] = entry_id

    return PastePlan(target=target_id, mints=tuple(mints), skipped=tuple(skipped))


@agent_facing(topic="sessions", since="0.9.7")
def paste_refusal_reason(tree: ConversationTree, plan: PastePlan) -> str | None:
    """Why the pasted subtree would be malformed where it lands, or ``None``.

    One fault: a copied ``toolResult`` whose call is on neither the target's path nor
    the copied run above it. Every message below such a result is unusable, and the
    reader cannot see why from the rows.

    A copied line that ENDS on an unanswered tool call is deliberately not refused. A
    paste does not move the cursor, so no turn starts there until someone navigates
    onto it — and navigating onto a node with a pending call is a state an ordinary
    interrupted turn reaches too. Refusing it here would invent a rule the rest of
    the browser does not apply.

    Args:
        tree: The tree the plan was made against.
        plan: The paste plan to check.

    Returns:
        A sentence naming the offending result, or ``None``.
    """
    base = entries_to_messages(tree.context_entries(plan.target))
    pending_at: dict[str | None, set[str]] = {None: _pending_after(base)}

    for mint in plan.mints:
        pending = set(pending_at[mint.parent_source_id])
        message = mint.payload.get("message")
        if isinstance(message, dict):
            role = message.get("role")
            if role == "assistant":
                pending = {
                    str(block["id"])
                    for block in message.get("content", [])
                    if isinstance(block, dict) and block.get("type") == "toolCall" and "id" in block
                }
            elif role == "toolResult":
                call_id = message.get("tool_call_id")
                if call_id is not None:
                    if str(call_id) not in pending:
                        return (
                            f"{mint.source_id} is a tool result for call {call_id}, and the "
                            "message "
                            "that made that call is neither in the copied subtree nor on the "
                            "path you are pasting onto."
                        )
                    pending.discard(str(call_id))
        pending_at[mint.source_id] = pending
    return None


# --- internals --------------------------------------------------------------


def _pending_after(messages: list[dict[str, Any]]) -> set[str]:
    """Tool call ids the last assistant message left unanswered."""
    pending: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            pending = {
                str(block["id"])
                for block in message.get("content", [])
                if isinstance(block, dict) and block.get("type") == "toolCall" and "id" in block
            }
        elif role == "toolResult":
            call_id = message.get("tool_call_id")
            if call_id is not None:
                pending.discard(str(call_id))
    return pending


def _tool_result_call_id(entry: dict[str, Any]) -> str | None:
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "toolResult":
        return None
    call_id = message.get("tool_call_id")
    return None if call_id is None else str(call_id)


def _row_order(tree: ConversationTree) -> list[str]:
    """Every id in the order the browser draws it: preorder, children by timestamp."""
    order: list[str] = []
    stack: list[TreeNode] = list(reversed(tree.tree()))
    while stack:
        node = stack.pop()
        order.append(node.id)
        stack.extend(reversed(node.children))
    return order


def _descendants(tree: ConversationTree, entry_id: str) -> list[str]:
    """``entry_id``'s descendants, excluding itself, parents before children.

    Preorder with children oldest-first — the order :meth:`ConversationTree.tree`
    draws them in, which is what makes a pasted copy read like the original.
    """
    out: list[str] = []
    stack = list(reversed(tree.children_of(entry_id)))
    while stack:
        node_id = stack.pop()
        out.append(node_id)
        stack.extend(reversed(tree.children_of(node_id)))
    return out
