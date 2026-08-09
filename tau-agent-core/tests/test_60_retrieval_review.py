"""Smoke test for ``examples/60_retrieval_review.py`` — the W7/G3 demo.

This test exists because the demo SHIPPED BROKEN. It was written, its machinery was
validated by a standalone script, and it was reported as working — but the extension
itself had never once been executed, and `/review` died on the first line of dispatch:
``register_command`` was handed a bare function instead of a ``{description, handler}``
dict, the handler took ``(args)`` instead of ``(args, ctx)``, and ``ui.panel`` was
called with a markdown blob and a ``title=`` kwarg it does not accept.

A demo that has never been run is the worst kind of placeholder: it is the artifact
whose whole job is to prove the feature works.

So this drives the REAL path — the extension registry, the real command dispatch
signature, the real panel validator — with only the network boundary faked. If any of
those contracts drift again, this fails instead of the user.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from tau_ai.constraints import ConstraintViolation, DecodeConstraints
from tau_ai.types import AssistantMessage, Model, TextContent

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "60_retrieval_review.py"
_spec = importlib.util.spec_from_file_location("retrieval_review_60_example", _PATH)
assert _spec is not None and _spec.loader is not None
retrieval_review = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = retrieval_review
_spec.loader.exec_module(retrieval_review)


def _model() -> Model:
    return Model(
        id="local-llm",
        name="local-llm",
        api="openai-completions",
        provider="openai",
        base_url="http://x/v1",
        context_window=8192,
        max_tokens=300,
        grammar_dialect="llguidance",
    )


# Candidate documents, shaped as JMFTS search hits. Since W15 the demo RETRIEVES its
# candidates instead of reading a literal list, so retrieval is now network — and is
# faked here exactly like `complete_simple` is, for the same reason: these tests are
# about the constrained fan-out, not about JMFTS. Retrieval itself is covered live in
# tau-jmfts/tests/test_enrich_jmfts.py and test_tools_jmfts.py.
HITS = [
    (1, "Guide: rotating TLS certs on the edge gateway, incl. cert-manager and ACME."),
    (2, "Recipe: sourdough starter maintenance in cold climates."),
    (3, "Reference: api-gateway.yaml — tls.certSecretRef, tls.minVersion fields."),
    (4, "Postmortem: 2024 outage caused by an expired gateway certificate."),
    (5, "Blog: why we migrated our CI from Jenkins to GitHub Actions."),
    (6, "Tutorial: training a small language model on a single GPU."),
]


@pytest.fixture
def session_and_calls(monkeypatch):
    """A real AgentSession; only the two network hops (retrieval, completion) are faked."""
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), api_key="k")
    calls: list[dict[str, Any]] = []

    # Patch the CLASS, not this module's `_retrieve`: load_extensions imports the example
    # afresh by path, so it gets a different module object than the one imported at the
    # top of this file — patching that one would silently miss. `JmftsClient` is resolved
    # from sys.modules by both, so it is the seam they actually share.
    from tau_jmfts.client import JmftsClient

    def fake_search(self, query, **kwargs):
        return [{"document": {"id": doc_id, "content": text}} for doc_id, text in HITS]

    monkeypatch.setattr(JmftsClient, "search", fake_search)

    async def fake_complete_simple(model, context, options=None):
        opts = options or {}
        calls.append({"messages": context["messages"], "constraints": opts.get("constraints")})
        # Answer "include" for anything cert/TLS/gateway-ish, else "exclude" — enough
        # to make the verdict split meaningful without pretending to be a judge.
        doc = context["messages"][-1]["content"].lower()
        verdict = "include" if ("cert" in doc or "tls" in doc) else "exclude"
        return AssistantMessage(
            content=[TextContent(text=verdict)],
            api="openai-completions",
            provider="openai",
            model="local-llm",
            stop_reason="stop",
            timestamp=0,
        )

    monkeypatch.setattr("tau_ai.client.complete_simple", fake_complete_simple)
    return session, calls


async def _dispatch(session: AgentSession, extension_path: Path, command: str, args: str):
    """Load the extension the way τ does, then run the command the way τ does.

    The `url` is what lets a non-JMFTS-backed session retrieve at all: without it the
    demo refuses to run rather than falling back to a built-in corpus (which is exactly
    what it used to do, and why its retrieval half was fiction).
    """
    await session.load_extensions(
        [str(extension_path)],
        extensions_config={extension_path.stem: {"url": "http://jmfts.test", "scope": "all"}},
    )
    return await session.run_extension_command(command, args)


class TestTheDemoActuallyRuns:
    async def test_review_dispatches_through_the_real_command_registry(
        self, session_and_calls
    ):
        """The regression that shipped: /review AttributeError'd on dispatch."""
        session, calls = session_and_calls

        result = await _dispatch(session, _PATH, "review", "expired TLS cert on the gateway")

        assert len(calls) == len(HITS), "one completion per retrieved hit"
        assert result and "included" in result.output

    async def test_every_completion_carries_the_verdict_constraint(self, session_and_calls):
        """G3: the constraint is on every call, not just the first."""
        session, calls = session_and_calls

        await _dispatch(session, _PATH, "review", "tls")

        for call in calls:
            constraints = call["constraints"]
            assert isinstance(constraints, DecodeConstraints)
            assert constraints.choices == retrieval_review.VERDICTS

    async def test_the_fan_out_writes_nothing_to_the_tree(self, session_and_calls):
        """C1 is stateless — that is what makes N-way asyncio.gather safe."""
        session, _ = session_and_calls
        log = session._session_log
        before = len(log.entries())

        await _dispatch(session, _PATH, "review", "tls")

        assert len(log.entries()) == before

    async def test_a_dropped_grammar_surfaces_instead_of_being_recorded(
        self, session_and_calls, monkeypatch
    ):
        """The failure this demo exists to make visible: the server dropped the grammar
        and returned free prose. It must reach the user as an ERROR, not land in the
        panel as if it were a verdict.

        Verification itself lives in the provider (openai.py, the single choke point
        covering streaming and complete_simple alike), so faking ``complete_simple``
        skips it — this fake therefore raises exactly what the provider raises. What is
        under test here is the EXTENSION's handling of it.
        """
        session, _ = session_and_calls
        prose = "Sure! I'd say this one looks relevant."

        async def grammar_died(model, context, options=None):
            raise ConstraintViolation(
                f"constrained output {prose!r} is not one of ['include', 'exclude']", prose
            )

        monkeypatch.setattr("tau_ai.client.complete_simple", grammar_died)

        notices: list[tuple[str, str]] = []

        class _Delegate:
            def notify(self, message, level="info", **kw):
                notices.append((level, message))

            def __getattr__(self, _name):  # set_status / panel / … are no-ops here
                return lambda *a, **k: None

        session.set_ui_delegate(_Delegate())

        result = await _dispatch(session, _PATH, "review", "tls")

        assert any(
            level == "error" and "constraint violated" in msg for level, msg in notices
        ), f"the violation must reach the user; got {notices!r}"
        assert not (result and result.output), "no verdict table on a violated constraint"


class TestVerificationIsNotDecoration:
    def test_the_verdict_constraint_rejects_out_of_set_output(self):
        """Directly: this is what stands between free prose and a recorded verdict."""
        constraints = DecodeConstraints(choices=retrieval_review.VERDICTS)

        constraints.verify_output("include")
        with pytest.raises(ConstraintViolation):
            constraints.verify_output("Sure! I'd say this one looks relevant.")
