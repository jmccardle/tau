"""Example 52: Red-team memory — a review swarm whose adversaries remember (E11, S73).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §7 S73. No pi original — this composes
the τ-native ``50_review_swarm`` baseline (S71) and turns exactly one new dial:
**the backplane accretes across sessions.**

## What this shows — S71 + a cross-session corpus

``/review`` still runs the full S71 pipeline (fan-out over ``LENSES``, dedupe,
adversarial re-check, panel triage → durable findings node). This demo wraps that
baseline with one addition — a :class:`ext_kit.state.FileStore` *red-team corpus*
that lives ABOVE any single conversation (``~/.tau/ext-state/…``), and two seams
that read and grow it:

1. **The adversaries are seeded with the corpus.** Before a survivor is put to a
   skeptic child (the S55 gate atom), the corpus of previously-confirmed findings
   is folded into the adversary's prompt as *calibration prior*: "these issue
   shapes survived refutation before, so they are known-genuine — do not dismiss a
   finding of the same shape without a concrete reason; still judge THIS finding on
   its own merits" (:func:`_adversary_prompt`). The red team gets sharper the more
   it has seen, WITHOUT ever fabricating a verdict — the corpus is a bias-toward-rigor
   nudge, not an automatic pass (Fail-Early: :func:`_survives` still requires the
   survival sentinel).
2. **Confirmed findings accrete into the corpus.** ``/review_keep`` keeps a
   survivor as a durable ``customEntry`` on the session tree (S71's conversation-
   scoped ledger) AND *promotes* it into the cross-session corpus
   (:func:`accrete_corpus`). Promotion is on KEEP, not on mere survival: the human
   triage gate is what decides a finding is real enough to remember across sessions
   — so an un-triaged (or later discarded) survivor never pollutes permanent memory.
   A re-confirmed finding bumps its ``keeps`` counter rather than duplicating: the
   memory strengthens, it does not bloat.

``/red_team`` lists the accreted corpus — the all-sessions memory, ranked by how
many times each finding has been re-confirmed.

## The invariant (tree-as-truth)

Two DURABLE writes, both legal. (a) A kept finding is an append-only ``customEntry``
on the active path (S71's :class:`ext_kit.state.TreeStore`) — persisted == rendered,
and excluded from ``convert_to_llm`` by construction. (b) The corpus is a
:class:`ext_kit.state.FileStore` blob the extension owns: it is NOT on the session
tree and NOT this session's model input. It reaches a model ONLY as context inside a
*spawned, isolated adversary child* — that child's own legitimate input, not a hidden
side-channel into this session's path. The main conversation's model input per call
is still exactly the system prompt + the linear active path, unchanged from S71.

## Headless parity (§6.3 CLI rule)

Same as S71: ``/review`` emits the triage as an ``{"type":"extension","kind":"panel",…}``
record on the ``--mode json`` stream, and every triage/memory verb (``/review_keep``,
``/review_discard``, ``/red_team``) is a plain command that runs identically headless
— so a child ``tau -p --mode json`` running this extension stays orchestratable.

## Fail-Early

The corpus is loaded with an explicit ``default=[]`` (the honest first-run empty
value) and RAISES on a corrupt / non-list blob rather than silently resetting real
cross-session memory (:func:`load_corpus`). An empty diff reports "nothing to review"
(no fabricated findings); an inconclusive adversarial re-check DROPS the finding; and
the corpus never fabricates a verdict — a seeded adversary that cannot re-confirm a
finding still refutes it.

## Usage

    tau -e examples/52_red_team_memory.py
    > (make some edits)
    > /review
    Review swarm: 3 lens(es), 4 raw finding(s) -> 2 survived re-check.
    ...
    > /review_keep all
    Kept 2 finding(s) to the session findings ledger. (2 new to red-team memory)
    > /red_team
    Red-team memory — 2 confirmed finding(s) across sessions:
    [security] high  auth.py:42  SQL built by string concat  (x1)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

# ``ext_kit`` and the sibling ``50_review_swarm`` baseline both live alongside the
# numbered examples, not inside an installed package — bootstrap ``examples/`` onto
# the path before importing them, whether run directly, imported, or loaded via
# ``-e`` (D-E6-3), the same as the other ext_kit-using demos (20_delegate, 51_delegate_fleet).
_EXAMPLES_DIR = str(Path(__file__).resolve().parent)
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

from ext_kit import gate, spawn  # noqa: E402  (path insertion must precede the import)
from ext_kit.state import FileStore  # noqa: E402

# The S71 baseline this demo builds on. ``50_review_swarm`` is not importable by a
# bare name (the leading digit is not a valid identifier), so load it by path — the
# same importlib bootstrap its own test uses. This is the "S71 + FileStore"
# composition the roadmap names: we reuse S71's swarm vocabulary verbatim (the
# lenses, the pure parse/dedupe, the gate predicate, the triage panel + report) and
# add only the cross-session corpus dial.
_REVIEW_PATH = Path(_EXAMPLES_DIR) / "50_review_swarm.py"
_review_spec = importlib.util.spec_from_file_location("_review_swarm_baseline", _REVIEW_PATH)
if _review_spec is None or _review_spec.loader is None:  # pragma: no cover - defensive
    raise ImportError(f"red_team_memory: cannot load the S71 review baseline at {_REVIEW_PATH}")
review = importlib.util.module_from_spec(_review_spec)
sys.modules[_review_spec.name] = review
_review_spec.loader.exec_module(review)

# The gate contract is SHARED with the S71 baseline: same verdict sentinels, same
# survival predicate. We reuse them verbatim so a corpus-seeded adversary speaks the
# exact protocol ``run_gate`` parses — one gate vocabulary, not two.
_survives = review._survives
_VERDICT_SURVIVES = review._VERDICT_SURVIVES
_VERDICT_REFUTED = review._VERDICT_REFUTED

#: The ``customEntry`` type this demo's conversation-scoped kept-findings live under
#: (S71's TreeStore ledger; distinct from ``50_review_swarm``'s so the two demos
#: never share a session ledger if both are ever loaded).
FINDING_CUSTOM_TYPE = "red_team_finding"

#: The keyed S68 panel this demo mounts for triage (its own key, not the baseline's).
PANEL_KEY = "red_team"

#: The cross-session corpus FileStore name (``~/.tau/ext-state/<name>.json``).
CORPUS_STORE_NAME = "red_team_corpus"

#: Cap on how many remembered findings seed one adversary prompt (bounded context —
#: the corpus can grow unboundedly across sessions, one prompt cannot).
CORPUS_SEED_LIMIT = 20


# ── the cross-session corpus (FileStore-backed; the S73 dial) ────────────────


def load_corpus(store: FileStore) -> list[dict[str, Any]]:
    """Load the red-team corpus (Fail-Early on a corrupt / wrong-shaped blob).

    A first-run store returns ``[]`` (the honest empty memory). A present blob that
    is not a list of dict records RAISES — the corpus is cross-session state, so a
    corrupt file must surface, never silently reset to empty.
    """
    data = store.load(default=[])
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise ValueError(
            f"red_team_memory: corpus at {store.path} is not a list of records "
            "(corrupt cross-session memory — refusing to silently reset it)"
        )
    return data


def corpus_record(finding: dict[str, Any]) -> dict[str, Any]:
    """A cross-session corpus record for one confirmed (kept) finding.

    Carries the finding's identity plus a ``keeps`` counter — the number of times
    this exact finding has been re-confirmed across sessions (how load-bearing the
    memory is).
    """
    return {
        "file": finding["file"],
        "line": finding.get("line"),
        "severity": finding.get("severity", "unknown"),
        "lenses": list(finding.get("lenses") or []),
        "summary": finding["summary"],
        "detail": finding.get("detail", ""),
        "keeps": 1,
    }


def accrete_corpus(
    corpus: list[dict[str, Any]], findings: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Fold newly-confirmed ``findings`` into ``corpus`` (pure), dedupe by signature.

    Returns ``(new_corpus, added)`` where ``added`` is the count of findings not
    already remembered. A re-confirmed finding (same S71 :func:`finding_signature`)
    bumps its ``keeps`` counter instead of duplicating — the memory strengthens, it
    does not bloat. The input ``corpus`` is not mutated (a fresh list of fresh
    records is returned), so a failed :meth:`FileStore.save` never leaves a
    half-updated in-RAM corpus.
    """
    result = [dict(record) for record in corpus]
    by_sig = {review.finding_signature(record): record for record in result}
    added = 0
    for finding in findings:
        sig = review.finding_signature(finding)
        existing = by_sig.get(sig)
        if existing is None:
            record = corpus_record(finding)
            result.append(record)
            by_sig[sig] = record
            added += 1
        else:
            existing["keeps"] = int(existing.get("keeps", 1)) + 1
    return result, added


