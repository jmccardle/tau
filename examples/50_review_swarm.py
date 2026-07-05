"""Example 50: Review swarm — a fan-out diff review with adversarial re-check (E11, S71).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §7 S71. No pi original — pi has no
tree-shaped backplane and no ``ext_kit``; this is the τ-native *static baseline*
the other E11 demos build on (``52_red_team_memory`` seeds this swarm's
adversaries from a cross-session corpus; the dial S71 itself turns is "none").

## What this shows — the four E8 atoms composed

``/review [ref]`` runs a full agentic review pipeline over the working diff,
composing four ``ext_kit`` primitives:

1. **Fan-out** (:class:`ext_kit.spawn.WorkerPool`, S53) — the diff against ``ref``
   (default ``HEAD``) is handed to one *isolated, read-only* ``tau`` child per
   review lens (:data:`LENSES`: security / performance / correctness). Each child
   gets its own context window and can only read the repo, never write it — the
   same isolation guarantee ``20_delegate``'s parallel mode enforces. The pool
   bounds concurrency.
2. **Dedupe** (pure) — the same defect flagged by two lenses (e.g. an unbounded
   loop is both a perf and a correctness finding) collapses to one record,
   merging the lenses that raised it (:func:`dedupe_findings`).
3. **Adversarial re-check** (:func:`ext_kit.gate.run_gate`, S55) — every survivor
   is put to a *skeptic* child spawned to REFUTE it. That adversary is an external
   check, so it runs through the gate atom: a predicate parse (:func:`_survives`)
   over the child's transcript decides ``GateResult.passed`` — a finding is kept
   only if the adversary could NOT refute it. A finding that cannot be re-confirmed
   (the adversary refuted it, timed out, or errored) is dropped as *inconclusive*
   — never fabricated into a survivor.
4. **Panel triage → durable node** (``ctx.ui.panel`` S68 + :class:`ext_kit.state.TreeStore`
   S56) — the survivors land in a keyed S68 panel table for triage. The human
   decides which to record (``/review_keep all`` or ``/review_keep 1 3 4``); only
   the kept findings become a durable ``customEntry`` node on the session tree.
   Because ``customEntry`` is excluded from ``convert_to_llm``, the findings ledger
   is backplane state: durable, rendered, reload-invariant, but never model input.
   ``/findings`` is the S46 listing of every kept finding.

## The invariant (tree-as-truth)

The only DURABLE, path-affecting write is :meth:`TreeStore.append` — an append-only
``customEntry`` per kept finding. The swarm's intermediate state (the diff, the raw
lens findings, the survivors awaiting triage) is EPHEMERAL triage state held in RAM
on the extension, exactly like the live contents of the S68 panel it renders: it is
never persisted, never model-visible, and never rewrites a prior node. Nothing here
is a hidden model-input side-channel — a kept finding is persisted == rendered, and
excluded from ``convert_to_llm`` by construction (it is a ``customEntry``, not a
message).

## Headless parity (§6.3 CLI rule)

Every surface has a JSON representation and a non-interactive policy: ``/review``
emits the triage as an ``{"type":"extension","kind":"panel",…}`` record on the
``--mode json`` stream (the panel is visible, its actions simply can't be pressed
without a TUI), and the triage decision is a plain command (``/review_keep all``)
that runs identically headless — so a child ``tau -p --mode json`` running this
extension stays orchestratable.

## Fail-Early

An empty diff reports "nothing to review" rather than inventing findings; a lens
child that fails or emits unparseable output contributes ZERO findings (with the
parse status surfaced), never a fabricated one; ``git diff`` failing (not a repo)
surfaces the git error; and an inconclusive adversarial re-check DROPS the finding
rather than keeping it unverified.

## Usage

    tau -e examples/50_review_swarm.py
    > (make some edits)
    > /review
    Review swarm: 3 lens(es), 4 raw finding(s) -> 2 survived re-check.
    #  lens                 sev     file:line          summary
    1  security             high    auth.py:42         SQL built by string concat
    2  correctness+perf     med     loader.py:88       unbounded retry loop
    > /review_keep 1
    Kept 1 finding(s) to the session findings ledger.
    > /findings
    [security] high  auth.py:42  SQL built by string concat
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

# ``ext_kit`` lives alongside the numbered examples, not inside an installed
# package — bootstrap ``examples/`` onto the path before importing it, whether run
# directly, imported, or loaded via ``-e`` (D-E6-3), the same as the other
# ext_kit-using demos (20_delegate, 41_bookmarks).
_EXAMPLES_DIR = str(Path(__file__).resolve().parent)
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

from ext_kit import gate, spawn  # noqa: E402  (path insertion must precede the import)
from ext_kit.state import TreeStore  # noqa: E402

#: The ``customEntry`` type kept findings live under (S39/S56). Cross-referenced by
#: ``52_red_team_memory`` (S73), which promotes survivors of this ledger into its
#: cross-session ``FileStore`` corpus.
FINDING_CUSTOM_TYPE = "review_finding"

#: The keyed S68 panel this demo mounts for triage (re-render / clear by this key).
PANEL_KEY = "review"

#: Read-only tool allowlist every swarm child gets — a reviewer inspects, it never
#: mutates the tree (the same isolation ``20_delegate`` parallel children enforce).
READONLY_TOOLS: tuple[str, ...] = ("read", "ls", "grep", "find")

#: The review lenses fanned out over the diff — name → the focus the child is told
#: to review for. One isolated child per lens (the fan-out atom).
LENSES: dict[str, str] = {
    "security": (
        "security vulnerabilities: injection, auth/authz gaps, unsafe deserialization, "
        "secrets in code, path traversal, unvalidated input reaching a sink"
    ),
    "performance": (
        "performance defects: unbounded loops, N+1 queries, needless O(n^2) work, "
        "blocking I/O on a hot path, repeated recomputation, unbounded memory growth"
    ),
    "correctness": (
        "correctness bugs: off-by-one, wrong conditionals, unhandled error/None cases, "
        "resource leaks, race conditions, contract violations between caller and callee"
    ),
}

#: Verdict sentinels the adversary is instructed to end on. The gate keeps a finding
#: iff SURVIVES is present and REFUTED is not (:func:`_survives`).
_VERDICT_SURVIVES = "VERDICT: SURVIVES"
_VERDICT_REFUTED = "VERDICT: REFUTED"


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
        raise ValueError(f"review_swarm: config '{key}' must be a positive integer, got {value!r}")
    return value


def _config_float(config: dict[str, Any], key: str, default: float) -> float:
    """Read a positive float (seconds) from ``api.config`` (S40), else ``default``."""
    if key not in config:
        return default
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"review_swarm: config '{key}' must be a positive number, got {value!r}")
    return float(value)


# ── the diff (Fail-Early: never fabricate a diff) ────────────────────────────


async def compute_diff(ref: str, cwd: str) -> str:
    """Return the working-tree diff against ``ref`` (``git diff <ref>``).

    Fail-Early: a failed ``git diff`` (not a repo, bad ref) RAISES ``RuntimeError``
    carrying git's own stderr — the swarm reviews a real diff or none, never a
    fabricated one. An empty diff returns ``""`` (an honest "no changes").
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        ref,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_bytes, err_bytes = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"review_swarm: `git diff {ref}` failed (exit {proc.returncode}): "
            f"{err_bytes.decode(errors='replace').strip()}"
        )
    return out_bytes.decode(errors="replace")


