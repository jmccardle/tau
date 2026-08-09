"""Negative control: shuffle event_time within each STALE haystack, re-run the recency arm.

WHY THIS EXISTS
    `new>old` is partly tautological for a recency term: M_new is BY CONSTRUCTION later than
    M_old, so any recency boost mechanically improves the metric. Read alone it cannot fail,
    which means the 0.11 -> 0.91 result cannot currently be stated without hedging.

    This is the control that removes the hedge. Permute each instance's 50 event_times among
    its 50 sessions (same multiset of timestamps, same decay curve, same everything —
    destroying ONLY the correspondence between a session's content and its time). The
    recency term still fires exactly as hard; it simply now points at a random session.

PREDICTION, STATED BEFORE RUNNING (so it cannot be rationalised after)
    new>old should collapse to ~0.5 (a coin flip between two arbitrary sessions), NOT to the
    0.11 baseline. That is the key subtlety: the baseline's 0.11 comes from the query being
    lexically drawn to M_old; shuffling does not undo that pull, it just makes the recency
    boost uninformative. So:
      ~0.50  => the term reads real temporal signal. The metric is measuring what we claim.
      ~0.91  => the metric is pure tautology and the headline result is an artifact.
      ~0.11  => the term does nothing at all and something else produced the lift.

    Anything near 0.91 invalidates the M2 staleness determination. This control is cheap and
    it is the one that can actually falsify the finding.

Restores the true timestamps afterwards, so the corpus is left exactly as found.

Run ON midlife:
    /home/john/jmfts-gpudev-env.sh shuffle_control.py
"""

import json
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select

from jmfts_core.database import get_session_factory
from jmfts_core.models.document import Document
from jmfts_core.repositories.search import SearchRepository

MANIFEST = "/fast/datasets/m2/stale_manifest.json"
SEED = 20260717
DIMS = ["dim1_query", "dim2_query", "dim3_query"]
ALPHA, HALFLIFE = 1.0, 365.0


def parse_ts(raw):
    return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def main() -> int:
    with open(MANIFEST) as fh:
        manifest = json.load(fh)
    factory = get_session_factory()
    rng = random.Random(SEED)

    # --- snapshot the truth, then permute within each instance ---
    original: dict[int, datetime] = {}
    with factory() as db:
        for inst in manifest:
            ids = inst["session_ids"]
            times = []
            for doc_id in ids:
                doc = db.get(Document, doc_id)
                original[doc_id] = doc.event_time
                times.append(doc.event_time)
            shuffled = times[:]
            rng.shuffle(shuffled)
            for doc_id, ts in zip(ids, shuffled):
                db.get(Document, doc_id).event_time = ts
        db.commit()
    print(f"permuted event_time within {len(manifest)} instances "
          f"({len(original)} docs); timestamp multiset per instance unchanged")

    # --- re-run the recency arm against the shuffled clock ---
    tallies = defaultdict(lambda: defaultdict(float))
    try:
        with factory() as db:
            repo = SearchRepository(db)
            for n, inst in enumerate(manifest, 1):
                now = parse_ts(inst["now"])
                for dim in DIMS:
                    query = inst["probing_queries"][dim]
                    for label, kw in (
                        ("baseline", {}),
                        ("recency", {"recency_weight": ALPHA,
                                     "recency_halflife_days": HALFLIFE, "now": now}),
                    ):
                        results = repo.hybrid_search(
                            query, limit=50, methods=["vector"],
                            parent_id=inst["root_id"], **kw,
                        )
                        ranks = {r.document.id: i for i, r in enumerate(results, 1)}
                        rn, ro = ranks.get(inst["new_doc_id"]), ranks.get(inst["old_doc_id"])
                        if rn is None or ro is None:
                            continue
                        t = tallies[(label, dim)]
                        t["n"] += 1
                        t["win"] += 1.0 if rn < ro else 0.0
                        t["new_at_5"] += 1.0 if rn <= 5 else 0.0
                if n % 100 == 0:
                    print(f"  {n}/{len(manifest)}", flush=True)
    finally:
        # Always restore, even on failure: a corpus left with shuffled timestamps would
        # silently corrupt every subsequent experiment on this instance.
        with factory() as db:
            for doc_id, ts in original.items():
                db.get(Document, doc_id).event_time = ts
            db.commit()
        print("restored true event_time")

    print(f"\n{'arm':>10} {'dim':>5} {'n':>5} {'new>old':>9} {'new@5':>8}")
    print("-" * 42)
    for (label, dim), t in sorted(tallies.items()):
        n = t["n"] or 1
        print(f"{label:>10} {dim.replace('_query',''):>5} {int(t['n']):>5} "
              f"{t['win']/n:>9.3f} {t['new_at_5']/n:>8.3f}")
    print("\nreference (true clock): baseline new>old ~0.11/0.09/0.44, "
          "recency a=1/365d ~0.91/0.95/0.92")
    print("expected here if the finding is real: recency ~0.50, NOT ~0.91")
    return 0


if __name__ == "__main__":
    sys.exit(main())
