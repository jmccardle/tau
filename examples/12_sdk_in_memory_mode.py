"""SDK Example 3: Use In-Memory Mode for Testing

In-memory mode is perfect for testing and CI/CD. No files are created
on disk — all session data stays in memory.
"""

import asyncio

from tau_agent_core.sdk import create_agent_session
from tau_agent_core.session_manager import SessionManager


async def main():
    # 1. Create an in-memory session manager
    session_manager = SessionManager.in_memory()

    # 2. Create a session using the in-memory manager
    session = create_agent_session(
        model="gpt-4o",
        session_manager=session_manager,
        tools=["read", "write"],
    )

    # 3. Use the session normally
    await session.prompt("Write a Python function")
    print(f"Messages: {len(session.messages)}")

    # 4. Multiple turns
    await session.prompt("Read the function back")
    print(f"Messages: {len(session.messages)}")

    # 5. Abort during a prompt
    session.abort()

    # 6. Verify the session manager has no files on disk
    sessions = session_manager.list()
    print(f"Sessions in manager: {len(sessions)}")
    print("In-memory mode: no files created on disk ✓")


async def test_prompt_returns_messages():
    """Example test function showing in-memory mode usage."""
    session_manager = SessionManager.in_memory()
    session = create_agent_session(
        model="gpt-4o",
        session_manager=session_manager,
    )

    messages = await session.prompt("Hello")

    # Assertions
    assert len(messages) > 0
    assert session.is_streaming is False

    user_msgs = [m for m in messages if m.get("role") == "user"]
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    assert len(user_msgs) >= 1
    assert len(assistant_msgs) >= 1


if __name__ == "__main__":
    asyncio.run(main())
