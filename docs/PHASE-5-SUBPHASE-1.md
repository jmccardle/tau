# Phase 5 Subphase 1 — Compaction Engine

> **Topic**: Implement the compaction engine that manages context window usage.

## Scope

This subphase implements the compaction logic in `tau_agent_core.compaction`. It:
1. Determines when compaction is needed (should_compact)
2. Prepares the context for compaction (prepare_compaction)
3. Runs compaction via LLM call (compact)
4. Writes compaction entries to the session file

## Reference

- `SUBPHASE-5-SUBPHASE-0.md`: compaction types
- `SUBPHASE-0.0.md` lines 220-260: session entry JSON schema
- `docs/tau-agent-core.md` lines 350-450: compaction design
- `docs/IMPLEMENTATION-PLAN.md` lines 360-420: compaction spec
- pi's `compaction.js` (reference)

## Implementation Outline

### `tau_agent_core/compaction.py`

```python
def should_compact(
    messages: list[Message],
    model_context_window: int,
    margin: int = 2000,
    estimated_tokens_per_message: int = 150,
) -> bool:
    """Check if the current context is approaching the model's context window.

    Returns True if compaction should be triggered.
    """
    estimated_tokens = len(messages) * estimated_tokens_per_message
    available = model_context_window - margin
    return estimated_tokens >= available


def prepare_compaction(
    entries: list[dict],
    first_kept_entry_id: str,
    custom_instructions: str | None = None,
) -> dict:
    """Prepare the context for compaction.

    Returns a dict with:
    - first_kept_entry: the first entry to keep in full
    - compacted_entries: entries to compact (before first_kept)
    - instructions: system prompt for compaction
    - messages: the full message list for the compaction prompt
    """
    # ... implement
    ...


async def compact(
    session: AgentSession,
    config: CompactionConfig,
    custom_instructions: str | None = None,
) -> CompactionResult:
    """Run compaction via LLM call.

    1. Build compaction prompt with context before the keep point
    2. Call the LLM to generate a summary
    3. Write a compaction entry to the session
    4. Return the result

    The compaction entry replaces all entries before first_kept_id
    with a summary message.
    """
    ...
```

### Compaction Prompt

The compaction prompt sent to the LLM:

```
You are a context compaction assistant. Given the following conversation history before the current point, provide a concise summary that captures the essential information, decisions, and context needed for future turns.

IMPORTANT:
- Be concise but comprehensive
- Include all file paths, code snippets, and configurations mentioned
- Note any decisions made or preferences expressed
- Do NOT include verbatim conversation - summarize
- Include the user's intent and the assistant's approach

[custom instructions if provided]

Conversation before this point:
{conversation_text}

Summary:
```

### Compaction Entry Format

```json
{
  "id": "uuid",
  "type": "compaction",
  "timestamp": 1718668800000,
  "parent_id": null,
  "first_kept_id": "entry_id_of_first_kept_message",
  "summary": "The user was working on...",
  "tokens_saved": 500,
  "compacted_entries": ["entry_1", "entry_2", ...]
}
```

### Active Path Reconstruction After Compaction

When building the active path:
1. Find all entries from root to `active_entry_id`
2. If a `compaction` entry is in the path, skip entries before `first_kept_id`
3. Add the compaction summary as a virtual user message
4. Continue with entries after `first_kept_id`

## Done Criteria

- `should_compact()` returns True when tokens exceed `context_window - margin`
- `should_compact()` returns False when tokens are below the threshold
- `prepare_compaction()` correctly identifies the first-kept entry and compacted entries
- `compact()` makes an LLM call with the compaction prompt
- `compact()` writes a compaction entry to the session
- `compact()` returns a `CompactionResult` with the summary and stats
- Compaction entries are properly handled in `get_active_messages()`
- The compaction summary appears as a user message in the active path
- Custom instructions are included in the compaction prompt
- `compact_callback` progress updates are emitted during compaction

## Testing Strategy

### Test 1: should_compact — should compact

```python
async def test_should_compact_yes():
    messages = [UserMessage(content=[TextContent(text="x")]) for _ in range(10000)]
    assert should_compact(messages, model_context_window=128000, margin=2000,
                          estimated_tokens_per_message=15)
    # 10000 * 15 = 150000 > 128000 - 2000 = 126000
```

### Test 2: should_compact — should not compact

```python
async def test_should_compact_no():
    messages = [UserMessage(content=[TextContent(text="x")]) for _ in range(100)]
    assert not should_compact(messages, model_context_window=128000, margin=2000,
                              estimated_tokens_per_message=15)
    # 100 * 15 = 1500 < 126000
```

### Test 3: prepare_compaction

```python
async def test_prepare_compaction():
    entries = [
        {"id": "e1", "type": "message", "timestamp": 1, "message": {...}},
        {"id": "e2", "type": "message", "timestamp": 2, "message": {...}},
        {"id": "e3", "type": "message", "timestamp": 3, "message": {...}},
    ]
    result = prepare_compaction(entries, first_kept_entry_id="e2")
    assert result["first_kept_entry"]["id"] == "e2"
    assert result["compacted_entries"][0]["id"] == "e1"
```

### Test 4: compact writes entry

```python
async def test_compact_writes_entry(mock_openai):
    mgr = SessionManager.in_memory()
    session_path = mgr.new_session()
    session = create_agent_session(
        model="gpt-4o",
        session_manager=mgr,
    )

    # Add some messages
    await session.prompt("hello")
    await session.prompt("world")

    # Compact
    result = await session.compact()

    assert result.summary is not None
    assert result.tokens_saved > 0
    # Check compaction entry was written
    entries = mgr._memory_store
    assert any(e.get("type") == "compaction" for e in entries)
```

### Test 5: Compaction summary in active path

```python
async def test_compaction_summary_in_active_path():
    mgr = SessionManager.in_memory()
    session_path = mgr.new_session()
    mgr._active_session_path = session_path

    # Simulate compaction entry
    mgr.append_entry({
        "id": "comp1", "type": "compaction", "timestamp": 3,
        "first_kept_id": "new1",
        "summary": "User was working on file.py",
        "tokens_saved": 500,
    })
    mgr.append_entry({
        "id": "new1", "type": "message", "timestamp": 4,
        "message": {"role": "user", "content": [{"type": "text", "text": "continue"}]},
    })

    messages = mgr.get_active_messages()
    # Should have the summary + continue message
    assert len(messages) == 2
    assert "User was working on file.py" in messages[0].content[0].text
```

### Test 6: Custom instructions in prompt

```python
async def test_custom_instructions_in_prompt(mock_openai):
    session = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
    )
    # Compact with custom instructions
    # The mock should capture the prompt
    # Verify "focus on recent changes" appears in the LLM prompt
    ...
```

## Success Signal

All 6 test categories pass. Compaction correctly identifies when it's needed, runs the LLM call, writes the compaction entry, and the active path correctly reconstructs with the summary. The compaction summary replaces compacted entries in the active path.
