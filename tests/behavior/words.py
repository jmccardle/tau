"""D16 section 6, first case, as a real module: a function that is a tool and a command, and the
module that lists it is the extension (it has a `name`)."""

from pathlib import Path


def word_count(path: Path) -> str:
    """Count the words in a file."""
    return f"{len(path.read_text().split())} words"


name = "words"
tools = commands = [word_count]
