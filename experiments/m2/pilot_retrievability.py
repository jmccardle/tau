"""Pilot: does a retrievability-scored importance term actually HELP recall?

THE CORRECTION THIS TESTS
    Determination (a) concluded "importance hurts recall, so it is a write-time signal, not
    a read-time one". That conclusion was reached using ONLY the Generative-Agents poignancy
    rubric — which validity.py then measured at AUC 0.551 against evidence-ness, i.e. it is
    ~random with respect to the retrieval task. Boosting a random prior can only displace
    query-relevant results, which is exactly the damage we observed. The experiment was
    sound; the term it tested was not the term worth testing.

    The retrievability rubric scores AUC 0.840 on the same turns. It knows which turns are
    evidence. A query-independent prior that is 0.84-predictive of evidence-ness is a
    completely different proposition, and it has never been through the recall ablation.

PREDICTION, STATED BEFORE RUNNING
    If AUC 0.84 transfers, beta>0 should IMPROVE recall over baseline — the first thing in
    M2 to beat plain vector search. If it still hurts, then the read-time conclusion is
    robust to the rubric, and "importance is a write-time signal" survives a much stronger
    test than it has faced so far.

Pilot scope: ONE conversation, so this costs ~25 minutes instead of ~5.5 hours. It is a
directional check to decide whether the full-corpus run is worth it, not a determination.
Scores are written only for this conversation's turns; recall is evaluated only over its
questions, so no other conversation is contaminated.

Run ON midlife (use --budget to force the end-of-thinking tag; 1000 measured within the
nondeterminism noise floor of unrestricted):
    /home/john/jmfts-gpudev-env.sh pilot_retrievability.py
"""

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
from sqlalchemy import select

from jmfts_core.database import get_session_factory
from jmfts_core.models.document import Document
from jmfts_core.repositories.search import SearchRepository

LLM = "http://127.0.0.1:8080/v1/chat/completions"
SLOTS = 2
GRAMMAR = 'root ::= "10" | [1-9]'
MANIFEST = "/fast/datasets/m2/locomo_manifest.json"
KS = [1, 5, 10, 20]
BETAS = [0.0, 0.5, 1.0, 2.0, 5.0]

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


def score(client, text):
    r = client.post(LLM, json={
        "messages": [{"role": "user", "content": RUBRIC_RETRIEVAL.format(memory=text)}],
        "grammar": GRAMMAR, "temperature": 0, "max_tokens": 6000,
    }, timeout=600.0)
    r.raise_for_status()
    c = (r.json()["choices"][0]["message"]["content"] or "").strip()
    if not c:
        raise RuntimeError("empty content — run the server with --reasoning-budget so the "
                           "end-of-thinking tag is forced")
    return int(c)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conv", type=int, default=0, help="index into the manifest")
    args = ap.parse_args()

    with open(MANIFEST) as fh:
        manifest = json.load(fh)
    conv = manifest[args.conv]
    root_id = conv["root_id"]
    print(f"pilot on {conv['sample_id']} (root {root_id}), "
          f"{len(conv['questions'])} scorable questions")

    factory = get_session_factory()
    with factory() as db:
        turns = db.execute(
            select(Document.id, Document.content).where(
                Document.usetype == "locomo_turn", Document.path.contains([root_id])
            )
        ).all()
    print(f"scoring {len(turns)} turns with the retrievability rubric (thinking on)")

    started = time.time()
    with httpx.Client() as client:
        with ThreadPoolExecutor(max_workers=SLOTS) as pool:
            scores = dict(zip([t[0] for t in turns],
                              pool.map(lambda t: score(client, t[1]), turns)))
    print(f"  scored in {time.time() - started:.0f}s; "
          f"distribution {dict(sorted(Counter(scores.values()).items()))}")

    # Overwrite this conversation's importance with the retrievability scores. Other
    # conversations keep whatever they have; we only evaluate this one.
    with factory() as db:
        for doc_id, s in scores.items():
            doc = db.get(Document, doc_id)
            doc.structured_content = {**(doc.structured_content or {}), "importance": s}
        db.commit()

    now = datetime.fromisoformat(conv["now"])
    tallies = defaultdict(lambda: defaultdict(float))
    with factory() as db:
        repo = SearchRepository(db)
        for q in conv["questions"]:
            for beta in BETAS:
                kw = {"importance_weight": beta} if beta else {}
                results = repo.hybrid_search(
                    q["question"], limit=max(KS), methods=["vector"],
                    parent_id=root_id, **kw,
                )
                got = [r.document.id for r in results]
                want = set(q["evidence_doc_ids"])
                tallies[beta]["n"] += 1
                for k in KS:
                    tallies[beta][f"recall@{k}"] += len(want & set(got[:k])) / len(want)

    print(f"\n{'beta':>6} {'n':>5} " + " ".join(f"{'R@'+str(k):>9}" for k in KS))
    print("-" * (13 + 10 * len(KS)))
    base = None
    for beta in BETAS:
        t = tallies[beta]
        n = t["n"] or 1
        cells = " ".join(f"{t['recall@'+str(k)]/n:>9.3f}" for k in KS)
        print(f"{beta:>6.1f} {int(t['n']):>5} {cells}")
        if beta == 0.0:
            base = t["recall@5"] / n
    best = max((tallies[b]["recall@5"] / (tallies[b]["n"] or 1)) for b in BETAS if b)
    print(f"\nbaseline R@5 {base:.3f} | best importance R@5 {best:.3f} | "
          f"delta {best - base:+.3f}")
    print("GA-poignancy importance on the full corpus was: R@5 0.602 -> 0.593 (beta=1), "
          "i.e. it never helped.")
    print("If delta is positive here, determination (a) needs re-running with this rubric.")

    with open(f"/fast/datasets/m2/pilot_retrievability_{args.conv}.json", "w") as fh:
        json.dump({"scores": {str(k): v for k, v in scores.items()},
                   "recall": {str(b): {k: tallies[b][f"recall@{k}"] / (tallies[b]["n"] or 1)
                                       for k in KS} for b in BETAS}}, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
