"""SDK Example 4: Custom System Prompt

Override the default system prompt to give the agent specific instructions,
behavior guidelines, and context.
"""

import asyncio

from tau_agent_core.sdk import create_agent_session


# Example 1: Minimal custom prompt
async def example_basic_custom_prompt():
    """Simple system prompt override."""
    session = create_agent_session(
        model="gpt-4o",
        system_prompt="You are a helpful coding assistant. Only respond with Python code.",
    )

    messages = await session.prompt("Write a fibonacci function")
    print("Response:", session.messages[-1])


# Example 2: Detailed system prompt with guidelines
async def example_detailed_system_prompt():
    """Detailed system prompt with multiple guidelines."""
    system_prompt = """You are τ, a senior software engineer working on a Python project.

Guidelines:
1. Always write clean, well-documented code
2. Follow PEP 8 style guide
3. Include type hints in all function signatures
4. Write unit tests for new functions
5. If you need to modify a file, read it first to understand the context

Available tools:
- read: Read file contents
- write: Create or overwrite a file
- bash: Run shell commands
"""

    session = create_agent_session(
        model="gpt-4o",
        tools=["read", "write", "bash"],
        system_prompt=system_prompt,
    )

    messages = await session.prompt("Write a unit test for the existing add function")
    print("Response:", session.messages[-1])


# Example 3: System prompt loaded from a file
async def example_system_prompt_from_file():
    """Load a system prompt from a file."""
    import tempfile
    import os

    # Create a system prompt file (simulating .tau/SYSTEM.md)
    system_prompt_text = """You are τ, a helpful AI assistant specialized in:
- Python development
- System administration
- Documentation

Be concise and direct in your responses."""

    # In practice, you'd load this from ~/.tau/SYSTEM.md or a project file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(system_prompt_text)
        prompt_file = f.name

    try:
        # Load from file
        with open(prompt_file, "r") as f:
            system_prompt = f.read()

        session = create_agent_session(
            model="gpt-4o",
            system_prompt=system_prompt,
        )

        messages = await session.prompt("Summarize the guidelines")
        print("Response:", session.messages[-1])
    finally:
        os.unlink(prompt_file)


# Example 4: Multiple custom prompts for different agents
async def example_multiple_agents():
    """Create multiple sessions with different system prompts."""
    agents = [
        {
            "name": "CodeReviewer",
            "prompt": "You are a strict code reviewer. Critique code for bugs, performance issues, and style violations.",
            "prompt_text": "Review this code:\n\ndef add(a, b):\n    return a + b",
        },
        {
            "name": "DocsWriter",
            "prompt": "You are a technical writer. Create clear, well-structured documentation.",
            "prompt_text": "Document the add function",
        },
    ]

    for agent_config in agents:
        session = create_agent_session(
            model="gpt-4o",
            system_prompt=agent_config["prompt"],
        )
        messages = await session.prompt(agent_config["prompt_text"])
        print(f"\n--- {agent_config['name']} ---")
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        for msg in assistant_msgs:
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    print(block["text"][:200])


if __name__ == "__main__":
    asyncio.run(example_basic_custom_prompt())
    print("\n" + "=" * 50 + "\n")
    asyncio.run(example_detailed_system_prompt())
