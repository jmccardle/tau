"""The providers that ship with τ next, and the one helper that builds them."""

from collections.abc import Mapping

from tau_next.protocols import Provider
from tau_next.providers.anthropic import AnthropicProvider
from tau_next.providers.mock import MockProvider
from tau_next.providers.openai import OpenAIProvider


def builtin_providers(environ: Mapping[str, str]) -> dict[str, Provider]:
    """The built-in providers by name (`"openai"`, `"anthropic"`, `"mock"`), ready to hand to
    `Driver` and `check_config`. `environ` is where the OpenAI and Anthropic providers look up
    API keys. The caller closes them (`await p.aclose()`)."""
    providers: list[Provider] = [
        OpenAIProvider(environ=environ),
        AnthropicProvider(environ=environ),
        MockProvider(),
    ]
    return {p.name: p for p in providers}
