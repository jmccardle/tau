"""The providers that ship with τ next, and the one helper that builds them."""

from collections.abc import Mapping

from tau_next.protocols import Provider
from tau_next.providers.gemini import GeminiProvider
from tau_next.providers.mock import MockProvider
from tau_next.providers.openai import OpenAIProvider


def builtin_providers(environ: Mapping[str, str]) -> dict[str, Provider]:
    """The built-in providers by name (`"openai"`, `"gemini"`, `"mock"`), ready to hand to
    `Driver` and `check_config`. `environ` is where the OpenAI and Gemini providers look up API
    keys. The caller closes them (`await p.aclose()`)."""
    providers: list[Provider] = [
        OpenAIProvider(environ=environ),
        GeminiProvider(environ=environ),
        MockProvider(),
    ]
    return {p.name: p for p in providers}
