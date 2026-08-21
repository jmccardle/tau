"""Do the importance rubrics predict EVIDENCE-ness? The external-criterion test.

WHY THIS IS THE TEST THAT MATTERS
    Everything so far is internal. Entropy says a scorer spreads its ratings; rank
    correlation says two rubrics disagree (rho 0.21). Neither says whether ANY rubric
    tracks something real, because both lack an external criterion.

    LoCoMo supplies one for free: a turn either IS or IS NOT evidence for some question.
    That is ground truth, independent of any rubric, and it is exactly the property
    retrieval needs to recover.

WHAT IT DECIDES
    AUC of each rubric's rating as a classifier of "is this turn evidence for some
    question", over the same 120 seeded turns already scored by rubric_correlation.py.

      AUC ~ 0.5  => the rubric carries NO information about evidence-ness. This is the
                    mechanistic confirmation of the read-time result: importance is
                    orthogonal to relevance, so boosting it can only displace. It also
                    substitutes for the missing random-importance control — a term with
                    AUC 0.5 IS random with respect to the task, by measurement rather
                    than by assumption.
      AUC > 0.6  => the rubric knows something about evidence-ness, and the read-time
                    failure is about HOW the term is applied (a query-independent
                    multiplier) rather than about what it measures.
      AUC < 0.4  => it is anti-correlated: importance actively marks the WRONG turns,
                    which would explain the damage scaling with signal quality.

    This is pure re-analysis — no LLM, no retrieval. It costs nothing and it is the
    control the previous conclusions were leaning on without having run.
"""

import json
import sys
from collections import Counter

CORR = "/fast/datasets/m2/rubric_correlation.json"
MANIFEST = "/fast/datasets/m2/locomo_manifest.json"


def auc(scores, labels):
    """Mann-Whitney U / rank-biserial AUC, with proper handling of the many ties a 1-10
    scale produces (a tie contributes 0.5, not 1.0 — ignoring that inflates AUC)."""
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return float("nan")
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def main() -> int:
    with open(CORR) as fh:
        corr = json.load(fh)
    with open(MANIFEST) as fh:
        manifest = json.load(fh)

    evidence_ids = set()
    for conv in manifest:
        for q in conv["questions"]:
            evidence_ids.update(q["evidence_doc_ids"])

    ids = corr["ids"]
    labels = [doc_id in evidence_ids for doc_id in ids]
    n_pos = sum(labels)
    print(
        f"{len(ids)} scored turns; {n_pos} are evidence for at least one question "
        f"({n_pos / len(ids):.0%}), {len(ids) - n_pos} are not"
    )
    if n_pos == 0 or n_pos == len(ids):
        raise RuntimeError("degenerate labels — cannot compute AUC")

    print(f"\n{'rubric':>16} {'AUC':>6} {'mean(evidence)':>15} {'mean(non-ev)':>13} {'delta':>7}")
    print("-" * 62)
    for name, key in (("ga_poignancy", "ga"), ("retrievability", "retrieval")):
        scores = corr[key]
        a = auc(scores, labels)
        me = sum(s for s, y in zip(scores, labels) if y) / n_pos
        mn = sum(s for s, y in zip(scores, labels) if not y) / (len(ids) - n_pos)
        print(f"{name:>16} {a:>6.3f} {me:>15.2f} {mn:>13.2f} {me - mn:>+7.2f}")

    print("\nreference: AUC 0.5 = the rating tells you nothing about whether a turn is evidence.")
    print(
        "If both sit at ~0.5, importance is measurably orthogonal to the retrieval task —\n"
        "which is the random-importance control, obtained by measurement rather than\n"
        "by assuming the mechanism."
    )

    for name, key in (("ga_poignancy", "ga"), ("retrievability", "retrieval")):
        d = Counter(corr[key])
        print(f"\n{name} distribution: {dict(sorted(d.items()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
