"""alpha-selector: a query-time recency POLICY. Does a corpus-blind classifier recover
STALE's staleness win WITHOUT the LoCoMo recall cost that a global recency weight pays?

See docs/ALPHA-SELECTOR-SPEC.md. This is the M2 build-debt: M2 proved recency is a
per-query trade whose discriminating variable is a property of the QUERY (observable at
query time); it never built the runtime function that reads that variable off a raw query
WITHOUT STALE's oracle dim-labels. This is that function, plus its eval.

Tau POLICY only. JMFTS ships the mechanism (hybrid_search's opt-in recency_weight);
choosing the weight per query is ours. DB-READ-ONLY: it classifies queries and runs
searches, and never mutates structured_content (unlike the importance scorers).

THE CLAIM UNDER TEST (single policy, blind to which corpus a query came from):
    baseline (a=0 everywhere)          STALE new>old 0.11   LoCoMo R@1 0.362
    global  (a=1/365d everywhere)      STALE new>old 0.91   LoCoMo R@1 0.206   <- the trade
    selector (per-query)               new>old >> 0.11      R@1 ~= 0.362       <- the target

The classifier NEVER sees the corpus tag or the oracle label. Its predicted class is
compared to the oracle only for diagnosis (which queries it misroutes), never fed back.

Run ON midlife:
    /home/john/jmfts-gpudev-env.sh alpha_selector.py               # classify (resumable) then eval
    /home/john/jmfts-gpudev-env.sh alpha_selector.py --limit 10    # smoke: 10 stale + 10 locomo-conv
    /home/john/jmfts-gpudev-env.sh alpha_selector.py --eval-only   # re-score from cached classes
"""

import argparse
import json
import random
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
from sqlalchemy import select  # noqa: F401  (kept parallel to sibling scripts)

from jmfts_core.database import get_session_factory
from jmfts_core.repositories.search import SearchRepository

LLM = "http://127.0.0.1:8080/v1/chat/completions"
HEALTH = "http://127.0.0.1:8080/health"
SLOTS = 2
GRAMMAR = "root ::= [0-2]"

# jf35 is tau's LIVE local-llm. Thinking+grammar needs the server-side --reasoning-budget
# flag to force the end-of-think tag (per-request budget is silently ignored on this build);
# without it a rare long-deliberation query never closes </think> and returns empty content.
# We restart jf35 with the budget and RESTORE it (no flag) in a finally, verified. This is
# the exact invocation captured live (pid 1034864) and used by budget_sweep.py.
SERVER_CWD = "/home/john/Development/turboquant_experiments/repos/llama-pr-thinking-grammar"
SERVER_BASE = (
    "./build-jf-cuda/bin/llama-server -m /fast/model/moe-compare/qwen36-35B-IQ4_XS.gguf "
    "--jinja --port 8080 -c 16384 -ngl 99 -np 2 --host 127.0.0.1"
)


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def restart_server(budget):
    """Restart jf35 with (budget) or without (None=restore) a reasoning budget; block
    until healthy. budget=None reproduces the canonical live invocation exactly."""
    flag = f" --reasoning-budget {budget}" if budget else ""
    inner = (
        f"export PATH=/usr/local/cuda-12.8/bin:$PATH && cd {SERVER_CWD} && "
        f"{SERVER_BASE}{flag} > /tmp/jf35.log 2>&1"
    )
    sh("tmux kill-session -t jf35 2>/dev/null; true")
    time.sleep(2)
    subprocess.run(["tmux", "new-session", "-d", "-s", "jf35", inner], check=True)
    for _ in range(120):
        if sh(f"curl -s -o /dev/null -w '%{{http_code}}' -m 3 {HEALTH}") == "200":
            return
        time.sleep(2)
    raise RuntimeError(f"jf35 did not come healthy with budget={budget}")


