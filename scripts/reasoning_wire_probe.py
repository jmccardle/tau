#!/usr/bin/env python3
"""What each thinking level changes on the wire, per provider: raw HTTP, no SDK.

Reference: docs/REASONING-WIRE.md §1, docs/probe-results/reasoning-wire-2026-10-17.md.

Reads keys from ``<τ home>/config.json`` by model ENTRY name and never prints one;
the τ home is the only τ code it uses, and nothing τ builds reaches the wire.
Prints one JSON line per arm: what was sent, status, reasoning tokens, visible
reasoning characters, the stop reason and which reasoning fields came back.

Usage::

    scripts/reasoning_wire_probe.py anthropic --entry my-anthropic-entry
    scripts/reasoning_wire_probe.py google --entry my-gemini-entry
    scripts/reasoning_wire_probe.py google-replay --entry my-gemini-entry
    scripts/reasoning_wire_probe.py openai --entry my-openai-entry
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

import httpx
from tau_agent_core.tau_home import resolve_tau_home

PROMPT = "Is 391 prime? Think it through, then answer in one line."


def _entry(name: str) -> dict[str, Any]:
    """The config entry ``name``, from the user's ``<τ home>/config.json``."""
    with open(resolve_tau_home() / "config.json") as fh:
        models: dict[str, Any] = json.load(fh)["models"]
    return dict(models[name])


def _emit(row: dict[str, Any]) -> None:
    """Print one result line."""
    print(json.dumps(row), flush=True)


def anthropic(entry: dict[str, Any], model: str) -> None:
    """Each thinking setting once against the Messages API."""
    arms: list[tuple[str, dict[str, Any]]] = [
        ("no thinking field", {}),
        ("disabled", {"thinking": {"type": "disabled"}}),
    ]
    arms += [
        (
            f"adaptive summarized effort={e}",
            {
                "thinking": {"type": "adaptive", "display": "summarized"},
                "output_config": {"effort": e},
            },
        )
        for e in ("low", "medium", "high", "xhigh", "max")
    ]
    arms += [
        ("effort=minimal", {"thinking": {"type": "adaptive"}, "output_config": {"effort": "minimal"}}),
        ("disabled effort=xhigh", {"thinking": {"type": "disabled"}, "output_config": {"effort": "xhigh"}}),
    ]
    base = entry.get("base_url") or "https://api.anthropic.com"
    for label, extra in arms:
        body = {"model": model, "max_tokens": 2000, "messages": [{"role": "user", "content": PROMPT}]}
        t0 = time.monotonic()
        r = httpx.post(
            f"{base}/v1/messages",
            json={**body, **extra},
            timeout=180,
            headers={"x-api-key": entry["api_key"], "anthropic-version": "2023-06-01"},
        )
        row: dict[str, Any] = {"wire": "anthropic", "model": model, "arm": label, "sent": extra,
                               "status": r.status_code, "seconds": round(time.monotonic() - t0, 2)}
        j = r.json()
        if r.status_code != 200:
            row["error"] = j.get("error", {}).get("message", "")[:300]
            _emit(row)
            continue
        blocks = j["content"]
        row["blocks"] = [b["type"] for b in blocks]
        row["thinking_chars"] = sum(len(b.get("thinking", "")) for b in blocks if b["type"] == "thinking")
        row["signature_chars"] = sum(len(b.get("signature", "")) for b in blocks if b["type"] == "thinking")
        u = j["usage"]
        row["input_tokens"] = u.get("input_tokens")
        row["output_tokens"] = u.get("output_tokens")
        row["output_tokens_details"] = u.get("output_tokens_details")
        row["stop_reason"] = j.get("stop_reason")
        _emit(row)


def _gemini_call(entry: dict[str, Any], model: str, contents: list[Any], config: dict[str, Any] | None) -> httpx.Response:
    """One non-streamed ``generateContent`` call."""
    generation: dict[str, Any] = {"maxOutputTokens": 3000}
    if config is not None:
        generation["thinkingConfig"] = config
    return httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        json={"contents": contents, "generationConfig": generation},
        timeout=180,
        headers={"x-goog-api-key": entry["api_key"]},
    )


