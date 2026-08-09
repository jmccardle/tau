"""Importance at OBSERVATION granularity: score RAPTOR summaries, not raw turns.

The turn-level importance leg collapsed — 96.1% of 5,882 LoCoMo turns scored 1 — because a
raw dialogue turn genuinely is mundane. Generative Agents does not score raw turns; it
scores *observations*, the synthesized memory items an agent forms. M1 already built us an
observation layer: RAPTOR cluster summaries.

Two questions, in order:

  1. DOES IMPORTANCE DISCRIMINATE AT ALL at this granularity? Compare the summary-score
     distribution's entropy against the turn-level baseline (~0 bits). If summaries also
     collapse, importance is dead for this corpus regardless of granularity, and the
     ranking experiment below is not worth running.

  2. DOES THAT SIGNAL HELP LEAF RETRIEVAL? LoCoMo's evidence is turn-level, so a summary
     cannot itself be a correct answer. We therefore PROPAGATE each summary's importance
     down to its member turns: a turn inherits the importance of the observation it belongs
     to. That is the honest way to let an observation-level signal act on a leaf-level task,
     and it is also a real design proposal, not just a measurement trick.

Summaries do not pollute retrieval: usetype='summary' is in JMFTS's default exclude_types,
so the recall task still ranks turns only.

Run ON midlife, AFTER the LoCoMo corpus is ingested:
    /home/john/jmfts-gpudev-env.sh raptor_importance.py [--rubric ga|retrieval] [--thinking]
"""

import argparse
import json
import math
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import select

from jmfts_core.database import get_session_factory
from jmfts_core.models.document import Document

API = "http://127.0.0.1:8101"
TOKEN = "jmfts-gpudev-local"
LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"
SLOTS = 2
GRAMMAR = 'root ::= "10" | [1-9]'
MANIFEST = "/fast/datasets/m2/locomo_manifest.json"

RUBRIC_GA = (
    "On the scale of 1 to 10, where 1 is purely mundane (e.g., brushing teeth, making bed) "
    "and 10 is extremely poignant (e.g., a break up, college acceptance), rate the likely "
    "poignancy of the following piece of memory.\nMemory: {memory}\nRating:"
)
RUBRIC_RETRIEVAL = (
    "Rate on a scale of 1 to 10 how much durable, specific, factual information this "
    "episode contains about the speaker's life — the kind a question asked months later "
    "might depend on.\n"
    "1 = pure social filler, no facts.\n"
    "3 = vague or generic, nothing a question could pin down.\n"
    "5 = a preference or opinion, but no specifics.\n"
    "8 = a concrete event, plan, or relationship with some detail.\n"
    "10 = dense specifics: names, dates, places, numbers, or commitments.\n"
    "Episode: {memory}\nRating:"
)


def entropy(scores):
    dist = Counter(scores)
    n = len(scores)
    return -sum((c / n) * math.log2(c / n) for c in dist.values())


# A summary is far longer than a turn, so its reasoning is longer too. The server runs
# -c 16384 across -np 2 = 8192 tokens per slot, so prompt + budget must fit under that;
# 6000 leaves comfortable room for a ~1-2k-token summary prompt. 3000 was not enough and
# the guard below caught it rather than letting a truncated call become a fake score.
THINKING_BUDGET = 6000


def score_one(client, prompt, thinking):
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "grammar": GRAMMAR,
        "temperature": 0,
        "max_tokens": THINKING_BUDGET if thinking else 4,
    }
    if not thinking:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    resp = client.post(LLM_URL, json=body, timeout=600.0)
    resp.raise_for_status()
    payload = resp.json()
    msg = payload["choices"][0]["message"]
    content = (msg["content"] or "").strip()
    if not content:
        # Never coerce this into a rating. An empty content means the reasoning never
        # reached the grammar-bound answer, and a fabricated default would poison the very
        # term under test.
        raise RuntimeError(
            "empty content — reasoning truncated before the grammar-bound answer "
            f"(finish_reason={payload['choices'][0].get('finish_reason')}, "
            f"reasoning_tokens~{len(msg.get('reasoning_content') or '') // 4})"
        )
    return int(content)


