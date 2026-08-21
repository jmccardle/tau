#!/usr/bin/env python3
"""Measure which thinking/reasoning knobs an OpenAI-compatible endpoint actually honors.

τ has ONE wire representation for a thinking level (`reasoning_effort`, sent from
`tau_llm.providers.openai.stream_chat`). A server may implement a different one, or
none. When it implements a different one the failure is SILENT: the request is
accepted, the parameter is ignored, and the generation comes back looking normal.
That is the shape of defect τ's guards exist for, and no amount of documentation
catches it — a doc is another sentence about the server, not a question put TO the
server.

So this script asks. Point it at an endpoint and it reports, per knob, whether the
endpoint's behavior changes when the knob changes.

    python scripts/llama_conformance_probe.py
    python scripts/llama_conformance_probe.py --url http://host:8080 --json out.json

Deliberately standalone: no τ imports, no ~/.tau/config.json, `httpx` only. It is
meant to be runnable by whoever is holding the endpoint, including people who do
not have τ checked out. `tau-llm/tests/test_llama_conformance.py` (marker `llama`)
is the thin pytest wrapper that calls `probe()` and asserts the recorded verdicts.

Fail-Early: a transport failure is reported as unreachable and exits 2 without
verdicts. Any OTHER failure — a non-200, a missing /tokenize, a response without
the fields it claims — raises. A server that connects and misbehaves is a finding,
never a reason to report less.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

# The server under test belongs to whoever is running the probe, so there is
# no default address here. Unset means "no endpoint", which probe() reports as
# Unreachable — the same outcome as a server that is down, and a skip upstream.
DEFAULT_URL = os.environ.get("TAU_LLAMA_TEST_URL", "")

# Short, and it reasons far more than it answers: baseline reasoning is ~500 tokens
# against a 3-token answer, so "did the knob shorten the thinking" has a wide, cheap
# signal. A prompt whose ANSWER is long would spend minutes measuring the wrong half
# (see the `content_tokens` column in a hard-prompt run: reasoning 2062, content 9844).
PROMPT = "What is 17*23? Answer with just the number."

# Every generation is greedy and seeded. Without this, "the output did not change"
# is unfalsifiable — two samples from a temperature>0 model differ whether or not
# the knob did anything. The determinism control below verifies the server actually
# honors it before any verdict is trusted.
BASE_BODY: dict[str, Any] = {
    "model": "probe",
    "messages": [{"role": "user", "content": PROMPT}],
    "temperature": 0,
    "seed": 1,
    "stream": False,
    "max_tokens": 12000,
}

BUDGET_SWEEP = [1, 64, 256]


class Unreachable(Exception):
    """Transport-level failure. The caller skips; it is not a finding."""


def _post(url: str, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    try:
        response = httpx.post(url + path, json=body, timeout=timeout)
    except httpx.TransportError as exc:
        raise Unreachable(f"{url}{path}: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(
            f"{url}{path} returned HTTP {response.status_code}: {response.text[:400]}"
        )
    return dict(response.json())


def _count_tokens(url: str, text: str) -> int:
    """Exact token count via the server's own tokenizer.

    Counted server-side rather than estimated client-side: the whole point of this
    script is to stop trusting descriptions of the endpoint over the endpoint.

    NOTE the systematic offset this introduces, so nobody reads the raw numbers as
    the server's internal accounting: `reasoning_content` is re-tokenized as a bare
    string, without the `<think>` delimiters it was generated with, and llama.cpp
    appends a short closing phrase when a budget cuts reasoning off. Measured
    against this build, `completion_tokens` runs a flat +20 over the requested
    budget. The verdicts below depend only on whether the count MOVES with the
    knob, never on an exact figure, precisely so that offset cannot invalidate them.
    """
    if not text:
        return 0
    return len(_post(url, "/tokenize", {"content": text}, timeout=30.0)["tokens"])


def _generate(url: str, extra: dict[str, Any]) -> dict[str, Any]:
    body = dict(BASE_BODY)
    body.update(extra)
    data = _post(url, "/v1/chat/completions", body, timeout=600.0)
    message = data["choices"][0]["message"]
    reasoning = message.get("reasoning_content") or ""
    content = message.get("content") or ""
    return {
        "reasoning_tokens": _count_tokens(url, reasoning),
        "content_tokens": _count_tokens(url, content),
        "completion_tokens": data["usage"]["completion_tokens"],
        "predicted_ms": data.get("timings", {}).get("predicted_ms"),
        "content": content,
    }


def _server_info(url: str) -> dict[str, Any]:
    props = _post_get(url, "/props")
    settings = props.get("default_generation_settings", {}).get("params", {})
    return {
        # The build string is the ONLY provenance this endpoint publishes. It is not
        # enough, and the gap is load-bearing: `thinking_budget_tokens: 0` changes
        # meaning with the server's `--reasoning-budget-message` LAUNCH flag, and no
        # /props field, generation param, or endpoint reports that flag. Two runs of
        # this script against one build can therefore disagree on the zero verdict
        # with nothing in either report explaining why. Record the build so the
        # disagreement is at least legible as "same build, different launch".
        "build_info": props.get("build_info"),
        "model_path": props.get("model_path"),
        "n_ctx": props.get("default_generation_settings", {}).get("n_ctx"),
        "reasoning_format": settings.get("reasoning_format"),
        "chat_format": settings.get("chat_format"),
        "chat_template_caps": props.get("chat_template_caps"),
    }


def _post_get(url: str, path: str) -> dict[str, Any]:
    try:
        response = httpx.get(url + path, timeout=10.0)
    except httpx.TransportError as exc:
        raise Unreachable(f"{url}{path}: {exc}") from exc
    if response.status_code in (401, 403):
        raise Unreachable(f"{url}{path}: server rejected the request with {response.status_code}")
    if response.status_code != 200:
        raise RuntimeError(f"{url}{path} returned HTTP {response.status_code}")
    return dict(response.json())


def probe(url: str = DEFAULT_URL) -> dict[str, Any]:
    """Run every measurement and return the report. Raises `Unreachable` if the
    endpoint cannot be reached at all — including when none was configured."""
    if not url:
        raise Unreachable("no endpoint configured: set $TAU_LLAMA_TEST_URL or pass --url")
    report: dict[str, Any] = {
        "probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url": url,
        "prompt": PROMPT,
        "server": _server_info(url),
        "measurements": {},
        "verdicts": {},
    }
    measurements: dict[str, Any] = report["measurements"]

    # ── Control: is the endpoint deterministic under temperature 0 + seed? ──
    # Runs FIRST because every "ignored" verdict below is an argument from two
    # identical outputs, and that argument is worthless against a sampling server.
    baseline = _generate(url, {})
    repeat = _generate(url, {})
    measurements["baseline"] = baseline
    measurements["baseline_repeat"] = repeat
    deterministic = (
        baseline["content"] == repeat["content"]
        and baseline["completion_tokens"] == repeat["completion_tokens"]
    )
    report["deterministic"] = deterministic

    # ── reasoning_effort: τ's ONLY current wire representation ──
    # An invalid value is included on purpose. A server that honors the parameter
    # should refuse "banana"; one that returns the baseline generation for it is
    # not validating the field, which is itself the evidence that it never reads it.
    efforts = {}
    for level in ("high", "low", "banana"):
        efforts[level] = _generate(url, {"reasoning_effort": level})
    measurements["reasoning_effort"] = efforts
    effort_moved = len({e["completion_tokens"] for e in efforts.values()}) > 1
    report["verdicts"]["reasoning_effort"] = "honored" if effort_moved else "ignored"

    # ── thinking_budget_tokens: llama.cpp's own representation ──
    budgets = {}
    for budget in BUDGET_SWEEP:
        budgets[str(budget)] = _generate(url, {"thinking_budget_tokens": budget})
    measurements["thinking_budget_tokens"] = budgets
    budget_moved = (
        all(
            budgets[str(b)]["reasoning_tokens"] < baseline["reasoning_tokens"] for b in BUDGET_SWEEP
        )
        and len({b["reasoning_tokens"] for b in budgets.values()}) > 1
    )
    report["verdicts"]["thinking_budget_tokens"] = "honored" if budget_moved else "ignored"

    # ── budget 0: the value whose documented meaning is "immediate end" ──
    # Measured separately from the sweep because the failure mode being checked for
    # is an INVERSION — a value that reads as "none" behaving as "unbounded". That
    # is not a smaller version of "ignored"; it is the most expensive request on the
    # wire wearing the name of the cheapest, so it gets its own verdict string.
    #
    # THIS VERDICT IS A PROPERTY OF THE SERVER'S LAUNCH, NOT OF ITS BUILD. Confirmed
    # on one machine: budget 0 produced 10187 reasoning tokens in 60.4 s, then 14
    # tokens in 7.0 s after a restart whose only change was the
    # `--reasoning-budget-message` flag. Nothing in /props, the generation params, or
    # any other endpoint reports that flag, so this script cannot record WHICH launch
    # it measured — see `_server_info`. Read this verdict as "what this running
    # process does", never as "what this version does".
    #
    # The consequence for a client is the whole reason the distinction is written
    # here rather than left in a chat log: a client cannot ask which behavior it is
    # about to get. `1` is unambiguous under both launches (15 reasoning tokens here,
    # versus 14 for `0`), so one token buys independence from invisible state.
    zero = _generate(url, {"thinking_budget_tokens": 0})
    measurements["thinking_budget_zero"] = zero
    smallest = min(budgets[str(b)]["reasoning_tokens"] for b in BUDGET_SWEEP)
    if zero["reasoning_tokens"] >= baseline["reasoning_tokens"]:
        zero_verdict = "inverted-unbounded"
    elif zero["reasoning_tokens"] <= smallest:
        zero_verdict = "floor"
    else:
        zero_verdict = "partial"
    report["verdicts"]["thinking_budget_zero"] = zero_verdict

    # ── Encoding sensitivity: is a MALFORMED budget refused, or ignored? ──
    # Added after a reported "budget 0 is unbounded" failed to reproduce here. It
    # is reproducible with one character changed: `"0"` as a JSON string behaves
    # byte-for-byte like not sending the parameter at all — same generation, same
    # token counts, same timing, HTTP 200. So do `null`, `-1`, and a misspelled
    # key. Any client that stringifies numeric params (a JS `String(v)`, a config
    # value read as text, a shell template) turns "no thinking" into "unbounded
    # thinking" and gets a success response for it.
    #
    # This is the more dangerous property, and the one worth carrying forward: the
    # failure is not a specific bad VALUE, it is that EVERY malformed spelling
    # degrades to the most expensive behavior and nothing on the wire says so. A
    # guard against a literal 0 would not have fired, because the client that trips
    # this was never sending 0 — it was sending "0".
    encodings = {
        "string_zero": {"thinking_budget_tokens": "0"},
        "null": {"thinking_budget_tokens": None},
        "negative_one": {"thinking_budget_tokens": -1},
        "misspelled_key": {"thinking_budget_token": 0},
    }
    encoded = {name: _generate(url, body) for name, body in encodings.items()}
    measurements["thinking_budget_encodings"] = encoded
    # "indistinguishable from absent" is the finding, so it is measured against the
    # baseline rather than against a threshold: matching the no-knob generation IS
    # the evidence the server discarded the parameter.
    ignored_encodings = sorted(
        name
        for name, result in encoded.items()
        if result["completion_tokens"] == baseline["completion_tokens"]
    )
    report["measurements"]["thinking_budget_encodings_ignored"] = ignored_encodings
    report["verdicts"]["thinking_budget_malformed"] = (
        "silently-ignored" if ignored_encodings else "refused"
    )

    # ── chat_template_kwargs.enable_thinking ──
    # The knob τ's own docs have listed as unreachable since the first RPC review
    # (PI_RPC_REPLACEMENT.md §3.3). Nested, which is why a flat "name the field"
    # config shape cannot express it.
    ctk = _generate(url, {"chat_template_kwargs": {"enable_thinking": False}})
    measurements["chat_template_enable_thinking_false"] = ctk
    report["verdicts"]["chat_template_enable_thinking"] = (
        "honored" if ctk["reasoning_tokens"] < baseline["reasoning_tokens"] else "ignored"
    )

    report["server_features"] = sorted(
        name for name, verdict in report["verdicts"].items() if verdict in ("honored", "floor")
    )
    return report


def _render(report: dict[str, Any]) -> str:
    lines = [
        f"llama conformance probe  {report['probed_at']}",
        f"  endpoint     {report['url']}",
        f"  build        {report['server']['build_info']}  "
        f"(launch flags are NOT reported by this server; see thinking_budget 0 below)",
        f"  model        {report['server']['model_path']}",
        f"  reasoning    chat_format={report['server']['chat_format']} "
        f"reasoning_format={report['server']['reasoning_format']}",
        f"  deterministic  {report['deterministic']}",
        "",
        f"  {'knob':<34}{'verdict':<20}reasoning tokens",
        f"  {'-' * 34}{'-' * 20}{'-' * 16}",
    ]
    m = report["measurements"]
    base = m["baseline"]["reasoning_tokens"]
    lines.append(f"  {'(baseline, no knob)':<34}{'-':<20}{base}")
    efforts = m["reasoning_effort"]
    effort_counts = "/".join(str(efforts[k]["reasoning_tokens"]) for k in ("high", "low", "banana"))
    lines.append(
        f"  {'reasoning_effort high/low/banana':<34}"
        f"{report['verdicts']['reasoning_effort']:<20}{effort_counts}"
    )
    budgets = m["thinking_budget_tokens"]
    budget_counts = "/".join(str(budgets[str(b)]["reasoning_tokens"]) for b in BUDGET_SWEEP)
    lines.append(
        f"  {'thinking_budget_tokens ' + '/'.join(map(str, BUDGET_SWEEP)):<34}"
        f"{report['verdicts']['thinking_budget_tokens']:<20}{budget_counts}"
    )
    lines.append(
        f"  {'thinking_budget_tokens 0':<34}"
        f"{report['verdicts']['thinking_budget_zero']:<20}{m['thinking_budget_zero']['reasoning_tokens']}"
    )
    lines.append(
        f"  {'thinking_budget malformed':<34}"
        f"{report['verdicts']['thinking_budget_malformed']:<20}"
        f"{','.join(m['thinking_budget_encodings_ignored']) or '(none ignored)'}"
    )
    lines.append(
        f"  {'chat_template enable_thinking=F':<34}"
        f"{report['verdicts']['chat_template_enable_thinking']:<20}"
        f"{m['chat_template_enable_thinking_false']['reasoning_tokens']}"
    )
    lines += ["", f"  server_features: {report['server_features']}"]
    if not report["deterministic"]:
        lines += [
            "",
            "  WARNING: two identical requests produced different output. Every",
            "  'ignored' verdict above compares outputs and is UNSOUND on this",
            "  endpoint. Fix determinism (temperature/seed/slot reuse) and rerun.",
        ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--url", default=DEFAULT_URL, help="endpoint (default: $TAU_LLAMA_TEST_URL)"
    )
    parser.add_argument("--json", dest="json_out", help="write the full report to this path")
    args = parser.parse_args()

    try:
        report = probe(args.url)
    except Unreachable as exc:
        print(f"unreachable: {exc}", file=sys.stderr)
        return 2

    print(_render(report))
    if args.json_out:
        with open(args.json_out, "w") as handle:
            json.dump(report, handle, indent=2)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