def corpus_seed_lines(corpus: list[dict[str, Any]], limit: int = CORPUS_SEED_LIMIT) -> list[str]:
    """The bounded, ranked one-line summaries that seed an adversary (pure).

    Ranked by ``keeps`` desc (most re-confirmed first), then summary for a stable
    order, and capped at ``limit`` so one prompt stays bounded even as the corpus
    grows across sessions.
    """
    ranked = sorted(
        corpus,
        key=lambda r: (-int(r.get("keeps", 1)), str(r.get("summary", ""))),
    )
    lines: list[str] = []
    for record in ranked[:limit]:
        lines.append(f"- {review._where(record)}: {record.get('summary', '')}")
    return lines


# ── the corpus-seeded adversary (the memory reaches the skeptic child) ────────


def _adversary_prompt(finding: dict[str, Any], diff: str, corpus: list[dict[str, Any]]) -> str:
    """The skeptic child's prompt, seeded with the red-team corpus.

    Identical to S71's adversary brief (REFUTE one finding against the diff, end on
    exactly one verdict sentinel) plus a RED-TEAM MEMORY section — the corpus of
    previously-confirmed findings as calibration prior. Empty corpus → the baseline
    prompt, unchanged.
    """
    where = finding["file"]
    if isinstance(finding.get("line"), int):
        where = f"{where}:{finding['line']}"
    memory = ""
    seed = corpus_seed_lines(corpus)
    if seed:
        memory = (
            "--- RED-TEAM MEMORY (findings confirmed in prior sessions) ---\n"
            "These issue shapes survived refutation before, so they are known to be "
            "genuine, load-bearing bugs. Use them as calibration: do NOT dismiss a "
            "finding of the same shape as a false positive without a concrete reason. "
            "This is a prior, not a verdict — judge the finding above on its own merits.\n"
            + "\n".join(seed)
            + "\n\n"
        )
    return (
        "You are an adversarial reviewer. Another reviewer raised the finding below. "
        "Your job is to REFUTE it: read the diff (and the repo if needed) and decide "
        "whether the finding is a genuine, load-bearing issue or a false positive.\n\n"
        f"Finding [{'+'.join(finding['lenses'])}] {where}: {finding['summary']}\n"
        f"Detail: {finding.get('detail', '')}\n\n"
        f"{memory}"
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
    corpus: list[dict[str, Any]],
) -> gate.GateResult:
    """Run one survivor through the corpus-seeded adversarial gate.

    Mirrors S71's ``recheck_finding`` — an isolated, read-only ``tau`` child put to
    the gate atom with the shared :func:`_survives` predicate — but its prompt is
    seeded with the red-team ``corpus`` (:func:`_adversary_prompt`). A timeout / an
    inconclusive transcript yields ``passed=False`` (Fail-Early), so the finding is
    dropped rather than kept unverified.
    """
    command, argv = spawn.tau_invocation(
        spawn.build_child_args(
            prompt=_adversary_prompt(finding, diff, corpus),
            model=model,
            tools=list(review.READONLY_TOOLS),
            system_prompt_path=None,
        )
    )
    return await gate.run_gate([command, *argv], parse=_survives, cwd=cwd, timeout=timeout)


