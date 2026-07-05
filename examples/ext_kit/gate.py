"""``ext_kit.gate`` — the *gate* atom.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §4 S55.

A *gate* is an external check whose verdict steers the conversation: run a
command (a test suite, a linter, a type-checker, a custom script), decide pass /
fail from its exit code or a regex over its output, and turn that verdict into a
**durable** node on the session path so the model — and a reloaded session —
sees exactly what the gate decided. This module is those three moves, composed on
τ's public surface only (it is part of the extension-side kit, **not** the
harness):

* :func:`run_gate` — run one gate command and return a :class:`GateResult`; the
  verdict is exit-code-based by default, or comes from a ``parse`` (a regex, or a
  predicate over the combined output).
* :func:`verdict_node` — render a :class:`GateResult` into the
  ``{customType, content, display, details}`` message an extension hands to
  ``api.send_message`` to append a durable ``customMessage`` verdict block to the
  active path (persisted == rendered; display-only by default, D-E6-1).
* :func:`revert_and_recheck` — the **anti-cheat** helper: stash the working-tree
  changes to a set of ``paths`` (reverting them to ``HEAD``), re-run the gate,
  then restore the stash. If the gate passed *with* the agent's edits but fails
  *without* them, those edits were load-bearing — the classic "the agent made the
  tests pass by editing the tests" tell.

**Fail-Early.** :func:`run_gate` never fabricates a verdict: an unparseable
``parse`` argument raises (``TypeError``), a timeout yields an honest
``verdict="timeout"`` / ``passed=False`` (not a silent pass), and a command that
cannot even be launched (``FileNotFoundError`` from the exec) propagates.
:func:`revert_and_recheck` refuses to run outside a git work tree and raises
loudly if the post-recheck ``git stash pop`` fails rather than leaving the tree
silently reverted — a broken restore is a real error a supervisor must see.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeAlias

#: How :func:`run_gate` decides pass / fail from a run:
#: ``None`` → exit code ``0`` passes; a ``str`` / compiled regex → a *search*
#: match over the combined output passes (and its text becomes ``matched``); a
#: predicate → its ``bool`` return over the combined output is the verdict.
GateParse: TypeAlias = "str | re.Pattern[str] | Callable[[str], bool]"

#: Canonical verdict labels (the ``verdict`` field of :class:`GateResult`).
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_TIMEOUT = "timeout"


# ── result carrier ───────────────────────────────────────────────────────────


@dataclass
class GateResult:
    """The outcome of one :func:`run_gate` invocation.

    ``passed`` is the verdict; ``verdict`` is its canonical label
    (:data:`VERDICT_PASS` / :data:`VERDICT_FAIL` / :data:`VERDICT_TIMEOUT`).
    ``exit_code`` is the process return code, or ``None`` when the gate did not
    exit normally (it was killed on ``timeout``). ``matched`` carries the regex
    match text when ``parse`` was a pattern (group 1 if the pattern has a group,
    else the whole match), and is ``None`` otherwise.
    """

    cmd: str
    passed: bool
    verdict: str
    exit_code: int | None
    stdout: str
    stderr: str
    matched: str | None = None
    timed_out: bool = False

    def output(self) -> str:
        """The combined ``stdout`` + ``stderr`` — the text ``parse`` sees."""
        return _combine(self.stdout, self.stderr)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cmd": self.cmd,
            "passed": self.passed,
            "verdict": self.verdict,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "matched": self.matched,
            "timed_out": self.timed_out,
        }


@dataclass
class RecheckResult:
    """The outcome of :func:`revert_and_recheck`.

    ``result`` is the gate verdict computed *with ``paths`` reverted to ``HEAD``*;
    ``reverted_paths`` is the subset of ``paths`` that actually had working-tree
    changes to stash (empty when the agent never touched them, in which case the
    recheck ran against the current tree and equals a plain :func:`run_gate`).
    """

    result: GateResult
    reverted_paths: list[str] = field(default_factory=list)

    def cheated(self, baseline: GateResult) -> bool:
        """``True`` iff the gate passed *with* the agent's edits (``baseline``)
        but fails *without* them (this recheck) — i.e. the edits to ``paths``
        were load-bearing for the pass (the anti-cheat tell). When nothing was
        reverted this is always ``False`` (there was no edit to be load-bearing).
        """
        if not self.reverted_paths:
            return False
        return baseline.passed and not self.result.passed


# ── output helpers ───────────────────────────────────────────────────────────


def _combine(stdout: str, stderr: str) -> str:
    """Join stdout and stderr into the single text a ``parse`` inspects."""
    if stdout and stderr:
        return f"{stdout}\n{stderr}"
    return stdout or stderr


def _evaluate(
    parse: GateParse | None,
    *,
    exit_code: int | None,
    output: str,
) -> tuple[bool, str | None]:
    """Apply the ``parse`` policy to a finished run → ``(passed, matched)``.

    * ``None`` — exit-code semantics: ``exit_code == 0`` passes.
    * ``str`` / ``re.Pattern`` — a regex *search* over ``output``; a match passes
      and its text (group 1 if present, else group 0) is returned as ``matched``.
    * predicate — ``bool(parse(output))`` is the verdict.

    Fail-Early: any other ``parse`` type raises ``TypeError`` rather than being
    coerced into a fabricated verdict.
    """
    if parse is None:
        return exit_code == 0, None
    if isinstance(parse, (str, re.Pattern)):
        pattern = re.compile(parse) if isinstance(parse, str) else parse
        match = pattern.search(output)
        if match is None:
            return False, None
        matched = match.group(1) if match.groups() else match.group(0)
        return True, matched
    if callable(parse):
        return bool(parse(output)), None
    raise TypeError(
        "run_gate: `parse` must be None, a regex (str | re.Pattern), or a "
        f"callable[[str], bool]; got {type(parse).__name__}"
    )


def _display_cmd(cmd: str | Sequence[str]) -> str:
    """A stable display string for either a shell string or an argv sequence."""
    return cmd if isinstance(cmd, str) else shlex.join(cmd)


# ── the gate runner ──────────────────────────────────────────────────────────


async def run_gate(
    cmd: str | Sequence[str],
    *,
    parse: GateParse | None = None,
    cwd: str | None = None,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
) -> GateResult:
    """Run one gate command to completion and return its :class:`GateResult`.

    ``cmd`` is a shell string (run via ``/bin/sh -c``) or an argv sequence (run
    directly, no shell). Both stdout and stderr are captured; the verdict is
    decided by ``parse`` (see :data:`GateParse` / :func:`_evaluate`) — exit-code
    ``0`` by default. ``cwd`` sets the working directory; ``env`` is *merged over*
    the current environment (so ``PATH`` survives) rather than replacing it.

    ``timeout`` (seconds) bounds the run: on expiry the child is killed and the
    result is an honest ``verdict="timeout"`` / ``passed=False`` / ``exit_code
    None`` — a gate that did not finish did not pass (Fail-Early, no silent pass).
    A command that cannot be launched at all raises (e.g. ``FileNotFoundError``).
    """
    merged_env = {**os.environ, **env} if env is not None else None
    if isinstance(cmd, str):
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            env=merged_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=merged_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    display = _display_cmd(cmd)
    try:
        out_bytes, err_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        out_bytes, err_bytes = await proc.communicate()
        return GateResult(
            cmd=display,
            passed=False,
            verdict=VERDICT_TIMEOUT,
            exit_code=None,
            stdout=out_bytes.decode(errors="replace"),
            stderr=err_bytes.decode(errors="replace"),
            matched=None,
            timed_out=True,
        )

    stdout = out_bytes.decode(errors="replace")
    stderr = err_bytes.decode(errors="replace")
    exit_code = proc.returncode
    passed, matched = _evaluate(parse, exit_code=exit_code, output=_combine(stdout, stderr))
    return GateResult(
        cmd=display,
        passed=passed,
        verdict=VERDICT_PASS if passed else VERDICT_FAIL,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        matched=matched,
    )


# ── verdict → durable node ───────────────────────────────────────────────────

#: The default ``customType`` for a gate verdict node.
DEFAULT_VERDICT_TYPE = "gate_verdict"


def verdict_node(
    result: GateResult,
    *,
    custom_type: str = DEFAULT_VERDICT_TYPE,
    label: str | None = None,
    display: bool = True,
) -> dict[str, Any]:
    """Render a :class:`GateResult` into an ``api.send_message`` payload.

    Returns the ``{customType, content, display, details}`` dict an extension
    passes to ``api.send_message(...)`` to append a durable ``customMessage``
    node — the gate's verdict as a text block on the active path, persisted and
    rendered exactly as shown (display-only by default per D-E6-1; pass
    ``options={"visible_to_model": True}`` to ``send_message`` to also feed it to
    the model). ``label`` names the gate in the headline (e.g. ``"tests"``); it
    defaults to the command. The full :meth:`GateResult.to_dict` rides in
    ``details`` so the structured verdict survives on the node.

    This helper is pure — it builds the message, it does not append it — so it is
    unit-testable without a session (the extension owns the actual append).
    """
    name = label or result.cmd
    mark = "PASS" if result.passed else result.verdict.upper()
    headline = f"Gate [{name}]: {mark}"
    detail_bits = []
    if result.exit_code is not None:
        detail_bits.append(f"exit={result.exit_code}")
    if result.matched is not None:
        detail_bits.append(f"matched={result.matched!r}")
    if result.timed_out:
        detail_bits.append("timed out")
    content = headline if not detail_bits else f"{headline} ({', '.join(detail_bits)})"
    return {
        "customType": custom_type,
        "content": content,
        "display": display,
        "details": result.to_dict(),
    }


# ── anti-cheat: revert the agent's edits and re-run the gate ─────────────────


async def _git(args: Sequence[str], *, cwd: str | None) -> tuple[int, str, str]:
    """Run one ``git`` subcommand → ``(exit_code, stdout, stderr)``."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_bytes, err_bytes = await proc.communicate()
    code = proc.returncode if proc.returncode is not None else -1
    return code, out_bytes.decode(errors="replace"), err_bytes.decode(errors="replace")