def google(entry: dict[str, Any], model: str) -> None:
    """Each thinkingConfig shape once against generateContent."""
    arms: list[tuple[str, dict[str, Any] | None]] = [
        ("no thinkingConfig", None),
        ("includeThoughts only", {"includeThoughts": True}),
    ]
    arms += [(f"thinkingLevel={lv}", {"includeThoughts": True, "thinkingLevel": lv})
             for lv in ("minimal", "low", "medium", "high", "xhigh")]
    arms += [
        ("thinkingBudget=0", {"includeThoughts": False, "thinkingBudget": 0}),
        ("thinkingBudget=1024", {"includeThoughts": True, "thinkingBudget": 1024}),
        ("level+budget", {"thinkingLevel": "low", "thinkingBudget": 1024}),
    ]
    contents = [{"role": "user", "parts": [{"text": PROMPT}]}]
    for label, config in arms:
        t0 = time.monotonic()
        r = _gemini_call(entry, model, contents, config)
        row: dict[str, Any] = {"wire": "google", "model": model, "arm": label, "sent": config,
                               "status": r.status_code, "seconds": round(time.monotonic() - t0, 2)}
        j = r.json()
        if r.status_code != 200:
            row["error"] = j.get("error", {}).get("message", "")[:300]
            _emit(row)
            continue
        cand = j["candidates"][0]
        parts = cand.get("content", {}).get("parts", [])
        row["thought_chars"] = sum(len(p.get("text", "")) for p in parts if p.get("thought"))
        row["parts_with_signature"] = sum(1 for p in parts if p.get("thoughtSignature"))
        row["usage"] = j.get("usageMetadata")
        row["finish_reason"] = cand.get("finishReason")
        _emit(row)


def google_replay(entry: dict[str, Any], model: str) -> None:
    """Does Gemini read a replayed answer signature? Turn-2 prompt tokens and recall, per arm."""
    config = {"thinkingLevel": "high"}
    u1 = {"role": "user", "parts": [{"text": "Pick a secret integer between 1 and 1000 by "
                                              "reasoning about it, then reply only 'Chosen.'"}]}
    t1 = _gemini_call(entry, model, [u1], config).json()
    parts = t1["candidates"][0]["content"]["parts"]
    _emit({"turn": 1, "parts": [{"text_len": len(p.get("text", "")),
                                  "signature_len": len(p.get("thoughtSignature", ""))} for p in parts],
           "usage": t1.get("usageMetadata")})
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    signature = next((p["thoughtSignature"] for p in parts if p.get("thoughtSignature")), "")
    u2 = {"role": "user", "parts": [{"text": "What was your secret number? Reply with the number only."}]}
    arms = {
        "signature on text part": [{"text": text, "thoughtSignature": signature}],
        "signature on empty trailing part": [{"text": text}, {"text": "", "thoughtSignature": signature}],
        "no signature": [{"text": text}],
    }
    for label, model_parts in arms.items():
        r = _gemini_call(entry, model, [u1, {"role": "model", "parts": model_parts}, u2], config)
        b = r.json()
        if r.status_code != 200:
            _emit({"turn": 2, "arm": label, "status": r.status_code,
                   "error": b.get("error", {}).get("message", "")[:300]})
            continue
        answer = "".join(p.get("text", "") for p in b["candidates"][0]["content"]["parts"] if not p.get("thought"))
        _emit({"turn": 2, "arm": label, "status": 200,
               "prompt_tokens": b["usageMetadata"].get("promptTokenCount"),
               "thoughts_tokens": b["usageMetadata"].get("thoughtsTokenCount"), "answer": answer[:40]})


def openai(entry: dict[str, Any], model: str) -> None:
    """Each reasoning_effort once against Chat Completions."""
    base = entry.get("base_url") or "https://api.openai.com/v1"
    arms: list[tuple[str, dict[str, Any]]] = [("no reasoning_effort", {})]
    arms += [(f"reasoning_effort={e}", {"reasoning_effort": e})
             for e in ("none", "minimal", "low", "medium", "high", "xhigh")]
    for label, extra in arms:
        body = {"model": model, "messages": [{"role": "user", "content": PROMPT}],
                "max_completion_tokens": 2000, **extra}
        t0 = time.monotonic()
        r = httpx.post(f"{base}/chat/completions", json=body, timeout=180,
                       headers={"authorization": f"Bearer {entry['api_key']}"})
        row: dict[str, Any] = {"wire": "openai-completions", "model": model, "arm": label, "sent": extra,
                               "status": r.status_code, "seconds": round(time.monotonic() - t0, 2)}
        j = r.json()
        if r.status_code != 200:
            err = j.get("error")
            row["error"] = (err.get("message") if isinstance(err, dict) else str(err))[:300]
            _emit(row)
            continue
        choice = j["choices"][0]
        message = choice.get("message", {})
        row["message_keys"] = sorted(message)
        for field in ("reasoning_content", "reasoning", "reasoning_text"):
            if isinstance(message.get(field), str):
                row[f"{field}_chars"] = len(message[field])
        row["usage"] = j.get("usage")
        row["finish_reason"] = choice.get("finish_reason")
        _emit(row)


def main() -> None:
    """Parse arguments and run one probe."""
    probes = {"anthropic": anthropic, "google": google, "google-replay": google_replay, "openai": openai}
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("probe", choices=sorted(probes))
    parser.add_argument("--entry", required=True, help="a model entry name in <τ home>/config.json")
    parser.add_argument("--model", help="the wire model id; defaults to the entry's `model`")
    args = parser.parse_args()
    entry = _entry(args.entry)
    probes[args.probe](entry, args.model or entry["model"])


if __name__ == "__main__":
    main()