# ── the swarm driver (S71 fan-out + dedupe; corpus-seeded re-check) ──────────


async def run_red_team_swarm(
    diff: str,
    corpus: list[dict[str, Any]],
    *,
    model: str | None,
    cwd: str,
    concurrency: int,
    child_timeout: float,
    signal: Any | None,
) -> dict[str, Any]:
    """Fan out the lenses, dedupe, re-check survivors against corpus-seeded adversaries.

    Same shape as S71's ``run_swarm`` (returns ``{lens_count, raw, survivors,
    dropped, lens_status}``) — reusing its lens prompts and pure parse/dedupe — with
    the one difference that each survivor's adversarial re-check goes through this
    module's corpus-aware :func:`recheck_finding`. Referenced module-level so a test
    can monkeypatch the re-check without a real subprocess.
    """
    pool = spawn.WorkerPool(concurrency)
    lens_items = list(review.LENSES.items())

    async def _lens(item: tuple[str, str], _index: int) -> tuple[str, spawn.ChildResult]:
        lens, focus = item
        result = await spawn.spawn_tau(
            review._lens_prompt(lens, focus, diff),
            model=model,
            tools=list(review.READONLY_TOOLS),
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
        found = review.parse_findings(child.final_output, lens)
        lens_status.append({"lens": lens, "status": "ok", "count": len(found)})
        raw.extend(found)

    deduped = review.dedupe_findings(raw)

    async def _gate(
        finding: dict[str, Any], _index: int
    ) -> tuple[dict[str, Any], gate.GateResult]:
        verdict = await recheck_finding(
            finding, diff, model=model, cwd=cwd, timeout=child_timeout, corpus=corpus
        )
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


# ── command handlers ─────────────────────────────────────────────────────────


async def _review_command(
    args: str,
    ctx: Any,
    *,
    pending: Any,
    config: dict[str, Any],
    corpus_store: FileStore,
) -> str:
    """``/review [ref]``: run the corpus-seeded swarm and present survivors for triage."""
    ref = args.strip() or "HEAD"
    cwd = getattr(ctx, "cwd", ".") or "."
    concurrency = review._config_int(config, "concurrency", len(review.LENSES))
    child_timeout = review._config_float(config, "timeout", 180.0)
    model = config.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError(f"red_team_memory: config 'model' must be a string, got {model!r}")

    diff = await review.compute_diff(ref, cwd)
    if not diff.strip():
        pending.survivors = []
        ctx.ui.panel(PANEL_KEY, None)
        return f"Nothing to review — no diff against {ref}."

    # Seed the adversaries with the cross-session memory (re-read per run so a corpus
    # another session grew is picked up).
    corpus = load_corpus(corpus_store)
    outcome = await run_red_team_swarm(
        diff,
        corpus,
        model=model,
        cwd=cwd,
        concurrency=concurrency,
        child_timeout=child_timeout,
        signal=getattr(ctx, "signal", None),
    )
    pending.survivors = outcome["survivors"]
    if pending.survivors:
        ctx.ui.panel(PANEL_KEY, review.triage_panel_spec(pending.survivors))
    else:
        ctx.ui.panel(PANEL_KEY, None)

    report = review._swarm_report(outcome)
    if corpus:
        report += f"\n(adversaries seeded from {len(corpus)} remembered finding(s))"
    return report


def _review_keep_command(
    args: str,
    ctx: Any,
    *,
    pending: Any,
    store: Any,
    corpus_store: FileStore,
) -> str:
    """``/review_keep <spec>``: keep survivors durably AND accrete them to the corpus.

    S71's keep (a durable ``customEntry`` per selected survivor) plus the S73 dial:
    the kept findings are promoted into the cross-session red-team corpus. Promotion
    is on this human triage gate — a survivor becomes permanent memory only when a
    person confirms it, so an un-triaged / discarded survivor never pollutes it.
    """
    if not pending.survivors:
        return "No pending review to keep from. Run /review first."
    selection = review._parse_keep_spec(args, len(pending.survivors))
    if isinstance(selection, str):
        return selection
    kept: list[dict[str, Any]] = []
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
        kept.append(finding)

    corpus = load_corpus(corpus_store)
    new_corpus, added = accrete_corpus(corpus, kept)
    corpus_store.save(new_corpus)

    pending.survivors = []
    ctx.ui.panel(PANEL_KEY, None)
    if added:
        tail = f" ({added} new to red-team memory)"
    else:
        tail = " (all already in red-team memory)"
    return f"Kept {len(selection)} finding(s) to the session findings ledger.{tail}"


def _review_discard_command(args: str, ctx: Any, *, pending: Any) -> str:
    """``/review_discard``: drop the pending survivors and clear the triage panel.

    Discarded survivors are NOT promoted — only a kept finding enters cross-session
    memory.
    """
    if not pending.survivors:
        return "No pending review to discard."
    count = len(pending.survivors)
    pending.survivors = []
    ctx.ui.panel(PANEL_KEY, None)
    return f"Discarded {count} pending finding(s)."


def _red_team_command(args: str, ctx: Any, *, corpus_store: FileStore) -> str:
    """``/red_team``: list the accreted cross-session corpus (S46 output)."""
    corpus = load_corpus(corpus_store)
    if not corpus:
        return "Red-team memory is empty. Confirm findings with /review_keep to accrete the corpus."
    ranked = sorted(
        corpus,
        key=lambda r: (-int(r.get("keeps", 1)), str(r.get("summary", ""))),
    )
    lines = [f"Red-team memory — {len(corpus)} confirmed finding(s) across sessions:"]
    for record in ranked:
        lenses = "+".join(record.get("lenses") or [])
        keeps = int(record.get("keeps", 1))
        lines.append(
            f"[{lenses}] {record.get('severity', '?')}  {review._where(record)}  "
            f"{record.get('summary', '')}  (x{keeps})"
        )
    return "\n".join(lines)


# ── extension entry point ────────────────────────────────────────────────────


def red_team_memory_extension(api: Any) -> None:
    """Register ``/review``, ``/review_keep``, ``/review_discard``, ``/findings``, ``/red_team``."""
    config = api.config
    # S71's conversation-scoped ledger (durable ``customEntry`` nodes on the tree).
    store = review.TreeStore(api, FINDING_CUSTOM_TYPE)
    # The cross-session corpus (test/opt-in override of the ext-state root via config).
    corpus_dir = config.get("corpus_dir")
    corpus_name = config.get("corpus_name", CORPUS_STORE_NAME)
    if not isinstance(corpus_name, str) or not corpus_name:
        raise ValueError("red_team_memory: config 'corpus_name' must be a non-empty string")
    corpus_store = FileStore(corpus_name, base_dir=corpus_dir)
    pending = review._Pending()

    async def review_handler(args: str, ctx: Any) -> str:
        return await _review_command(
            args, ctx, pending=pending, config=config, corpus_store=corpus_store
        )

    def review_keep_handler(args: str, ctx: Any) -> str:
        return _review_keep_command(
            args, ctx, pending=pending, store=store, corpus_store=corpus_store
        )

    def review_discard_handler(args: str, ctx: Any) -> str:
        return _review_discard_command(args, ctx, pending=pending)

    def findings_handler(args: str, ctx: Any) -> str:
        # The conversation-scoped ledger listing is identical to S71 (store-only,
        # no panel) — reuse the baseline handler verbatim.
        return review._findings_command(args, ctx, store=store)

    def red_team_handler(args: str, ctx: Any) -> str:
        return _red_team_command(args, ctx, corpus_store=corpus_store)

    api.register_command(
        "review",
        {
            "description": "Run a corpus-seeded review swarm over the diff (usage: /review [ref])",
            "handler": review_handler,
        },
    )
    api.register_command(
        "review_keep",
        {
            "description": "Keep findings + accrete them to red-team memory "
            "(usage: /review_keep all | /review_keep <n ...>)",
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
            "description": "List every kept finding in this session",
            "handler": findings_handler,
        },
    )
    api.register_command(
        "red_team",
        {
            "description": "List the cross-session red-team memory corpus",
            "handler": red_team_handler,
        },
    )


#: The module-level ``register`` the file-path loader looks up (``tau -e
#: examples/52_red_team_memory.py`` → ``getattr(module, "register")``).
register = red_team_memory_extension
