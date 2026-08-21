"""Why did importance collapse to 96%-at-1? Separate the three candidate causes.

The turn-level importance leg produced a degenerate distribution. Three explanations are
live, and they imply completely different fixes:

  (a) GRANULARITY — a raw dialogue turn really is mundane; importance needs an observation
      layer (tested separately, in raptor_importance.py).
  (b) RUBRIC — Generative-Agents' scale measures *emotional poignancy*, which is arguably
      orthogonal to *retrievability*. "Caroline goes to yoga on Tuesdays at 6" is a 1 for
      poignancy and gold for answering a later question. If so, the term isn't wrong, we
      asked the wrong question.
  (c) THINKING — the rating was decoded with thinking disabled (a grammar binds the first
      token, which on a reasoning model is inside <think>). Our llama.cpp fix removes that
      constraint, so the model can now reason THEN obey the grammar. Maybe one-shot
      ratings are just lazy and collapse to the anchor.

This probes (b) and (c) as a 2x2 on a shared, seeded subsample, and reports the
distribution of each cell. Running the full 5,882-turn scoring under thinking would cost
~6.8h (1,503 completion tokens vs 3), so measuring the distribution shift on a subsample
first is the cheap way to find out whether it is worth it.

What we are looking for is DISCRIMINATION, not a higher mean: a term that assigns every
document the same value cannot reorder anything, whatever that value is.

Run ON midlife:
    /home/john/jmfts-gpudev-env.sh rubric_probe.py [--n 200]
"""

import argparse
import math
import random
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import select

from jmfts_core.database import get_session_factory
from jmfts_core.models.document import Document

LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"
SLOTS = 2
GRAMMAR = 'root ::= "10" | [1-9]'
SEED = 20260717

# Park et al., Generative Agents — verbatim. Measures emotional poignancy.
RUBRIC_GA = (
    "On the scale of 1 to 10, where 1 is purely mundane (e.g., brushing teeth, making bed) "
    "and 10 is extremely poignant (e.g., a break up, college acceptance), rate the likely "
    "poignancy of the following piece of memory.\nMemory: {memory}\nRating:"
)

# Ours. Measures retrievability — how much durable, specific fact is in here that a later
# question could need. This is what the recall task actually rewards, and it is a different
# question from poignancy.
RUBRIC_RETRIEVAL = (
    "Rate on a scale of 1 to 10 how much durable, specific, factual information this "
    "message contains about the speaker's life — the kind a question asked months later "
    "might depend on.\n"
    "1 = pure social filler, no facts (greetings, 'that's great!', acknowledgements).\n"
    "3 = vague or generic statement, nothing a question could pin down.\n"
    "5 = a preference or opinion, but no specifics.\n"
    "8 = a concrete event, plan, or relationship with some detail.\n"
    "10 = dense specifics: names, dates, places, numbers, or commitments.\n"
    "Message: {memory}\nRating:"
)

RUBRICS = {"ga_poignancy": RUBRIC_GA, "retrievability": RUBRIC_RETRIEVAL}


def score_one(client: httpx.Client, prompt: str, thinking: bool) -> int:
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "grammar": GRAMMAR,
        "temperature": 0,
        # Thinking needs headroom: truncated reasoning never reaches the grammar-bound
        # answer and returns content='', which int() then refuses — loudly, by design.
        "max_tokens": 3000 if thinking else 4,
    }
    if not thinking:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    resp = client.post(LLM_URL, json=body, timeout=300.0)
    resp.raise_for_status()
    msg = resp.json()["choices"][0]["message"]
    content = (msg["content"] or "").strip()
    if not content:
        raise RuntimeError(
            "empty content: reasoning likely truncated before the grammar-bound answer "
            f"(reasoning len={len(msg.get('reasoning_content') or '')})"
        )
    return int(content)


def stats(scores: list[int]) -> dict:
    dist = Counter(scores)
    n = len(scores)
    # Shannon entropy in bits: the direct measure of how much a term can reorder anything.
    ent = -sum((c / n) * math.log2(c / n) for c in dist.values())
    return {
        "n": n,
        "mean": sum(scores) / n,
        "frac_at_1": dist[1] / n,
        "distinct": len(dist),
        "entropy_bits": ent,
        "dist": dict(sorted(dist.items())),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--usetype", default="locomo_turn")
    args = ap.parse_args()

    factory = get_session_factory()
    with factory() as db:
        rows = db.execute(
            select(Document.id, Document.content).where(Document.usetype == args.usetype)
        ).all()
    random.seed(SEED)
    sample = random.sample(rows, min(args.n, len(rows)))
    print(f"probing {len(sample)} {args.usetype} docs (seed {SEED})\n")

    results = {}
    with httpx.Client() as client:
        for rubric_name, template in RUBRICS.items():
            for thinking in (False, True):
                cell = f"{rubric_name}/{'think' if thinking else 'nothink'}"
                started = time.time()

                def work(row):
                    return score_one(client, template.format(memory=row[1]), thinking)

                with ThreadPoolExecutor(max_workers=SLOTS) as pool:
                    scores = list(pool.map(work, sample))
                s = stats(scores)
                s["seconds"] = time.time() - started
                results[cell] = s
                print(
                    f"{cell:>28}  mean {s['mean']:>5.2f}  at1 {s['frac_at_1']:>5.1%}  "
                    f"distinct {s['distinct']:>2}  entropy {s['entropy_bits']:>4.2f} bits  "
                    f"({s['seconds']:.0f}s)"
                )
                print(f"{'':>28}  dist {s['dist']}")

    print("\n--- verdict inputs ---")
    print("entropy is the number that matters: a term with ~0 bits cannot reorder anything.")
    base = results["ga_poignancy/nothink"]["entropy_bits"]
    for cell, s in results.items():
        print(
            f"  {cell:>28}  {s['entropy_bits']:>4.2f} bits  ({s['entropy_bits'] - base:+.2f} vs GA/nothink)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
