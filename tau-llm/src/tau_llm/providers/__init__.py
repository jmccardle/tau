"""τ-llm providers: LLM provider abstraction.

Exports:
    Provider: Abstract base class for LLM providers.

Provider *pooling* (the actual "which provider instance serves this call"
question) lives in ``tau_llm.client`` — see ``docs/PROVIDER-LIFETIME.md``. There
is no separate provider registry: a prior ``ProviderRegistry`` class existed
here but was never wired to anything (``tau_llm.client.stream_simple`` built a
fresh, throwaway one on every call — dead code, see PROVIDER-LIFETIME.md §1)
and was removed rather than kept around describing behaviour it didn't have.
"""

from tau_llm.providers.base import Provider

__all__ = [
    "Provider",
]
