"""60 — Retrieval review: real JMFTS hits, N concurrent grammar-constrained verdicts.

The motivating story for `ctx.complete()` (JMFTS-INTEGRATION-PLAN §9), the payoff of
constrained decoding (CONSTRAINED-GEN-AND-BRANCHING-PLAN §4.2), and — since W13/W15 —
the payoff of the JMFTS integration itself.

A query is searched against JMFTS. Each hit becomes ONE `ctx.complete()` call asking
"does this help answer the query?", constrained to a fixed verdict set. Because the
whole answer is grammar-forced, each verdict costs ~1 forward pass; under jump-forward
decoding it gets cheaper still, since every token but the decision point is forced.

The retrieval half used to be a lie. This file shipped with a hardcoded `CORPUS` and a
comment promising that "in the real story these are JMFTS search hits" — because until
W13/W15 there was no way to get any: τ wrote every conversation into JMFTS with
`auto_embed=False`, so nothing it stored was searchable by anything. The demo judged
eight strings someone typed. Now it judges what the store actually returns, and if the
store cannot answer, it says so rather than falling back to a corpus that always
"works".

Four properties worth noticing:

- **Real retrieval.** Hits come from `JmftsClient.search` — including this
  conversation's own history, once `enrich` has run over it.
- **Concurrent.** `ctx.complete()` is stateless — no tree writes, no cursor move — so
  N of them run under `asyncio.gather` safely. Dozens of documents, one round trip's
  worth of wall clock. (Measured: 8 verdicts in 0.60s against a 35B on one GPU.)
- **Verified.** A verdict outside the declared set raises `ConstraintViolation`. This
  is not theoretical: llguidance can die mid-generation, log server-side, and let the
  model run free (GRAMMAR_DECODING_RECON.md:36) — and a `response_format` reaching the
  same request silently overrides the grammar entirely. Both were reproduced live.
  Without verification, free prose gets recorded as a verdict.
- **Routed through config.** `model="..."` resolves against the same `models` registry
  the TUI picks from, so the reviewer model is configured inline with the agent's.

Run:  tau --store jmfts -e examples/60_retrieval_review.py    then:  /review <query>

Config (`"extensions": {"60_retrieval_review": {...}}`)::

    {"model": "local-llm-small", "url": "http://…:8100", "scope": "all", "limit": 8}

`scope: "conversation"` restricts the search to this conversation's own subtree.

Requires a model with grammar support declared in ~/.tau/config.json:
    "local-llm": { ..., "grammar": "llguidance" }

Without that key the call raises rather than silently returning an UNCONSTRAINED
answer — τ never infers capability from a base_url.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tau_llm.constraints import ConstraintViolation, DecodeConstraints

VERDICTS = ["include", "exclude"]

# The labels must be DEFINED, not just named. An earlier version of this prompt only
# said "reply with one word: include or exclude" and scored 2/8 — the grammar dutifully
# forced a well-formed answer every time while the model guessed at what the words
# meant. A constraint guarantees the SHAPE of an answer, never its correctness.
JUDGE_SYSTEM = (
    "You are a retrieval relevance judge. Given a QUERY and a DOCUMENT, decide whether "
    "the document would help someone answer the query.\n"
    'Reply with exactly one word: "include" if the document is relevant and would help '
    'answer the query, or "exclude" if it is off-topic or would not help.'
)


def _retrieve(ctx: Any, url: str | None, query: str, scope: str, limit: int) -> list[dict]:
    """The candidates to judge — from the store, not from a literal in this file.

    Fail-Early throughout. If `tau_jmfts` is not installed, or no JMFTS is reachable, or
    the search returns nothing, this raises or returns empty and the command SAYS so. It
    does not quietly substitute a built-in corpus: a demo that always produces a tidy
    table regardless of whether retrieval works is a demo of nothing, and that is exactly
    what this file used to be.
    """
    from tau_jmfts.client import JmftsClient  # noqa: PLC0415 — optional dependency

    log = ctx._require_session().session_log
    client = getattr(log, "client", None)
    if not isinstance(client, JmftsClient):
        if not url:
            raise RuntimeError(
                "60_retrieval_review: this session is not JMFTS-backed and no 'url' is "
                "configured, so there is nothing to retrieve FROM. Run with "
                "`--store jmfts`, or set extensions.60_retrieval_review.url."
            )
        client = JmftsClient(str(url))

    parent_id = None
    if scope == "conversation":
        parent_id = getattr(log, "root_doc_id", None)
        if parent_id is None:
            raise RuntimeError(
                "60_retrieval_review: scope='conversation' needs a JMFTS-backed session."
            )

    return client.search(query, method="hybrid", limit=limit, parent_id=parent_id)


def register(api: Any) -> None:
    config = api.config or {}
    reviewer_model = config.get("model")  # None -> the session's current model
    url = config.get("url")
    scope = config.get("scope", "all")
    limit = int(config.get("limit", 8))

    async def judge(ctx: Any, query: str, doc: str) -> str:
        """One constrained verdict. The whole answer is grammar-forced."""
        return await ctx.complete_text(
            [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": f"QUERY: {query}\n\nDOCUMENT: {doc}"},
            ],
            model=reviewer_model,
            constraints=DecodeConstraints(choices=VERDICTS),
        )

    async def review(args: str, ctx: Any) -> str | None:
        query = args.strip()
        if not query:
            api.ui.notify("usage: /review <query>", level="warning")
            return None

        api.ui.set_status("review", "retrieving…")
        try:
            hits = _retrieve(ctx, url, query, scope, limit)
            if not hits:
                # An empty result is a real answer: the store has nothing on this. It is
                # NOT an invitation to review something else instead.
                api.ui.notify(f"no JMFTS hits for {query!r} (scope={scope})", level="warning")
                return None

            docs = [
                (h["document"]["id"], (h["document"].get("content") or "").strip()) for h in hits
            ]
            api.ui.set_status("review", f"judging {len(docs)} hits…")
            # Stateless => safe to fan out. This is the whole point of C1.
            verdicts = await asyncio.gather(*(judge(ctx, query, text) for _, text in docs))
        except ConstraintViolation as exc:
            # The grammar did not hold: the server returned an UNCONSTRAINED generation.
            # Surface it — do not record free prose as if it were a verdict.
            api.ui.notify(
                f"constraint violated (the server dropped the grammar): {exc.output!r}",
                level="error",
            )
            return None
        finally:
            api.ui.set_status("review", None)  # None clears; "" would leave a blank slot

        kept = sum(1 for v in verdicts if v == "include")
        api.ui.panel(
            "retrieval-review",
            {
                "title": f"Retrieval review — {query}",
                "table": {
                    "columns": ["", "verdict", "doc", "content"],
                    # The doc id is in the table because a verdict you cannot trace back
                    # to a document is an opinion, not a review.
                    "rows": [
                        ["✓" if v == "include" else "·", v, str(doc_id), text[:80]]
                        for (doc_id, text), v in zip(docs, verdicts)
                    ],
                },
            },
        )
        return (
            f"{kept}/{len(docs)} JMFTS hits included. Every verdict grammar-forced and "
            "verified in-set."
        )

    api.register_command(
        "review",
        {
            "description": "Fan out a query over the corpus, one constrained verdict per "
            "doc (usage: /review <query>)",
            "handler": review,
        },
    )