def verify_restored():
    """After restore, jf35 must be healthy AND carry no --reasoning-budget flag."""
    healthy = sh(f"curl -s -o /dev/null -w '%{{http_code}}' -m 5 {HEALTH}") == "200"
    live = sh("pgrep -af 'llama-server.*8080' | grep -v reasoning-budget || true")
    has_budget = "reasoning-budget" in sh("pgrep -af 'llama-server.*8080'")
    if not healthy or has_budget or not live:
        raise RuntimeError(
            f"RESTORE FAILED: healthy={healthy} has_budget={has_budget} live={live!r}"
        )
    print(f"jf35 restored + verified: {live.splitlines()[-1] if live else '?'}", flush=True)


STALE_MANIFEST = "/fast/datasets/m2/stale_manifest.json"
LOCOMO_MANIFEST = "/fast/datasets/m2/locomo_manifest.json"
CLASSES = "/fast/datasets/m2/alpha_selector_classes.json"
OUT = "/fast/datasets/m2/alpha_selector_results.json"
DIMS = ["dim1_query", "dim2_query", "dim3_query"]
KS = [1, 5, 10, 20]
HALFLIFE = 365.0  # fixed at the M2 best; per-query t-half is out of scope (see spec)

# The classifier's prompt. State-vs-fact, three anchored classes that line up with the M2
# breakdown: dim1/dim2 (restate a possibly-stale premise) -> 2, dim3 (state but no restated
# premise) -> 1, LoCoMo (specific past fact) -> 0. Thinking is ON (the 0.29->2.45-bit M2
# finding: enable_thinking:false destroys judgement signal on exactly this kind of task).
CLASSIFY = (
    "A memory system answers a question by searching a person's past conversations. "
    "Decide how much the correct answer depends on RECENCY. Reply with one digit.\n\n"
    "2 = the question asks about the person's CURRENT state and takes an earlier answer "
    "for granted, so only the most RECENT relevant memory is correct and older ones are "
    "stale. e.g. 'Where does she live now?', 'What is his current job?', 'Is she still "
    "vegetarian?'\n"
    "1 = the question is about a state or situation but does not assume a prior answer; "
    "recency only breaks ties and an older memory could still be right. e.g. 'What kind of "
    "neighbourhood does she live in?', 'What does he do for work?'\n"
    "0 = the question asks about a SPECIFIC PAST event or fact that is correct no matter "
    "when it was recorded, so recency is irrelevant or actively misleading. e.g. 'When did "
    "Caroline go to the support group?', 'What did they name the dog?', 'How many siblings "
    "does he have?'\n\n"
    "Question: {q}\n"
    "Digit:"
)

# Few-shot variant. The zero-shot confusion (2026-07-17) had two costly errors: (1) dim2
# 'Since the user [stated fact], can you [task]?' queries routed to 0 — the classifier read
# the task request and missed that the conditioning premise is the staleness-sensitive fact
# (the STALE shortfall, ~25% of dim1/2); (2) LoCoMo fact-recall questions with STATE FLAVOUR
# but no staleness marker routed to 2 (the LoCoMo cost, ~25%). The refined rubric makes the
# class-2 trigger explicit — a staleness MARKER ('still/now/currently/these days') OR a
# conditioning premise ('since the user is X') — so bare state-flavoured recall falls to 0/1
# instead of 2. Exemplars are hand-written to the observed patterns, NOT verbatim corpus
# queries, so there is no train/test leakage and nothing to exclude from the eval.
FEWSHOT_EXAMPLES = [
    # 2: explicit staleness marker, OR a premise the query conditions on (a newer memory
    #    could overturn it, so retrieval must surface the freshest value)
    ("Based on the conversation history, is the user still working as a nurse?", 2),
    (
        "Since the user says they live right by the coast, can you suggest a few weekend "
        "activities that make the most of being near the water?",
        2,
    ),
    # 1: state / habit / preference with NO staleness marker and no conditioning premise
    (
        "Could you help me draft a short email suggesting a good place to meet a friend this week?",
        1,
    ),
    ("What does the user usually do for work?", 1),
    # 0: recall of a specific past event or fact the person mentioned
    ("When did the user first mention adopting their dog?", 0),
    ("How many siblings did the user say they have?", 0),
]
CLASSIFY_FEWSHOT = (
    "A memory system answers a question by searching a person's past conversations. Decide "
    "how much the correct answer depends on RECENCY — whether a NEWER memory should override "
    "an older one. Reply with one digit.\n\n"
    "2 = the question assumes a PRIOR answer that may now be STALE, so only the most recent "
    "memory is correct. Triggered by a staleness marker ('still', 'now', 'currently', 'these "
    "days', 'anymore') OR by CONDITIONING on a stated fact about the person ('since the user "
    "is X...', 'based on the user being Y...') — that premise is exactly what a newer memory "
    "could overturn.\n"
    "1 = a state, habit, or preference question with NO staleness marker and no conditioning "
    "premise; recency only breaks ties and an older memory could still be right.\n"
    "0 = recall of a SPECIFIC past event or fact the person mentioned ('when did...', 'what "
    "did they name...', 'how many...'), correct no matter when it was said.\n\n"
    "Examples:\n"
    + "\n".join(f"Question: {q}\nDigit: {c}" for q, c in FEWSHOT_EXAMPLES)
    + "\n\nQuestion: {q}\nDigit:"
)
CLASSES_FEWSHOT = "/fast/datasets/m2/alpha_selector_classes_fewshot.json"
SAMPLE_SEED = 20260717

