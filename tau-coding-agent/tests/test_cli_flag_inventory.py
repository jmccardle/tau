"""Every ``CLIArgs`` field must have a STATED disposition under ``--mode rpc``.

The defect this file prevents is not a bug in any one flag — it is the shape
``--no-session`` had for a release: ``cli.py`` parsed it, the ``--mode rpc``
validation block did not reject it, and ``rpc_mode.run_rpc`` never read it. All
three of those are individually reasonable, and no test in the suite could see
the combination, because each of the three files was self-consistent. A flag
that is accepted and ignored is the exact failure "Fail-Early" exists to
prevent, and the surrounding code already refuses several other combinations by
hand (``--no-session`` + ``--session-dir``, ``--print`` + ``--mode rpc``) — so
the rule was understood and the coverage simply had a hole the size of one
field.

**What this test can and cannot do.** It does NOT detect inertness. Proving
that ``--mode rpc`` ignores a field would require following it through
``resolve_model_config``, ``create_backend`` and the extension loader, which is
real dataflow analysis and would be a worse thing to maintain than the bug. It
instead forces a DECLARATION: every field of ``CLIArgs`` appears in exactly one
of the five sets below, and a newly added flag makes this file red until
somebody writes down what ``--mode rpc`` does with it. That moves the question
to the moment the flag is added, which is when answering it is cheap and when
the person answering still knows.

The sets are hand-maintained on purpose. A generated list would agree with the
code by construction and assert nothing.
"""

from __future__ import annotations

import dataclasses

from tau_coding_agent.cli import CLIArgs

#: Rejected by ``cli.py``'s ``--mode rpc`` block: passing one is exit code 2
#: with a message naming the wire verb that replaces it. Pinned by
#: ``test_cli.py``/``test_session_dir_flag.py``, not here.
REJECTED_UNDER_RPC = {
    "print_mode",
    "messages",
    "resume",
    "continue_session",
    "session",
    "fork",
    "name",
    "store",
}

#: Read by ``rpc_mode.run_rpc`` (directly or through the shared
#: ``resolve_model_config``/``create_backend``/``load_extensions`` path it
#: takes with ``--print``) and changing what the run does.
HONORED_UNDER_RPC = {
    "mode",
    "model",
    "provider",
    "tools",
    "no_tools",
    "exclude_tools",
    "no_builtin_tools",
    "extensions",
    "no_extensions",
    "bus",
    "ext_config",
    "ui_defaults",
    "system_prompt",
    "append_system_prompt",
    # -nc rides the same `resolve_model_config` -> `create_backend` path: it
    # lands on the model config as `no_context_files`, and `TauBackend` passes
    # it to `_build_system_prompt`, so an RPC run really does start with no
    # AGENTS.md/CLAUDE.md in its prompt. Checked against rpc_mode.py:226,248.
    "no_context_files",
    "thinking",
    "session_dir",
    # --max-turns rides the same `resolve_model_config` -> `create_backend` path
    # as -nc: it lands on the model config as `max_turns`, `TauBackend` forwards
    # it to `AgentSession`, and `AgentSession._turn_cap` puts it in the
    # `AgentLoopConfig`. Checked against rpc_mode.py:225,249.
    "max_turns",
    # The field this file exists because of. `--mode rpc` selects
    # `session_catalog.create_ephemeral` for the startup session when it is
    # set, which is what `--print` has always done on the same seam.
    "no_session",
}

#: Accepted and genuinely global — they do not describe the run's shape, so
#: "ignored by RPC mode" is not a thing they could be.
GLOBAL_UNDER_RPC = {
    "verbose",
}

#: One-shot commands that ``cli.py`` dispatches and exits from BEFORE the
#: ``--mode rpc`` branch is reached, and that already refuse every mode-shaped
#: flag combination in their own validation block. ``--mode rpc`` never sees
#: them because there is no run left to configure.
TERMINAL_BEFORE_RPC = {
    "import_session",
    "export_session",
}

