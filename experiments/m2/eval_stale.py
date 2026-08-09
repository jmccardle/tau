"""M2 determination (b): does read-time recency resolve implicit staleness?

Tau-side POLICY. JMFTS supplies the scoring mechanism (hybrid_search's opt-in
recency_weight); the weights, the metric and the verdict are ours.

WHAT IS MEASURED — retrieval only, no LLM, no judge.
    Each STALE instance has a session stating M_old and a later session that implicitly
    invalidates it (M_new). We ask a probing query and ask one question of the ranking:

        does the M_new session outrank the M_old session?

    That is the exact lever M2 adds, isolated. It is a PROXY for the paper's
    answer-quality number (Mem0's 0.40 -> 0.84), not the same measurement: a model can
    still reason badly over correctly-ranked evidence. Generating and judging answers
    is a documented follow-up, deliberately not folded in here — it would confound the
    retrieval question with a generation question.

ABLATIONS
    Baseline is plain vector search (recency_weight=0), which is byte-for-byte the
    behaviour that shipped before M2. Everything else varies alpha (term strength) and
    the decay half-life. We report the grid rather than a tuned single number, because
    a single configuration cannot distinguish "recency works" from "we found a weight
    that flatters this corpus".

TIME REFERENCE
    `now` is the instance's LAST session timestamp, not wall-clock now: the agent is
    being asked at the end of its own conversation. Real now is years past the corpus,
    which would decay every session to ~0 and make the term measure nothing. This is
    why hybrid_search takes an explicit `now`.

Run ON midlife:
    /home/john/jmfts-gpudev-env.sh eval_stale.py [--limit N]
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone

from jmfts_core.database import get_session_factory
from jmfts_core.repositories.search import SearchRepository

MANIFEST = "/fast/datasets/m2/stale_manifest.json"
OUT = "/fast/datasets/m2/stale_results.json"

# (alpha, halflife_days). alpha=0 is the baseline: the rerank is skipped entirely.
GRID = [
    (0.0, None),
    (0.5, 90.0),
    (1.0, 90.0),
    (2.0, 90.0),
    (1.0, 30.0),
    (1.0, 180.0),
    (1.0, 365.0),
    (5.0, 180.0),
]

DIMS = ["dim1_query", "dim2_query", "dim3_query"]


def parse_ts(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def rank_of(results, doc_id):
    """1-based rank, or None if the doc never surfaced in the candidate set."""
    for i, r in enumerate(results, 1):
        if r.document.id == doc_id:
            return i
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--manifest", default=MANIFEST)
    args = ap.parse_args()

    with open(args.manifest) as fh:
        manifest = json.load(fh)
    if args.limit:
        manifest = manifest[: args.limit]

    factory = get_session_factory()
    # tallies[(alpha, halflife, dim)] -> counters
    tallies = defaultdict(lambda: defaultdict(int))
    per_instance = []

    with factory() as db:
        repo = SearchRepository(db)
        for n, inst in enumerate(manifest, 1):
            now = parse_ts(inst["now"])
            for dim in DIMS:
                query = inst["probing_queries"][dim]
                for alpha, halflife in GRID:
                    kwargs = {}
                    if alpha:
                        kwargs = {"recency_weight": alpha, "recency_halflife_days": halflife, "now": now}
                    results = repo.hybrid_search(
                        query,
                        limit=50,  # the whole haystack, so ranks are always comparable
                        methods=["vector"],
                        parent_id=inst["root_id"],
                        **kwargs,
                    )
                    r_new = rank_of(results, inst["new_doc_id"])
                    r_old = rank_of(results, inst["old_doc_id"])
                    key = (alpha, halflife, dim)
                    t = tallies[key]
                    t["n"] += 1
                    if r_new is None or r_old is None:
                        # Both live under the same root and limit spans the haystack,
                        # so this should be unreachable. Count it rather than assume.
                        t["missing"] += 1
                        continue
                    if r_new < r_old:
                        t["new_over_old"] += 1
                    t["sum_rank_new"] += r_new
                    t["sum_rank_old"] += r_old
                    if r_new == 1:
                        t["new_at_1"] += 1
                    if r_new <= 5:
                        t["new_at_5"] += 1
                    per_instance.append(
                        {"uid": inst["uid"], "type": inst["type"], "dim": dim,
                         "alpha": alpha, "halflife": halflife, "rank_new": r_new, "rank_old": r_old}
                    )
            if n % 25 == 0:
                print(f"  {n}/{len(manifest)} instances", flush=True)

    rows = []
    for (alpha, halflife, dim), t in sorted(tallies.items()):
        n = t["n"] or 1
        rows.append({
            "alpha": alpha, "halflife": halflife, "dim": dim, "n": t["n"],
            "new_over_old": t["new_over_old"] / n,
            "new_at_1": t["new_at_1"] / n,
            "new_at_5": t["new_at_5"] / n,
            "mean_rank_new": t["sum_rank_new"] / n,
            "mean_rank_old": t["sum_rank_old"] / n,
            "missing": t["missing"],
        })

    with open(OUT, "w") as fh:
        json.dump({"summary": rows, "per_instance": per_instance}, fh)

    print()
    print(f"{'alpha':>6} {'half-life':>10} {'dim':>5} {'n':>5} "
          f"{'new>old':>8} {'new@1':>7} {'new@5':>7} {'rank_new':>9} {'rank_old':>9}")
    print("-" * 78)
    for r in rows:
        hl = "-" if r["halflife"] is None else f"{r['halflife']:.0f}d"
        dim = r["dim"].replace("_query", "")
        print(f"{r['alpha']:>6.1f} {hl:>10} {dim:>5} {r['n']:>5} "
              f"{r['new_over_old']:>8.3f} {r['new_at_1']:>7.3f} {r['new_at_5']:>7.3f} "
              f"{r['mean_rank_new']:>9.2f} {r['mean_rank_old']:>9.2f}")
    print(f"\nfull results: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
