"""RPC wire envelope: the JSON-RPC 2.0 request/response/event dataclasses.

Block [2] Dialect, per docs/REMOTE-CONTROL.md section 3/4. Split out of the
former rpc.py (docs/REMOTE-CONTROL.md section 7.3, requirement X1) — this
module has no transport or dispatch concerns, only the envelope shapes.

Reference: docs/PHASE-6-SUBPHASE-0.md
Reference: docs/SUBPHASE-0.0.md lines 260-340
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from tau_llm.docs import agent_facing

#: JSON-RPC 2.0 §5.1 standard error codes. Named here (block [2] Dialect) rather
#: than in handler.py or commands.py, because they are part of the wire envelope
#: contract, not the dispatch logic that raises them — docs/REMOTE-CONTROL.md
#: §4[3] C2: "unknown method returns JSON-RPC error -32601, never a free-text
#: error and never silence", extended to the full standard set rather than the
#: single generic -32603 the six-verb handler used for everything.
#: Also the answer for a line that is not valid UTF-8 (T7's sibling input
#: class, closed at the Tier B review's integration): JSON text is UTF-8, so
#: those bytes are not JSON. Deliberately NOT a code of its own, unlike
#: `REQUEST_TOO_LARGE` below — see `transport._refuse_undecodable_request`
#: for why the two cases part company on exactly that point.
PARSE_ERROR = -32700  # the client sent bytes that are not valid JSON
INVALID_REQUEST = -32600  # valid JSON, but not a valid Request object (no `method`)
METHOD_NOT_FOUND = -32601  # `method` names no table entry, or a declined one
INVALID_PARAMS = -32602  # `params` fails the method's params_schema
INTERNAL_ERROR = -32603  # the handler raised something it did not raise deliberately

#: -32000..-32099 is the range JSON-RPC 2.0 reserves for implementation-defined
#: "server error" codes. A rejected Submission is not a protocol violation or an
#: unexpected crash — SubmissionResult.rejection_reason is a structured, EXPECTED
#: outcome (submission.py's own docstring: "a refusal is a result, not an
#: exception", the LSP ApplyWorkspaceEditResult shape) — so it earns its own code
#: rather than overloading INTERNAL_ERROR, which stays reserved for "the handler
#: raised something nobody planned for" (docs/REMOTE-CONTROL.md §4[3] C3).
SUBMISSION_REJECTED = -32000
#: A resolved command whose `CommandOutcome.performer` is `"frontend"` — the core
#: identified WHAT it is (`/tree`, `/fork`, `/extensions`, `/compact`) and the RPC
#: wire, having no screen to push a modal onto or panel to paint, cannot perform
#: it (tau_agent_core.commands module docstring: "a frontend that cannot must
#: raise UnsupportedCommandError rather than no-op"). Also a structured, expected
#: refusal rather than a crash, so it gets its own code alongside
#: SUBMISSION_REJECTED instead of overloading INTERNAL_ERROR.
COMMAND_NOT_SUPPORTED = -32001
#: A `new_session`/`fork`/`switch_session` call requested the in-flight turn
#: stop (`AgentSession.abort()`) and waited on `AgentSession.turn_lock`, but
#: the turn did not free it within the bounded wait (phase-3 review Finding
#: 1: `abort()` is a REQUEST, not a guarantee — a provider that never sends a
#: line never even reaches the cooperative-cancellation check, and the
#: session log has NOT been swapped). An expected, structured refusal — the
#: turn may still finish on its own, retry or abort again and wait for
#: `agent_end` first — never a crash, and never an unbounded hang: the RPC
#: reader is strictly serial (`transport._read_stdin` awaits every
#: `_handle_line` before parsing the next byte), so a handler that instead
#: blocked forever here would wedge every later request behind it,
#: INCLUDING `abort` itself — the host's only other recourse besides killing
#: the process. See `AgentSessionRuntime.DEFAULT_SWAP_TIMEOUT_S` for the
#: timeout value and its rationale.
TURN_STILL_RUNNING = -32002
#: One inbound request LINE exceeded `transport.MAX_REQUEST_LINE_BYTES` and was
#: discarded unread (T7, Tier B review finding 9). A refusal, not a crash and
#: not a parse failure: the bytes were never handed to `json.loads` at all, so
#: nothing here claims anything about whether they WERE valid JSON — which is
#: exactly why this is not `PARSE_ERROR`, and why it is not `INVALID_REQUEST`
#: either (that code means "valid JSON, but not a Request object", a judgement
#: the reader deliberately never formed). It sits in the same implementation-
#: defined -32000..-32099 range as the three codes above, for the same reason
#: all of them are there: a structured, EXPECTED outcome the protocol has a
#: considered answer for, rather than "the handler raised something nobody
#: planned for" (`INTERNAL_ERROR`).
#:
#: Two consequences a host must know, both carried on the error itself:
#: `id` is `null` — the request's own id is inside the bytes that were never
#: parsed, so it is unknowable (JSON-RPC 2.0 §5: `id` MUST be null when it
#: cannot be determined) — and `data` carries `max_request_line_bytes`, the
#: same number `get_capabilities` advertises under `limits`, so a host that
#: never read the capability document still learns the bound from the refusal
#: rather than from a dead process.
REQUEST_TOO_LARGE = -32003
#: A verb that APPENDS a session-log entry was called on a session with no
#: durable location — the product of `new_session {"persist": false}`, a
#: process started with `--no-session` (whose STARTUP session is ephemeral,
#: so a host can meet this on its very first request), or a catalog whose log
#: declares no location at all. D-7 (docs/RPC-TIER-B.md): `set_model`,
#: `set_session_name` and `compact` refuse; `set_auto_compaction` and every
#: read do not, because they append nothing.
#:
#: A host does not have to discover this by tripping it: `get_state` reports
#: `addressable`, the same predicate, for whatever session the connection is
#: on at the time.
#:
#: This code exists because of round-3 finding 4 of the Tier B review. The
#: refusal first shipped as `INTERNAL_ERROR`, on the argument that "the
#: `dialect` error set is a published, closed vocabulary, and one unusual
#: refusal does not earn an addition to it". That argument loses to the one
#: `SUBMISSION_REJECTED`, `COMMAND_NOT_SUPPORTED`, `TURN_STILL_RUNNING` and
#: `REQUEST_TOO_LARGE` all make above, and loses TWICE over here: this refusal
#: is the most deliberate thing in the tier — guarded, documented in three
#: places, checked as `compact`'s first statement — while `-32603` is defined,
#: in the reference generated from this very file, as "the handler raised
#: something it did not raise on purpose". A host was being asked to tell a
#: considered refusal from a τ crash by matching English prose.
#:
#: It is host-ACTIONABLE, which is the real test: put the connection on a
#: persisted session (`fork`, `switch_session`, or `new_session` with `persist`
#: left at its default) and retry. No bespoke `data`: D2 already carries
#: the `method` that refused, which is the only detail there is.
SESSION_NOT_PERSISTED = -32004


@agent_facing(topic="rpc")
@dataclass
class RPCRequest:
    """A JSON-RPC 2.0 request message.

    Attributes:
        jsonrpc: JSON-RPC protocol version (always "2.0").
        id: Request ID (int) for matching responses. None for notifications.
        method: RPC method name ("send_prompt", "send_tool_result",
                "get_commands", etc.).
        params: Method-specific parameters, or None.
    """

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | None = None
    method: str = ""
    params: dict[str, Any] | None = None

    def to_json_line(self) -> str:
        """Serialize to a single LF-delimited JSON line.

        Returns:
            JSON string suitable for LF-delimited framing.
        """
        import json

        return json.dumps(self.__dict__, separators=(",", ":"))

    @classmethod
    def from_json_line(cls, line: str) -> RPCRequest:
        """Deserialize from a LF-delimited JSON line.

        Args:
            line: A single LF-delimited JSON string.

        Returns:
            An RPCRequest instance.
        """
        import json

        data = json.loads(line)
        return cls(**data)


@agent_facing(topic="rpc")
@dataclass
class RPCResponse:
    """A JSON-RPC 2.0 response message.

    Either `result` or `error` must be set (never both).
    For error responses, `result` is None and `error` is an error dict.
    For success responses, `error` is None and `result` contains the result.

    Attributes:
        jsonrpc: JSON-RPC protocol version (always "2.0").
        id: Request ID matching the original request. None for notifications.
        result: The response result, or None on error.
        error: The error dict on failure, or None on success.
    """

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def to_json_line(self) -> str:
        """Serialize to a single LF-delimited JSON line.

        Returns:
            JSON string suitable for LF-delimited framing.
        """
        import json

        return json.dumps(self.__dict__, separators=(",", ":"))

    @classmethod
    def from_json_line(cls, line: str) -> RPCResponse:
        """Deserialize from a LF-delimited JSON line.

        Args:
            line: A single LF-delimited JSON string.

        Returns:
            An RPCResponse instance.
        """
        import json

        data = json.loads(line)
        return cls(**data)

    def is_error(self) -> bool:
        """Check if this response represents an error."""
        return self.error is not None


@agent_facing(topic="rpc")
@dataclass
class RPCEvent:
    """A JSON-RPC 2.0 event notification (fire-and-forget).

    Events use method="event" and carry an AgentEvent as params.
    They have no request ID and expect no response.

    Attributes:
        jsonrpc: JSON-RPC protocol version (always "2.0").
        method: Always "event" for notifications.
        params: The event payload (AgentEvent serialized as dict).
    """

    jsonrpc: Literal["2.0"] = "2.0"
    method: Literal["event"] = "event"
    params: dict[str, Any] = field(default_factory=dict)

    def to_json_line(self) -> str:
        """Serialize to a single LF-delimited JSON line.

        Returns:
            JSON string suitable for LF-delimited framing.
        """
        import json

        return json.dumps(self.__dict__, separators=(",", ":"))

    @classmethod
    def from_json_line(cls, line: str) -> RPCEvent:
        """Deserialize from a LF-delimited JSON line.

        Args:
            line: A single LF-delimited JSON string.

        Returns:
            An RPCEvent instance.
        """
        import json

        data = json.loads(line)
        return cls(**data)
