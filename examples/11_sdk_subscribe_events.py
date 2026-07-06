"""SDK Example 2: Subscribe to Events

Subscribe to agent events to get real-time updates on what the agent is doing.
Events include: agent_start, agent_end, turn_start, turn_end, message_start,
message_update, message_end, tool_execution_start, tool_execution_end, etc.
"""

import asyncio

from tau_agent_core.sdk import create_agent_session
from tau_agent_core.events import AgentEvent


async def main():
    session = create_agent_session(
        model="gpt-4o",
        tools=["read", "write"],
    )

    # 1. Subscribe to all events
    def on_all_events(event: AgentEvent):
        print(f"[{event.type}] turn={event.turn_index}, "
              f"tool={event.tool_name}, error={event.is_error}")

    unsub = session.subscribe(on_all_events)

    # 2. Send a prompt — events will be printed in real-time
    print("Sending prompt...")
    messages = await session.prompt("Write a file called hello.txt")

    # 3. Unsubscribe when done
    unsub()

    # 4. Send another prompt without events
    print("\nSending second prompt (no events)...")
    messages = await session.prompt("Read hello.txt")

    print("\nDone!")
    print(f"Total messages: {len(session.messages)}")


if __name__ == "__main__":
    asyncio.run(main())
