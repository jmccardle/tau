"""A config entry with no api_key must send none, not invent one.

`TauBackend.__init__` once substituted the local-server sentinel `"not-needed"`
for an absent key, so a config naming only a keyless model sent `not-needed` to
`https://api.openai.com/v1`. An absent key now goes absent, and the endpoint's
own 401 is the answer (docs/PROVIDER-CREDENTIALS.md).

Two properties, held apart on purpose:

* an ABSENT api_key reaches the provider absent, so the endpoint can refuse;
* an EXPLICIT "not-needed" is passed through, because that is how a local server
  says "no credential required" and the shipped template writes exactly that.
"""

from __future__ import annotations

from tau_coding_agent.backends import TauBackend


def _backend(config: dict) -> TauBackend:
    return TauBackend({"model": "x", **config})


def test_an_absent_api_key_is_not_replaced_by_a_sentinel():
    backend = _backend({"backend": "openai"})
    assert not backend.agent_session.summarizer()[1], (
        f"a missing api_key became {backend.agent_session.summarizer()[1]!r} — the provider's "
        f"'No API key for provider' gate can never fire against a truthy value"
    )


def test_an_explicit_local_sentinel_survives():
    backend = _backend({"backend": "openai", "api_key": "not-needed"})
    assert backend.agent_session.summarizer()[1] == "not-needed"


def test_a_real_key_survives():
    backend = _backend({"backend": "openai", "api_key": "sk-real"})
    assert backend.agent_session.summarizer()[1] == "sk-real"


def test_the_provider_sends_no_credential_with_no_key():
    """The other half: with the key absent, nothing stands in for it. The request
    goes with no ``Authorization`` and the endpoint answers (#77)."""
    from tau_llm.providers.openai import OpenAICompletionsProvider

    provider = OpenAICompletionsProvider(api_key=None)
    assert "Authorization" not in provider._get_client().headers
