"""Example 54: Consequence engine — /what-if carries a change to what it breaks (E11, S75).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §7 S75. No pi original — pi has neither
``ext_kit`` nor a tree-shaped backplane; this is the τ-native *bespoke* E11 demo.
The dial it turns is the last of the catalog's four: **composition generated per
task** — a bounded *composer-lite*. Where ``50_review_swarm`` fans out a STATIC set
of lenses, ``/what-if`` first asks a composer child to *derive*, from the specific
change, the bounded set of downstream consequences worth testing; the swarm shape is
generated per task, not hardcoded.

## What ``/what-if <change>`` does — spawn (worktree cwd) + gate + S46 report

``/what-if <change>`` takes a natural-language hypothesis ("drop the retry limit to
0", "make ``/admin`` public") and answers *what would break*, composing two
``ext_kit`` atoms plus a git-worktree isolation the demo owns:

1. **Compose** (:func:`compose_hypotheses`, the composer-lite) — one isolated,
   read-only ``tau`` child (:func:`ext_kit.spawn.spawn_tau`, S53) reads the repo and
   the change and proposes a **bounded** JSON list of consequences, each paired with
   a ``check`` command that PASSES iff that consequence still holds. The list is
   sliced to ``max_hypotheses`` (Fail-Early bounding regardless of what the child
   returns) — the "bounded" in bounded composer-lite.
2. **Carry, in a worktree** (:func:`score_hypothesis`) — each consequence gets its
   OWN git worktree (:func:`add_worktree`: ``git worktree add --detach``), a throwaway
   checkout of ``HEAD``. A ``tau`` carrier child is spawned with that worktree as its
   ``cwd`` and write tools, told to carry the change to its consequences *there* — so
   the user's tree is never touched. The worktree is always removed afterward
   (:func:`remove_worktree`), even on error.
3. **Gate scores the survivor** (:func:`ext_kit.gate.run_gate`, S55) — the
   consequence's ``check`` command runs in the mutated worktree. A carrier that
   *carried* the change is a survivor; the gate then scores it: a failing check names
   a BROKEN consequence, a passing one a consequence that HELD. A carrier that failed
   (or a check that timed out) is left UNSCORED — never fabricated into a pass/fail.
4. **Report + durable record** (S46 + S56) — :func:`consequence_report` names what
   breaks (the command-output channel), and the run is recorded as ONE durable
   ``customEntry`` node (:class:`ext_kit.state.TreeStore`) so ``/consequences`` can
   list every what-if this session asked, reload-invariant.

## The invariant (tree-as-truth)

The only DURABLE, path-affecting write is :meth:`TreeStore.append` — one append-only
``customEntry`` per ``/what-if`` run. Everything else is EPHEMERAL, off-tree state: the
composed hypotheses, the throwaway worktrees, the per-check verdicts. The worktrees are
external OS state the demo creates and tears down (like ``50``'s live panel); they are
never persisted, never model-visible, and never rewrite a prior node. Because
``customEntry`` is excluded from ``convert_to_llm``, the consequence ledger is
backplane state: durable, rendered in the tree, reload-invariant, but never model input
— no hidden side-channel.

## Fail-Early

An empty ``<change>`` returns usage, never an invented hypothesis. A composer that
proposes nothing usable reports "0 consequences" honestly. ``add_worktree`` RAISES
outside a git work tree (it cannot isolate a change it has nowhere to carry). A carrier
that fails leaves its consequence UNSCORED (``broke=None``), and a check that times out
is UNSCORED too (a gate that did not finish did not score) — neither is fabricated into
a verdict. A failed ``git worktree remove`` RAISES rather than silently leaking a
worktree a supervisor should see.

## Usage

    tau -e examples/54_consequence_engine.py
    > /what-if drop the retry limit to zero
    What-if: "drop the retry limit to zero"
    Composed 3 consequence(s); 3 worktree(s) scored.
      BREAKS  callers relying on at-least-one retry   (check: pytest tests/test_retry.py -> fail)
      holds   the config schema still validates        (check: python -m app.config --check -> pass)
      ?       the metrics dashboard query               (carrier max_turns — unscored)
    Summary: 1 consequence(s) break.
    > /consequences
    "drop the retry limit to zero" — 1 break(s), 1 hold(s), 1 unscored
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ``ext_kit`` lives alongside the numbered examples, not inside an installed package —
# bootstrap ``examples/`` onto the path before importing it, whether run directly,
# imported, or loaded via ``-e`` (D-E6-3), the same as the other ext_kit-using demos.
_EXAMPLES_DIR = str(Path(__file__).resolve().parent)
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

from ext_kit import gate, spawn  # noqa: E402  (path insertion must precede the import)
from ext_kit.state import TreeStore  # noqa: E402

#: This extension's own file stem — the ``api.config`` slice key (S40).
EXTENSION_STEM = "54_consequence_engine"

#: The ``customEntry`` type each ``/what-if`` run is recorded under (S39/S56). One
#: durable node per run; ``/consequences`` lists them along the active path.
REPORT_CUSTOM_TYPE = "consequence_report"

#: Read-only tools the composer child gets — it inspects the repo to derive the
#: consequences, it never mutates it (the same isolation ``50``'s lenses enforce).
COMPOSER_TOOLS: tuple[str, ...] = ("read", "ls", "grep", "find")

#: Tools the carrier child gets — it carries the change *in its own worktree*, so it
#: needs to write; the isolation is the throwaway worktree, not a read-only guard.
CARRY_TOOLS: tuple[str, ...] = ("read", "write", "edit", "ls", "grep", "find")

#: Default cap on composed consequences (the "bounded" in bounded composer-lite).
DEFAULT_MAX_HYPOTHESES = 4

#: Default per-child wall-clock cap (seconds) for the composer / carrier children.
DEFAULT_TIMEOUT = 180.0


# ── config resolution (extension supplies its own defaults; harness never does) ──


def _config_int(config: dict[str, Any], key: str, default: int) -> int:
    """Read a positive int from ``api.config`` (S40), else the demo's own default.

    Fail-Early: a present-but-non-positive / non-int value RAISES rather than being
    silently coerced — an unconfigured extension reads ``{}`` and gets ``default``,
    but a configured typo must surface.
    """
    if key not in config:
        return default
    value = config[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(
            f"consequence_engine: config '{key}' must be a positive integer, got {value!r}"
        )
    return value


def _config_float(config: dict[str, Any], key: str, default: float) -> float:
    """Read a positive float (seconds) from ``api.config`` (S40), else ``default``."""
    if key not in config:
        return default
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(
            f"consequence_engine: config '{key}' must be a positive number, got {value!r}"
        )
    return float(value)


def _config_model(config: dict[str, Any]) -> str | None:
    """Read the optional child ``model`` id (a string) from ``api.config``.

    ``None`` (unset) lets each child use τ's default model. Fail-Early: a present but
    non-string value is a config mistake that RAISES rather than being coerced.
    """
    model = config.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError(f"consequence_engine: config 'model' must be a string, got {model!r}")
    return model


# ── the composed hypothesis (composition generated per task) ─────────────────


@dataclass(frozen=True)
class Hypothesis:
    """One consequence the composer derived from the change.

    :attr:`consequence` is the one-line downstream effect the change might break;
    :attr:`check` is the shell command whose PASS (exit 0) means that consequence
    still holds and whose FAIL means it broke — the verdict the gate scores.
    """

    consequence: str
    check: str


def _extract_json_array(text: str) -> str | None:
    """The JSON array substring in ``text`` — a ```json fenced block if present, else
    the first balanced ``[ … ]`` span. ``None`` when there is no array.
    """
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence is not None:
        return fence.group(1)
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_hypotheses(text: str) -> list[Hypothesis]:
    """Parse the composer child's transcript into consequences (pure, testable).

    Extracts the JSON array (:func:`_extract_json_array`) and keeps only well-formed
    items — a non-empty ``consequence`` AND a non-empty ``check`` (a consequence with
    no way to score it is dropped, never fabricated into a scorable one). A transcript
    with no parseable array yields ``[]`` — an honest "the composer proposed nothing
    usable", never an invented hypothesis.
    """
    span = _extract_json_array(text)
    if span is None:
        return []
    try:
        items = json.loads(span)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    out: list[Hypothesis] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        consequence = str(item.get("consequence", "")).strip()
        check = str(item.get("check", "")).strip()
        if not consequence or not check:
            continue
        out.append(Hypothesis(consequence=consequence, check=check))
    return out


def _compose_prompt(change: str, max_n: int) -> str:
    """The prompt the composer child derives the consequences under (per task)."""
    return (
        "You are a change-impact analyst. A developer is considering the change below.\n"
        "Read the repository and derive the concrete DOWNSTREAM CONSEQUENCES this change "
        "would have — the specific things elsewhere that could BREAK if it were carried "
        f"through. Propose at MOST {max_n}, ranked most-likely-to-break first.\n\n"
        "For each consequence, give a shell ``check`` command that, run at the repo root, "
        "EXITS 0 iff that consequence still holds and NON-ZERO iff it broke (e.g. a "
        "targeted test, an import, a type-check, a grep for a now-invalid pattern).\n\n"
        "Respond with a single JSON array (and nothing else), each item:\n"
        '  {"consequence": "<one line>", "check": "<shell command>"}\n'
        "If the change has no testable consequences, respond with []\n\n"
        f"--- CHANGE ---\n{change}\n"
    )


def _carry_prompt(change: str, hyp: Hypothesis) -> str:
    """The prompt the carrier child gets: carry the change through, in this checkout."""
    return (
        "You are working in a throwaway, isolated checkout of this repository. Apply the "
        "following change and follow it THROUGH to its consequences: update everything "
        "that must change as a result, so the tree reflects the change fully carried.\n\n"
        f"--- CHANGE ---\n{change}\n\n"
        "In particular, make sure this downstream area is brought into a consistent "
        f"state with the change:\n  {hyp.consequence}\n\n"
        "Edit files directly. When the change is fully carried, stop."
    )


# ── the scored consequence ───────────────────────────────────────────────────


@dataclass
class ConsequenceResult:
    """The outcome of carrying the change and scoring one consequence.

    ``carried`` is whether the carrier child completed (a survivor of the carry).
    ``broke`` is the gate's score of a survivor: ``True`` (the check failed — the
    consequence broke), ``False`` (the check passed — it held), or ``None`` (UNSCORED
    — the carrier failed, or the check timed out; never fabricated into a verdict).
    ``verdict`` is a stable label for the report and the durable record.
    """

    consequence: str
    check: str
    carried: bool
    broke: bool | None
    verdict: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "consequence": self.consequence,
            "check": self.check,
            "carried": self.carried,
            "broke": self.broke,
            "verdict": self.verdict,
            "detail": self.detail,
        }


# ── git worktree isolation (the demo owns this; not an ext_kit atom) ─────────


async def _git(args: Sequence[str], *, cwd: str) -> tuple[int, str, str]:
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


async def add_worktree(repo_cwd: str, slug: str) -> str:
    """Create a throwaway detached worktree of ``HEAD`` and return its path.

    ``git worktree add --detach <path> HEAD`` gives each hypothesis its own isolated
    checkout — a real working tree sharing the repo's object store, so a carrier can
    mutate it freely without touching the user's tree. The worktree lands under a fresh
    temp base dir (outside the repo, so it never shows up in the repo's own status).

    Fail-Early: RAISES ``RuntimeError`` (carrying git's stderr) when ``repo_cwd`` is
    not a git work tree or the add fails — the engine cannot isolate a change it has
    nowhere to carry, so it does not silently fall back to the live tree.
    """
    base = tempfile.mkdtemp(prefix="tau-what-if-")
    path = os.path.join(base, slug)
    code, _out, err = await _git(["worktree", "add", "--detach", path, "HEAD"], cwd=repo_cwd)
    if code != 0:
        shutil.rmtree(base, ignore_errors=True)
        raise RuntimeError(
            f"consequence_engine: `git worktree add` failed in {repo_cwd!r} "
            f"(exit {code}): {err.strip()}"
        )
    return path


async def remove_worktree(repo_cwd: str, path: str) -> None:
    """Tear a worktree created by :func:`add_worktree` down, temp base and all.

    ``git worktree remove --force <path>`` drops the checkout and its administrative
    metadata even with uncommitted carrier edits, then the temp base dir is removed.

    Fail-Early: RAISES ``RuntimeError`` if the ``git worktree remove`` fails — a leaked
    worktree is a real fault a supervisor must see, not something to swallow. The temp
    base dir is still cleaned up first so the failure names only the git-side leak.
    """
    code, _out, err = await _git(["worktree", "remove", "--force", path], cwd=repo_cwd)
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    if code != 0:
        raise RuntimeError(
            f"consequence_engine: `git worktree remove {path}` failed (exit {code}): "
            f"{err.strip()} — the worktree may be leaked; run `git worktree prune`"
        )


def _slug(index: int, consequence: str) -> str:
    """A filesystem-safe worktree leaf name for a consequence."""
    stub = re.sub(r"[^a-z0-9]+", "-", consequence.lower()).strip("-")[:32]
    return f"wt-{index}-{stub}" if stub else f"wt-{index}"


# ── the composer + the carry/score of one hypothesis ─────────────────────────


async def compose_hypotheses(
    change: str,
    *,
    model: str | None,
    cwd: str,
    timeout: float,
    signal: Any | None,
    max_n: int,
) -> list[Hypothesis]:
    """Derive the bounded consequence set for ``change`` (the composer-lite).

    Spawns one isolated, read-only ``tau`` child to read the repo and propose the
    downstream consequences, parses its JSON transcript (:func:`parse_hypotheses`), and
    slices the result to ``max_n`` — the bounding is enforced HERE, not trusted to the
    child (Fail-Early: a child that over-proposes is capped, never let past the bound).
    A composer that fails or emits nothing usable yields ``[]``.
    """
    result = await spawn.spawn_tau(
        _compose_prompt(change, max_n),
        model=model,
        tools=list(COMPOSER_TOOLS),
        cwd=cwd,
        limits=spawn.SpawnLimits(max_seconds=timeout),
        signal=signal,
    )
    if result.failed:
        return []
    return parse_hypotheses(result.final_output)[:max_n]


async def score_hypothesis(
    change: str,
    hyp: Hypothesis,
    index: int,
    *,
    model: str | None,
    repo_cwd: str,
    timeout: float,
    signal: Any | None,
) -> ConsequenceResult:
    """Carry ``change`` into a fresh worktree and score ``hyp``'s consequence.

    Spawns a carrier child (write tools) with a throwaway worktree as its ``cwd`` to
    carry the change to its consequences there, then runs ``hyp.check`` in that mutated
    worktree through :func:`ext_kit.gate.run_gate`. The worktree is ALWAYS removed
    afterward (a ``finally``), even if the carry or the gate raises.

    Scoring (Fail-Early, never a fabricated verdict):

    * carrier failed → ``carried=False``, ``broke=None`` — UNSCORED (``verdict`` names
      the carrier's stop reason);
    * check timed out → ``broke=None`` — UNSCORED (a gate that did not finish did not
      score);
    * check failed → ``broke=True`` — the consequence BROKE;
    * check passed → ``broke=False`` — the consequence HELD.
    """
    worktree = await add_worktree(repo_cwd, _slug(index, hyp.consequence))
    try:
        carrier = await spawn.spawn_tau(
            _carry_prompt(change, hyp),
            model=model,
            tools=list(CARRY_TOOLS),
            cwd=worktree,
            limits=spawn.SpawnLimits(max_seconds=timeout),
            signal=signal,
        )
        if carrier.failed:
            return ConsequenceResult(
                consequence=hyp.consequence,
                check=hyp.check,
                carried=False,
                broke=None,
                verdict=f"carrier {carrier.stop_reason}",
                detail="the change could not be carried, so the consequence is unscored",
            )

        verdict = await gate.run_gate(hyp.check, cwd=worktree, timeout=timeout)
        if verdict.timed_out:
            return ConsequenceResult(
                consequence=hyp.consequence,
                check=hyp.check,
                carried=True,
                broke=None,
                verdict="check timeout",
                detail="the check did not finish, so the consequence is unscored",
            )
        broke = not verdict.passed
        return ConsequenceResult(
            consequence=hyp.consequence,
            check=hyp.check,
            carried=True,
            broke=broke,
            verdict="breaks" if broke else "holds",
            detail=f"exit={verdict.exit_code}",
        )
    finally:
        await remove_worktree(repo_cwd, worktree)


# ── the engine driver ─────────────────────────────────────────────────────────


async def run_engine(
    change: str,
    *,
    model: str | None,
    cwd: str,
    timeout: float,
    signal: Any | None,
    max_n: int,
) -> dict[str, Any]:
    """Compose, carry-and-score each consequence, and return the run outcome.

    Returns ``{change, results}`` where ``results`` is the ordered list of
    :meth:`ConsequenceResult.to_dict` — one per composed consequence. The worktrees
    are carried and scored SEQUENTIALLY: each hypothesis's carry mutates a heavy git
    worktree it then tears down, so the engine keeps at most one live at a time and
    guarantees a clean cleanup per consequence.
    """
    hypotheses = await compose_hypotheses(
        change, model=model, cwd=cwd, timeout=timeout, signal=signal, max_n=max_n
    )
    results: list[dict[str, Any]] = []
    for i, hyp in enumerate(hypotheses):
        scored = await score_hypothesis(
            change, hyp, i, model=model, repo_cwd=cwd, timeout=timeout, signal=signal
        )
        results.append(scored.to_dict())
    return {"change": change, "results": results}


# ── rendering (the S46 report + the durable record listing, both pure) ───────


def _counts(results: list[dict[str, Any]]) -> tuple[int, int, int]:
    """``(breaks, holds, unscored)`` counts over a run's consequence results."""
    breaks = sum(1 for r in results if r.get("broke") is True)
    holds = sum(1 for r in results if r.get("broke") is False)
    unscored = sum(1 for r in results if r.get("broke") is None)
    return breaks, holds, unscored


def consequence_report(outcome: dict[str, Any]) -> str:
    """The S46 text report for ``/what-if`` — names what breaks (command-output channel)."""
    change = outcome["change"]
    results = outcome["results"]
    if not results:
        return (
            f'What-if: "{change}"\n'
            "Composed 0 consequence(s) — nothing testable to carry (the composer "
            "proposed no scorable consequence)."
        )
    breaks, _holds, _unscored = _counts(results)
    lines = [
        f'What-if: "{change}"',
        f"Composed {len(results)} consequence(s); {len(results)} worktree(s) scored.",
    ]
    for r in results:
        if r["broke"] is True:
            mark, tail = "BREAKS", f"(check: {r['check']} -> fail)"
        elif r["broke"] is False:
            mark, tail = "holds ", f"(check: {r['check']} -> pass)"
        else:
            mark, tail = "?     ", f"({r['verdict']} — unscored)"
        lines.append(f"  {mark}  {r['consequence']}   {tail}")
    lines.append(f"Summary: {breaks} consequence(s) break.")
    return "\n".join(lines)


def _record_line(record: dict[str, Any]) -> str:
    """One ``/consequences`` listing line for a stored what-if record."""
    breaks, holds, unscored = _counts(record.get("results", []))
    return f'"{record.get("change", "")}" — {breaks} break(s), {holds} hold(s), {unscored} unscored'


# ── command handlers ─────────────────────────────────────────────────────────


async def _what_if_command(
    args: str,
    ctx: Any,
    *,
    store: TreeStore[dict[str, Any]],
    config: dict[str, Any],
) -> str:
    """``/what-if <change>``: run the engine, record the run, name what breaks."""
    change = args.strip()
    if not change:
        return "usage: /what-if <change> — describe the change to carry to its consequences"

    cwd = getattr(ctx, "cwd", ".") or "."
    model = _config_model(config)
    timeout = _config_float(config, "timeout", DEFAULT_TIMEOUT)
    max_n = _config_int(config, "max_hypotheses", DEFAULT_MAX_HYPOTHESES)

    outcome = await run_engine(
        change,
        model=model,
        cwd=cwd,
        timeout=timeout,
        signal=getattr(ctx, "signal", None),
        max_n=max_n,
    )
    # One durable customEntry per run — the consequence ledger (backplane state,
    # excluded from convert_to_llm; persisted == rendered, reload-invariant).
    store.append({"change": outcome["change"], "results": outcome["results"]})
    return consequence_report(outcome)


def _consequences_command(args: str, ctx: Any, *, store: TreeStore[dict[str, Any]]) -> str:
    """``/consequences``: list every what-if this session asked (S46; reload-safe read)."""
    records = store.load()
    if not records:
        return "No what-ifs run yet. Ask one with /what-if <change>."
    return "\n".join(_record_line(r) for r in records)


# ── extension entry point ────────────────────────────────────────────────────


def consequence_engine_extension(api: Any) -> None:
    """Register ``/what-if`` and ``/consequences`` (the consequence engine, S75)."""
    store: TreeStore[dict[str, Any]] = TreeStore(api, REPORT_CUSTOM_TYPE)
    config = api.config

    async def what_if_handler(args: str, ctx: Any) -> str:
        return await _what_if_command(args, ctx, store=store, config=config)

    def consequences_handler(args: str, ctx: Any) -> str:
        return _consequences_command(args, ctx, store=store)

    api.register_command(
        "what-if",
        {
            "description": "Carry a change to its consequences in worktrees (usage: /what-if <change>)",
            "handler": what_if_handler,
        },
    )
    api.register_command(
        "consequences",
        {
            "description": "List every what-if run this session (reload-safe)",
            "handler": consequences_handler,
        },
    )


#: The module-level ``register`` the file-path loader looks up (``tau -e
#: examples/54_consequence_engine.py`` → ``getattr(module, "register")``).
register = consequence_engine_extension