#: Flags that configure the interactive TUI's appearance and reach no run at all.
#: ``--mode rpc`` and ``--print`` render no TUI, so there is nothing for these to
#: be ignored *by* — the surface they address does not exist in those modes.
#:
#: This is a distinct disposition from :data:`GLOBAL_UNDER_RPC`, not a softer
#: spelling of it: a global flag still does something in RPC mode, and one of
#: these provably cannot. It is also distinct from :data:`REJECTED_UNDER_RPC`,
#: where ``--resume`` sits — that flag names a workflow ``--mode rpc`` replaces
#: with a wire verb, so passing it means the caller misunderstood the mode.
#: Passing ``--fun`` to an RPC server means nothing of the sort, and exiting 2
#: over a tagline would be a worse answer than doing nothing.
TUI_ONLY = {
    # tau_coding_agent.tagline: picks the startup tagline at random. Reaches one
    # string on one widget on the empty chat pane, and stops there.
    "fun",
    # tau_coding_agent.themes: the TUI colour theme for this run. Read in exactly
    # one place — ``_launch_tui`` puts it in ``cli_overrides`` — and ``rpc_mode.py``
    # never mentions it. Checked: ``grep -n theme cli.py`` is the flag definition,
    # the ``CLIArgs`` field, the ``parse_cli_args`` line and the ``_launch_tui``
    # block, and nothing else.
    "theme",
}


def test_every_cli_field_has_a_stated_disposition_under_mode_rpc():
    """The whole point of the file: no field may be silently unclassified.

    A new flag lands here as an unclassified name, and the fix is one line in
    whichever set above describes what ``--mode rpc`` does with it — after
    checking that the answer is true, which is the step ``--no-session``
    skipped.
    """
    declared = (
        REJECTED_UNDER_RPC | HONORED_UNDER_RPC | GLOBAL_UNDER_RPC | TERMINAL_BEFORE_RPC | TUI_ONLY
    )
    fields = {f.name for f in dataclasses.fields(CLIArgs)}

    unclassified = fields - declared
    assert unclassified == set(), (
        "these CLIArgs fields have no stated disposition under --mode rpc: "
        f"{sorted(unclassified)}. Add each to REJECTED_UNDER_RPC, "
        "HONORED_UNDER_RPC, GLOBAL_UNDER_RPC, TERMINAL_BEFORE_RPC or TUI_ONLY "
        "in this file — and verify the answer against rpc_mode.py before writing it "
        "down. A flag that --mode rpc accepts and ignores is the defect this "
        "test exists to catch."
    )


def test_no_field_is_classified_twice():
    """The sets are dispositions, not tags: a flag cannot be both rejected and
    honored. Without this, a stale entry left behind by a reclassification
    keeps the test above green while describing the flag wrongly."""
    sets = {
        "REJECTED_UNDER_RPC": REJECTED_UNDER_RPC,
        "HONORED_UNDER_RPC": HONORED_UNDER_RPC,
        "GLOBAL_UNDER_RPC": GLOBAL_UNDER_RPC,
        "TERMINAL_BEFORE_RPC": TERMINAL_BEFORE_RPC,
        "TUI_ONLY": TUI_ONLY,
    }
    overlaps = {
        f"{a} ∩ {b}": sorted(sets[a] & sets[b])
        for i, a in enumerate(sets)
        for b in list(sets)[i + 1 :]
        if sets[a] & sets[b]
    }
    assert overlaps == {}, overlaps


def test_no_set_names_a_field_that_no_longer_exists():
    """The inverse drift: a flag is removed from ``CLIArgs`` and its entry here
    outlives it, quietly shrinking what the first test checks."""
    declared = (
        REJECTED_UNDER_RPC | HONORED_UNDER_RPC | GLOBAL_UNDER_RPC | TERMINAL_BEFORE_RPC | TUI_ONLY
    )
    fields = {f.name for f in dataclasses.fields(CLIArgs)}

    phantom = declared - fields
    assert phantom == set(), (
        f"these names are classified here but are not CLIArgs fields: {sorted(phantom)}"
    )
