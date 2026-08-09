"""The single resolver-routed completion door (C1 — Piece A).

Every non-loop model completion in τ — ``ctx.complete()``, branch summarization,
and compaction's two summary calls — used to open its OWN door onto
``complete_simple``, three of them reaching straight past the model resolver into
``session._model`` / ``session._api_key``. This module extracts the one primitive
they all now sit on: :func:`resolved_complete`, which resolves a model (by name,
via a supplied resolver, or an already-built ``Model``), makes the call, and runs
the *shared* error/aborted ``stop_reason`` check.

What it deliberately does NOT do, because those differ per caller and unifying them
would change behavior (see docs/WORKSTREAM-CROSSWALK.md, C1):

* **billing** — ``ctx.complete()`` bills internally (and even on the error path,
  because the provider charged for the tokens); the summary callers return
  ``(text, usage)`` and let *their* caller bill. So this primitive never touches a
  usage ledger. On the error path it hands the offending response back inside
  :class:`CompletionFailed` so a caller that must bill still can.
* **the ``length`` policy** — ``ctx.complete()`` rejects a truncated answer;
  compaction instead *budgets* ``max_tokens`` up front and lets a short answer
  stand. Only the error/aborted check is shared here; ``length`` stays with the
  caller that owns the policy.
* **error taxonomy** — this primitive raises one exception type,
  :class:`CompletionFailed`, carrying enough (``stop_reason`` + ``error_message``)
  for each caller to translate into its own: ``RuntimeError`` for ctx.complete /
  branch summaries, ``CompactionError`` (with the aborted-vs-error code split) for
  compaction.

Reference: docs/JMFTS-INTEGRATION-PLAN.md §9.1 (C1); pi has no single analogue —
this consolidates what pi spreads across agent-session.ts and compaction.ts.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from tau_ai.types import AssistantMessage, Model


class CompletionFailed(RuntimeError):
    """A completion returned a terminal ``error``/``aborted`` ``stop_reason``.

    Raised by :func:`resolved_complete` so every caller shares ONE error/aborted
    check while keeping its own error taxonomy. It carries:

    * ``response`` — the offending ``AssistantMessage``, so a caller that must bill
      for a completion it cannot USE (``ctx.complete``: the provider charged for the
      tokens regardless) can still read ``usage_of(response)``.
    * ``stop_reason`` — so the compaction path can keep its ``aborted`` vs
      ``summarization_failed`` code split.
    * ``error_message`` / ``detail`` — the provider's message, for each caller's own
      wrapped exception text.
    """

    def __init__(self, response: Any, stop_reason: str | None, error_message: str | None) -> None:
        self.response = response
        self.stop_reason = stop_reason
        self.error_message = error_message
        self.detail = error_message or stop_reason or "unknown error"
        super().__init__(f"completion failed: {self.detail}")


async def resolved_complete(
    model: Model | str,
    context: dict[str, Any],
    *,
    options: dict[str, Any] | None = None,
    resolver: Callable[[str], Model] | None = None,
    complete_fn: Callable[..., Awaitable[AssistantMessage]] | None = None,
) -> AssistantMessage:
    """Resolve ``model``, run one completion, and apply the shared error check.

    Args:
        model: an already-built ``Model``, or a **string** name resolved via
            ``resolver``. Fail-Early: a name with no ``resolver`` raises — this
            primitive never silently falls back to a default model.
        context: the ``{"messages": [...], ...}`` payload for ``complete_simple``.
        options: provider options (``max_tokens``, ``api_key``, ``reasoning``,
            ``constraints``, …). Forwarded verbatim — so a caller that computed a
            ``max_tokens`` budget keeps it, and one that passes ``None`` keeps that.
        resolver: maps a model name to a ``Model`` (the session's model registry).
        complete_fn: the completion callable. Defaults to
            ``tau_ai.client.complete_simple``, imported *at call time* so a test
            that patches ``tau_ai.client.complete_simple`` is honored. The
            compaction path passes its own module-level ``complete_simple`` instead,
            so a patch of ``tau_agent_core.compaction.complete_simple`` is honored
            there — the two patch sites the suite relies on both keep working.

    Returns:
        The ``AssistantMessage`` on a non-error, non-aborted ``stop_reason``.

    Raises:
        RuntimeError: a string ``model`` with no ``resolver``.
        CompletionFailed: ``stop_reason`` is ``error`` or ``aborted``.
    """
    if isinstance(model, str):
        if resolver is None:
            raise RuntimeError(
                f"cannot resolve model {model!r} by name: no model resolver was supplied"
            )
        resolved: Model = resolver(model)
    else:
        resolved = model

    fn = complete_fn
    if fn is None:
        from tau_ai.client import complete_simple

        fn = complete_simple

    response = await fn(resolved, context, options)

    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason in ("error", "aborted"):
        raise CompletionFailed(response, stop_reason, getattr(response, "error_message", None))
    return response
