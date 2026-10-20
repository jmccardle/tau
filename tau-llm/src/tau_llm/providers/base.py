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

    def __aiter__(self) -> AsyncIterator[Any]:
        """Iterate the provider's events.

        Returns:
            An async iterator of typed streaming events that ends after the
            terminal ``DoneEvent`` or ``ErrorEvent``.
        """
        ...


@agent_facing(topic="providers")
class Provider(ABC):
    """Abstract base class for LLM chat providers.

    A Provider instance is bound to ONE endpoint: it bakes in a base URL and a
    credential at construction (that is why ``tau_llm.client`` pools instances
    per endpoint rather than per class). ``id`` and ``name`` say which vendor
    that endpoint belongs to — pi's ``Provider.id``/``.name``
    (``models.ts:98-99``).

    Reference: SUBPHASE-0.0.md, Phase 1 Subphase 0 — Provider interface.

    Attributes:
        id: Vendor id (``"openai"``, ``"groq"``), matching ``Model.provider``.
            Empty on the class; the :class:`ApiFactory` stamps it after construction.
        name: Human-readable vendor label (``"OpenAI"``, ``"Groq"``). Same
            stamping rule as ``id``.
    """

    id: str = ""
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
            credential, and only for a request to ``base_url``
            (:meth:`reads_env_for`). An entry with its own endpoint never gets
            them (docs/PROVIDER-CREDENTIALS.md §5).
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

    def reads_env_for(self, base_url: str | None) -> bool:
        """Whether a request to ``base_url`` may take its key from ``api_key_env``.

        True only for this vendor's own registered endpoint, compared without a
        trailing slash, or for no ``base_url`` (which means that endpoint). A
        vendor with no registered endpoint never lends its variables.
        """
        if self.base_url is None:
            return False
        return base_url is None or base_url.rstrip("/") == self.base_url.rstrip("/")

    def resolve_api_key(self) -> str | None:
        """Look up this vendor's credential in the environment.

        Returns:
            The first non-empty value among ``api_key_env``, in order, or None
            when none is set (including when ``api_key_env`` is empty).
        """
        for var in self.api_key_env:
            value = os.environ.get(var)
            if value:
                return value
        return None


_API_FACTORIES: dict[str, ApiFactory] = {}
_SENDS_WITHOUT_KEY: set[str] = set()
_PROVIDER_SPECS: dict[str, ProviderSpec] = {}