# name -> (class:int) -> (recency_weight, halflife_days). weight 0 => rerank skipped.
POLICIES = {
    "baseline": lambda c: (0.0, None),
    "global": lambda c: (1.0, HALFLIFE),
    "selector_binary": lambda c: (1.0, HALFLIFE) if c >= 1 else (0.0, None),
    "selector_ordinal": lambda c: {0: (0.0, None), 1: (0.5, HALFLIFE), 2: (1.0, HALFLIFE)}[c],
}
# The distinct (weight, halflife) any policy can emit, so each query runs each search once.
SEARCH_CONFIGS = [(0.0, None), (0.5, HALFLIFE), (1.0, HALFLIFE)]


def classify_one(client, text, template=CLASSIFY):
    r = client.post(
        LLM,
        json={
            "messages": [{"role": "user", "content": template.format(q=text)}],
            "grammar": GRAMMAR,
            "temperature": 0,
            "max_tokens": 4000,
        },
        timeout=600.0,
    )
    r.raise_for_status()
    c = (r.json()["choices"][0]["message"]["content"] or "").strip()
    if not c:
        # Fail-Early: a truncated-before-answer completion is not a class. Do NOT fabricate
        # a default (it would poison the very term under test). If this fires broadly the
        # prompt deliberates too long; simplify it rather than restart the shared jf35.
        raise RuntimeError("empty content — reasoning ran past max_tokens before the digit")
    return int(c)


def parse_stale_now(raw):
    return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def rank_of(results, doc_id):
    for i, r in enumerate(results, 1):
        if r.document.id == doc_id:
            return i
    return None


def load_queries(stale, locomo):
    """Every query, corpus-tagged, with the oracle group used ONLY for later diagnosis."""
    q = {}
    for inst in stale:
        for dim in DIMS:
            # oracle: dim1/dim2 restate the premise (expect 2), dim3 does not (expect 1)
            oracle = 2 if dim in ("dim1_query", "dim2_query") else 1
            q[f"stale:{inst['uid']}:{dim}"] = {
                "text": inst["probing_queries"][dim],
                "corpus": "stale",
                "oracle": oracle,
            }
    for conv in locomo:
        for i, question in enumerate(conv["questions"]):
            q[f"locomo:{conv['sample_id']}:{i}"] = {
                "text": question["question"],
                "corpus": "locomo",
                "oracle": 0,
            }
    return q


