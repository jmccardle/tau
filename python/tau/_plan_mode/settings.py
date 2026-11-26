"""Plan mode's settings: constants an administrator edits, never options a client sets.

Only assignments. To change them, copy this package to `$TAU_HOME/extensions/plan_mode` (it
shadows the built-in) and edit this file there; the next start reads it.
"""

READ_ONLY_TOOLS = ("read", "grep", "find")
"""The tools plan mode offers and lets run, as far as the driver has them. A tool added here can
change files the moment a model calls it, so add only tools that read."""

READ_ONLY_COMMANDS = (
    "ls",
    "pwd",
    "cat",
    "head",
    "tail",
    "wc",
    "git status",
    "git diff",
    "git log",
)
"""The `bash` commands plan mode lets run: a command that is one of these, or starts with one
followed by a space, and has no shell syntax. Each one added lets the model run it with any
arguments but `REFUSED_ARGUMENTS`."""

REFUSED_ARGUMENTS = ("--output", "--ext-diff")
"""Arguments that make a read-only command write or run something: `git diff --output=f` and
`git log --output f` write `f`, and `git log --ext-diff` runs the diff program the repository
configures. A command with a word equal to one of these, or starting with one and `=`, is
refused."""

BRIEF = (
    "PLAN MODE. Research with the read-only tools, then reply with a step-by-step plan and "
    "make no changes: write, edit and shell commands are refused until the person approves."
)
"""What the model is told once when plan mode begins. A change reaches the model from the next
stay in plan mode."""

APPROVED = "The plan is approved. Carry it out."
"""What the model is told when the person approves the plan."""
