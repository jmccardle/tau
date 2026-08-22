"""Probe OpenAI-compatible backends and compare reality against τ's detection.

For each endpoint this measures, from the outside:

  raw_max_tokens        does a bare `max_tokens` request succeed?
  raw_max_completion    does a bare `max_completion_tokens` request succeed?
  raw_stream_options    does `stream_options: {include_usage: true}` succeed?
  tau_stream            does τ complete a streamed turn?
  tau_buffered          does τ complete a `stream: false` turn?
  tau_tool_call         does a streamed tool call arrive with a name?
  usage                 are token counts non-zero?
  finish_reason         did the stream carry one?
  retry headers         does the server say WHEN to try again?

Then it prints what `detect_compat` claims for the same endpoint, so the two can
be compared. The raw probes go under τ deliberately: τ's job is to PICK a
spelling, so asking τ which one works would be circular.

The retry measurement is under τ for a harder reason: τ reads no response headers
at all. `_error_event_from_response` (providers/openai.py:1483) formats the status
into a message STRING and drops the rest, so a `retry-after` that arrived could
not be recovered anywhere above the provider. Asking τ what the server said about
retrying would return nothing on every backend, correct or not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from tau_llm.compat import detect_compat
from tau_llm.streaming import DoneEvent, ErrorEvent, TextDeltaEvent
from tau_llm.tools import define_tool
from tau_llm.types import Model
from tau_llm.client import stream_simple

PROMPT = "Reply with the single word: ok"
TOOL_PROMPT = "What is the weather in Paris? Use the get_weather tool."

WEATHER_TOOL = define_tool(
    name="get_weather",
    label="Get weather",
    description="Get the current weather for a city.",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name"}},
        "required": ["city"],
    },
    execute=lambda city: {"weather": "sunny"},
)


@dataclass
class Endpoint:
    label: str
    base_url: str
    model_id: str
    api_key: str | None = None
    provider: str = "openai"
    max_tokens: int = 32
    skip: str | None = None
    gap: float = 0.0   # seconds to wait between probes (rate-limited free tiers)
    notes: list[str] = field(default_factory=list)


def _headers(ep: Endpoint) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if ep.api_key:
        h["Authorization"] = f"Bearer {ep.api_key}"
    return h


# The headers a backoff implementation could act on, lowercased because httpx
# matches case-insensitively but a JSON result file does not.
#
# `retry-after` is the standard (RFC 9110 §10.2.3) and carries either a delay in
# seconds or an HTTP date. The `x-ratelimit-*` family is OpenAI's, echoed by most
# gateways that imitate it, and is the only one that says anything BEFORE the
# limit is hit. `x-should-retry` is Anthropic's explicit override, which pi reads
# in `utils/provider-retry.ts` — a server saying "don't bother" outranks any
# status-code heuristic.
_RETRY_HEADERS = (
    "retry-after",
    "x-should-retry",
    "x-ratelimit-limit-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
)


def _retry_evidence(r: httpx.Response) -> dict[str, Any]:
    """What this response says about when to try again.

    Recorded for EVERY response, not only for a 429, because the two interesting
    findings are opposite shapes. A 429 with no `retry-after` means a backoff
    implementation has nothing to honour and must fall back to exponential — and
    that absence has to be written down, not inferred from a missing key. A 200
    carrying `x-ratelimit-remaining-requests` means the budget is knowable before
    the limit is hit at all.

    `present` is therefore always a list, empty when nothing was sent, and
    `headers` holds only what actually arrived. The distinction that matters is
    "asked and got nothing" versus "never asked", and a missing key cannot tell
    those apart.
    """
    found = {h: r.headers[h] for h in _RETRY_HEADERS if h in r.headers}
    ev: dict[str, Any] = {
        "status": r.status_code,
        "present": sorted(found),
        "headers": found,
    }
    # Some gateways put the interval in the BODY instead of a header
    # ("try again in 47s"), which no header-only reader would find. Keep the
    # body verbatim on a rejection so a later reading can look for one; a 2xx
    # body is the completion itself and is not evidence about retrying.
    if r.status_code // 100 != 2:
        ev["body"] = r.text[:400].strip()
    return ev


def _raw(
    ep: Endpoint, body: dict[str, Any], *, stream: bool = False
) -> tuple[bool | None, str, dict[str, Any] | None]:
    """POST a body verbatim. Returns (ok, detail, retry_evidence).

    `retry_evidence` is None only when no response arrived at all (a transport
    exception): there were no headers to read, which is different from a
    response that carried none.
    """
    payload = {
        "model": ep.model_id,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": stream,
        **body,
    }
    try:
        r = httpx.post(
            f"{ep.base_url.rstrip('/')}/chat/completions",
            headers=_headers(ep),
            json=payload,
            timeout=120.0,
        )
    except Exception as exc:
        # A transport failure says nothing about the field either.
        return None, f"{type(exc).__name__}: {exc} (field not evaluated)", None
    ev = _retry_evidence(r)
    if r.status_code // 100 == 2:
        return True, f"{r.status_code}", ev
    # A 429 says nothing about whether the FIELD is accepted — it is the tier
    # answering before the body was ever looked at. Reported as unknown (None)
    # so a rate limit is never read as "this server rejects this spelling".
    #
    # It says a great deal about RETRYING, though, which is why `ev` is carried
    # out alongside the verdict: PLAN-0.9.3 §4.3 wants backoff, and whether these
    # servers name their own interval is the question that decides whether τ can
    # honour one or must guess. Measure it; do not assume the header is there.
    if r.status_code == 429:
        named = ", ".join(ev["present"]) if ev["present"] else "no retry headers"
        return None, f"{r.status_code} rate-limited (field not evaluated; {named})", ev
    # 5xx is the gateway or origin failing, not the body being judged. Measured
    # against UnoRouter, whose non-streaming path takes 95-125s and trips
    # Cloudflare's 100s ceiling with a 524 — which read as "rejects max_tokens"
    # until the same field returned 200 in 1.6s on the streamed path.
    if r.status_code // 100 == 5:
        return None, f"{r.status_code} upstream failure (field not evaluated)", ev
    return False, f"{r.status_code} {r.text[:160].strip()}", ev


def _model(ep: Endpoint, **overrides) -> Model:
    kwargs: dict[str, Any] = {
        "id": ep.model_id,
        "name": ep.model_id,
        "api": "openai-completions",
        "provider": ep.provider,
        "base_url": ep.base_url,
        "context_window": 4096,
        "max_tokens": ep.max_tokens,
    }
    kwargs.update(overrides)
    return Model(**kwargs)


async def _tau_turn(
    ep: Endpoint, *, stream: bool, tools: list | None = None, max_tokens: int | None = None
) -> dict[str, Any]:
    """Run one τ turn and summarise what came back."""
    if ep.api_key:
        os.environ["OPENAI_API_KEY"] = ep.api_key
    out: dict[str, Any] = {
        "ok": False,
        "detail": "",
        "text": "",
        "tool_calls": [],
        "usage": None,
        "stop_reason": None,
        "deltas": 0,
    }
    context: dict[str, Any] = {
        "messages": [{"role": "user", "content": TOOL_PROMPT if tools else PROMPT}],
    }
    if tools:
        context["tools"] = tools
    try:
        m = _model(ep, max_tokens=max_tokens) if max_tokens else _model(ep)
        events = await stream_simple(m, context, {"stream": stream})
        async for ev in events:
            if isinstance(ev, TextDeltaEvent):
                out["deltas"] += 1
                out["text"] += ev.delta
            elif isinstance(ev, ErrorEvent):
                out["detail"] = ev.message[:200]
            elif isinstance(ev, DoneEvent):
                final = ev.final
                out["ok"] = final.stop_reason != "error"
                out["stop_reason"] = final.stop_reason
                if final.error_message:
                    out["detail"] = final.error_message[:200]
                out["usage"] = {
                    "input": final.usage.input_tokens,
                    "output": final.usage.output_tokens,
                }
                out["tool_calls"] = [
                    {"name": tc.name, "args": tc.arguments} for tc in final.get_tool_calls()
                ]
                if not out["text"]:
                    out["text"] = "".join(
                        b.text for b in final.content if getattr(b, "type", "") == "text"
                    )
    except Exception as exc:
        out["detail"] = f"{type(exc).__name__}: {exc}"[:200]
    return out


def probe(ep: Endpoint) -> dict[str, Any]:
    started = time.time()
    result: dict[str, Any] = {"label": ep.label, "base_url": ep.base_url, "model": ep.model_id}

    detected = detect_compat(ep.provider, ep.base_url)
    result["detected"] = {
        "max_tokens_field": detected.max_tokens_field,
        "supports_usage_in_streaming": detected.supports_usage_in_streaming,
    }

    if ep.skip:
        result["skipped"] = ep.skip
        return result

    def pace():
        if ep.gap:
            time.sleep(ep.gap)

    ok, detail, ev = _raw(ep, {"max_tokens": 16})
    result["raw_max_tokens"] = {"ok": ok, "detail": detail, "retry": ev}
    pace()
    ok, detail, ev = _raw(ep, {"max_completion_tokens": 16})
    result["raw_max_completion_tokens"] = {"ok": ok, "detail": detail, "retry": ev}
    pace()
    ok, detail, ev = _raw(
        ep, {"max_tokens": 16, "stream_options": {"include_usage": True}}, stream=True
    )
    result["raw_stream_options"] = {"ok": ok, "detail": detail, "retry": ev}
    pace()

    result["tau_stream"] = asyncio.run(_tau_turn(ep, stream=True))
    pace()
    result["tau_buffered"] = asyncio.run(_tau_turn(ep, stream=False))
    pace()
    result["tau_tool_call"] = asyncio.run(
        _tau_turn(ep, stream=True, tools=[WEATHER_TOOL], max_tokens=256)
    )

    result["seconds"] = round(time.time() - started, 1)
    return result


def _mark(v: bool | None) -> str:
    return {True: "yes", False: "NO", None: "-"}[v]


def render(results: list[dict[str, Any]]) -> str:
    lines = []
    for r in results:
        lines.append("=" * 78)
        lines.append(f"{r['label']}  —  {r['model']}")
        lines.append(f"  {r['base_url']}")
        if "skipped" in r:
            lines.append(f"  SKIPPED: {r['skipped']}")
            lines.append(
                f"  τ would detect: {r['detected']['max_tokens_field']}, "
                f"stream_options={r['detected']['supports_usage_in_streaming']}"
            )
            continue

        raw_mt = r["raw_max_tokens"]["ok"]
        raw_mct = r["raw_max_completion_tokens"]["ok"]
        if raw_mt is None or raw_mct is None:
            truth = "unknown (rate-limited)"
        elif raw_mt and raw_mct:
            truth = "both"
        elif raw_mt:
            truth = "max_tokens"
        elif raw_mct:
            truth = "max_completion_tokens"
        else:
            truth = "neither"
        detected_field = r["detected"]["max_tokens_field"]
        if truth.startswith("unknown"):
            verdict = "UNTESTED"
        elif truth in (detected_field, "both"):
            verdict = "OK"
        else:
            verdict = "MISMATCH"

        lines.append(f"  accepts max_tokens ............ {_mark(raw_mt)}   {r['raw_max_tokens']['detail']}")
        lines.append(
            f"  accepts max_completion_tokens . {_mark(raw_mct)}   {r['raw_max_completion_tokens']['detail']}"
        )
        lines.append(f"  accepts stream_options ........ {_mark(r['raw_stream_options']['ok'])}   {r['raw_stream_options']['detail']}")
        lines.append(f"  τ detects ..................... {detected_field}  [{verdict}, server takes: {truth}]")

        # Retry evidence, pooled across the three raw probes (PLAN-0.9.3 §4.3).
        # Printed for every endpoint including the ones that never failed, because
        # "this server sent no rate-limit headers on a successful call either" is
        # the finding that says a backoff cannot be informed here.
        seen: dict[str, str] = {}
        statuses: set[int] = set()
        for key in ("raw_max_tokens", "raw_max_completion_tokens", "raw_stream_options"):
            ev = r[key].get("retry")
            if not ev:
                continue
            statuses.add(ev["status"])
            seen.update(ev["headers"])
        if seen:
            shown = ", ".join(f"{k}={v}" for k, v in sorted(seen.items()))
            lines.append(f"  retry headers ................. {shown}")
        else:
            got = ", ".join(str(s) for s in sorted(statuses)) or "no response"
            lines.append(f"  retry headers ................. none sent (statuses: {got})")

        for key, name in (
            ("tau_stream", "τ streamed turn"),
            ("tau_buffered", "τ buffered turn"),
            ("tau_tool_call", "τ tool call"),
        ):
            t = r[key]
            bits = [_mark(t["ok"])]
            if t["usage"]:
                bits.append(f"usage in={t['usage']['input']} out={t['usage']['output']}")
            if key == "tau_stream":
                bits.append(f"{t['deltas']} deltas")
            if key == "tau_tool_call":
                calls = t["tool_calls"]
                bits.append(f"{len(calls)} calls: {[c['name'] for c in calls]}")
                if calls:
                    bits.append(f"args={calls[0]['args']}")
            if t["stop_reason"]:
                bits.append(f"stop={t['stop_reason']}")
            if t["detail"]:
                bits.append(f"! {t['detail']}")
            lines.append(f"  {name:.<29} " + "  ".join(bits))
        lines.append(f"  ({r['seconds']}s)")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="substring filter on endpoint label")
    ap.add_argument("--json", help="also write raw results here")
    args = ap.parse_args()

    cfg_path = os.path.expanduser("~/.tau/config.json")
    cfg = json.load(open(cfg_path)) if os.path.exists(cfg_path) else {}
    models = cfg.get("models") or {}

    def key_for(name: str) -> str | None:
        return (models.get(name) or {}).get("api_key")

    endpoints = [
        Endpoint(
            label="llama.cpp b10567 (Llama-3.2-1B-Q8, CPU)",
            base_url="http://127.0.0.1:8091/v1",
            model_id="llama-3.2-1b-instruct",
            provider="local-llm",
            api_key="not-needed",
        ),
        Endpoint(
            label="Ollama 0.32.15 (llama3.2:1b, CPU)",
            base_url="http://127.0.0.1:11435/v1",
            model_id="llama3.2:1b",
            provider="ollama",
            api_key="not-needed",
        ),
        Endpoint(
            label="vLLM 0.27.1 CPU (Qwen2.5-Coder-0.5B-Instruct, tool parser on)",
            base_url="http://127.0.0.1:8000/v1",
            model_id="Qwen/Qwen2.5-Coder-0.5B-Instruct",
            provider="vllm",
            api_key="not-needed",
        ),
        Endpoint(
            label="LM Studio (app not running)",
            base_url="http://127.0.0.1:1234/v1",
            model_id="(any)",
            provider="lmstudio",
            api_key="not-needed",
            skip="LM Studio desktop app is not running; `lms` cannot wake it (stale AppImage mount at /tmp/.mount_LM-StuB1Abbg)",
        ),
        Endpoint(
            label="llama.cpp remote (Qwen3.8-27B-Q4)",
            base_url="http://192.168.1.100:8080/v1",
            model_id="/fast/Qwen3.8-27B-Q4_0.gguf",
            provider="local-llm",
            api_key=key_for("local-llm") or "not-needed",
        ),
        Endpoint(
            label="UnoRouter (glm-5.2:free)",
            base_url="https://api.unorouter.com/v1",
            model_id="glm-5.2:free",
            provider="unorouter",
            api_key=key_for("glm-5.2:free"),
            gap=65.0,
        ),
        Endpoint(
            label="OpenAI (gpt-4o)",
            base_url="https://api.openai.com/v1",
            model_id="gpt-4o",
            provider="openai",
            api_key=key_for("gpt-4o"),
        ),
    ]

    extra = os.environ.get("PROBE_EXTRA")
    if extra:
        for spec in json.loads(extra):
            endpoints.append(Endpoint(**spec))

    if args.only:
        endpoints = [e for e in endpoints if args.only.lower() in e.label.lower()]

    results = []
    for ep in endpoints:
        if ep.api_key is None and "://" in ep.base_url and not ep.base_url.startswith("http://"):
            ep.skip = "no API key in ~/.tau/config.json"
        print(f"probing {ep.label} …", file=sys.stderr, flush=True)
        results.append(probe(ep))

    print(render(results))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
