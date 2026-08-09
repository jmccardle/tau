#!/usr/bin/env python3
"""W1 — live grammar spike against llama-server's OpenAI-compat endpoint.

Decides the W3 (G1) wire mapping empirically. The recon only ever tested the
NATIVE /completion endpoint; tau talks to /v1/chat/completions. Source reading says
oaicompat_chat_params_parse accepts grammar/json_schema/response_format, and that
grammar+tools raises ONLY when tool_choice != "none". Verify all of it for real.

THINKING MODELS: Qwen3/Qwen3.6 with --jinja emit <think> first. With a small
max_tokens the whole budget is spent reasoning and `content` comes back EMPTY —
which looks exactly like "the grammar produced nothing". It is not a grammar
failure. Every request here suppresses thinking and leaves generous headroom, so
an empty `content` means what it should mean.

Run:  venv/bin/python scripts/w1_grammar_spike.py [base_url]
Exits non-zero if any load-bearing assertion fails.
"""

import json
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.1.100:8080"
URL = f"{BASE}/v1/chat/completions"

VERDICT_GRAMMAR = '%llguidance {}\nstart: "include" | "exclude" | "examine-children"'
VERDICTS = {"include", "exclude", "examine-children"}

# Suppress <think> so the token budget buys us an ANSWER, not reasoning.
NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}, "max_tokens": 400}

VERDICT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "verdict",
        "schema": {
            "type": "object",
            "properties": {"verdict": {"type": "string", "enum": ["include", "exclude"]}},
            "required": ["verdict"],
        },
    },
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file from disk",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    }
]

MSGS = [{"role": "user", "content": "Should this document be included? Answer with the verdict."}]
# Phrased so a tool-capable model MUST want the tool: the answer is not in context.
TOOL_MSGS = [{"role": "user", "content": "Read the file /etc/hostname and tell me what it says."}]

failures: list[str] = []


def post(name: str, body: dict) -> dict | None:
    """POST and return {'status', 'content', 'tool_calls', 'detail'}, printing as we go."""
    try:
        r = httpx.post(URL, json={"model": "q", **NO_THINK, **body}, timeout=180)
    except Exception as exc:  # noqa: BLE001
        print(f"\n{name}\n   EXC   : {exc}")
        return None

    if r.status_code != 200:
        detail = r.text[:200].replace("\n", " ")
        print(f"\n{name}\n   status: HTTP {r.status_code}\n   detail: {detail}")
        return {"status": r.status_code, "detail": detail, "content": None, "tool_calls": []}

    msg = r.json()["choices"][0]["message"]
    content = msg.get("content")
    tcalls = msg.get("tool_calls") or []
    print(f"\n{name}\n   status: 200")
    print(f"   content   : {content!r}")
    print(f"   tool_calls: {[t['function']['name'] for t in tcalls] or '(none)'}")
    for t in tcalls:
        print(f"      -> {t['function']['name']}({t['function']['arguments']})")
    return {"status": 200, "detail": "", "content": content, "tool_calls": tcalls}


def check(label: str, ok: bool, why: str) -> None:
    print(f"   {'PASS' if ok else 'FAIL'}: {label} — {why}")
    if not ok:
        failures.append(label)


print("=" * 96)
print(f"W1 SPIKE — {URL}")
print("=" * 96)

# --- 1. Raw llguidance grammar constrains output. ------------------------------
r = post("1. grammar (llguidance), no tools", {"messages": MSGS, "grammar": VERDICT_GRAMMAR})
if r:
    check(
        "grammar constrains output",
        (r["content"] or "").strip() in VERDICTS,
        f"output must be one of {sorted(VERDICTS)}",
    )

# --- 2. response_format json_schema constrains output. -------------------------
r = post("2. response_format json_schema", {"messages": MSGS, "response_format": VERDICT_SCHEMA})
if r:
    try:
        parsed = json.loads(r["content"] or "")
        ok = parsed.get("verdict") in {"include", "exclude"}
    except Exception:  # noqa: BLE001
        ok = False
    check("json_schema constrains output", ok, "output must parse and match the schema")

# --- 3. Baseline: tools alone DO fire. -----------------------------------------
# Without this control, case 5's "no tool call" proves nothing — the model might
# simply not want the tool. This is the control that makes case 5 a real finding.
r = post("3. tools alone (the control)", {"messages": TOOL_MSGS, "tools": TOOLS})
tools_fire_unconstrained = bool(r and r["tool_calls"])
if r:
    check(
        "tools alone fire",
        tools_fire_unconstrained,
        "the control: the model wants this tool when unconstrained",
    )

