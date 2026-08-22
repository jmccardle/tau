#!/usr/bin/env python3
"""Measure the per-model Google capabilities that docs/ANTHROPIC-GOOGLE-CLIENTS.md O2 leaves open.

O2 asks how τ should answer two per-model questions Google's converter needs —
`requires_tool_call_id` and `supports_multimodal_function_response` — and settles
the shape (a vendored table, overridable by a `Model` field, unknown models
landing on the safe branch) except for ONE thing:

    "For `requires_tool_call_id` it is not obvious [what safe means] — sending an
    id to a model that does not expect one may itself be rejected. That needs
    measuring before it is decided."

This script is that measurement. It also measures the failure O1's option B would
guard: whether a model without tool call ids actually mis-pairs two calls to the
same tool name in one turn.

    export GEMINI_API_KEY=...            # or write the key to ~/.tau/gemini-key
    python scripts/gemini_capability_probe.py
    python scripts/gemini_capability_probe.py --model gemini-3.6-flash --json out.json

The key file exists because an environment variable only reaches processes that
inherit it. A non-interactive, non-login shell reads neither ~/.bashrc nor
~/.profile, so `export` in a profile does not reach a tool that spawns `bash -c`.
The file also keeps the key out of process arguments, where `ps` would show it.

Deliberately standalone: no τ imports, no ~/.tau/config.json, `httpx` only. It is
meant to be runnable by whoever holds the key, including people who do not have τ
checked out — the same contract as scripts/llama_conformance_probe.py.

WHY A NON-200 IS DATA HERE, UNLIKE THE LLAMA PROBE
--------------------------------------------------
The llama probe treats any non-200 as a bug, because every request it sends is
one the server should accept. Half the requests here are sent precisely to find
out whether Google accepts them, so a 400 is the answer, not an error.

That inverts the Fail-Early burden: the script must prove the 400 came from the
thing under test and not from a malformed probe request. Every check that can
report "rejected" therefore carries a CONTROL — the same request with only the
field under test removed. If the control does not return 200, the check raises
instead of reporting a verdict. A measurement that cannot tell "Google refused
this field" from "I built the body wrong" is worse than no measurement.

Statuses that mean the probe itself cannot run — 401/403 (key) and 5xx (Google) —
raise Unreachable and exit 2 without verdicts, the same as a server that is down.

429 is a middle case, and MEASURED 2026-08-22 to be one: a run died on
`gemini-2.5-flash-image` with a rate limit while the key still had quota for
other models. Free-tier quota is per model, so a 429 during candidate selection
means "not this model, right now" and the probe tries the next candidate. Three
in a row means the key itself is spent and the run stops with nothing measured.

FREE-TIER LIMITS ARE THE DESIGN CONSTRAINT, NOT AN EDGE CASE (measured 2026-08-22)
----------------------------------------------------------------------------------
From the console's own rate-limit table: most text-out Gemini models allow **5
requests per minute** and **20 requests per day, per model**. 2.5 Flash Lite
allows 10 RPM.

Both numbers shape this script. The daily cap is why it shares one setup turn
across checks (7 requests per model, not 10) and why a half-measured model keeps
what it measured — a discarded partial costs a third of the day's allowance for
that model. The per-minute cap is why every request is paced, not just retries:
an earlier build sent 12 per minute against a limit of 5, and spent Gemini 3.5
Flash's entire day (22/20 requests) without completing a single measurement.

Pace with --rpm if a model's limit differs. Going faster does not get an answer
sooner; it converts the day's allowance into 429s.

LISTMODELS IS A CATALOGUE, NOT AN ACCESS LIST (measured 2026-08-22)
-------------------------------------------------------------------
`GET /models` returned `gemini-2.5-flash` with `generateContent` among its
`supportedGenerationMethods`, and `generateContent` on it returned 404: "no
longer available to new users … use models/gemini-3.6-flash". So the list
endpoint describes what the API has, not what this key may call, and automatic
selection built on it picks models that do not answer.

A 404 on the first call therefore marks the model unavailable, records Google's
message and any replacement it names, and skips that model's remaining checks.
Every other non-200 on a baseline still raises. The replacement is never
substituted automatically: retrying against a model the operator did not choose
would silently move what the run measured.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import struct
import sys
import time
import zlib
from datetime import datetime, timezone
from typing import Any

import httpx

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# No default key. Unset means "no endpoint", reported as Unreachable and a skip
# upstream — never a silent run against nothing.
API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""

#: Checked when neither environment variable is set. A file, because an
#: environment variable only reaches processes that inherit it: a non-interactive,
#: non-login shell reads neither ~/.bashrc nor ~/.profile, so `export` in a shell
#: profile does not reach a tool that spawns `bash -c`. Reading the key from a
#: file also keeps it out of process arguments, where `ps` would show it.
DEFAULT_KEY_FILE = os.path.expanduser("~/.tau/gemini-key")


def read_key_file(path: str) -> str:
    """The first non-empty line of `path`, or "" if it does not exist.

    Only the first line, so a file written with a trailing newline or a trailing
    comment still yields a usable key. A file that exists but holds nothing is an
    error, not an empty key: it means someone intended to put a key there.
    """
    if not os.path.exists(path):
        return ""
    with open(path) as handle:
        for line in handle:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return stripped
    raise Unreachable(f"{path} exists but holds no key")


def resolve_api_key(explicit_file: str | None = None) -> str:
    """Environment first, then the key file. Named so the order is testable."""
    if API_KEY:
        return API_KEY
    return read_key_file(explicit_file or DEFAULT_KEY_FILE)


# One tool, two cities, and a response payload that does NOT name the city. The
# omission is the experiment: if the payload said "Paris: 11", the model could
# recover the mapping from content no matter how the API paired the responses,
# and the check would pass while measuring nothing. With a bare number, only
# POSITION can disambiguate two identically-named functionResponses.
WEATHER_TOOL: dict[str, Any] = {
    "name": "get_temperature",
    "description": "Get the current temperature in a city, in Celsius.",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name"}},
        "required": ["city"],
    },
}

TWO_CITY_PROMPT = (
    "Get the current temperature in Paris and in Tokyo. "
    "Call get_temperature separately for each city, both in the same turn."
)

ONE_CITY_PROMPT = "Get the current temperature in Paris."


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> str:
    """A valid solid-colour RGB PNG, base64-encoded.

    Built rather than pasted so the size is a parameter. MEASURED 2026-08-22: a
    1x1 PNG is structurally valid and Google still refuses it — "Unable to
    process input image" — as an ORDINARY user part, nothing to do with nesting.
    An earlier build used one and reported that nested images were rejected; the
    plain-part control in the multimodal check is what caught that the image, not
    the nesting, was at fault.

    A degenerate input is the wrong instrument even when it parses.
    """
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode("ascii")


#: The image the multimodal check sends. Its content is irrelevant to the
#: question; only that Google will decode it, which the check proves per run
#: before drawing any conclusion from a rejection.
PROBE_PNG = _solid_png(64, 64, (32, 96, 160))


class Unreachable(Exception):
    """The probe cannot run: no key, a rejected key, a rate limit, or a Google 5xx.

    The caller skips. It is not a finding about any model's capabilities.
    """


class RateLimited(Unreachable):
    """Google returned 429 for one request.

    Its own class because free-tier quota is PER MODEL, so a 429 while trying
    candidates says "not this model, right now", not "the key is spent". The
    preflight records it and moves to the next candidate; a run of them in a row
    means the quota really is global and the probe stops.
    """


class ProbeError(RuntimeError):
    """The probe's own request was wrong, or a response lacked a field it claims.

    Always a bug in this script or a change in the API's shape. Never reported as
    a model capability.
    """


# ──────────────────────────────────────────────────────────────────────────
# Transport
# ──────────────────────────────────────────────────────────────────────────


def _retry_delay_seconds(payload: dict[str, Any]) -> float | None:
    """The server's own `retryDelay` from a 429 body, in seconds, if it gives one.

    Google puts a RetryInfo in `error.details` with a duration like "31s". Waiting
    the interval the server ASKED for is not a workaround for a rate limit — it is
    how the documented protocol is used. Guessing an interval would be.
    """
    for detail in payload.get("error", {}).get("details", []) or []:
        raw = detail.get("retryDelay")
        if isinstance(raw, str):
            match = re.fullmatch(r"(\d+(?:\.\d+)?)s", raw.strip())
            if match:
                return float(match.group(1))
    return None


#: Longest single server-requested pause the probe will sit through.
MAX_RETRY_WAIT = 65.0

#: Retries per request. MEASURED 2026-08-22: one was not enough — the first 429
#: named a 37s delay, and the request after that wait was refused again with a
#: retryDelay of "0s". A server that says 0 is not asking for no wait; it is
#: declining to name one, so the probe uses its own interval for that case.
MAX_RETRIES = 3
UNSPECIFIED_BACKOFF = 20.0

#: Total time this run will spend waiting on rate limits before giving up. A cap
#: because an unattended probe that sleeps indefinitely against a spent key looks
#: exactly like one that is working.
MAX_TOTAL_WAIT = 240.0

#: Requests per minute the probe will not exceed. MEASURED 2026-08-22 from the
#: free tier's own rate-limit table: most text-out Gemini models allow 5 RPM
#: (2.5 Flash Lite allows 10). An earlier 5-SECOND interval sent 12 per minute —
#: over double the limit — which is what produced a 429 storm and spent a model's
#: whole daily allowance without completing one measurement.
#:
#: Also from that table: RPD is 20 per model per day. At 7 requests per model
#: this probe fits, but only just, which is why nothing here retries loosely.
DEFAULT_RPM = 5

#: Derived at startup from --rpm. One extra second because the server's window is
#: not aligned to the probe's.
MIN_REQUEST_INTERVAL = 60.0 / DEFAULT_RPM + 1.0

#: Mutable run state. Lists rather than globals so the retry accounting is
#: readable in the report and testable without reimporting the module.
RETRY_COUNT = [0]
TOTAL_WAITED = [0.0]
LAST_REQUEST_AT = [0.0]


def _request(
    api_key: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    """Send one request and return (status, parsed body) WITHOUT raising on 4xx.

    A 400 is a measurement here, so it is returned rather than raised. Only the
    statuses that make every verdict meaningless raise.

    A 429 is retried up to MAX_RETRIES times, waiting the delay the server names
    (or UNSPECIFIED_BACKOFF when it names none), and the run gives up once it has
    spent MAX_TOTAL_WAIT waiting in total.
    """
    url = f"{BASE_URL}{path}"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    for attempt in range(MAX_RETRIES + 1):
        # Pace every request, not just retries. The limit is per minute, so
        # arriving under it is what avoids the 429; reacting to one is recovery.
        since = time.monotonic() - LAST_REQUEST_AT[0]
        if LAST_REQUEST_AT[0] and since < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - since)
        LAST_REQUEST_AT[0] = time.monotonic()

        try:
            if body is None:
                response = httpx.get(url, headers=headers, timeout=60.0)
            else:
                response = httpx.post(url, headers=headers, json=body, timeout=120.0)
        except httpx.TransportError as exc:
            raise Unreachable(f"{path}: {exc}") from exc

        if response.status_code in (401, 403):
            raise Unreachable(f"{path}: Google rejected the key ({response.status_code})")
        if response.status_code >= 500:
            raise Unreachable(f"{path}: Google returned {response.status_code}")

        if response.status_code == 429:
            try:
                body_429 = dict(response.json())
            except ValueError:
                body_429 = {}
            named = _retry_delay_seconds(body_429)
            # "0s" means the server declined to name an interval, not that it
            # wants an immediate retry — retrying at once just spends another
            # request against the same exhausted minute.
            delay = named if named else UNSPECIFIED_BACKOFF
            budget_left = MAX_TOTAL_WAIT - TOTAL_WAITED[0]
            if attempt < MAX_RETRIES and delay <= MAX_RETRY_WAIT and delay <= budget_left:
                RETRY_COUNT[0] += 1
                TOTAL_WAITED[0] += delay
                source = f"{named:g}s as asked" if named else f"{delay:g}s (server named none)"
                print(
                    f"   [429 on {path} — waiting {source}, attempt {attempt + 1}]",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            asked = f" (server asked for {named:g}s)" if named else ""
            raise RateLimited(
                f"{path}: rate limited (429){asked} after {attempt} retr"
                f"{'y' if attempt == 1 else 'ies'} and {TOTAL_WAITED[0]:g}s waited."
            )

        try:
            parsed = dict(response.json())
        except ValueError as exc:
            raise ProbeError(f"{path}: response was not JSON: {response.text[:300]}") from exc
        return response.status_code, parsed

    raise ProbeError(f"{path}: retry loop fell through")


def _generate(
    api_key: str, model: str, contents: list[dict[str, Any]], *, tools: bool = True
) -> tuple[int, dict[str, Any]]:
    body: dict[str, Any] = {
        "contents": contents,
        # Greedy. "The answer changed" must not be a sampling artifact.
        "generationConfig": {"temperature": 0},
    }
    if tools:
        body["tools"] = [{"functionDeclarations": [WEATHER_TOOL]}]
    return _request(api_key, f"/models/{model}:generateContent", body)


def _error_message(payload: dict[str, Any]) -> str:
    """The human-readable reason from a Google error body, or the raw body."""
    message = payload.get("error", {}).get("message")
    if isinstance(message, str) and message:
        return message
    return json.dumps(payload)[:400]


def _suggested_model(message: str) -> str | None:
    """The replacement model Google names in a retirement 404, if it names one.

    Recorded, never acted on. Retrying automatically against a model the operator
    did not choose would silently move what the run measured.
    """
    match = re.search(r"use models/([A-Za-z0-9._-]+)", message)
    return match.group(1) if match else None


# ──────────────────────────────────────────────────────────────────────────
# Response readers
# ──────────────────────────────────────────────────────────────────────────


def _parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = payload.get("candidates") or []
    if not candidates:
        return []
    return list(candidates[0].get("content", {}).get("parts") or [])


def _function_calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [p["functionCall"] for p in _parts(payload) if "functionCall" in p]


def _text(payload: dict[str, Any]) -> str:
    return "".join(p.get("text", "") for p in _parts(payload))


def _model_turn(payload: dict[str, Any]) -> dict[str, Any]:
    """The assistant turn verbatim, to be replayed as conversation history.

    Replayed rather than reconstructed: a reconstructed turn would drop
    thoughtSignature, and dropping it is itself a behaviour change that would
    contaminate every verdict downstream.
    """
    candidates = payload.get("candidates") or []
    if not candidates:
        raise ProbeError("no candidates in a response the probe needs to continue from")
    content = candidates[0].get("content")
    if not isinstance(content, dict):
        raise ProbeError("candidate carried no content")
    return content


# ──────────────────────────────────────────────────────────────────────────
# Model selection
# ──────────────────────────────────────────────────────────────────────────


def list_models(api_key: str) -> list[str]:
    """Every model this key can call generateContent on, newest naming first."""
    _, payload = _request(api_key, "/models")
    names = []
    for entry in payload.get("models", []):
        methods = entry.get("supportedGenerationMethods") or []
        if "generateContent" not in methods:
            continue
        name = str(entry.get("name", ""))
        names.append(name.removeprefix("models/"))
    return sorted(names)


def _gemini_major(model: str) -> int | None:
    """pi's own rule (google-shared.ts:115), reproduced so the probe can straddle it.

    pi answers `requiresToolCallId` with `major >= 3`. The point of this script is
    to test BOTH sides of that boundary, so the selection below needs the same
    parse pi uses — not to endorse it, but to make sure the sample includes a
    model on each side of the line the rule draws.
    """
    match = re.match(r"^gemini(?:-live)?-(\d+)", model.lower())
    return int(match.group(1)) if match else None


#: How many listed models to try per major version before giving that version up.
#: Bounded because each attempt costs a request, and because a version whose top
#: three candidates are all uncallable is retired, not unlucky.
CANDIDATES_PER_MAJOR = 3

#: Consecutive 429s that mean the KEY is spent rather than one model's quota.
RATE_LIMIT_GIVE_UP = 3

#: Task-specific variants that are the wrong instrument for this measurement.
#: MEASURED 2026-08-22: `gemini-2.5-flash-image` was picked as a candidate purely
#: because "flash" is in its name, and spent quota on a 429 before anything was
#: learned. This probe measures TOOL CALLING; an image, audio or embedding
#: endpoint cannot answer the question however well it ranks.
#:
#: A denylist ages, which is the exact objection O2 raises against pi's model
#: matching. It is acceptable HERE and nowhere else in this work: it only orders
#: a convenience default, `--model` overrides it entirely, and the run reports
#: what it filtered. It must not become the shape of τ's capability handling.
TASK_SPECIFIC_TOKENS = ("image", "tts", "audio", "embedding", "aqa", "imagen", "veo", "vision")


def _is_task_specific(model: str) -> bool:
    return any(token in model.lower() for token in TASK_SPECIFIC_TOKENS)


def select_candidates(available: list[str]) -> dict[int, list[str]]:
    """Ranked candidates per major Gemini version, preferring `flash`, avoiding previews.

    A LIST per version, not one model, because the catalogue lists models this key
    cannot call (see the module docstring). One guess per version turned a
    retirement into a dead end for that whole side of pi's boundary; the probe
    walks the list instead and reports what it skipped.

    Straddling the >= 3 boundary is the whole point: a run that only saw one side
    could not tell "Google ignores the id" from "this model happens to accept it".
    """
    by_major: dict[int, list[str]] = {}
    for model in available:
        major = _gemini_major(model)
        if major is None or _is_task_specific(model):
            continue
        by_major.setdefault(major, []).append(model)

    def rank(model: str) -> tuple[int, int, int, str]:
        return (
            0 if "flash" in model else 1,
            1 if any(tag in model for tag in ("preview", "exp", "thinking")) else 0,
            len(model),
            model,
        )

    return {
        major: sorted(models, key=rank)[:CANDIDATES_PER_MAJOR]
        for major, models in sorted(by_major.items())
    }


# ──────────────────────────────────────────────────────────────────────────
# The checks
# ──────────────────────────────────────────────────────────────────────────


def check_tool_call_shape(api_key: str, model: str) -> dict[str, Any]:
    """C1 — does this model emit a functionCall, and does that call carry an `id`?

    Provenance for everything after it, and the callability preflight: every later
    check assumes the model answers at all, so this one runs first and the run
    skips a model it reports unavailable.
    """
    try:
        status, payload = _generate(
            api_key, model, [{"role": "user", "parts": [{"text": ONE_CITY_PROMPT}]}]
        )
    except RateLimited as exc:
        # Free-tier quota is per model, so this candidate being spent says nothing
        # about the next one. Recorded and skipped here; probe() stops the run if
        # enough of them come back in a row to mean the whole key is spent.
        return {"available": False, "rate_limited": True, "reason": str(exc)}

    # MEASURED 2026-08-22: ListModels advertises models generateContent then
    # refuses. `gemini-2.5-flash` came back from /models with generateContent in
    # supportedGenerationMethods and 404'd here with "no longer available to new
    # users". So the list endpoint describes the catalogue, not this key's access
    # to it, and selection built on it cannot be trusted.
    #
    # This is data about the endpoint, not a probe bug, so it is recorded and the
    # model is skipped — while every OTHER non-200 still raises. The distinction
    # matters: "this key cannot call that model" must never look like "the model
    # rejected the field under test".
    if status == 404:
        message = _error_message(payload)
        return {
            "available": False,
            "reason": message,
            "google_suggested": _suggested_model(message),
        }
    if status != 200:
        raise ProbeError(
            f"{model}: baseline tool call returned {status}: {_error_message(payload)}"
        )

    calls = _function_calls(payload)
    return {
        "available": True,
        # The setup turn, kept so the later checks REUSE it. They each used to
        # re-send this identical request, costing three of every model's ~9
        # requests to obtain the same assistant turn three times. On a free tier
        # that is the difference between finishing a model and not. Stripped
        # before the report is written; it is plumbing, not a finding.
        "_setup_payload": payload,
        "called_the_tool": bool(calls),
        "call_count": len(calls),
        # The answer to "does Google hand us an id to send back". pi only sends
        # one when it decides the model requires it; whether one ARRIVES is a
        # different question, and τ's converter needs both answers.
        "call_carries_id": bool(calls and "id" in calls[0]),
        "observed_id": calls[0].get("id") if calls else None,
        "first_call_name": calls[0].get("name") if calls else None,
    }


def check_tool_result_id(api_key: str, model: str, setup: dict[str, Any]) -> dict[str, Any]:
    """C2/C3 — the O2 question: is an `id` on a functionResponse accepted or rejected?

    Replies to the preflight's setup turn twice, differing only in whether the
    functionResponse carries `id`. The no-id arm is the control: if it does not
    return 200, the id arm's verdict is meaningless and the check raises.
    """
    first = setup["_setup_payload"]
    calls = _function_calls(first)
    if not calls:
        return {"exercised": False, "reason": "model did not call the tool"}

    call = calls[0]
    history = [
        {"role": "user", "parts": [{"text": ONE_CITY_PROMPT}]},
        _model_turn(first),
    ]

    def reply(include_id: bool) -> tuple[int, dict[str, Any]]:
        response_part: dict[str, Any] = {
            "name": call["name"],
            "response": {"output": "11"},
        }
        if include_id:
            # An id Google itself did not supply is still what τ would send on a
            # model whose table entry says to: pi synthesises and sanitises ids
            # (google-shared.ts:133). Using the observed id when there is one
            # keeps the arm honest for models that do carry them.
            response_part["id"] = call.get("id") or "probe_call_0"
        return _generate(
            api_key,
            model,
            [*history, {"role": "user", "parts": [{"functionResponse": response_part}]}],
        )

    control_status, control_body = reply(include_id=False)
    if control_status != 200:
        raise ProbeError(
            f"{model}: the no-id CONTROL returned {control_status}, so no id verdict is "
            f"trustworthy: {_error_message(control_body)}"
        )

    id_status, id_body = reply(include_id=True)

    return {
        "exercised": True,
        "google_supplied_an_id": "id" in call,
        "without_id_status": control_status,
        "with_id_status": id_status,
        # The verdict O2 is waiting on. "accepted" means an id is harmless, so the
        # safe branch for an unknown model is to SEND one. "rejected" means the
        # table is load-bearing and the safe branch is to omit it.
        "id_verdict": "accepted" if id_status == 200 else "rejected",
        "id_error": None if id_status == 200 else _error_message(id_body),
        "pi_would_send_id": (_gemini_major(model) or 0) >= 3,
    }


def check_duplicate_name_pairing(api_key: str, model: str, setup: dict[str, Any]) -> dict[str, Any]:
    """C4 — O1's ambiguity, measured rather than assumed.

    O1 option B guards the case where one turn holds two calls to the same tool
    name on a model without ids. The guard is only worth its ~20 lines if the case
    actually mis-pairs. So: call get_temperature twice, answer both by name only
    with bare numbers, and ask which city is warmer.

    Paris gets 11, Tokyo gets 29, in call order. The payload never names a city,
    so a correct answer can only come from positional pairing.
    """
    status, first = _generate(
        api_key, model, [{"role": "user", "parts": [{"text": TWO_CITY_PROMPT}]}]
    )
    if status != 200:
        raise ProbeError(f"{model}: two-city turn returned {status}: {_error_message(first)}")

    calls = _function_calls(first)
    if len(calls) < 2:
        # Not a failure of the model and not a verdict. Some models split the two
        # calls across turns, which sidesteps the ambiguity entirely.
        return {
            "exercised": False,
            "reason": f"model emitted {len(calls)} call(s) in one turn, not 2",
            "call_count": len(calls),
        }

    cities = [str(c.get("args", {}).get("city", "")).lower() for c in calls[:2]]
    temperatures = ["11", "29"]

    # Both functionResponses go in ONE user turn, matching pi's merge
    # (google-shared.ts:261) and τ's Anthropic client. Splitting them would
    # measure a message layout τ does not send.
    result_parts = [
        {"functionResponse": {"name": call["name"], "response": {"output": temp}}}
        for call, temp in zip(calls[:2], temperatures)
    ]
    question = (
        "Using only the tool results, answer with exactly two lines in the form "
        "'<city>: <temperature>' for the two cities you looked up."
    )
    status, second = _generate(
        api_key,
        model,
        [
            {"role": "user", "parts": [{"text": TWO_CITY_PROMPT}]},
            _model_turn(first),
            {"role": "user", "parts": result_parts},
            {"role": "user", "parts": [{"text": question}]},
        ],
    )
    if status != 200:
        raise ProbeError(
            f"{model}: duplicate-name reply returned {status}: {_error_message(second)}"
        )

    answer = _text(second)
    lowered = answer.lower()

    # Read the mapping the model reports back. Positional pairing is correct when
    # each city sits with the temperature its OWN call was answered with.
    expected = dict(zip(cities, temperatures))
    correct = None
    if all(city in lowered for city in expected):
        correct = all(
            _temperature_next_to(lowered, city) == temp for city, temp in expected.items()
        )

    return {
        "exercised": True,
        "calls_carried_ids": all("id" in c for c in calls[:2]),
        "call_order": cities,
        "sent": dict(zip(cities, temperatures)),
        "answer": answer.strip()[:300],
        # None = the answer did not name both cities, so pairing is unreadable
        # from it. Recorded as unreadable, never guessed at.
        "paired_positionally": correct,
    }


def _temperature_next_to(text: str, city: str) -> str | None:
    """The first number appearing after `city` on its line, or None."""
    for line in text.splitlines():
        if city in line.lower():
            match = re.search(r"(-?\d+)", line[line.lower().index(city) :])
            if match:
                return match.group(1)
    return None


def check_multimodal_function_response(
    api_key: str, model: str, setup: dict[str, Any]
) -> dict[str, Any]:
    """C5 — is an image nested in functionResponse.parts accepted?

    pi sends nested image parts only on Gemini 3+ and falls back to a separate
    user image turn below that (google-shared.ts:236). Same control discipline as
    C2: the arm without `parts` must return 200 or there is no verdict.
    """
    first = setup["_setup_payload"]
    calls = _function_calls(first)
    if not calls:
        return {"exercised": False, "reason": "model did not call the tool"}

    call = calls[0]
    history = [
        {"role": "user", "parts": [{"text": ONE_CITY_PROMPT}]},
        _model_turn(first),
    ]

    def reply(nest_image: bool) -> tuple[int, dict[str, Any]]:
        response_part: dict[str, Any] = {
            "name": call["name"],
            "response": {"output": "(see attached image)"},
        }
        if nest_image:
            response_part["parts"] = [{"inlineData": {"mimeType": "image/png", "data": PROBE_PNG}}]
        return _generate(
            api_key,
            model,
            [*history, {"role": "user", "parts": [{"functionResponse": response_part}]}],
        )

    control_status, control_body = reply(nest_image=False)
    if control_status != 200:
        raise ProbeError(
            f"{model}: the no-image CONTROL returned {control_status}, so no multimodal "
            f"verdict is trustworthy: {_error_message(control_body)}"
        )

    # SECOND control, and the one that makes the verdict attributable. MEASURED
    # 2026-08-22: gemini-3-flash-preview rejected the nested image with HTTP 400
    # "Request contains an invalid argument" — a message that fits BOTH "images
    # may not be nested here" and "your PNG is malformed". Sending the identical
    # inlineData as an ordinary user part separates them: if Google accepts the
    # image there, the rejection is about nesting; if it refuses it there too,
    # the probe's own image is at fault and there is no verdict to report.
    plain_status, plain_body = _generate(
        api_key,
        model,
        [
            {
                "role": "user",
                "parts": [
                    {"text": "Describe this image in one word."},
                    {"inlineData": {"mimeType": "image/png", "data": PROBE_PNG}},
                ],
            }
        ],
        tools=False,
    )
    if plain_status != 200:
        return {
            "exercised": False,
            "reason": (
                f"the probe's own image was refused as a plain user part (HTTP {plain_status}: "
                f"{_error_message(plain_body)}), so a nested-image rejection cannot be "
                "attributed to nesting"
            ),
            "image_valid_as_plain_part": False,
        }

    image_status, image_body = reply(nest_image=True)
    return {
        "exercised": True,
        "image_valid_as_plain_part": True,
        "without_image_status": control_status,
        "with_image_status": image_status,
        "verdict": "accepted" if image_status == 200 else "rejected",
        "error": None if image_status == 200 else _error_message(image_body),
        "pi_would_nest": (_gemini_major(model) or 0) >= 3,
    }


# ──────────────────────────────────────────────────────────────────────────
# Driver
# ──────────────────────────────────────────────────────────────────────────

# check_tool_call_shape is the preflight and runs separately, so a model it
# reports unavailable skips the rest instead of raising through them.
CHECKS = (
    ("tool_result_id", check_tool_result_id),
    ("duplicate_name_pairing", check_duplicate_name_pairing),
    ("multimodal_function_response", check_multimodal_function_response),
)


def probe(api_key: str | None = None, models: list[str] | None = None) -> dict[str, Any]:
    api_key = api_key or resolve_api_key()
    if not api_key:
        raise Unreachable(
            f"no key: set GEMINI_API_KEY (or GOOGLE_API_KEY), or write one to {DEFAULT_KEY_FILE}"
        )

    available = list_models(api_key)

    # An explicit --model list is taken literally: the operator named it, so an
    # unavailable one is reported rather than quietly replaced. Automatic
    # selection walks each version's candidates until one answers.
    if models:
        candidates: dict[int, list[str]] = {}
        for model in models:
            candidates.setdefault(_gemini_major(model) or -1, []).append(model)
    else:
        candidates = select_candidates(available)
    if not candidates:
        raise Unreachable("this key lists no Gemini model that supports generateContent")

    report: dict[str, Any] = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "available_model_count": len(available),
        "available_gemini_models": [m for m in available if _gemini_major(m) is not None],
        "candidates": candidates,
        "skipped_as_uncallable": {},
        "results": {},
        # Set when quota ran out partway. The run then STOPS but keeps whatever
        # completed: losing a finished major-2 measurement because major 3 ran
        # dry is the wrong failure, and it is the one this probe hit on its
        # first real run.
        "incomplete": None,
    }

    # A 429 per candidate means "not this model right now"; enough of them in a
    # row means the key itself is spent, and every further request would burn
    # quota to learn nothing. Counted ACROSS versions, and reset by any candidate
    # that answers.
    consecutive_rate_limits = 0

    for major, ranked in sorted(candidates.items()):
        if report["incomplete"]:
            break
        for model in ranked:
            model_report: dict[str, Any] = {"pi_major_version": _gemini_major(model)}
            shape = check_tool_call_shape(api_key, model)
            model_report["tool_call_shape"] = shape
            if not shape.get("available"):
                if shape.get("rate_limited"):
                    consecutive_rate_limits += 1
                    if consecutive_rate_limits >= RATE_LIMIT_GIVE_UP:
                        report["incomplete"] = (
                            f"{consecutive_rate_limits} candidates in a row returned 429 — "
                            "the key's free-tier quota is spent, not one model's."
                        )
                        break
                # Recorded, not discarded: which models the catalogue offers but
                # refuses is a finding about the API, and it is the reason a
                # version can end up unmeasured.
                report["skipped_as_uncallable"][model] = shape["reason"]
                if not models:
                    continue  # try this version's next candidate
                report["results"][model] = model_report
                continue
            consecutive_rate_limits = 0

            # A 429 PAST the preflight leaves this model half-measured. What
            # completed is KEPT: the usage graph for the first real run showed 8
            # successful requests against gemini-3.5-flash whose results were all
            # discarded because the model had not finished. Every check that ran
            # is a real measurement; the ones that did not run render as "not
            # reached" and are never mistaken for a verdict.
            try:
                for name, check in CHECKS:
                    model_report[name] = check(api_key, model, shape)
            except RateLimited as exc:
                report["incomplete"] = f"quota ran out partway through {model}: {exc}"
                shape.pop("_setup_payload", None)
                report["results"][model] = model_report
                break

            shape.pop("_setup_payload", None)
            report["results"][model] = model_report
            if not models:
                break  # this major version is measured; move to the next
        else:
            if not models:
                report.setdefault("unmeasured_majors", []).append(major)

    if not report["results"] and report["incomplete"]:
        # Nothing survived, so there is no partial report worth printing.
        raise RateLimited(f"{report['incomplete']} Nothing was measured; retry later.")

    report["models_probed"] = list(report["results"])
    report["rate_limit_waits"] = RETRY_COUNT[0]

    # Whether the run straddled pi's `>= 3` boundary, stated rather than left for
    # a reader to work out from the model names. A run that only saw one side
    # cannot tell "Google ignores the id" from "this model happens to accept it",
    # and O2 must not be decided from one.
    measured = [
        _gemini_major(m)
        for m, r in report["results"].items()
        if r["tool_call_shape"].get("available")
    ]
    report["straddles_pi_boundary"] = any(v is not None and v < 3 for v in measured) and any(
        v is not None and v >= 3 for v in measured
    )

    return report


#: Stands in for a check the run never got to. Distinct from a check that ran and
#: reported "not exercised": one is missing data, the other IS data.
NOT_REACHED: dict[str, Any] = {"exercised": False, "reason": "run stopped before this check"}


def _render(report: dict[str, Any]) -> str:
    lines = [
        f"Gemini capability probe — {report['measured_at']}",
        f"models probed: {', '.join(report['models_probed']) or '(none)'}",
        "",
    ]
    if report.get("incomplete"):
        lines.append(f"INCOMPLETE RUN: {report['incomplete']}")
        lines.append("Everything below was measured; everything absent was not reached.")
        lines.append("")
    if report.get("rate_limit_waits"):
        lines.append(
            f"waited out {report['rate_limit_waits']} rate limit(s) at the server's "
            "requested delay during this run."
        )
        lines.append("")
    skipped = report.get("skipped_as_uncallable") or {}
    if skipped:
        lines.append("listed by /models but not callable by this key:")
        for model, reason in skipped.items():
            lines.append(f"   {model}: {reason[:150]}")
        lines.append("")
    if report.get("unmeasured_majors"):
        versions = ", ".join(str(v) for v in report["unmeasured_majors"])
        lines.append(
            f"major version(s) {versions} went unmeasured — every candidate was uncallable."
        )
        lines.append("")
    for model, result in report["results"].items():
        lines.append(f"── {model}  (pi major version: {result['pi_major_version']})")

        shape = result["tool_call_shape"]
        if not shape.get("available"):
            lines.append(f"   NOT CALLABLE by this key — {shape['reason'][:200]}")
            if shape.get("google_suggested"):
                lines.append(
                    f"       Google suggests: {shape['google_suggested']}"
                    "  (pass it with --model; the probe never substitutes it itself)"
                )
            lines.append("")
            continue

        lines.append(
            f"   call carries an id from Google : {shape['call_carries_id']}"
            f"   (tool called: {shape['called_the_tool']})"
        )

        # A missing check key means the run stopped before that check. Rendered
        # as "not reached" rather than crashing, so a partial report is still
        # readable — and never mistaken for a check that returned nothing.
        ids = result.get("tool_result_id", NOT_REACHED)
        if ids.get("exercised"):
            lines.append(
                f"   sending an id back            : {ids['id_verdict'].upper()}"
                f"  (HTTP {ids['with_id_status']}, control {ids['without_id_status']})"
                f"  [pi would send: {ids['pi_would_send_id']}]"
            )
            if ids.get("id_error"):
                lines.append(f"       reason: {ids['id_error'][:160]}")
        else:
            lines.append(f"   sending an id back            : not exercised — {ids['reason']}")

        pairing = result.get("duplicate_name_pairing", NOT_REACHED)
        if pairing.get("exercised"):
            paired = pairing["paired_positionally"]
            verdict = {True: "CORRECT", False: "MIS-PAIRED", None: "unreadable answer"}[paired]
            lines.append(
                f"   two same-name calls in a turn : {verdict}"
                f"  (ids present: {pairing['calls_carried_ids']})"
            )
            lines.append(f"       sent {pairing['sent']}, model said: {pairing['answer'][:100]!r}")
        else:
            lines.append(f"   two same-name calls in a turn : not exercised — {pairing['reason']}")

        multimodal = result.get("multimodal_function_response", NOT_REACHED)
        if multimodal.get("exercised"):
            lines.append(
                f"   image nested in the result    : {multimodal['verdict'].upper()}"
                f"  (HTTP {multimodal['with_image_status']}, control {multimodal['without_image_status']},"
                f" same image as a plain part: accepted)"
                f"  [pi would nest: {multimodal['pi_would_nest']}]"
            )
            if multimodal.get("error"):
                lines.append(f"       reason: {multimodal['error'][:160]}")
        else:
            lines.append(
                f"   image nested in the result    : not exercised — {multimodal['reason']}"
            )
        lines.append("")

    lines.append("O2 reads the 'sending an id back' and 'image nested' rows.")
    lines.append("O1 option B reads the 'two same-name calls' row.")
    if not report.get("straddles_pi_boundary"):
        lines.append("")
        lines.append(
            "WARNING: this run did NOT measure both sides of pi's `>= 3` boundary, so it "
            "cannot separate 'Google ignores the id' from 'this model happens to accept "
            "it'. Do not decide O2 from it alone — pass --model to reach the other side."
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        help="Probe this model (repeatable). Default: one per major Gemini version.",
    )
    parser.add_argument("--json", dest="json_path", help="Write the full report to this path.")
    parser.add_argument(
        "--api-key-file",
        dest="api_key_file",
        help=f"Read the key from this file instead of the environment (default: {DEFAULT_KEY_FILE}).",
    )
    parser.add_argument(
        "--rpm",
        type=float,
        default=DEFAULT_RPM,
        help=(
            "Requests per minute ceiling (default: %(default)s, the free tier's limit for "
            "most text-out Gemini models). Exceeding it spends the daily allowance on 429s."
        ),
    )
    parser.add_argument(
        "--list-models", action="store_true", help="List the catalogue and exit (see the caveat)."
    )
    args = parser.parse_args()

    if args.rpm <= 0:
        parser.error("--rpm must be positive")
    globals()["MIN_REQUEST_INTERVAL"] = 60.0 / args.rpm + 1.0

    try:
        api_key = resolve_api_key(args.api_key_file)
        if not api_key:
            raise Unreachable(
                "no key: set GEMINI_API_KEY (or GOOGLE_API_KEY), or write one to "
                f"{args.api_key_file or DEFAULT_KEY_FILE}"
            )
        if args.list_models:
            for model in list_models(api_key):
                print(model)
            return 0
        report = probe(api_key=api_key, models=args.models)
    except Unreachable as exc:
        print(f"unreachable: {exc}", file=sys.stderr)
        return 2

    print(_render(report))
    if args.json_path:
        with open(args.json_path, "w") as handle:
            json.dump(report, handle, indent=2)
        print(f"\nwrote {args.json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
