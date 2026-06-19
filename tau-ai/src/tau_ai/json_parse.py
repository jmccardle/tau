"""τ-ai json_parse: tolerant JSON parsing for streaming tool-call arguments.

Python port of pi's ``packages/ai/src/utils/json-parse.ts``.

Tool-call arguments arrive from OpenAI-compatible streams as a sequence of
*fragments* that are concatenated into a growing buffer. Two helpers are needed:

* ``parse_json_with_repair`` — strict parse for a *complete* buffer. Repairs the
  common malformations real models emit (raw control characters and invalid
  escape sequences inside strings) and otherwise **raises**. Use this at finalize:
  a complete-but-unparseable tool-call payload is a real error, not something to
  paper over (see CLAUDE.md, "Fail Early").

* ``parse_streaming_json`` — best-effort parse for a *partial* (mid-stream) buffer.
  Always returns a dict, completing unterminated strings/containers so the live
  display can show arguments as they stream. Returns ``{}`` when nothing usable
  can be recovered yet. This leniency is intentional and scoped to display only.

Reference: pi ``json-parse.ts`` (``repairJson`` / ``parseJsonWithRepair`` /
``parseStreamingJson``).
"""

from __future__ import annotations

import json
from typing import Any

_VALID_JSON_ESCAPES = set('"\\/bfnrtu')

_CONTROL_ESCAPES = {
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _escape_control_character(char: str) -> str:
    """Escape a single control character for inclusion in a JSON string."""
    if char in _CONTROL_ESCAPES:
        return _CONTROL_ESCAPES[char]
    return "\\u{:04x}".format(ord(char))


def repair_json(json_str: str) -> str:
    """Repair malformed JSON string literals.

    Mirrors pi's ``repairJson``:
    - escapes raw control characters (U+0000–U+001F) appearing inside strings
    - doubles backslashes that precede an invalid escape character

    Args:
        json_str: Possibly-malformed JSON text.

    Returns:
        Repaired JSON text (may still be invalid for other reasons).
    """
    repaired: list[str] = []
    in_string = False
    i = 0
    n = len(json_str)

    while i < n:
        char = json_str[i]

        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            i += 1
            continue

        if char == '"':
            repaired.append(char)
            in_string = False
            i += 1
            continue

        if char == "\\":
            next_char = json_str[i + 1] if i + 1 < n else None
            if next_char is None:
                repaired.append("\\\\")
                i += 1
                continue
            if next_char == "u":
                unicode_digits = json_str[i + 2 : i + 6]
                if len(unicode_digits) == 4 and all(
                    c in "0123456789abcdefABCDEF" for c in unicode_digits
                ):
                    repaired.append("\\u" + unicode_digits)
                    i += 6
                    continue
            if next_char in _VALID_JSON_ESCAPES:
                repaired.append("\\" + next_char)
                i += 2
                continue
            repaired.append("\\\\")
            i += 1
            continue

        if ord(char) <= 0x1F:
            repaired.append(_escape_control_character(char))
        else:
            repaired.append(char)
        i += 1

    return "".join(repaired)


def parse_json_with_repair(json_str: str) -> Any:
    """Strict JSON parse, retrying once after :func:`repair_json`.

    Raises ``json.JSONDecodeError`` if the text cannot be parsed even after
    repair. Mirrors pi's ``parseJsonWithRepair``.
    """
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        repaired = repair_json(json_str)
        if repaired != json_str:
            return json.loads(repaired)
        raise


def _complete_structures(buf: str) -> str:
    """Close any unterminated string and open containers in ``buf``.

    Walks the buffer tracking string/escape state and a stack of open
    ``{``/``[``, then appends the matching closers. A trailing dangling
    backslash is dropped. This does not fix dangling keys/commas — the caller
    trims trailing characters and retries for those.
    """
    stack: list[str] = []
    in_string = False
    escape = False

    for ch in buf:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if stack:
                stack.pop()

    out = buf
    if escape:
        out = out[:-1]
    if in_string:
        out += '"'
    out += "".join(reversed(stack))
    return out


def parse_streaming_json(partial_json: str | None) -> dict[str, Any]:
    """Best-effort parse of a possibly-incomplete JSON object during streaming.

    Always returns a dict. Tries a strict (repaired) parse first; failing that,
    completes unterminated strings/containers, trimming trailing characters until
    a prefix parses. Returns ``{}`` if nothing usable can be recovered.

    Mirrors the contract of pi's ``parseStreamingJson`` (display-only leniency).
    """
    if not partial_json or not partial_json.strip():
        return {}

    try:
        result = parse_json_with_repair(partial_json)
        return result if isinstance(result, dict) else {}
    except json.JSONDecodeError:
        pass

    buf = partial_json
    while buf:
        completed = _complete_structures(buf)
        try:
            result = json.loads(completed)
            return result if isinstance(result, dict) else {}
        except json.JSONDecodeError:
            buf = buf[:-1]
    return {}
