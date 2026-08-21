"""Does the importance signal survive a smaller thinking budget?

CONTEXT
    Enabling reasoning is what made importance scoring work at all (0.29 -> 2.45 bits). But
    it costs ~1,503 completion tokens per rating vs 3, which puts a full 5,882-turn re-score
    at ~7.2h and is the only thing blocking the raw-turn-vs-RAPTOR comparison. If a 1k budget
    holds the signal, that experiment becomes affordable; if only 6k does, it does not.

    llama.cpp's per-request `reasoning_budget` / `thinking_budget` fields are SILENTLY
    IGNORED on this build (verified: budget 0, 300 and -1 all returned byte-identical output
    with 4,157 chars of reasoning). So the budget is a server flag, and each cell needs a
    restart. `--reasoning-budget N` forces the end-of-thinking tag when the budget is spent,
    so a truncated thought still reaches the grammar-bound answer rather than returning
    empty content.

MEASURES, per budget: entropy of the rating distribution (the thing that decides whether the
term can reorder anything), agreement with the unrestricted 6k reference on the SAME turns
(Spearman + exact-match), and wall-clock. A budget that keeps entropy but loses agreement is
not the same scorer, just a differently-noisy one — so both are reported.

SAFETY: tau's live local-llm runs on this server. The original invocation is captured and
restored in a finally, and the restore is verified before exit.

Run ON midlife (it restarts llama-server; do not run concurrently with other LLM work):
    /home/john/jmfts-gpudev-env.sh budget_sweep.py [--n 120]
"""

import argparse
import json
import math
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
import random
from sqlalchemy import select

from jmfts_core.database import get_session_factory
from jmfts_core.models.document import Document

LLM = "http://127.0.0.1:8080"
SLOTS = 2
GRAMMAR = 'root ::= "10" | [1-9]'
SEED = 20260717
BUDGETS = [500, 1000, 2000, 3000, 4000, 5000]
REFERENCE = None  # unrestricted

SERVER_CWD = "/home/john/Development/turboquant_experiments/repos/llama-pr-thinking-grammar"
SERVER_BASE = (
    "./build-jf-cuda/bin/llama-server -m /fast/model/moe-compare/qwen36-35B-IQ4_XS.gguf "
    "--jinja --port 8080 -c 16384 -ngl 99 -np 2 --host 127.0.0.1"
)

RUBRIC = (
    "On the scale of 1 to 10, where 1 is purely mundane (e.g., brushing teeth, making bed) "
    "and 10 is extremely poignant (e.g., a break up, college acceptance), rate the likely "
    "poignancy of the following piece of memory.\nMemory: {memory}\nRating:"
)


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def restart_server(budget):
    """Restart jf35 with (or without) a reasoning budget; block until healthy."""
    flag = f" --reasoning-budget {budget}" if budget else ""
    inner = (
        f"export PATH=/usr/local/cuda-12.8/bin:$PATH && cd {SERVER_CWD} && "
        f"{SERVER_BASE}{flag} > /tmp/jf35.log 2>&1"
    )
    sh("tmux kill-session -t jf35 2>/dev/null; true")
    time.sleep(2)
    subprocess.run(["tmux", "new-session", "-d", "-s", "jf35", inner], check=True)
    for _ in range(120):
        code = sh(f"curl -s -o /dev/null -w '%{{http_code}}' -m 3 {LLM}/health")
        if code == "200":
            return
        time.sleep(2)
    raise RuntimeError(f"llama-server did not come healthy with budget={budget}")


def score(client, text, max_tokens=6000):
    r = client.post(
        f"{LLM}/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": RUBRIC.format(memory=text)}],
            "grammar": GRAMMAR,
            "temperature": 0,
            "max_tokens": max_tokens,
        },
        timeout=600.0,
    )
    r.raise_for_status()
    m = r.json()["choices"][0]["message"]
    c = (m["content"] or "").strip()
    if not c:
        raise RuntimeError("empty content — budget did not force the end-of-thinking tag")
    return int(c), len(m.get("reasoning_content") or "") // 4


def entropy(vals):
    d, n = Counter(vals), len(vals)
    return -sum((c / n) * math.log2(c / n) for c in d.values())


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


def run_cell(sample, budget):
    restart_server(budget)
    started = time.time()
    with httpx.Client() as client:
        with ThreadPoolExecutor(max_workers=SLOTS) as pool:
            out = list(pool.map(lambda row: score(client, row[1]), sample))
    scores = [o[0] for o in out]
    think_tokens = [o[1] for o in out]
    return {
        "budget": budget,
        "scores": scores,
        "entropy": entropy(scores),
        "mean": sum(scores) / len(scores),
        "frac_at_1": Counter(scores)[1] / len(scores),
        "mean_think_tokens": sum(think_tokens) / len(think_tokens),
        "seconds": time.time() - started,
    }


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
    print(f"budget sweep on {len(sample)} turns (seed {SEED})\n")

    results = []
    try:
        ref = run_cell(sample, REFERENCE)
        ref["label"] = "unrestricted"
        results.append(ref)
        print(
            f"  unrestricted: entropy {ref['entropy']:.2f}  mean {ref['mean']:.2f}  "
            f"think~{ref['mean_think_tokens']:.0f} tok  ({ref['seconds']:.0f}s)",
            flush=True,
        )
        for b in BUDGETS:
            cell = run_cell(sample, b)
            cell["label"] = str(b)
            cell["spearman_vs_ref"] = spearman(ref["scores"], cell["scores"])
            cell["exact_match"] = sum(
                1 for x, y in zip(ref["scores"], cell["scores"]) if x == y
            ) / len(sample)
            results.append(cell)
            print(
                f"  budget {b:>5}: entropy {cell['entropy']:.2f}  mean {cell['mean']:.2f}  "
                f"rho {cell['spearman_vs_ref']:.2f}  exact {cell['exact_match']:.0%}  "
                f"think~{cell['mean_think_tokens']:.0f} tok  ({cell['seconds']:.0f}s)",
                flush=True,
            )
    finally:
        print("\nrestoring llama-server to its original unrestricted invocation…")
        restart_server(None)
        cmd = sh(
            "tr '\\0' ' ' < /proc/$(pgrep -f build-jf-cuda/bin/llama-server | head -1)/cmdline"
        )
        ok = "--reasoning-budget" not in cmd
        print(f"  restored: {cmd}")
        print(f"  clean (no budget flag): {ok}")

    with open("/fast/datasets/m2/budget_sweep.json", "w") as fh:
        json.dump(results, fh)

    print(
        f"\n{'budget':>12} {'entropy':>8} {'mean':>6} {'at1':>6} {'rho':>6} "
        f"{'exact':>6} {'think_tok':>10} {'sec':>6}"
    )
    print("-" * 68)
    for r in results:
        rho = f"{r.get('spearman_vs_ref', float('nan')):.2f}" if "spearman_vs_ref" in r else "-"
        ex = f"{r['exact_match']:.0%}" if "exact_match" in r else "-"
        print(
            f"{r['label']:>12} {r['entropy']:>8.2f} {r['mean']:>6.2f} {r['frac_at_1']:>6.1%} "
            f"{rho:>6} {ex:>6} {r['mean_think_tokens']:>10.0f} {r['seconds']:>6.0f}"
        )
    print("\nturn-level nothink reference: 0.29 bits, 95.5% at 1")
    print("A budget is usable if it keeps entropy AND agrees with unrestricted (rho, exact).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