def _changed_paths(porcelain: str) -> list[str]:
    """Parse the paths out of ``git status --porcelain`` output.

    Each line is ``XY <path>`` (or ``XY <old> -> <new>`` for a rename); we take
    the final path token so a stash of exactly these paths is a no-op check.
    """
    changed: list[str] = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        rest = line[3:] if len(line) > 3 else line
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        changed.append(rest.strip())
    return changed


async def revert_and_recheck(
    paths: str | Sequence[str],
    gate: Callable[[], Awaitable[GateResult]],
    *,
    cwd: str | None = None,
) -> RecheckResult:
    """Re-run ``gate`` with the working-tree changes to ``paths`` reverted.

    The anti-cheat move: ``git stash push`` the changes to ``paths`` (reverting
    them to ``HEAD``), run ``gate`` — a zero-arg coroutine factory, typically
    ``lambda: run_gate(...)`` — against the reverted tree, then ``git stash pop``
    to restore the agent's edits. Compare the returned :attr:`RecheckResult.result`
    to the baseline you ran before: if the baseline passed and this recheck fails,
    the edits to ``paths`` were load-bearing for the pass
    (:meth:`RecheckResult.cheated`).

    When ``paths`` have no uncommitted changes there is nothing to revert: the
    recheck runs against the current tree and ``reverted_paths`` is empty (an
    honest "no edits to isolate", not a fabricated verdict).

    Fail-Early: raises ``RuntimeError`` if ``cwd`` is not inside a git work tree,
    if the stash push fails, or — critically — if the post-recheck ``git stash
    pop`` fails, since that would silently leave the tree reverted. The stash is
    always attempted-restored even if ``gate`` raises.
    """
    path_list = [paths] if isinstance(paths, str) else list(paths)
    if not path_list:
        raise ValueError("revert_and_recheck: `paths` is empty — nothing to revert")

    inside_code, inside_out, _ = await _git(["rev-parse", "--is-inside-work-tree"], cwd=cwd)
    if inside_code != 0 or inside_out.strip() != "true":
        raise RuntimeError(
            f"revert_and_recheck: {cwd or os.getcwd()!r} is not inside a git work tree "
            "(cannot stash the agent's edits to isolate the gate)"
        )

    status_code, status_out, status_err = await _git(
        ["status", "--porcelain", "--", *path_list], cwd=cwd
    )
    if status_code != 0:
        raise RuntimeError(f"revert_and_recheck: `git status` failed: {status_err.strip()}")
    reverted = _changed_paths(status_out)

    stashed = False
    if reverted:
        push_code, _, push_err = await _git(
            ["stash", "push", "--include-untracked", "--", *path_list], cwd=cwd
        )
        if push_code != 0:
            raise RuntimeError(
                f"revert_and_recheck: `git stash push` failed, tree untouched: {push_err.strip()}"
            )
        stashed = True

    try:
        result = await gate()
    finally:
        if stashed:
            pop_code, _, pop_err = await _git(["stash", "pop"], cwd=cwd)
            if pop_code != 0:
                raise RuntimeError(
                    "revert_and_recheck: `git stash pop` failed — the agent's edits to "
                    f"{path_list} are still stashed; restore them manually: {pop_err.strip()}"
                )

    return RecheckResult(result=result, reverted_paths=reverted)
