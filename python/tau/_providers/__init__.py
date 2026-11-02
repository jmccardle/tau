"""The providers that ship with τ next, and the one helper that builds them."""

from collections.abc import Mapping

from tau._protocols import Provider
from tau._providers.anthropic import AnthropicProvider
from tau._providers.gemini import GeminiProvider
from tau._providers.mock import MockProvider
from tau._providers.openai import OpenAIProvider


def builtin_providers(environ: Mapping[str, str]) -> dict[str, Provider]:
    """The built-in providers by name (`"openai"`, `"anthropic"`, `"gemini"`, `"mock"`), ready
    to hand to `Driver` and `check_config`. `environ` is where the hosted providers look up API
    keys. The caller closes them (`await p.aclose()`)."""
    providers: list[Provider] = [
        OpenAIProvider(environ=environ),
        AnthropicProvider(environ=environ),
        GeminiProvider(environ=environ),
        MockProvider(),
    ]
    return {p.name: p for p in providers}
