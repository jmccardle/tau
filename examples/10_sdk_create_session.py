"""SDK Example 1: Create a Session and Send a Prompt

The most basic way to use τ is to create a session and send a prompt.
This example shows the minimal setup required.
"""

import asyncio

from tau_agent_core.sdk import create_agent_session


async def main():
    # 1. Create a session with default settings
    session = create_agent_session(
        model="gpt-4o",
    )

    # 2. Send a prompt
    messages = await session.prompt("Say hello, world!")

    # 3. Check the response
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", [])
        print(f"\n[{role}]")
        for block in content:
            if isinstance(block, dict):
                print(block.get("text", ""))


if __name__ == "__main__":
    asyncio.run(main())
