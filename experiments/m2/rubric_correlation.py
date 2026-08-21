"""Do poignancy and retrievability rank the same turns the same way?

WHY (the calibration guard)
    The 2x2 probe showed thinking lifts rating entropy 0.29 -> 2.45 bits while the rubric
    barely moves it (+0.11). Entropy alone cannot tell "found real structure" apart from
    "spread the ratings around more". Before "score with thinking enabled" becomes library
    guidance, we need evidence the spread is SIGNAL.

    Rank correlation between two DIFFERENT rubrics on the SAME turns is that evidence, and
    it is a two-sided test:
      high rho  => both rubrics recover the same underlying ordering. The spread is a
                   property of the turns, not of the prompt. (It would also mean the rubric
                   is nearly irrelevant, consistent with the +0.11 bits result.)
      low rho   => the rubrics disagree, so at most one tracks anything real and the
                   "thinking finds signal" claim is unsupported.

    Interpreted against the NOISE FLOOR, not against 1.0. The budget sweep established that
    this scorer is not deterministic at temperature 0 (unbinding budgets 2000-5000 agree
    with an identical reference config at only rho 0.83-0.92 / 70-82% exact — MoE routing
    under continuous batching at -np 2). So rho >= ~0.83 means the two rubrics are as close
    as one rubric is TO ITSELF, which is the strongest agreement observable here.

Run ON midlife:
    /home/john/jmfts-gpudev-env.sh rubric_correlation.py [--n 120]
"""

import argparse
import json
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

LLM = "http://127.0.0.1:8080/v1/chat/completions"
SLOTS = 2
GRAMMAR = 'root ::= "10" | [1-9]'
SEED = 20260717  # same seed as budget_sweep => same turns => cross-comparable
NOISE_FLOOR = (0.83, 0.92)  # measured: unbinding-budget cells vs reference

RUBRIC_GA = (
    "On the scale of 1 to 10, where 1 is purely mundane (e.g., brushing teeth, making bed) "
    "and 10 is extremely poignant (e.g., a break up, college acceptance), rate the likely "
    "poignancy of the following piece of memory.\nMemory: {memory}\nRating:"
)
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


def score(client, prompt):
    r = client.post(
        LLM,
        json={
            "messages": [{"role": "user", "content": prompt}],
            "grammar": GRAMMAR,
            "temperature": 0,
            "max_tokens": 6000,
        },
        timeout=600.0,
    )
    r.raise_for_status()
    c = (r.json()["choices"][0]["message"]["content"] or "").strip()
    if not c:
        raise RuntimeError("empty content — reasoning truncated before the answer")
    return int(c)


def spearman(a, b):
    def rank(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        r = [0.0] * len(xs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    ra, rb = rank(a), rank(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else float("nan")


def entropy(vals):
    d, n = Counter(vals), len(vals)
    return -sum((c / n) * math.log2(c / n) for c in d.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120)
    args = ap.parse_args()

    factory = get_session_factory()
    with factory() as db:
        rows = db.execute(
            select(Document.id, Document.content).where(Document.usetype == "locomo_turn")
        ).all()
    random.seed(SEED)
    sample = random.sample(rows, args.n)
    print(f"rubric correlation on {len(sample)} turns (seed {SEED}, thinking on)\n")

    out = {}
    with httpx.Client() as client:
        for name, tmpl in (("ga_poignancy", RUBRIC_GA), ("retrievability", RUBRIC_RETRIEVAL)):
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=SLOTS) as pool:
                out[name] = list(
                    pool.map(lambda r: score(client, tmpl.format(memory=r[1])), sample)
                )
            print(
                f"  {name:>16}: entropy {entropy(out[name]):.2f}  "
                f"mean {sum(out[name]) / len(sample):.2f}  ({time.time() - t0:.0f}s)",
                flush=True,
            )

    a, b = out["ga_poignancy"], out["retrievability"]
    rho = spearman(a, b)
    exact = sum(1 for x, y in zip(a, b) if x == y) / len(a)
    within1 = sum(1 for x, y in zip(a, b) if abs(x - y) <= 1) / len(a)

    print(f"\nspearman rho          {rho:.2f}")
    print(f"exact agreement       {exact:.0%}")
    print(f"within +/-1           {within1:.0%}")
    print(
        f"noise floor (rho)     {NOISE_FLOOR[0]:.2f}-{NOISE_FLOOR[1]:.2f}  "
        f"(same config vs itself, from budget_sweep)"
    )
    if rho >= NOISE_FLOOR[0]:
        print(
            "\n=> AT/ABOVE the noise floor: the two rubrics recover the same ordering, so the\n"
            "   2.45-bit spread is a property of the turns, not of the prompt. Signal, not\n"
            "   spread-noise. Consistent with the rubric being worth only +0.11 bits."
        )
    elif rho >= 0.5:
        print(
            "\n=> BELOW the noise floor but positive: the rubrics partly agree. The spread is\n"
            "   not pure noise, but rubric choice does move the ordering — so 'use thinking'\n"
            "   is safe guidance while 'the rubric does not matter' is NOT."
        )
    else:
        print(
            "\n=> LOW: the rubrics disagree. At most one tracks anything real, and the\n"
            "   'thinking finds signal' claim is UNSUPPORTED by this evidence."
        )

    with open("/fast/datasets/m2/rubric_correlation.json", "w") as fh:
        json.dump(
            {
                "ids": [r[0] for r in sample],
                "ga": a,
                "retrieval": b,
                "rho": rho,
                "exact": exact,
                "within1": within1,
            },
            fh,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