# --- 4. grammar + tools + tool_choice=auto -> 400 (server-common.cpp:1067) ------
r = post(
    "4. grammar + tools (tool_choice=auto)",
    {"messages": TOOL_MSGS, "grammar": VERDICT_GRAMMAR, "tools": TOOLS, "tool_choice": "auto"},
)
if r:
    check(
        "grammar+tools is a LOUD 400",
        r["status"] == 400 and "grammar constraints with tools" in r["detail"],
        "the server refuses it explicitly — tau's raise mirrors a real failure",
    )

# --- 5. grammar + tools + tool_choice=none -> 200 (the refinement) --------------
r = post(
    "5. grammar + tools (tool_choice=none)",
    {"messages": MSGS, "grammar": VERDICT_GRAMMAR, "tools": TOOLS, "tool_choice": "none"},
)
if r:
    check(
        "grammar+tools+tool_choice=none is ALLOWED",
        r["status"] == 200 and (r["content"] or "").strip() in VERDICTS,
        "so a blanket 'constraints+tools -> raise' rule would be too strict",
    )

# --- 6. json_schema + tools -> THE FINDING --------------------------------------
# Undocumented, and the server has no equivalent check. Watch the tool call vanish.
r = post(
    "6. json_schema + tools (tool_choice=auto)",
    {"messages": TOOL_MSGS, "tools": TOOLS, "response_format": VERDICT_SCHEMA},
)
if r and tools_fire_unconstrained:
    check(
        "json_schema SILENTLY KILLS tool calling",
        r["status"] == 200 and not r["tool_calls"],
        "HTTP 200, no 400, tool call silently dropped vs the case-3 control "
        "— tau is the ONLY line of defence, hence the Fail-Early raise",
    )

# --- 7a. grammar + TOP-LEVEL json_schema -> loud 500 ----------------------------
r = post(
    "7a. grammar + top-level json_schema",
    {"messages": MSGS, "grammar": VERDICT_GRAMMAR, "json_schema": VERDICT_SCHEMA["json_schema"]["schema"]},
)
if r:
    check(
        "grammar + json_schema is rejected (loud)",
        r["status"] != 200,
        "server refuses both at once — the guard works on THIS field",
    )

# --- 7b. grammar + response_format -> THE SECOND FINDING -------------------------
# Same semantic collision, different field, and the guard does not see it: the grammar
# is silently discarded and response_format wins. See UPSTREAM-LLAMACPP-SILENT-OVERRIDES.md.
r = post(
    "7b. grammar + response_format",
    {"messages": MSGS, "grammar": VERDICT_GRAMMAR, "response_format": VERDICT_SCHEMA},
)
if r:
    got = (r["content"] or "").strip()
    check(
        "response_format SILENTLY OVERRIDES grammar",
        r["status"] == 200 and got not in VERDICTS,
        "200, no error, and the grammar is GONE — the mutual-exclusion check is keyed "
        "on `json_schema`, but `response_format` parses on another path and wins. "
        "This is why tau reserves grammar/json_schema/response_format: smuggled past "
        "DecodeConstraints they collide and the server picks a winner silently.",
    )

# --- 8. The final SSE chunk carries usage AND timings ---------------------------
print("\n8. streaming final chunk (G4 telemetry)")
usage_keys: list[str] = []
timings_keys: list[str] = []
same_chunk = False
with httpx.stream(
    "POST",
    URL,
    json={
        "model": "q",
        "messages": MSGS,
        **NO_THINK,
        "stream": True,
        "stream_options": {"include_usage": True},
    },
    timeout=180,
) as resp:
    for line in resp.iter_lines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        chunk = json.loads(line[6:])
        if "usage" in chunk and chunk["usage"]:
            usage_keys = sorted(chunk["usage"])
            timings_keys = sorted(chunk.get("timings") or {})
            same_chunk = bool(timings_keys)

print(f"   usage keys  : {usage_keys}")
print(f"   timings keys: {timings_keys}")
check(
    "timings rides the SAME chunk as usage",
    same_chunk,
    "so G4 is a few lines in _usage_from_openai, not a subsystem",
)
check(
    "n_ff_total absent on stock server",
    "n_ff_total" not in timings_keys,
    "jump-forward decoding is fork-only — a stock build cannot report forced-token share",
)

print("\n" + "=" * 96)
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("ALL ASSERTIONS PASSED — the W3 wire mapping and the Fail-Early rules are grounded.")
