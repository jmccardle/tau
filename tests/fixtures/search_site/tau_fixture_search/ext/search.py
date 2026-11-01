"""The module the `search` registration names: an extension by C3's rule (it has a `name`)."""

name = "search"


def web_fetch(url: str) -> str:
    """Fetch a page (the fixture fetches nothing)."""
    return f"fetched {url}"


async def find(query: str) -> str:
    """Search the web (the fixture finds nothing)."""
    return f"found nothing for {query}"


tools = (web_fetch,)
commands = (find,)