@agent_facing(topic="providers")
def register_api(
    api: str, factory: ApiFactory, *, replace: bool = False, sends_without_key: bool = False
) -> None:
    """Register the factory that builds clients for one wire protocol.

    Args:
        api: Wire-protocol id, matching ``Model.api`` (e.g. ``"openai-completions"``).
        factory: See :class:`ApiFactory`.
        replace: Required to overwrite an existing registration. Without it a
            second registration of the same id raises: two libraries silently
            fighting over which client serves a protocol is the kind of
            invisible mis-routing this whole module exists to prevent.
        sends_without_key: The factory's provider sends a request when no key
            resolves, and the endpoint's own refusal is the error
            (docs/PROVIDER-CREDENTIALS.md §2). False: ``tau_llm.client``
            refuses before any request.

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
    if sends_without_key:
        _SENDS_WITHOUT_KEY.add(api)
    else:
        _SENDS_WITHOUT_KEY.discard(api)


@agent_facing(topic="providers")
def api_sends_without_key(api: str) -> bool:
    """Whether ``api``'s provider sends a request when no key resolves.

    Args:
        api: Wire-protocol id, as in ``Model.api``.
    """
    return api in _SENDS_WITHOUT_KEY


@agent_facing(topic="providers")
def unregister_api(api: str) -> None:
    """Remove a wire-protocol registration.

    Args:
        api: Wire-protocol id previously passed to :func:`register_api`.

    Raises:
        KeyError: If nothing is registered under that id — undoing a
            registration that never happened means the caller's model of the
            registry is wrong, and saying so is cheaper than not.
    """
    if api not in _API_FACTORIES:
        raise KeyError(f"no api registered as {api!r}")
    del _API_FACTORIES[api]
    _SENDS_WITHOUT_KEY.discard(api)


@agent_facing(topic="providers")
def registered_apis() -> tuple[str, ...]:
    """List the registered wire protocols.

    Returns:
        Every registered wire-protocol id, sorted.
    """
    return tuple(sorted(_API_FACTORIES))


@agent_facing(topic="providers")
def get_api_factory(api: str) -> ApiFactory:
    """Look up the client factory for a wire protocol.

    Args:
        api: Wire-protocol id, as in ``Model.api``.

    Returns:
        The factory registered under ``api``.

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

    Args:
        provider_id: Vendor id, as in ``ProviderSpec.id``.

    Raises:
        KeyError: If that vendor was never registered.
    """
    if provider_id not in _PROVIDER_SPECS:
        raise KeyError(f"no provider registered as {provider_id!r}")
    del _PROVIDER_SPECS[provider_id]


@agent_facing(topic="providers")
def resolve_request_key(provider_id: str, api_key: str | None, base_url: str | None) -> str | None:
    """The key a request to ``provider_id`` at ``base_url`` carries; None for none.

    ``api_key`` when non-empty; else the vendor's ``api_key_env``, only when
    ``base_url`` is the vendor's own endpoint (:meth:`ProviderSpec.reads_env_for`).
    The one place τ resolves a key: ``tau_llm.client`` sends this, and
    :func:`resolves_api_key` reports it.

    Args:
        provider_id: Vendor id, as in ``Model.provider``.
        api_key: The key the caller passes; None or ``""`` for none.
        base_url: The request's endpoint; None for the vendor's default.
    """
    if api_key:
        return api_key
    spec = _PROVIDER_SPECS.get(provider_id)
    if spec is None or not spec.reads_env_for(base_url):
        return None
    return spec.resolve_api_key()


@agent_facing(topic="providers")
def resolves_api_key(provider_id: str, api_key: str | None, base_url: str | None) -> bool:
    """Whether :func:`resolve_request_key` finds a key for this request.

    Args:
        provider_id: Vendor id, as in ``Model.provider``.
        api_key: The key the caller passes; None or ``""`` for none.
        base_url: The request's endpoint; None for the vendor's default.
    """
    return resolve_request_key(provider_id, api_key, base_url) is not None


@agent_facing(topic="providers")
def credential_hint(provider_id: str, base_url: str | None) -> str:
    """What to set so a request to ``provider_id`` at ``base_url`` carries a key.

    Appended to an HTTP 401 (:class:`~tau_llm.errors.ProviderHttpError`).

    Args:
        provider_id: Vendor id, as in ``Model.provider``.
        base_url: The request's endpoint; None for the vendor's default.

    Returns:
        One sentence naming the model's ``api_key`` in τ's config.json, and the
        vendor's environment variables when :func:`resolve_request_key` would
        read them for ``base_url``.
    """
    spec = _PROVIDER_SPECS.get(provider_id)
    reads_env = spec is not None and spec.reads_env_for(base_url)
    env = " or ".join(spec.api_key_env) if spec is not None and reads_env else ""
    config = '"api_key" on the model\'s "models" entry in τ\'s config.json'
    return f"To send a key, set {env} or {config}." if env else f"To send a key, set {config}."


@agent_facing(topic="providers")
def registered_providers() -> tuple[str, ...]:
    """List the registered vendors.

    Returns:
        Every registered vendor id, sorted.
    """
    return tuple(sorted(_PROVIDER_SPECS))


@agent_facing(topic="providers")
def get_provider_spec(provider_id: str) -> ProviderSpec | None:
    """Look up a vendor's defaults.

    Args:
        provider_id: Vendor id, as in ``Model.provider``.

    Returns:
        The registered spec, or None if that vendor was never registered. None
        is not an error: an unregistered vendor id is a free-form label on a
        ``Model`` that carries its own endpoint; only an unknown ``api`` cannot
        be served.
    """
    return _PROVIDER_SPECS.get(provider_id)


@agent_facing(topic="providers")
def split_tool_result_content(content: Any) -> tuple[list[str], list[tuple[str, str]]]:
    """A tool result's content as (text parts, [(mime_type, base64 data), ...]).

    Every client needs the same split, because every client has to put the text
    somewhere the wire format calls a tool result and the images somewhere it
    does not. It lives here rather than three times over: the copies in
    ``openai.py`` and ``google.py`` had already drifted on the join separator and
    on which non-block shapes they tolerated, and a shape rule that differs per
    provider is a shape rule nobody can state.

    Accepts the three shapes a tool result reaches a client in: a bare string,
    a list of pydantic blocks (the live path, ``ToolResultMessage.content``), and
    a list of raw dicts (the persisted path — a reloaded session arrives as
    ``model_dump()``ed messages).

    Text parts come back unjoined, because the separator is the caller's: the
    OpenAI client has always joined with a space and the Google client with an
    empty string, and quietly changing how a multi-block text result reads is not
    something an image change should do.

    Args:
        content: The tool result's content, in any of the three shapes above.

    Returns:
        A ``(text_parts, images)`` pair. ``images`` holds ``(mime_type, data)``
        with ``data`` still base64-encoded.

    Raises:
        TypeError: If ``content`` is neither a string nor an iterable of blocks,
            or if it holds a block of a type this cannot read. Fabricating text
            from an unreadable value is how an image became a filename in the
            first place; a wrong tool result must not reach the model looking
            like a right one.
    """
    if isinstance(content, str):
        return [content], []
    if content is None:
        return [], []
    if not isinstance(content, (list, tuple)):
        raise TypeError(
            f"tool result content must be a string or a list of blocks, got "
            f"{type(content).__name__}: {content!r}"
        )

    parts: list[str] = []
    images: list[tuple[str, str]] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if isinstance(block, dict):
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "image":
                images.append((block.get("mime_type", ""), block.get("data", "")))
            elif "content" in block:
                parts.append(str(block["content"]))
            else:
                raise TypeError(f"unreadable tool result block: {block!r}")
            continue
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(block.text)
        elif btype == "image":
            images.append((block.mime_type, block.data))
        else:
            raise TypeError(f"unreadable tool result block: {block!r}")
    return parts, images
