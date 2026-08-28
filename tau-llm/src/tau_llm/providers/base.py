"""τ-llm providers.base: the Provider contract, and the two registries τ dispatches through.

Reference: SUBPHASE-0.0.md, "1. Messages" and Phase 1 Subphase 0 sections.
docs/PLAN-0.9.3.md §4.4 steps 1–3 — multi-vendor breadth.

pi's equivalents, at pi ``5cd93f688``: the ``Provider`` interface
(``packages/ai/src/models.ts:97``) and the ``createProvider()`` factory
(``models.ts:762``). What is ported here is the SHAPE, not the TypeScript.

Two registries, because a vendor and a wire protocol are different things and
τ dispatches on both:

* **API registry** (``register_api``) — maps a wire-protocol id
  (``"openai-completions"``) to the factory that builds a client for it. This
  is the registry that CHOOSES A CLASS, and therefore the one that must refuse
  an id it does not know. Before it existed, ``tau_llm.client`` built an
  ``OpenAICompletionsProvider`` for every model regardless of ``model.api`` —
  so a model declaring ``api="openai-responses"``, which τ has never
  implemented, was silently served over the completions wire.

* **Provider registry** (``register_provider``) — maps a vendor id
  (``"openai"``, ``"groq"``) to a :class:`ProviderSpec`: display name, the wire
  protocol that vendor speaks, its default base URL, and the environment
  variables its credential lives in. A vendor is DATA, not a class, which is
  what makes a new OpenAI-compatible vendor six lines rather than a file::

      register_provider(ProviderSpec(
          id="groq",
          name="Groq",
          api="openai-completions",
          base_url="https://api.groq.com/openai/v1",
          api_key_env=("GROQ_API_KEY",),
      ))

  (pi's ``providers/groq.ts`` is the same six fields plus a model catalog; τ
  ships no model catalogs.)

Registering a vendor is OPTIONAL and always has been: ``Model`` carries its own
``base_url``, so an unregistered vendor id — ``"local-llm"``, an internal
gateway — keeps working as a free-form label. Registration adds the defaults;
it is not a gate. The gate is ``model.api``.

A note on the registry that was here before: an earlier ``ProviderRegistry``
was deleted for being dead code — ``stream_simple`` built a throwaway one on
every call and dispatched through none of it (PROVIDER-LIFETIME.md §1). These
two are reached on every single completion: ``tau_llm.client._resolve_request``
cannot construct a provider without them.

Usage:
    class MyProvider(Provider):
        async def stream_chat(self, model, messages, tools=None, options=None):
            ...
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Protocol
from tau_llm.docs import agent_facing

if TYPE_CHECKING:
    from tau_llm.tools import ToolSpec
    from tau_llm.types import Model


@agent_facing(topic="providers")
class StreamEventStream(Protocol):
    """Structural return type for ``Provider.stream_chat``.

    A provider stream is async-iterable over typed streaming events
    (TextDeltaEvent / ThinkingDeltaEvent / ToolCallDeltaEvent / DoneEvent /
    ErrorEvent). The client (``stream_simple``) wraps it once in
    ``AssistantMessageEventStream`` (streaming.py) — the single stream type
    that adds queue buffering and the terminal ``result()``.
    """

    def __aiter__(self) -> AsyncIterator[Any]: ...


@agent_facing(topic="providers")
class Provider(ABC):
    """Abstract base class for LLM chat providers.

    A Provider instance is bound to ONE endpoint: it bakes in a base URL and a
    credential at construction (that is why ``tau_llm.client`` pools instances
    per endpoint rather than per class). ``id`` and ``name`` say which vendor
    that endpoint belongs to — pi's ``Provider.id``/``.name``
    (``models.ts:98-99``).

    Reference: SUBPHASE-0.0.md, Phase 1 Subphase 0 — Provider interface.

    Methods:
        stream_chat(model, messages, tools, options):
            Returns a StreamEventStream yielding typed StreamEvents.
        aclose():
            Releases whatever the instance holds open.
    """

    #: Vendor id this instance talks to ("openai", "groq"). Stamped by the
    #: factory registered with :func:`register_api`; empty on an instance
    #: constructed directly, which is honest — such an instance was never told
    #: which vendor it serves. Deliberately a plain attribute rather than an
    #: abstract property: making it abstract would break every direct
    #: ``MyProvider()`` construction, including this module's own docstring.
    id: str = ""
    #: Human-readable vendor label ("OpenAI", "Groq"). Same stamping rule.
    name: str = ""

    @abstractmethod
    async def stream_chat(
        self,
        model: Model,
        messages: list[Any],
        tools: list[ToolSpec] | None = None,
        options: dict[str, Any] | None = None,
    ) -> StreamEventStream:
        """Stream chat completions from the LLM.

        Args:
            model: The Model configuration to use for the request.
            messages: List of τ message objects (user/assistant/toolResult).
            tools: Optional list of objects satisfying ToolSpec. The agent
                loop passes ``AgentTool`` wrappers, not ``ToolDefinition``;
                see ToolSpec for why this is a Protocol.
            options: Optional provider-specific options (temperature, max_tokens, etc.).

        Returns:
            A StreamEventStream — an async iterator of typed streaming events
            (TextDeltaEvent, ThinkingDeltaEvent, ToolCallDeltaEvent, DoneEvent,
            ErrorEvent). ``stream_simple`` wraps it in AssistantMessageEventStream,
            which exposes the terminal AssistantMessage via ``result()``.

        Raises:
            NotImplementedError: If the provider hasn't implemented this method.
        """
        ...

    async def aclose(self) -> None:
        """Release anything this instance holds open (HTTP connections, …).

        ``tau_llm.client.aclose_providers`` calls this on EVERY pooled provider,
        so the contract has to live on the base class — otherwise a provider
        registered through :func:`register_api` would crash the shutdown path of
        an application that never heard of it. The default is a no-op because a
        provider that opens nothing has nothing to close; one that does (see
        ``OpenAICompletionsProvider.aclose``) overrides it, and must stay
        idempotent — the pool may close an instance that never issued a request.
        """
        return None


@agent_facing(topic="providers")
class ApiFactory(Protocol):
    """Builds a :class:`Provider` for one wire protocol, bound to one endpoint.

    Keyword-only, so a factory can ignore what it does not need and so adding a
    field later does not silently shift a positional argument.
    """

    def __call__(
        self,
        *,
        provider_id: str,
        name: str,
        base_url: str,
        api_key: str | None,
    ) -> Provider:
        """Build the provider.

        Args:
            provider_id: Vendor id to stamp onto ``Provider.id``.
            name: Vendor display name to stamp onto ``Provider.name``.
            base_url: Fully resolved endpoint — the caller has already applied
                the model's own ``base_url`` and any vendor default, so a
                factory must NOT substitute one of its own.
            api_key: Resolved credential, or None when neither the call, the
                model nor the vendor's environment variables supplied one. A
                factory that requires a key raises at request time (Fail-Early:
                no fabricated credential).

        Returns:
            A provider bound to that endpoint.
        """
        ...


@agent_facing(topic="providers")
@dataclass(frozen=True)
class ProviderSpec:
    """One vendor, as data.

    Everything τ needs to reach an OpenAI-compatible vendor that is not OpenAI.
    pi's ``CreateProviderOptions`` (``models.ts:738``) minus the parts τ has no
    consumer for — see this module's docstring and ``Provider`` above.

    Attributes:
        id: Vendor id. Matches ``Model.provider``; that is the lookup key.
        name: Display name. Defaults to ``id``, as pi's ``createProvider`` does.
        api: The ONE wire protocol this vendor speaks. A model claiming a
            different ``api`` for this vendor is a configuration error, not a
            request to be attempted — see ``client._resolve_request``. A gateway
            that genuinely speaks two protocols registers two ids.
        base_url: Default endpoint, used only when the ``Model`` carries no
            ``base_url`` of its own. None means the vendor has no fixed endpoint
            (a self-hosted server), and a model must then supply one.
        api_key_env: Environment variables searched, in order, for this vendor's
            credential. Empty means "this vendor has no known credential
            source"; a NON-empty tuple that resolves to nothing is a hard error
            rather than a silent fall-through to another vendor's key.
    """

    id: str
    api: str
    name: str = ""
    base_url: str | None = None
    api_key_env: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("ProviderSpec.id must be a non-empty vendor id")
        if not self.api:
            raise ValueError(f"ProviderSpec(id={self.id!r}).api must name a wire protocol")
        if not self.name:
            object.__setattr__(self, "name", self.id)

    def resolve_api_key(self) -> str | None:
        """First non-empty value among ``api_key_env``, or None if none is set."""
        for var in self.api_key_env:
            value = os.environ.get(var)
            if value:
                return value
        return None


# ──────────────────────────────────────────────────────────────────────────
# The registries. Process-global and mutable on purpose: an embedding
# application registers its vendors at import time, exactly as it would with
# pi's provider factories.
# ──────────────────────────────────────────────────────────────────────────

_API_FACTORIES: dict[str, ApiFactory] = {}
_PROVIDER_SPECS: dict[str, ProviderSpec] = {}


@agent_facing(topic="providers")
def register_api(api: str, factory: ApiFactory, *, replace: bool = False) -> None:
    """Register the factory that builds clients for one wire protocol.

    Args:
        api: Wire-protocol id, matching ``Model.api`` (e.g. ``"openai-completions"``).
        factory: See :class:`ApiFactory`.
        replace: Required to overwrite an existing registration. Without it a
            second registration of the same id raises: two libraries silently
            fighting over which client serves a protocol is the kind of
            invisible mis-routing this whole module exists to prevent.

    Raises:
        ValueError: On an empty id, or on a duplicate without ``replace=True``.
    """
    if not api:
        raise ValueError("register_api() needs a non-empty api id")
    if api in _API_FACTORIES and not replace:
        raise ValueError(
            f"api {api!r} is already registered; pass replace=True to override it deliberately"
        )
    _API_FACTORIES[api] = factory


@agent_facing(topic="providers")
def unregister_api(api: str) -> None:
    """Remove a wire-protocol registration.

    Raises:
        KeyError: If nothing is registered under that id — undoing a
            registration that never happened means the caller's model of the
            registry is wrong, and saying so is cheaper than not.
    """
    if api not in _API_FACTORIES:
        raise KeyError(f"no api registered as {api!r}")
    del _API_FACTORIES[api]


@agent_facing(topic="providers")
def registered_apis() -> tuple[str, ...]:
    """Every registered wire-protocol id, sorted."""
    return tuple(sorted(_API_FACTORIES))


@agent_facing(topic="providers")
def get_api_factory(api: str) -> ApiFactory:
    """The factory for ``api``.

    Raises:
        ValueError: If ``api`` is unknown. The message names what was asked for
            and what exists — an unimplemented protocol must fail loudly here,
            never fall through to whichever client happens to be built in.
    """
    factory = _API_FACTORIES.get(api)
    if factory is None:
        known = ", ".join(registered_apis()) or "<none>"
        raise ValueError(
            f"No provider implementation for api {api!r}. Registered apis: {known}. "
            f"Register one with tau_llm.providers.register_api({api!r}, factory)."
        )
    return factory


@agent_facing(topic="providers")
def register_provider(spec: ProviderSpec, *, replace: bool = False) -> None:
    """Register a vendor's defaults.

    Args:
        spec: The vendor, as data.
        replace: Required to overwrite an existing registration, for the same
            reason as :func:`register_api` — a silently redirected vendor sends
            prompts and credentials somewhere the operator did not choose.

    Raises:
        ValueError: On a duplicate id without ``replace=True``.
    """
    if spec.id in _PROVIDER_SPECS and not replace:
        raise ValueError(
            f"provider {spec.id!r} is already registered; "
            f"pass replace=True to override it deliberately"
        )
    _PROVIDER_SPECS[spec.id] = spec


@agent_facing(topic="providers")
def unregister_provider(provider_id: str) -> None:
    """Remove a vendor registration.

    Raises:
        KeyError: If that vendor was never registered.
    """
    if provider_id not in _PROVIDER_SPECS:
        raise KeyError(f"no provider registered as {provider_id!r}")
    del _PROVIDER_SPECS[provider_id]


@agent_facing(topic="providers")
def registered_providers() -> tuple[str, ...]:
    """Every registered vendor id, sorted."""
    return tuple(sorted(_PROVIDER_SPECS))


@agent_facing(topic="providers")
def get_provider_spec(provider_id: str) -> ProviderSpec | None:
    """The vendor's spec, or None if it was never registered.

    None is not an error: an unregistered vendor id is a free-form label on a
    ``Model`` that already carries its own endpoint. Only a model whose ``api``
    is unknown cannot be served.
    """
    return _PROVIDER_SPECS.get(provider_id)
