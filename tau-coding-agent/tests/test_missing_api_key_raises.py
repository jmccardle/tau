"""A config entry with no api_key must raise, not invent one.

`OpenAICompletionsProvider` has raised `No API key for provider: …` on a falsy
key for a while, and the docs say so. The gate never fired from the TUI/CLI
path, because `TauBackend.__init__` substituted the local-server sentinel
`"not-needed"` first — a truthy string. The effect, measured in a clean
container: a config naming only a keyless model sent `not-needed` to
`https://api.openai.com/v1` (the base_url default) and surfaced a third party's
401 instead of a startup error about the missing credential.

Two properties, held apart on purpose:

* an ABSENT api_key reaches the provider absent, so the provider can refuse;
* an EXPLICIT "not-needed" is passed through, because that is how a local server
  says "no credential required" and the shipped template writes exactly that.
"""

from __future__ import annotations

import pytest

from tau_coding_agent.backends import TauBackend


def _backend(config: dict) -> TauBackend:
    return TauBackend({"model": "x", **config})


def test_an_absent_api_key_is_not_replaced_by_a_sentinel():
    backend = _backend({"backend": "openai"})
    assert not backend._api_key, (
        f"a missing api_key became {backend._api_key!r} — the provider's "
        f"'No API key for provider' gate can never fire against a truthy value"
    )


def test_an_explicit_local_sentinel_survives():
    backend = _backend({"backend": "openai", "api_key": "not-needed"})
    assert backend._api_key == "not-needed"


def test_a_real_key_survives():
    backend = _backend({"backend": "openai", "api_key": "sk-real"})
    assert backend._api_key == "sk-real"


@pytest.mark.asyncio
async def test_the_provider_refuses_a_turn_with_no_key():
    """The other half: with the key absent, the provider raises the documented
    error rather than sending a request anywhere."""
    from tau_llm.providers.openai import OpenAICompletionsProvider
    from tau_llm.types import Model

    provider = OpenAICompletionsProvider(api_key=None)
    model = Model(
        id="x",
        name="x",
        provider="openai",
        api="openai-completions",
        base_url="https://api.openai.com/v1",
        context_window=8192,
        max_tokens=1024,
    )

    with pytest.raises(ValueError, match="No API key for provider"):
        stream = await provider.stream_chat(model, [], {})
        async for _ in stream:
            pass