def build_raptor(root_ids):
    """RAPTOR each conversation. Idempotent-ish: skips a root that already has summaries."""
    factory = get_session_factory()
    with httpx.Client(headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        for root_id in root_ids:
            with factory() as db:
                existing = db.execute(
                    select(Document.id).where(
                        Document.usetype == "summary", Document.path.contains([root_id])
                    )
                ).first()
            if existing:
                print(f"  root {root_id}: summaries already present, skipping", flush=True)
                continue
            started = time.time()
            resp = client.post(f"{API}/documents/{root_id}/raptor", json={"max_depth": 3}, timeout=1800.0)
            resp.raise_for_status()
            body = resp.json()
            layers = body.get("layers", [])
            made = sum(len(x.get("summary_ids", []) or []) for x in layers) if layers else "?"
            print(f"  root {root_id}: {len(layers)} layers, {made} summaries "
                  f"({time.time() - started:.0f}s)", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rubric", choices=["ga", "retrieval"], default="retrieval")
    ap.add_argument("--thinking", action="store_true")
    ap.add_argument("--skip-raptor", action="store_true")
    args = ap.parse_args()
    template = RUBRIC_GA if args.rubric == "ga" else RUBRIC_RETRIEVAL

    with open(MANIFEST) as fh:
        manifest = json.load(fh)
    root_ids = [c["root_id"] for c in manifest]

    if not args.skip_raptor:
        print(f"RAPTOR over {len(root_ids)} conversations")
        build_raptor(root_ids)

    factory = get_session_factory()
    with factory() as db:
        summaries = db.execute(
            select(Document.id, Document.content, Document.structured_content).where(
                Document.usetype == "summary"
            )
        ).all()
    if not summaries:
        raise RuntimeError("no summaries found — RAPTOR produced nothing to score")

    print(f"\nscoring {len(summaries)} summaries "
          f"(rubric={args.rubric}, thinking={args.thinking})")
    started = time.time()
    with httpx.Client() as client:
        def work(row):
            return row[0], score_one(client, template.format(memory=row[1]), args.thinking)
        with ThreadPoolExecutor(max_workers=SLOTS) as pool:
            scored = dict(pool.map(work, summaries))
    print(f"  scored in {time.time() - started:.0f}s")

    vals = list(scored.values())
    dist = Counter(vals)
    ent = entropy(vals)
    print(f"\nsummary-level distribution: mean {sum(vals)/len(vals):.2f}  "
          f"at1 {dist[1]/len(vals):.1%}  distinct {len(dist)}  entropy {ent:.2f} bits")
    for r in sorted(dist):
        print(f"  {r:>2}: {dist[r]:>4}  {'#' * int(50 * dist[r] / max(dist.values()))}")
    print("\n(turn-level GA/nothink baseline was ~0.29 bits, 96.1% at 1)")

    # Propagate: each member turn inherits its summary's importance. Where RAPTOR nests
    # summaries, a turn can sit under several; take the MAX — an observation being important
    # at any level is a claim about the turn, and averaging would wash it out.
    sc_by_id = {row[0]: (row[2] or {}) for row in summaries}
    propagated: dict[int, int] = {}
    for sid, score in scored.items():
        for member in sc_by_id.get(sid, {}).get("member_ids", []) or []:
            propagated[member] = max(propagated.get(member, 0), score)
    print(f"\npropagated to {len(propagated)} member documents")

    with factory() as db:
        # Only turns carry the propagated value; summaries keep their own.
        turn_ids = [
            r[0] for r in db.execute(
                select(Document.id).where(Document.usetype == "locomo_turn")
            ).all()
        ]
        # CLEAR FIRST. The turns still hold the previous turn-level GA/nothink scores
        # (96% at 1). Writing propagated values over the top would leave every turn RAPTOR
        # did not reach carrying a stale rating from a different experiment — the ablation
        # would silently be measuring a mixture of the two. Absent importance is neutral by
        # definition, which is the honest state for a turn no observation covers.
        for doc_id in turn_ids:
            doc = db.get(Document, doc_id)
            sc = dict(doc.structured_content or {})
            if "importance" in sc:
                sc.pop("importance")
                doc.structured_content = sc
        db.commit()

        turn_set = set(turn_ids)
        hit = 0
        for doc_id, score in propagated.items():
            if doc_id not in turn_set:
                continue
            doc = db.get(Document, doc_id)
            doc.structured_content = {**(doc.structured_content or {}), "importance": score}
            hit += 1
        db.commit()
    coverage = hit / max(len(turn_ids), 1)
    print(f"cleared stale importance from {len(turn_ids)} turns, then wrote propagated "
          f"importance to {hit} ({coverage:.1%} coverage)")
    if coverage < 0.5:
        print(f"  WARNING: {1 - coverage:.1%} of turns are outside any scored observation "
              f"and score neutral. Read the ablation with that in mind.")

    with open("/fast/datasets/m2/raptor_importance.json", "w") as fh:
        json.dump({"summary_scores": {str(k): v for k, v in scored.items()},
                   "entropy_bits": ent,
                   "propagated": {str(k): v for k, v in propagated.items()}}, fh)
    print("\nnow re-run: locomo_recall.py --eval-only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
