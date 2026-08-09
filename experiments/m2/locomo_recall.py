"""M2 determination (a): does the scoring rerank beat plain vector search on recall?

Tau-side POLICY. Ingests LoCoMo and measures evidence recall@k over the same alpha x
half-life grid as the staleness leg, so the two determinations are directly comparable.

WHY THIS LEG MATTERS AS A CONTRAST, NOT A CONFIRMATION
    STALE rewards recency: a later observation invalidates an earlier one, so promoting
    recent evidence is the whole point. LoCoMo asks about specific past events ("When
    did Caroline go to the LGBTQ support group?") where the answer is a deliberately OLD
    turn. A recency term should therefore be neutral-to-harmful here. If recency helps
    both, that is evidence the metric is measuring something other than what we think.
    A term that helps one and hurts the other is the honest, expected shape — and it is
    what tells us recency must be a per-query policy, not a global default.

Granularity: one document per dialogue turn (LoCoMo's evidence ids are turn ids,
"D1:3" -> dia_id). All turns in a session share that session's event_time; intra-session
order is carried by CR-1 `position` (sequential=True) rather than by inventing
sub-timestamps the corpus does not have.

Category 5 is LoCoMo's adversarial/unanswerable class and questions may carry no
evidence; both are excluded from recall, since there is no correct document to retrieve.

Run ON midlife:
    /home/john/jmfts-gpudev-env.sh locomo_recall.py [--limit N] [--ingest/--eval-only]
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

from jmfts_core.database import get_session_factory
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import SearchRepository

LOCOMO = "/fast/datasets/m2/locomo10.json"
MANIFEST = "/fast/datasets/m2/locomo_manifest.json"
OUT = "/fast/datasets/m2/locomo_results.json"
ROOT_USETYPE = "locomo_conv"
TURN_USETYPE = "locomo_turn"

# (recency alpha, half-life days, importance beta). alpha=beta=0 is the baseline: the
# rerank is skipped entirely and this is plain vector search.
GRID = [
    (0.0, None, 0.0),
    # recency leg
    (0.5, 90.0, 0.0),
    (1.0, 90.0, 0.0),
    (2.0, 90.0, 0.0),
    (1.0, 30.0, 0.0),
    (1.0, 180.0, 0.0),
    (1.0, 365.0, 0.0),
    (5.0, 180.0, 0.0),
    # importance leg — isolated, so a null result cannot be blamed on recency
    (0.0, None, 0.5),
    (0.0, None, 1.0),
    (0.0, None, 2.0),
    (0.0, None, 5.0),
    # both, at each leg's best solo setting
    (1.0, 365.0, 1.0),
]
KS = [1, 5, 10, 20]
ADVERSARIAL_CATEGORY = 5


def parse_locomo_date(raw: str) -> datetime:
    """'1:56 pm on 8 May, 2023' -> aware UTC.

    Strict: an unparseable date raises rather than falling back to a default, because a
    silently-defaulted timestamp is precisely the failure this whole experiment exists
    to detect.
    """
    return datetime.strptime(raw.strip(), "%I:%M %p on %d %B, %Y").replace(tzinfo=timezone.utc)


def ingest(corpus, factory):
    manifest = []
    for conv in corpus:
        c = conv["conversation"]
        session_keys = sorted(
            (k for k in c if re.fullmatch(r"session_\d+", k)),
            key=lambda k: int(k.split("_")[1]),
        )
        with factory() as db:
            repo = DocumentRepository(db)
            root = repo.create(
                title=f"LoCoMo {conv['sample_id']}",
                usetype=ROOT_USETYPE,
                structured_content={"sample_id": conv["sample_id"]},
                auto_embed=False,
            )
            db.flush()
            dia_to_doc = {}
            last_ts = None
            for skey in session_keys:
                ts = parse_locomo_date(c[f"{skey}_date_time"])
                last_ts = ts if last_ts is None or ts > last_ts else last_ts
                for turn in c[skey]:
                    child = repo.create(
                        title=f"{skey} {turn['dia_id']} ({turn['speaker']})",
                        content=f"{turn['speaker']}: {turn['text']}",
                        parent_id=root.id,
                        usetype=TURN_USETYPE,
                        structured_content={"dia_id": turn["dia_id"], "session": skey},
                        event_time=ts,
                        auto_embed=True,
                        embed_tokens=False,
                        sequential=True,
                    )
                    dia_to_doc[turn["dia_id"]] = child.id
            questions = []
            for q in conv["qa"]:
                evidence = q.get("evidence") or []
                if q.get("category") == ADVERSARIAL_CATEGORY or not evidence:
                    continue
                doc_ids = [dia_to_doc[e] for e in evidence if e in dia_to_doc]
                if not doc_ids:
                    continue
                questions.append({
                    "question": q["question"],
                    "category": q["category"],
                    "evidence": evidence,
                    "evidence_doc_ids": doc_ids,
                    "unresolved_evidence": [e for e in evidence if e not in dia_to_doc],
                })
            manifest.append({
                "sample_id": conv["sample_id"],
                "root_id": root.id,
                "questions": questions,
                "now": last_ts.isoformat(),
            })
            db.commit()
        print(f"  ingested {conv['sample_id']}: {len(dia_to_doc)} turns, "
              f"{len(questions)} scorable questions", flush=True)
    with open(MANIFEST, "w") as fh:
        json.dump(manifest, fh)
    return manifest


def evaluate(manifest, factory):
    tallies = defaultdict(lambda: defaultdict(float))
    # Same tallies, split by LoCoMo question category. The STALE breakdown showed the
    # recency trade tracks QUERY TYPE, not corpus, so the equivalent split here is the
    # check on whether that generalises: if some LoCoMo category also rewards recency,
    # "recency is bad for recall" is really "recency is bad for most recall questions".
    by_cat = defaultdict(lambda: defaultdict(float))
    with factory() as db:
        repo = SearchRepository(db)
        for conv in manifest:
            now = datetime.fromisoformat(conv["now"])
            for q in conv["questions"]:
                for alpha, halflife, beta in GRID:
                    kwargs = {}
                    if alpha:
                        kwargs = {"recency_weight": alpha, "recency_halflife_days": halflife, "now": now}
                    if beta:
                        kwargs["importance_weight"] = beta
                    results = repo.hybrid_search(
                        q["question"], limit=max(KS), methods=["vector"],
                        parent_id=conv["root_id"], **kwargs,
                    )
                    got = [r.document.id for r in results]
                    want = set(q["evidence_doc_ids"])
                    key = (alpha, halflife, beta)
                    tallies[key]["n"] += 1
                    ckey = (alpha, halflife, beta, q["category"])
                    by_cat[ckey]["n"] += 1
                    for k in KS:
                        hit = len(want & set(got[:k]))
                        tallies[key][f"recall@{k}"] += hit / len(want)
                        by_cat[ckey][f"recall@{k}"] += hit / len(want)
    rows = []
    for (alpha, halflife, beta), t in sorted(tallies.items()):
        n = t["n"] or 1
        row = {"alpha": alpha, "halflife": halflife, "beta": beta, "n": int(t["n"])}
        for k in KS:
            row[f"recall@{k}"] = t[f"recall@{k}"] / n
        rows.append(row)

    print("\n--- by LoCoMo question category (baseline vs best recency vs importance) ---")
    cats = sorted({k[3] for k in by_cat})
    print(f"{'config':>14} " + " ".join(f"{'cat'+str(c):>10}" for c in cats))
    print("-" * (15 + 11 * len(cats)))
    for alpha, hl, beta in [(0.0, None, 0.0), (1.0, 365.0, 0.0), (0.0, None, 1.0)]:
        label = "baseline" if not alpha and not beta else (
            f"a={alpha:g}/{hl:g}d" if alpha else f"b={beta:g}")
        cells = []
        for c in cats:
            t = by_cat[(alpha, hl, beta, c)]
            n = t["n"] or 1
            cells.append(f"{t['recall@5']/n:>10.3f}")
        print(f"{label:>14} " + " ".join(cells))
    print(f"{'n':>14} " + " ".join(
        f"{int(by_cat[(0.0, None, 0.0, c)]['n']):>10}" for c in cats))
    print("(R@5. If any category prefers recency, 'recency hurts recall' is too coarse.)")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--eval-only", action="store_true")
    args = ap.parse_args()

    factory = get_session_factory()
    if args.eval_only:
        with open(MANIFEST) as fh:
            manifest = json.load(fh)
    else:
        with open(LOCOMO) as fh:
            corpus = json.load(fh)
        if args.limit:
            corpus = corpus[: args.limit]
        manifest = ingest(corpus, factory)

    rows = evaluate(manifest, factory)
    with open(OUT, "w") as fh:
        json.dump(rows, fh)

    print()
    header = (f"{'alpha':>6} {'half-life':>10} {'beta':>6} {'n':>5} "
              + " ".join(f"{'R@'+str(k):>9}" for k in KS))
    print(header)
    print("-" * len(header))
    for r in rows:
        hl = "-" if r["halflife"] is None else f"{r['halflife']:.0f}d"
        cells = " ".join(f"{r['recall@'+str(k)]:>9.3f}" for k in KS)
        print(f"{r['alpha']:>6.1f} {hl:>10} {r['beta']:>6.1f} {r['n']:>5} {cells}")
    print(f"\nfull results: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
