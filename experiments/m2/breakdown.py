"""Subgroup breakdown of the M2 results. Pure re-analysis — no LLM, no retrieval.

Decides whether the recency trade is per-QUERY or per-CORPUS. This matters more than it
looks: the headline conclusion was "recency must be a per-query policy, so build a per-query
alpha selector". If the effect actually splits by corpus rather than by question type, that
selector is conditioning on the wrong variable and the conclusion is wrong.

STALE ships a type per instance (T1/T2, 200 each) and three probing dimensions with
different relationships to the stale premise:
  dim1 - explicit state validation ("does the user still live in Seattle?")
  dim2 - stale-premise robustness (asserts the stale fact, then asks)
  dim3 - implicit downstream planning (never restates the premise)
dim1/dim2 restate the premise and so are lexically biased toward M_old; dim3 is not. If the
trade is per-query, dim3 should behave differently from dim1/dim2 — and it already does at
baseline (0.435 vs 0.110/0.092), which is the first evidence that "query type" is the real
variable.

Reads stale_results.json (written by eval_stale.py).
"""

import argparse
import json
from collections import defaultdict


def load(path):
    with open(path) as fh:
        return json.load(fh)["per_instance"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="/fast/datasets/m2/stale_results.json")
    args = ap.parse_args()
    rows = load(args.path)

    # --- by STALE type x dim, at baseline and the best recency config ---
    agg = defaultdict(lambda: defaultdict(float))
    for r in rows:
        key = (r["alpha"], r["halflife"], r["type"], r["dim"])
        t = agg[key]
        t["n"] += 1
        t["win"] += 1.0 if r["rank_new"] < r["rank_old"] else 0.0
        t["rank_new"] += r["rank_new"]
        t["rank_old"] += r["rank_old"]
        t["new_at_5"] += 1.0 if r["rank_new"] <= 5 else 0.0

    configs = [(0.0, None), (1.0, 365.0)]
    print("=" * 74)
    print("STALE: does the recency trade split by TYPE (T1/T2) or by QUERY DIM?")
    print("=" * 74)
    print(
        f"{'config':>14} {'type':>5} {'dim':>5} {'n':>4} {'new>old':>9} {'new@5':>8} {'rank_new':>9}"
    )
    print("-" * 74)
    for alpha, hl in configs:
        label = "baseline" if alpha == 0 else f"a={alpha:g}/{hl:g}d"
        for typ in ("T1", "T2"):
            for dim in ("dim1_query", "dim2_query", "dim3_query"):
                t = agg[(alpha, hl, typ, dim)]
                n = t["n"] or 1
                print(
                    f"{label:>14} {typ:>5} {dim.replace('_query', ''):>5} {int(t['n']):>4} "
                    f"{t['win'] / n:>9.3f} {t['new_at_5'] / n:>8.3f} {t['rank_new'] / n:>9.2f}"
                )
        print("-" * 74)

    # --- variance decomposition: which factor moves the number more? ---
    print("\nspread of new>old at baseline (the pre-existing failure, before any term):")
    for factor, idx in (("type", 2), ("dim", 3)):
        vals = defaultdict(list)
        for r in rows:
            if r["alpha"] != 0.0:
                continue
            key = r["type"] if factor == "type" else r["dim"]
            vals[key].append(1.0 if r["rank_new"] < r["rank_old"] else 0.0)
        means = {k: sum(v) / len(v) for k, v in vals.items()}
        spread = max(means.values()) - min(means.values())
        pretty = {k: round(v, 3) for k, v in sorted(means.items())}
        print(f"  by {factor:>4}: {pretty}   spread={spread:.3f}")

    print("\nlift from recency (a=1/365d minus baseline), same split:")
    for factor in ("type", "dim"):
        base, best = defaultdict(list), defaultdict(list)
        for r in rows:
            key = r["type"] if factor == "type" else r["dim"]
            win = 1.0 if r["rank_new"] < r["rank_old"] else 0.0
            if r["alpha"] == 0.0:
                base[key].append(win)
            elif r["alpha"] == 1.0 and r["halflife"] == 365.0:
                best[key].append(win)
        lifts = {
            k: round(sum(best[k]) / len(best[k]) - sum(base[k]) / len(base[k]), 3) for k in base
        }
        spread = max(lifts.values()) - min(lifts.values())
        print(f"  by {factor:>4}: {lifts}   spread={spread:.3f}")

    print("\nRead: the factor with the LARGER spread is the one the policy must condition on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