def classify(queries, template=CLASSIFY, cache_path=CLASSES):
    try:
        with open(cache_path) as fh:
            cache = json.load(fh)
    except FileNotFoundError:
        cache = {}
    todo = [(qid, meta["text"]) for qid, meta in queries.items() if qid not in cache]
    print(f"{len(queries)} queries; {len(cache)} cached, {len(todo)} to classify", flush=True)
    if not todo:
        return cache
    started = time.time()
    done = 0
    with httpx.Client() as client:
        with ThreadPoolExecutor(max_workers=SLOTS) as pool:
            for (qid, _), cls in zip(
                todo, pool.map(lambda t: classify_one(client, t[1], template), todo)
            ):
                cache[qid] = cls
                done += 1
                if done % 100 == 0:
                    rate = done / (time.time() - started)
                    print(
                        f"  {done}/{len(todo)} classified ({rate:.1f}/s, "
                        f"~{(len(todo) - done) / rate / 60:.0f}m left)",
                        flush=True,
                    )
                    with open(cache_path, "w") as fh:
                        json.dump(cache, fh)
    with open(cache_path, "w") as fh:
        json.dump(cache, fh)
    print(
        f"  classified {len(todo)} in {time.time() - started:.0f}s; "
        f"distribution {dict(sorted(Counter(cache[q] for q in queries).items()))}",
        flush=True,
    )
    return cache


def binary_ok(qids, queries, pred):
    """Retrieval-relevant binary accuracy: a state query (oracle>=1) should get recency
    (pred>=1); a fact query (oracle==0) should get none (pred==0). This — not strict 3-class
    accuracy — is the metric that maps to retrieval, since dim3->1 vs dim3->2 both route
    recency and both help STALE (the oracle ceiling proved routing is the whole game)."""
    state = [q for q in qids if queries[q]["oracle"] >= 1]
    fact = [q for q in qids if queries[q]["oracle"] == 0]
    s = sum(1 for q in state if pred.get(q, 0) >= 1) / (len(state) or 1)
    f = sum(1 for q in fact if pred.get(q, 9) == 0) / (len(fact) or 1)
    return s, len(state), f, len(fact)


def eval_stale(stale, classes, repo):
    # tallies[policy] over instances x dims
    t = defaultdict(lambda: defaultdict(float))
    for inst in stale:
        now = parse_stale_now(inst["now"])
        for dim in DIMS:
            qid = f"stale:{inst['uid']}:{dim}"
            cls = classes[qid]
            # run each distinct search config once, then let each policy pick its result
            ranks = {}
            for w, hl in SEARCH_CONFIGS:
                kw = {"recency_weight": w, "recency_halflife_days": hl, "now": now} if w else {}
                res = repo.hybrid_search(
                    inst["probing_queries"][dim],
                    limit=50,
                    methods=["vector"],
                    parent_id=inst["root_id"],
                    **kw,
                )
                ranks[(w, hl)] = (
                    rank_of(res, inst["new_doc_id"]),
                    rank_of(res, inst["old_doc_id"]),
                )
            for name, fn in POLICIES.items():
                r_new, r_old = ranks[fn(cls)]
                if r_new is None or r_old is None:
                    t[name]["missing"] += 1
                    continue
                t[name]["n"] += 1
                t[name]["new_over_old"] += 1 if r_new < r_old else 0
                t[name]["new_at_1"] += 1 if r_new == 1 else 0
                t[name]["new_at_5"] += 1 if r_new <= 5 else 0
                t[name]["sum_rank_new"] += r_new
    return t