# ── lens fan-out → raw findings ──────────────────────────────────────────────


def _lens_prompt(lens: str, focus: str, diff: str) -> str:
    """The prompt one lens child reviews the diff under.

    Instructs a strict JSON-array output so :func:`parse_findings` can reconstruct
    the findings; the child is read-only and reviews only what the diff shows.
    """
    return (
        f"You are a code reviewer focused ONLY on {lens} issues — that is, {focus}.\n"
        "Review the unified diff below. Report ONLY genuine issues you can point to a "
        "specific changed line for; do not speculate about code you cannot see.\n\n"
        "Respond with a single JSON array (and nothing else) of findings, each:\n"
        '  {"file": "<path>", "line": <int or null>, "severity": "high|medium|low", '
        '"summary": "<one line>", "detail": "<why it is a problem>"}\n'
        "If there are no issues, respond with []\n\n"
        f"--- DIFF ---\n{diff}\n"
    )


def _coerce_line(raw: Any) -> int | None:
    """Normalize a reported ``line`` to ``int`` or ``None`` (never fabricate a line)."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def _extract_json_array(text: str) -> str | None:
    """The JSON array substring in ``text`` — a ```json fenced block if present,
    else the first balanced ``[ … ]`` span. ``None`` when there is no array.
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


def parse_findings(text: str, lens: str) -> list[dict[str, Any]]:
    """Parse a lens child's transcript into normalized findings (pure, testable).

    Extracts the JSON array (:func:`_extract_json_array`), keeps only well-formed
    items (a non-empty ``file`` and ``summary``), and stamps each with ``lens``.
    A transcript with no parseable array yields ``[]`` — an honest "this lens
    reported nothing usable", never a fabricated finding.
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
    findings: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        file = str(item.get("file", "")).strip()
        summary = str(item.get("summary", "")).strip()
        if not file or not summary:
            continue
        findings.append(
            {
                "lens": lens,
                "lenses": [lens],
                "file": file,
                "line": _coerce_line(item.get("line")),
                "severity": str(item.get("severity", "")).strip() or "unknown",
                "summary": summary,
                "detail": str(item.get("detail", "")).strip(),
            }
        )
    return findings


def finding_signature(finding: dict[str, Any]) -> tuple[str, int | None, str]:
    """Dedupe key for a finding: normalized ``(file, line, summary)`` — lens-agnostic,
    so the same defect raised by two lenses collapses to one record.
    """
    line = finding.get("line")
    return (
        str(finding.get("file", "")).strip().lower(),
        line if isinstance(line, int) else None,
        str(finding.get("summary", "")).strip().lower(),
    )


def dedupe_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate findings (pure), merging the lenses that raised each.

    First occurrence wins for the record body; a later duplicate only contributes
    its ``lens`` to the survivor's ``lenses`` list (so a defect both perf and
    correctness flagged shows ``lenses: [performance, correctness]``).
    """
    by_sig: dict[tuple[str, int | None, str], dict[str, Any]] = {}
    for finding in findings:
        sig = finding_signature(finding)
        existing = by_sig.get(sig)
        if existing is None:
            by_sig[sig] = dict(finding)
            by_sig[sig]["lenses"] = list(finding.get("lenses") or [finding["lens"]])
            continue
        for lens in finding.get("lenses") or [finding["lens"]]:
            if lens not in existing["lenses"]:
                existing["lenses"].append(lens)
    return list(by_sig.values())


# ── adversarial re-check (the gate atom) ─────────────────────────────────────


def _survives(output: str) -> bool:
    """Gate predicate: the finding SURVIVED the adversary's refutation attempt.

    Kept iff the adversary emitted the survival sentinel and NOT the refutation
    one. A transcript with neither (an off-script adversary) is treated as a
    refutation — Fail-Early conservative: an inconclusive re-check does not keep
    an unverified finding.
    """
    return _VERDICT_SURVIVES in output and _VERDICT_REFUTED not in output


def _adversary_prompt(finding: dict[str, Any], diff: str) -> str:
    """The prompt the skeptic child gets: try to REFUTE one finding against the diff."""
    where = finding["file"]
    if isinstance(finding.get("line"), int):
        where = f"{where}:{finding['line']}"
    return (
        "You are an adversarial reviewer. Another reviewer raised the finding below. "
        "Your job is to REFUTE it: read the diff (and the repo if needed) and decide "
        "whether the finding is a genuine, load-bearing issue or a false positive.\n\n"
        f"Finding [{'+'.join(finding['lenses'])}] {where}: {finding['summary']}\n"
        f"Detail: {finding.get('detail', '')}\n\n"
        "End your reply with EXACTLY one line, either:\n"
        f"  {_VERDICT_SURVIVES}   (you could not refute it — it is a real issue)\n"
        f"  {_VERDICT_REFUTED}    (it is a false positive or already handled)\n\n"
        f"--- DIFF ---\n{diff}\n"
    )


async def recheck_finding(
    finding: dict[str, Any],
    diff: str,
    *,
    model: str | None,
    cwd: str,
    timeout: float,
) -> gate.GateResult:
    """Run one survivor through the adversarial gate — spawn a skeptic, parse verdict.

    The adversary is an EXTERNAL check (an isolated, read-only ``tau`` child built
    with :func:`ext_kit.spawn.build_child_args` / :func:`ext_kit.spawn.tau_invocation`),
    so it runs through :func:`ext_kit.gate.run_gate` with the :func:`_survives`
    predicate: ``GateResult.passed`` is ``True`` iff the finding survived refutation.
    A timeout yields ``passed=False`` (Fail-Early — an unfinished re-check is not a
    pass), so the finding is dropped as inconclusive.
    """
    command, argv = spawn.tau_invocation(
        spawn.build_child_args(
            prompt=_adversary_prompt(finding, diff),
            model=model,
            tools=list(READONLY_TOOLS),
            system_prompt_path=None,
        )
    )
    return await gate.run_gate([command, *argv], parse=_survives, cwd=cwd, timeout=timeout)


# ── the swarm driver ─────────────────────────────────────────────────────────


async def run_swarm(
    diff: str,
    *,
    model: str | None,
    cwd: str,
    concurrency: int,
    child_timeout: float,
    signal: Any | None,
) -> dict[str, Any]:
    """Fan out the lenses, dedupe, adversarially re-check — return the swarm outcome.

    Returns ``{lens_count, raw, survivors, dropped, lens_status}`` where ``raw`` is
    the deduped finding count, ``survivors`` the findings that beat the adversary,
    and ``dropped`` those the re-check could not confirm. Pure orchestration over the
    spawn + gate atoms — the command handlers own the diff, the panel, and the store.
    """
    pool = spawn.WorkerPool(concurrency)
    lens_items = list(LENSES.items())

    async def _lens(item: tuple[str, str], _index: int) -> tuple[str, spawn.ChildResult]:
        lens, focus = item
        result = await spawn.spawn_tau(
            _lens_prompt(lens, focus, diff),
            model=model,
            tools=list(READONLY_TOOLS),
            cwd=cwd,
            limits=spawn.SpawnLimits(max_seconds=child_timeout),
            signal=signal,
        )
        return lens, result

    lens_results = await pool.map(_lens, lens_items)

    raw: list[dict[str, Any]] = []
    lens_status: list[dict[str, Any]] = []
    for lens, child in lens_results:
        if child.failed:
            lens_status.append(
                {"lens": lens, "status": f"failed ({child.stop_reason})", "count": 0}
            )
            continue
        found = parse_findings(child.final_output, lens)
        lens_status.append({"lens": lens, "status": "ok", "count": len(found)})
        raw.extend(found)

    deduped = dedupe_findings(raw)

    async def _gate(finding: dict[str, Any], _index: int) -> tuple[dict[str, Any], gate.GateResult]:
        verdict = await recheck_finding(finding, diff, model=model, cwd=cwd, timeout=child_timeout)
        return finding, verdict

    gate_results = await pool.map(_gate, deduped)
    survivors: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for finding, verdict in gate_results:
        (survivors if verdict.passed else dropped).append(finding)

    return {
        "lens_count": len(lens_items),
        "raw": len(deduped),
        "survivors": survivors,
        "dropped": dropped,
        "lens_status": lens_status,
    }


# ── rendering (panel spec + text report, both pure) ──────────────────────────


def _where(finding: dict[str, Any]) -> str:
    """``file:line`` (or bare ``file`` when the line is unknown)."""
    if isinstance(finding.get("line"), int):
        return f"{finding['file']}:{finding['line']}"
    return str(finding["file"])


def triage_panel_spec(survivors: list[dict[str, Any]]) -> dict[str, Any]:
    """The S68 panel spec presenting the survivors for triage (pure).

    A table of the survivors plus two action buttons that dispatch back into this
    extension as command calls (the panel→extension loop): keep all, or discard.
    Granular keep (a subset) is the typed ``/review_keep 1 3`` command — an action
    label cannot carry a runtime-chosen subset, so the buttons cover the two
    whole-set decisions and the command covers the rest.
    """
    rows = [
        [
            str(i + 1),
            "+".join(f["lenses"]),
            str(f["severity"]),
            _where(f),
            str(f["summary"]),
        ]
        for i, f in enumerate(survivors)
    ]
    return {
        "title": f"Review — {len(survivors)} finding(s) to triage",
        "table": {"columns": ["#", "lens", "sev", "file:line", "summary"], "rows": rows},
        "actions": [
            {"label": "Keep all", "command": "review_keep", "args": "all"},
            {"label": "Discard", "command": "review_discard", "args": ""},
        ],
    }


def _swarm_report(outcome: dict[str, Any]) -> str:
    """The S46 text report for ``/review`` (the command-output channel)."""
    survivors = outcome["survivors"]
    header = (
        f"Review swarm: {outcome['lens_count']} lens(es), {outcome['raw']} raw finding(s) "
        f"-> {len(survivors)} survived re-check."
    )
    if not survivors:
        dropped = len(outcome["dropped"])
        tail = f" ({dropped} dropped on re-check)" if dropped else ""
        return f"{header}{tail}\nNo findings to triage."
    lines = [header, ""]
    for i, f in enumerate(survivors):
        lens = "+".join(f["lenses"])
        lines.append(f"{i + 1}  {lens:<20} {str(f['severity']):<7} {_where(f)}  {f['summary']}")
    lines.append("")
    lines.append("Triage: /review_keep all | /review_keep <n ...> | /review_discard")
    return "\n".join(lines)


# ── the pending-triage state (ephemeral, never durable/model-visible) ────────


class _Pending:
    """The survivors of the current review awaiting triage.

    EPHEMERAL RAM state (like the live panel it mirrors) — never persisted, never
    model-visible. Only the KEPT subset becomes durable, via :class:`TreeStore`.
    """

    def __init__(self) -> None:
        self.survivors: list[dict[str, Any]] = []


def _parse_keep_spec(args: str, count: int) -> list[int] | str:
    """Parse ``/review_keep`` args → 0-based indices, or an error string.

    ``"all"`` (or empty) selects every survivor; otherwise a whitespace/comma list
    of 1-based indices. Fail-Early: an out-of-range or non-numeric token is a
    reported error, not a silently-ignored selection.
    """
    spec = args.strip().lower()
    if spec in ("", "all"):
        return list(range(count))
    indices: list[int] = []
    for token in re.split(r"[\s,]+", spec):
        if not token:
            continue
        if not token.isdigit():
            return f"Not a finding number: {token!r}. Use /review_keep all | /review_keep <n ...>"
        n = int(token)
        if not (1 <= n <= count):
            return f"Finding {n} is out of range (1..{count})."
        if n - 1 not in indices:
            indices.append(n - 1)
    if not indices:
        return "No findings selected. Use /review_keep all | /review_keep <n ...>"
    return indices


# ── command handlers ─────────────────────────────────────────────────────────


async def _review_command(
    args: str,
    ctx: Any,
    *,
    pending: _Pending,
    config: dict[str, Any],
) -> str:
    """``/review [ref]``: run the swarm and present the survivors for triage."""
    ref = args.strip() or "HEAD"
    cwd = getattr(ctx, "cwd", ".") or "."
    concurrency = _config_int(config, "concurrency", len(LENSES))
    child_timeout = _config_float(config, "timeout", 180.0)
    model = config.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError(f"review_swarm: config 'model' must be a string, got {model!r}")

    diff = await compute_diff(ref, cwd)
    if not diff.strip():
        pending.survivors = []
        ctx.ui.panel(PANEL_KEY, None)
        return f"Nothing to review — no diff against {ref}."

    outcome = await run_swarm(
        diff,
        model=model,
        cwd=cwd,
        concurrency=concurrency,
        child_timeout=child_timeout,
        signal=getattr(ctx, "signal", None),
    )
    pending.survivors = outcome["survivors"]
    if pending.survivors:
        ctx.ui.panel(PANEL_KEY, triage_panel_spec(pending.survivors))
    else:
        ctx.ui.panel(PANEL_KEY, None)
    return _swarm_report(outcome)


def _review_keep_command(args: str, ctx: Any, *, pending: _Pending, store: TreeStore) -> str:
    """``/review_keep <spec>``: record the selected survivors as a durable node.

    The panel-action target and the CLI triage verb: writes each selected pending
    finding as a durable ``customEntry`` (:meth:`TreeStore.append`) — the kept
    findings node — then clears the triage panel. ``spec`` is ``all`` (default) or
    a 1-based index list.
    """
    if not pending.survivors:
        return "No pending review to keep from. Run /review first."
    selection = _parse_keep_spec(args, len(pending.survivors))
    if isinstance(selection, str):
        return selection
    for i in selection:
        finding = pending.survivors[i]
        store.append(
            {
                "file": finding["file"],
                "line": finding.get("line"),
                "severity": finding["severity"],
                "lenses": list(finding["lenses"]),
                "summary": finding["summary"],
                "detail": finding.get("detail", ""),
            }
        )
    pending.survivors = []
    ctx.ui.panel(PANEL_KEY, None)
    return f"Kept {len(selection)} finding(s) to the session findings ledger."


def _review_discard_command(args: str, ctx: Any, *, pending: _Pending) -> str:
    """``/review_discard``: drop the pending survivors and clear the triage panel."""
    if not pending.survivors:
        return "No pending review to discard."
    count = len(pending.survivors)
    pending.survivors = []
    ctx.ui.panel(PANEL_KEY, None)
    return f"Discarded {count} pending finding(s)."


def _findings_command(args: str, ctx: Any, *, store: TreeStore) -> str:
    """``/findings``: list every kept finding (S46 output; reload-safe read)."""
    kept = store.load()
    if not kept:
        return "No findings kept yet. Run /review, then /review_keep."
    lines = []
    for f in kept:
        where = f["file"] if f.get("line") is None else f"{f['file']}:{f['line']}"
        lens = "+".join(f.get("lenses") or [])
        lines.append(f"[{lens}] {f.get('severity', '?')}  {where}  {f['summary']}")
    return "\n".join(lines)


# ── extension entry point ────────────────────────────────────────────────────


def review_swarm_extension(api: Any) -> None:
    """Register ``/review``, ``/review_keep``, ``/review_discard``, ``/findings``."""
    store: TreeStore[dict[str, Any]] = TreeStore(api, FINDING_CUSTOM_TYPE)
    pending = _Pending()
    config = api.config

    async def review_handler(args: str, ctx: Any) -> str:
        return await _review_command(args, ctx, pending=pending, config=config)

    def review_keep_handler(args: str, ctx: Any) -> str:
        return _review_keep_command(args, ctx, pending=pending, store=store)

    def review_discard_handler(args: str, ctx: Any) -> str:
        return _review_discard_command(args, ctx, pending=pending)

    def findings_handler(args: str, ctx: Any) -> str:
        return _findings_command(args, ctx, store=store)

    api.register_command(
        "review",
        {
            "description": "Fan out a read-only review swarm over the diff (usage: /review [ref])",
            "handler": review_handler,
        },
    )
    api.register_command(
        "review_keep",
        {
            "description": "Keep triaged findings (usage: /review_keep all | /review_keep <n ...>)",
            "handler": review_keep_handler,
        },
    )
    api.register_command(
        "review_discard",
        {
            "description": "Discard the pending review without keeping any finding",
            "handler": review_discard_handler,
        },
    )
    api.register_command(
        "findings",
        {
            "description": "List every kept review finding",
            "handler": findings_handler,
        },
    )


#: The module-level ``register`` the file-path loader looks up (``tau -e
#: examples/50_review_swarm.py`` → ``getattr(module, "register")``).
register = review_swarm_extension
