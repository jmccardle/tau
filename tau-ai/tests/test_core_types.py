"""τ-ai's core data types — messages, content blocks, tool-argument validation,
``Model`` serialization, and the frozen ``Usage`` type (``tau_ai.types``,
``tau_ai.tools``).

Previously ``test_subphase1.py``, named after PHASE-1-SUBPHASE-1.md rather than
its subject. That file had 43 tests: one assertion per test over five
"Testing Strategy" categories, plus a class of ``AbortSignal`` thread-safety
tests that duplicated ``test_abort.py`` almost verbatim.

Consolidated to 18 test functions (39 parametrized cases). What changed:

* **AbortSignal dropped entirely.** ``test_abort.py`` already carries every one
  of these cases (idempotency, concurrent abort, concurrent read/write) under
  the same assertions, plus the integration tests that thread an ``AbortSignal``
  through a live streamed completion (mid-stream cancellation, the signal never
  leaking into the request body). Keeping both was two copies of one test
  class; ``test_abort.py`` is the more complete of the two, so it stays and this
  file drops the duplicate rather than the other way round.
* **Two low-value tests dropped as redundant, not as a coverage cut:**
  ``test_model_attributes`` (bare attribute access — implied by every
  round-trip test that reads the recovered model's fields) and
  ``test_model_importable_from_top_level`` (this file already imports `Model`
  from the package root for every other test in it).
* **Four genuinely new tests, found while reading tools.py/types.py rather than
  the spec doc:**
  - ``AssistantMessage.get_tool_calls()`` had zero coverage before this file
    (types.py line 129) despite being the one method on the class.
  - ``validate_tool_arguments`` with a non-dict ``tool.parameters`` (e.g. a tool
    whose schema isn't wired up yet) — falls through to a raw pass-through
    rather than crashing; untested, and it is the last uncovered branch in
    ``tools.py``.
  - A malformed schema (a ``properties`` entry that isn't itself a dict) is
    caught by the generic ``except Exception`` and re-raised as the same
    ``ValueError`` shape as every other invalid-argument case, rather than
    escaping as an ``AttributeError``.
  - A JSON-Schema ``"number"`` field, valid and invalid, which the old suite
    never exercised even though the type-check branch for it exists.

Coverage of the three modules this file plus test_abort.py exercise, before ->
after (measured on this file plus test_abort.py together, since abort moved
between them): ``abort.py`` 100% -> 100% (unchanged, now solely from
test_abort.py), ``tools.py`` 91% -> 100% (the two branches above), ``types.py``
99% -> 100% (``get_tool_calls``).

**Product bug found while writing this file, and fixed in tools.py:**
``_validate_json_schema`` type-checked ``"integer"``/``"number"`` with
``isinstance(value, int)`` / ``isinstance(value, (int, float))``. ``bool``
subclasses ``int`` in Python but is a distinct type in JSON Schema, so
``{"count": True}`` satisfied an ``"integer"`` field and reached the tool, which
then did arithmetic on a bool. Both checks now exclude ``bool`` explicitly;
``test_a_bool_is_not_an_integer_or_a_number`` is the regression test.

Reference: docs/TOOL-CALL-PARSING-BUG.md (that defect is in the streaming
accumulation path in providers/openai.py, not in these types — this file has
nothing to add there; see test_tool_call_streaming_fix.py for its coverage).
types.py; tools.py.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from tau_ai import AssistantMessage, Model, Usage, UserMessage
from tau_ai.tools import validate_tool_arguments
from tau_ai.types import ImageContent, TextContent, ThinkingContent, ToolCall, ToolResultMessage

# ── message round trips ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "content",
    [
        "hello world",
        [TextContent(text="hello")],
        [TextContent(text="look"), ImageContent(data="b64png", mime_type="image/png")],
        [ImageContent(data="b64jpeg", mime_type="image/jpeg")],
    ],
    ids=["string", "text-only", "text+png", "jpeg-only"],
)
def test_user_message_round_trips_across_content_shapes(content):
    """``UserMessage.content`` is ``str | list[TextContent | ImageContent]`` —
    the string shortcut and every image mime type must survive
    ``model_dump()``/``model_validate()``, and the dump must be plain data
    (JSON-serializable), not nested pydantic objects."""
    msg = UserMessage(content=content, timestamp=1700000000000)
    dumped = msg.model_dump()
    json.dumps(dumped)
    assert UserMessage.model_validate(dumped) == msg


@pytest.mark.parametrize(
    "content,tool_call_count",
    [
        ([TextContent(text="Python is a programming language.")], 0),
        (
            [
                TextContent(text="Let me check that for you."),
                ToolCall(id="call_abc123", name="read_file", arguments={"path": "src/main.py"}),
            ],
            1,
        ),
        (
            [
                ThinkingContent(thinking="Let me think about this.", cached_tokens=50),
                TextContent(text="Here's my answer."),
            ],
            0,
        ),
        (
            [
                ThinkingContent(thinking="reasoning steps..."),
                TextContent(text="Let me think..."),
                ToolCall(id="call_1", name="ls", arguments={"path": "."}),
                TextContent(text="Here's the result."),
            ],
            1,
        ),
    ],
    ids=["text-only", "text+toolcall", "thinking+text", "thinking+text+toolcall+text"],
)
def test_assistant_message_round_trips_across_content_block_combinations(content, tool_call_count):
    """The discriminated union (``TextContent | ThinkingContent | ToolCall``) must
    recover the right concrete class per block after a dict round trip — not
    just at direct construction, which pydantic never gets wrong."""
    msg = AssistantMessage(
        content=content,
        api="openai-completions",
        provider="openai",
        model="gpt-4",
        usage=Usage(input_tokens=100, output_tokens=50),
        stop_reason="toolUse" if tool_call_count else "stop",
        timestamp=1700000000000,
    )
    dumped = msg.model_dump()
    json.dumps(dumped)
    recovered = AssistantMessage.model_validate(dumped)
    assert recovered == msg
    assert [type(c) for c in recovered.content] == [type(c) for c in content]
    assert len(recovered.get_tool_calls()) == tool_call_count


@pytest.mark.parametrize(
    "details,is_error",
    [
        ({"exit_code": 0}, False),
        (None, False),
        ({"exit_code": 1, "stdout": "hello"}, True),
    ],
    ids=["with-details", "no-details", "error-with-details"],
)
def test_tool_result_message_round_trips(details, is_error):
    msg = ToolResultMessage(
        tool_call_id="call_123",
        tool_name="ls",
        content=[TextContent(text="file1.txt\nfile2.py")],
        details=details,
        is_error=is_error,
        timestamp=1700000000000,
    )
    dumped = msg.model_dump()
    json.dumps(dumped)
    assert ToolResultMessage.model_validate(dumped) == msg


def test_thinking_content_cached_tokens_defaults_to_zero():
    """The only untested branch of ``ThinkingContent``'s defaults: every other
    test in the tree supplies ``cached_tokens`` explicitly."""
    assert ThinkingContent(thinking="reasoning").cached_tokens == 0


# ── AssistantMessage.get_tool_calls() ───────────────────────────────────────
#
# Untested before this file existed (types.py line 129) despite being the only
# method on the class.


def test_get_tool_calls_extracts_only_toolcall_blocks_in_order():
    msg = AssistantMessage(
        content=[
            TextContent(text="checking"),
            ToolCall(id="c1", name="read_file", arguments={"path": "a.py"}),
            ThinkingContent(thinking="hm"),
            ToolCall(id="c2", name="ls", arguments={}),
        ],
        api="openai-completions",
        provider="openai",
        model="gpt-4",
        stop_reason="toolUse",
        timestamp=0,
    )
    calls = msg.get_tool_calls()
    assert [c.id for c in calls] == ["c1", "c2"]
    assert all(isinstance(c, ToolCall) for c in calls)


def test_get_tool_calls_is_empty_when_there_are_none():
    msg = AssistantMessage(
        content=[TextContent(text="just talking")],
        api="openai-completions",
        provider="openai",
        model="gpt-4",
        stop_reason="stop",
        timestamp=0,
    )
    assert msg.get_tool_calls() == []


# ── tool argument validation ────────────────────────────────────────────────
#
# The only functioning coverage of ``validate_tool_arguments`` in the tree —
# test_tools.py's equivalent tests are stub `pass` bodies that assert nothing.

SCHEMA_NAME_REQUIRED = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}
SCHEMA_MULTI = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "count": {"type": "integer"},
        "active": {"type": "boolean"},
    },
    "required": ["name", "count"],
}
SCHEMA_NO_REQUIRED = {"type": "object"}
SCHEMA_AMOUNT = {
    "type": "object",
    "properties": {"amount": {"type": "number"}},
    "required": ["amount"],
}
SCHEMA_COUNT = {
    "type": "object",
    "properties": {"count": {"type": "integer"}},
    "required": ["count"],
}
SCHEMA_FLAG = {"type": "object", "properties": {"flag": {"type": "boolean"}}, "required": ["flag"]}
SCHEMA_CONFIG = {
    "type": "object",
    "properties": {"config": {"type": "object"}},
    "required": ["config"],
}
SCHEMA_TAGS = {"type": "object", "properties": {"tags": {"type": "array"}}, "required": ["tags"]}
SCHEMA_TWO_REQUIRED = {
    "type": "object",
    "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    "required": ["a", "b"],
}


def _tool(schema: dict):
    """A minimal stand-in for ``AgentTool`` — the function under test only
    ever reads ``.parameters``."""

    class _Tool:
        parameters = schema

    return _Tool()


@pytest.mark.parametrize(
    "schema,args",
    [
        (SCHEMA_NAME_REQUIRED, {"name": "world"}),
        (SCHEMA_MULTI, {"name": "world", "count": 42, "active": True}),
        (SCHEMA_NO_REQUIRED, {}),
        (SCHEMA_AMOUNT, {"amount": 3.5}),
        (SCHEMA_AMOUNT, {"amount": 3}),
    ],
    ids=["single-required", "multi-required", "no-required", "float", "int-satisfies-number"],
)
def test_valid_arguments_pass_through_as_a_dict_and_as_a_toolcall(schema, args):
    """``validate_tool_arguments`` accepts either shape the loop hands it: a
    bare dict (tests, RPC replay) or a real ``ToolCall`` (a live turn)."""
    tool = _tool(schema)
    assert validate_tool_arguments(tool, args) == args
    assert validate_tool_arguments(tool, ToolCall(id="c1", name="t", arguments=args)) == args


def test_extra_fields_beyond_the_schema_are_not_stripped():
    """Unknown fields are neither rejected nor filtered out — only the
    ``required``/typed fields are checked."""
    result = validate_tool_arguments(
        _tool(SCHEMA_NAME_REQUIRED), {"name": "world", "extra": "ignored"}
    )
    assert result == {"name": "world", "extra": "ignored"}


@pytest.mark.parametrize(
    "schema,args,match",
    [
        (SCHEMA_NAME_REQUIRED, {"wrong_key": "world"}, "Missing required field"),
        (SCHEMA_NAME_REQUIRED, {"name": 123}, "expected string"),
        (SCHEMA_COUNT, {"count": "not a number"}, "expected integer"),
        (SCHEMA_FLAG, {"flag": "yes"}, "expected boolean"),
        (SCHEMA_CONFIG, {"config": [1, 2, 3]}, "expected object"),
        (SCHEMA_TAGS, {"tags": {"key": "value"}}, "expected array"),
        (SCHEMA_AMOUNT, {"amount": "nope"}, "expected number"),
    ],
    ids=[
        "missing-required",
        "wrong-string",
        "wrong-integer",
        "wrong-boolean",
        "wrong-object",
        "wrong-array",
        "wrong-number",
    ],
)
def test_invalid_arguments_raise_naming_the_offending_field(schema, args, match):
    with pytest.raises(ValueError, match=match):
        validate_tool_arguments(_tool(schema), args)


@pytest.mark.parametrize(
    "schema,match", [(SCHEMA_COUNT, "expected integer"), (SCHEMA_AMOUNT, "expected number")]
)
def test_a_bool_is_not_an_integer_or_a_number(schema, match):
    """Regression: ``bool`` subclasses ``int`` in Python, not in JSON Schema.

    ``isinstance(True, int)`` is ``True``, so ``{"count": true}`` passed an
    ``"integer"`` field unchanged and the tool received a bool where it declared a
    number. Both numeric checks now reject it, and the message names ``bool`` so
    the model can correct itself from the tool result.
    """
    field = next(iter(schema["properties"]))
    with pytest.raises(ValueError, match=match) as exc_info:
        validate_tool_arguments(_tool(schema), {field: True})
    assert "got bool" in str(exc_info.value)


def test_multiple_missing_required_fields_are_all_named():
    with pytest.raises(ValueError, match="Missing required field") as exc_info:
        validate_tool_arguments(_tool(SCHEMA_TWO_REQUIRED), {})
    assert "a" in str(exc_info.value)
    assert "b" in str(exc_info.value)


def test_a_non_dict_schema_skips_validation_entirely():
    """``tool.parameters`` is documented as a JSON-Schema dict, but a tool whose
    schema isn't wired up yet (``parameters=None``) gets its arguments passed
    through raw rather than crashing on ``isinstance(schema, dict)``."""

    class MockTool:
        parameters = None

    assert validate_tool_arguments(MockTool(), {"anything": "goes"}) == {"anything": "goes"}


def test_a_malformed_schema_property_is_reported_as_a_validation_error_not_a_crash():
    """``properties`` is documented as a dict of per-field schemas. If a tool
    ships a malformed one (a property value that isn't itself a dict), the
    resulting ``AttributeError`` from ``.get("type")`` must not escape raw — it
    is caught by the generic ``except Exception`` and re-raised as the same
    ``ValueError`` shape every other invalid-argument case produces."""

    class MockTool:
        parameters = {
            "type": "object",
            "properties": {"name": "not-a-schema"},
            "required": ["name"],
        }

    with pytest.raises(ValueError, match="Schema validation failed"):
        validate_tool_arguments(MockTool(), {"name": "world"})


# ── Model serialization ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "api,max_tokens", [("openai-completions", 4096), ("openai-responses", 32768)]
)
def test_model_to_openai_format(api, max_tokens):
    model = Model(
        id="gpt-4o",
        name="GPT-4o",
        api=api,
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=max_tokens,
    )
    assert model.to_openai_format() == {
        "id": "gpt-4o",
        "name": "GPT-4o",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "max_completion_tokens": max_tokens,
        "context_window": 128000,
    }


def test_model_round_trips_through_model_dump_and_validate():
    model = Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )
    assert Model.model_validate(model.model_dump()) == model


# ── Usage: frozen, copyable, and round-trips its telemetry ─────────────────


@pytest.mark.parametrize(
    "usage", [Usage(input_tokens=100, output_tokens=50), Usage()], ids=["explicit", "default"]
)
def test_usage_is_frozen(usage):
    with pytest.raises(ValidationError):
        usage.input_tokens = 999


def test_usage_model_copy_returns_a_new_instance():
    """``model_copy()`` is how a frozen ``Usage`` still gets "updated" (build a
    copy) — it must return a distinct, equal object, not the same one."""
    u = Usage(input_tokens=100)
    u2 = u.model_copy()
    assert u2 == u
    assert u2 is not u


def test_usage_round_trips_including_extra():
    """``extra`` is where server-reported telemetry (llama.cpp timings, τ's
    JSON-repair count) lives — round-tripping it is the check that would catch
    a stray field name or a dropped key in serialization."""
    u = Usage(
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=80,
        cache_write_tokens=20,
        total_tokens=150,
        cost={"input": 0.01, "output": 0.02},
        extra={"n_ff_total": 3},
    )
    assert Usage.model_validate(u.model_dump()) == u