def eval_locomo(locomo, classes, repo):
    t = defaultdict(lambda: defaultdict(float))
    for conv in locomo:
        now = datetime.fromisoformat(conv["now"])
        for i, q in enumerate(conv["questions"]):
            cls = classes[f"locomo:{conv['sample_id']}:{i}"]
            want = set(q["evidence_doc_ids"])
            got = {}
            for w, hl in SEARCH_CONFIGS:
                kw = {"recency_weight": w, "recency_halflife_days": hl, "now": now} if w else {}
                res = repo.hybrid_search(
                    q["question"],
                    limit=max(KS),
                    methods=["vector"],
                    parent_id=conv["root_id"],
                    **kw,
                )
                got[(w, hl)] = [r.document.id for r in res]
            for name, fn in POLICIES.items():
                ids = got[fn(cls)]
                t[name]["n"] += 1
                for k in KS:
                    t[name][f"recall@{k}"] += len(want & set(ids[:k])) / len(want)
    return t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--limit", type=int, default=None, help="first N stale instances / locomo convs"
    )
    ap.add_argument("--eval-only", action="store_true", help="re-score from cached classes")
    ap.add_argument("--classify-only", action="store_true", help="classify, skip retrieval eval")
    ap.add_argument(
        "--budget",
        type=int,
        default=None,
        help="restart jf35 with --reasoning-budget N for classification, restore after",
    )
    ap.add_argument(
        "--oracle-ceiling",
        action="store_true",
        help="also route by the KNOWN oracle group — the perfect-classifier ceiling, "
        "which isolates classifier-error from policy-error",
    )
    ap.add_argument(
        "--fewshot",
        action="store_true",
        help="classify with the few-shot prompt + its own cache (full run)",
    )
    ap.add_argument(
        "--sample",
        type=int,
        default=None,
        help="A/B ONLY: few-shot-classify a stratified sample of ~N and compare "
        "retrieval-relevant binary accuracy to the zero-shot cache; no retrieval",
    )
    args = ap.parse_args()

    with open(STALE_MANIFEST) as fh:
        stale = json.load(fh)
    with open(LOCOMO_MANIFEST) as fh:
        locomo = json.load(fh)
    if args.limit:
        stale, locomo = stale[: args.limit], locomo[: args.limit]

    queries = load_queries(stale, locomo)

    # Few-shot A/B: classify a stratified sample with the few-shot prompt, compare the
    # retrieval-relevant binary accuracy to the zero-shot cache. Cheap gate before the full run.
    if args.sample:
        random.seed(SAMPLE_SEED)
        groups = defaultdict(list)
        for qid, meta in queries.items():
            groups[meta["oracle"]].append(qid)
        per = args.sample // 3
        sample_qids = []
        for o in (0, 1, 2):
            sample_qids += random.sample(sorted(groups[o]), min(per, len(groups[o])))
        sample_queries = {q: queries[q] for q in sample_qids}
        print(f"few-shot A/B on {len(sample_qids)} stratified queries ({per}/group)", flush=True)
        if args.budget:
            restart_server(args.budget)
            try:
                fs = classify(sample_queries, CLASSIFY_FEWSHOT, CLASSES_FEWSHOT)
            finally:
                restart_server(None)
                verify_restored()
        else:
            fs = classify(sample_queries, CLASSIFY_FEWSHOT, CLASSES_FEWSHOT)
        zs = json.load(open(CLASSES))
        print(f"\n{'prompt':>10} {'state->recency':>15} {'fact->none':>13}")
        print("-" * 40)
        for label, pred in (("zero-shot", zs), ("few-shot", fs)):
            s, sn, f, fn = binary_ok(sample_qids, queries, pred)
            print(f"{label:>10} {s:>12.3f}({sn}) {f:>10.3f}({fn})")
        for label, pred in (("zero-shot", zs), ("few-shot", fs)):
            print(f"\n  {label} per oracle group:")
            for o, name in ((2, "dim1/2 want>=1"), (1, "dim3 want>=1"), (0, "locomo want 0")):
                qs = [q for q in sample_qids if queries[q]["oracle"] == o]
                dist = dict(sorted(Counter(pred[q] for q in qs).items()))
                ok = sum(1 for q in qs if (pred[q] >= 1) == (o >= 1)) / (len(qs) or 1)
                print(f"    {name:>15}: {dist}  ok={ok:.3f} (n={len(qs)})")
        print(
            "\nRun the full few-shot eval only if BOTH columns rise (state->recency is the "
            "pure-gain\ndirection — those are all STALE queries, so lifting it costs no recall)."
        )
        return 0

    template = CLASSIFY_FEWSHOT if args.fewshot else CLASSIFY
    cache_path = CLASSES_FEWSHOT if args.fewshot else CLASSES
    if args.eval_only:
        classes = json.load(open(cache_path))
    elif args.budget:
        restart_server(args.budget)
        try:
            classes = classify(queries, template, cache_path)
        finally:
            restart_server(None)  # restore: canonical invocation, no reasoning budget
            verify_restored()
    else:
        classes = classify(queries, template, cache_path)
    missing = [q for q in queries if q not in classes]
    if missing:
        raise RuntimeError(f"{len(missing)} queries unclassified; run without --eval-only")
    if args.classify_only:
        return 0

    # Classifier validation vs oracle — DIAGNOSIS ONLY, never fed into routing.
    print("\n--- classifier vs oracle (predicted class distribution per oracle group) ---")
    by_oracle = defaultdict(Counter)
    for qid, meta in queries.items():
        by_oracle[meta["oracle"]][classes[qid]] += 1
    for oracle in sorted(by_oracle):
        grp = {0: "locomo/fact(exp 0)", 1: "stale dim3 (exp 1)", 2: "stale dim1/2 (exp 2)"}[oracle]
        dist = dict(sorted(by_oracle[oracle].items()))
        n = sum(dist.values())
        print(f"  {grp:>22}: {dist}  (n={n})")

    # Perfect-routing ceiling: route by the oracle group instead of the predicted class.
    # The gap between selector_* here and under `classes` is the classifier's cost;
    # the gap between this and `global`/`baseline` is the policy's own ceiling.
    oracle_classes = {qid: meta["oracle"] for qid, meta in queries.items()}

    factory = get_session_factory()
    with factory() as db:
        repo = SearchRepository(db)
        print("\nscoring STALE...", flush=True)
        st = eval_stale(stale, classes, repo)
        print("scoring LoCoMo...", flush=True)
        lc = eval_locomo(locomo, classes, repo)
        st_oracle = lc_oracle = None
        if args.oracle_ceiling:
            print("scoring ORACLE ceiling (perfect routing)...", flush=True)
            st_oracle = eval_stale(stale, oracle_classes, repo)
            lc_oracle = eval_locomo(locomo, oracle_classes, repo)

    def stale_row(name, t):
        n = t["n"] or 1
        print(
            f"{name:>17} {int(t['n']):>5} {t['new_over_old'] / n:>8.3f} {t['new_at_1'] / n:>7.3f} "
            f"{t['new_at_5'] / n:>7.3f} {t['sum_rank_new'] / n:>9.2f}"
        )

    def locomo_row(name, t):
        n = t["n"] or 1
        print(
            f"{name:>17} {int(t['n']):>5} "
            + " ".join(f"{t['recall@' + str(k)] / n:>8.3f}" for k in KS)
        )

    print(f"\n{'STALE':>17} {'n':>5} {'new>old':>8} {'new@1':>7} {'new@5':>7} {'rank_new':>9}")
    print("-" * 57)
    for name in POLICIES:
        stale_row(name, st[name])
    if st_oracle:
        for name in ("selector_binary", "selector_ordinal"):
            stale_row("oracle_" + name.split("_")[1], st_oracle[name])

    print(f"\n{'LoCoMo':>17} {'n':>5} " + " ".join(f"{'R@' + str(k):>8}" for k in KS))
    print("-" * 49)
    for name in POLICIES:
        locomo_row(name, lc[name])
    if lc_oracle:
        for name in ("selector_binary", "selector_ordinal"):
            locomo_row("oracle_" + name.split("_")[1], lc_oracle[name])

    print(
        "\nTARGET: a selector row with STALE new>old well above baseline AND LoCoMo R@1 "
        "at ~baseline.\nThe 'global' row is the M2 trade (best STALE, worst LoCoMo); "
        "beating it on LoCoMo\nwhile keeping most of its STALE gain is the whole point. "
        "The oracle_* rows are the\nperfect-routing ceiling: selector-vs-oracle gap = "
        "classifier cost; oracle-vs-baseline = policy ceiling."
    )

    out_path = OUT.replace(".json", "_fewshot.json") if args.fewshot else OUT
    with open(out_path, "w") as fh:
        json.dump(
            {
                "stale": {k: dict(v) for k, v in st.items()},
                "locomo": {k: dict(v) for k, v in lc.items()},
                "stale_oracle": {k: dict(v) for k, v in st_oracle.items()} if st_oracle else None,
                "locomo_oracle": {k: dict(v) for k, v in lc_oracle.items()} if lc_oracle else None,
                "oracle_confusion": {str(o): dict(c) for o, c in by_oracle.items()},
            },
            fh,
        )
    print(f"\nfull results: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
