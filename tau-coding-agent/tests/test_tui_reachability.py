"""Every declared mutation is reachable from the TUI, or says in writing why not.

The 1:1 map of headless capability to human interface was measured by hand three
times during the architecture overhaul and asserted nowhere, so it could rot
between reviews. This is that measurement, made into a check.

The rule the count uses: PERSON-reachable means mid-session, so a startup flag
does not count. A mutation is reachable either because a :class:`Flow` names it —
which projects a slash name, a palette entry and a completion candidate from one
declaration — or because a head offers a gesture for it, which is head-local and
therefore has to be named here by hand.

The pattern is `test_rpc_capability_audit.py`'s: two maps that must together
cover the set exactly, with a real reason for every entry in the second. A
mutation that is neither fails, and "it is internal" is not a reason.

Reference: docs/TUI-STYLE-GUIDE.md §2.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tau_agent_core.capabilities import CAPABILITIES, FLOWS

from tau_coding_agent import app, backends, dialogs

MUTATIONS = {name for name, cap in CAPABILITIES.items() if cap.kind == "mutation"}

FLOW_BACKED = {flow.mutation for flow in FLOWS}

BY_GESTURE: dict[str, str] = {
    "submit": "the editor: type and press the send key (`enter_key` decides which)",
    "abort": "escape while a turn is running — `TauApp.action_cancel_generation`",
    "new_session": "the sidebar's new-chat control — `TauApp.action_new_chat`",
    "navigate": "the tree browser: enter on a row — `TauBackend.navigate_tree`",
    "summarize_and_navigate": (
        "the tree browser's mode modal, which is `navigate_tree(summarize=True)` — "
        "the core split the two because they differ in cost, and the head keeps the "
        "one flag its three modes already speak"
    ),
    "elide_span": "the tree browser: the elide chord — `TauBackend.elide_span`",
    "commit_branch": "the tree browser: ctrl+B on marked rows — `TauBackend.commit_branch`",
    "paste_subtree": "the tree browser: v — `TauBackend.paste_subtree`",
}

NOT_REACHABLE: dict[str, str] = {
    "set_extension_config": (
        "No gesture, deliberately. A Flow declares a fixed tuple of Arguments and "
        "this mutation's second one is a form whose fields are whatever the chosen "
        "extension declared — not sayable in the Argument vocabulary, which is why "
        "the capability carries arguments=None. Offering it needs a registration "
        "path into the registry (register_flow), not head code; adding head-local "
        "dispatch for it would re-open the finding the flow projection closed. "
        "Machine-reachable today via the RPC verb of the same name."
    ),
}


def test_the_three_maps_cover_every_mutation_exactly():
    """Neither a hole nor a stale entry. Both directions, so a deleted capability
    leaves a name behind here and fails rather than sitting unread."""
    triaged = FLOW_BACKED | set(BY_GESTURE) | set(NOT_REACHABLE)
    assert triaged == MUTATIONS, {
        "untriaged": sorted(MUTATIONS - triaged),
        "named here but not a mutation": sorted(triaged - MUTATIONS),
    }


def test_no_mutation_is_claimed_twice():
    """A flow-backed mutation listed as a gesture would hide the flow's disappearance."""
    assert not FLOW_BACKED & set(BY_GESTURE)
    assert not FLOW_BACKED & set(NOT_REACHABLE)
    assert not set(BY_GESTURE) & set(NOT_REACHABLE)


@pytest.mark.parametrize("mutation", sorted(BY_GESTURE))
def test_each_named_gesture_names_something_that_exists(mutation: str):
    """The reasons cite `TauApp.action_*` and `TauBackend.*` by name; the names have
    to resolve, or the map is prose about a head that changed underneath it."""
    reason = BY_GESTURE[mutation]
    cited = re.findall(r"`(TauApp|TauBackend)\.([a-z_]+)`", reason)
    for owner, method in cited:
        cls = app.TauApp if owner == "TauApp" else backends.TauBackend
        assert hasattr(cls, method), f"{mutation}: {owner}.{method} does not exist"


@pytest.mark.parametrize("mutation", sorted(NOT_REACHABLE))
def test_an_unreachable_mutation_gives_a_real_reason(mutation: str):
    """"Internal" is not a reason. Say what a person would do, or what is missing."""
    reason = NOT_REACHABLE[mutation]
    assert len(reason) > 80
    assert "internal" not in reason.lower()


STYLESHEET = Path(dialogs.__file__).with_name("tau.tcss")

STYLED_ID_CAP = 35
"""Distinct `#id` selectors the stylesheet may name. Measured 2026-09-04.

An id names one mounted widget, so an id rule is a proper noun and a vocabulary
of proper nouns is the one thing a style guide cannot constrain. This was 53 when
the four dialog roles were restated once per modal; the shell took it to 35.
Anything per-modal, per-message or per-lane takes a class.
"""


def _styled_ids() -> set[str]:
    css = re.sub(r"/\*.*?\*/", "", STYLESHEET.read_text(), flags=re.S)
    ids: set[str] = set()
    for rule in re.finditer(r"^([^{}\n][^{}]*)\{", css, flags=re.M):
        ids.update(re.findall(r"#([A-Za-z0-9_-]+)", rule.group(1)))
    return ids


def test_the_styled_id_count_does_not_grow():
    """The check docs/TUI-STYLE-GUIDE.md §6.2 named and nobody wrote.

    Lowering `STYLED_ID_CAP` alongside a removal is the point; raising it is the
    thing to argue for in the commit message rather than do silently.
    """
    ids = _styled_ids()
    assert len(ids) <= STYLED_ID_CAP, sorted(ids)
