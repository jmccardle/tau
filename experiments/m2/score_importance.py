"""Score LoCoMo turns for Generative-Agents "poignancy". M2 determination (a), importance leg.

Tau-side POLICY, and emphatically so: the prompt, the scale, and what counts as important
are opinions. JMFTS only reads the number out of structured_content['importance'].

The prompt is Generative-Agents' verbatim (Park et al., the "rate the likely poignancy"
scale), so a negative result indicts our substrate rather than our paraphrase.

GRAMMAR-CONSTRAINED, NOT PARSE-AND-HOPE.
    The rating is decoded under `root ::= "10" | [1-9]`, so the model CANNOT emit an
    out-of-range or non-numeric score. This matters beyond convenience: the alternative is
    parsing free text across 5,463 calls and inventing a policy for the failures, which
    would mean either fabricating a default (poisoning the very term under test) or
    silently dropping turns (biasing the corpus). The constraint removes the failure mode
    at the source rather than handling it downstream. jmfts's own scorer validates the
    1-10 range and RAISES on a violation, so a fabricated default could not pass anyway.

    Thinking is disabled on the call. That is required, not incidental: a grammar binds the
    model's FIRST token, which on a reasoning model lands inside <think> — see
    docs/REASONING-VS-CONSTRAINED-DECODING.md. A 1-10 rating wants no chain of thought.

Concurrency matches the server's slot count (-np 2). The server is tau's live local-llm;
this deliberately does not restart it to widen throughput.

Run ON midlife:
    /home/john/jmfts-gpudev-env.sh score_importance.py [--limit N] [--usetype locomo_turn]
"""

import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import select

from jmfts_core.database import get_session_factory
from jmfts_core.models.document import Document

LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"
MODEL_SLOTS = 2  # llama-server runs -np 2; more workers just queue

# Park et al., Generative Agents — verbatim.
PROMPT = (
    "On the scale of 1 to 10, where 1 is purely mundane (e.g., brushing teeth, making bed) "
    "and 10 is extremely poignant (e.g., a break up, college acceptance), rate the likely "
    "poignancy of the following piece of memory.\nMemory: {memory}\nRating:"
)
GRAMMAR = 'root ::= "10" | [1-9]'


def score_one(client: httpx.Client, text: str) -> int:
    """One rating. Raises on transport/HTTP failure — a silently-skipped turn would bias
    the corpus, and this is the term under test."""
    resp = client.post(
        LLM_URL,
        json={
            "messages": [{"role": "user", "content": PROMPT.format(memory=text)}],
            "grammar": GRAMMAR,
            "chat_template_kwargs": {"enable_thinking": False},
            "max_tokens": 4,
            "temperature": 0,
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    # The grammar makes this total; if it ever raises, the grammar was not applied and we
    # want to know loudly rather than coerce.
    return int(content)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--usetype", default="locomo_turn")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    factory = get_session_factory()
    with factory() as db:
        stmt = select(Document.id, Document.content).where(Document.usetype == args.usetype)
        if args.limit:
            stmt = stmt.limit(args.limit)
        rows = db.execute(stmt).all()

    print(f"scoring {len(rows)} {args.usetype} documents at concurrency {MODEL_SLOTS}")
    started = time.time()
    scores: dict[int, int] = {}

    with httpx.Client() as client:

        def work(row):
            return row[0], score_one(client, row[1])

        with ThreadPoolExecutor(max_workers=MODEL_SLOTS) as pool:
            for n, (doc_id, score) in enumerate(pool.map(work, rows), 1):
                scores[doc_id] = score
                if n % 250 == 0:
                    rate = n / (time.time() - started)
                    eta = (len(rows) - n) / rate / 60
                    print(f"  {n}/{len(rows)}  {rate:.1f}/s  eta {eta:.1f}m", flush=True)

    # Write back. Reassign structured_content rather than mutating it: JSONB mutation is
    # not tracked by SQLAlchemy, so an in-place update would silently never persist.
    with factory() as db:
        for doc_id, score in scores.items():
            doc = db.get(Document, doc_id)
            doc.structured_content = {**(doc.structured_content or {}), "importance": score}
        db.commit()

    dist = Counter(scores.values())
    elapsed = time.time() - started
    print(f"\nscored {len(scores)} in {elapsed:.0f}s ({len(scores) / elapsed:.1f}/s)")
    print("distribution:")
    for rating in sorted(dist):
        bar = "#" * int(60 * dist[rating] / max(dist.values()))
        print(f"  {rating:>2}: {dist[rating]:>5}  {bar}")
    mean = sum(k * v for k, v in dist.items()) / len(scores)
    print(f"mean {mean:.2f}, distinct {len(dist)}")
    with open("/fast/datasets/m2/importance_scores.json", "w") as fh:
        json.dump({str(k): v for k, v in scores.items()}, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
