"""τ-ai providers.registry: Provider registry for LLM providers.

Reference: SUBPHASE-0.0.md, Phase 1 Subphase 0 sections.

Registry is the central registry for LLM providers. It supports
registering, looking up, and listing providers by ID.

Usage:
    registry = ProviderRegistry()
    registry.register("openai", OpenAIProvider())
    provider = registry.get("openai")
    for pid, provider in registry.list_all().items():
        ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tau_ai.providers.base import Provider

if TYPE_CHECKING:
    pass


class ProviderRegistry:
    """Registry for LLM providers.

    Singleton-like registry that maps provider IDs to provider instances.
    Providers are registered at application startup and looked up during
    agent loop execution.

    Reference: SUBPHASE-0.0.md, Phase 1 Subphase 0 — Provider registry.

    Attributes:
        _providers: Dict mapping provider_id to Provider instances.

    Example:
        >>> registry = ProviderRegistry()
        >>> registry.register("openai", OpenAIProvider())
        >>> provider = registry.get("openai")
        >>> assert provider is not None
    """

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    def register(self, provider_id: str, provider: Provider) -> None:
        """Register a provider implementation.

        Args:
            provider_id: Unique provider identifier (e.g. "openai").
            provider: Provider instance to register.

        Raises:
            ValueError: If provider_id is already registered.

        Example:
            >>> registry = ProviderRegistry()
            >>> registry.register("openai", OpenAIProvider())
        """
        if provider_id in self._providers:
            raise ValueError(f"Provider '{provider_id}' is already registered.")
        self._providers[provider_id] = provider

    def get(self, provider_id: str) -> Provider:
        """Get a provider by ID.

        Args:
            provider_id: Provider ID to look up.

        Returns:
            The registered provider.

        Raises:
            KeyError: If provider_id is not found.
        """
        if provider_id not in self._providers:
            raise KeyError(f"Provider '{provider_id}' is not registered.")
        return self._providers[provider_id]

    def list_all(self) -> dict[str, Provider]:
        """Get all registered providers.

        Returns:
            Dict mapping provider IDs to provider instances.
        """
        return dict(self._providers)

    def get_or_default(self, provider_id: str, default: Provider | None = None) -> Provider | None:
        """Get a provider by ID, or return default.

        Args:
            provider_id: Provider ID to look up.
            default: Default value if not found.

        Returns:
            The provider, or default if not found.
        """
        return self._providers.get(provider_id, default)


# Alias to match the contract from SUBPHASE-0.0.md
Registry = ProviderRegistry
