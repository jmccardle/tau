"""W2/G0 — Model decode-capability fields reach (or correctly stay off) the wire.

``Model.extra_body`` is the static half of the request-body passthrough: llama-server
knobs (``cache_prompt``, ``min_p``, samplers) that were previously unreachable without
editing τ source. ``grammar_dialect`` is the capability GATE consumed in W3/G1 — here
we only assert it is carried and defaults to None (opt in, don't guess).

Reuses the payload-capture harness from test_reasoning_effort.py (``_CapturingClient``)
rather than building a second one.

Reference: docs/CONSTRAINED-GEN-AND-BRANCHING-PLAN.md §2 (capability model).
"""

from __future__ import annotations

import pytest

from tau_ai.types import Model

from test_reasoning_effort import _model, _patch_client, _run


class TestDefaults:
    def test_grammar_dialect_defaults_to_none(self):
        """Opt in, don't guess: an endpoint declares grammar support or has none."""
        assert Model(
            id="m",
            name="m",
            api="openai-completions",
            provider="openai",
            base_url="u",
            context_window=1,
            max_tokens=1,
        ).grammar_dialect is None

    def test_extra_body_and_features_default_empty(self):
        m = _model()
        assert m.extra_body == {}
        assert m.server_features == []

    def test_extra_body_is_not_a_shared_mutable_default(self):
        a, b = _model(), _model()
        a.extra_body["cache_prompt"] = True
        assert b.extra_body == {}


class TestExtraBodyOnTheWire:
    def test_extra_body_lands_in_the_payload(self, monkeypatch):
        _patch_client(monkeypatch)
        payload = _run(_model(extra_body={"cache_prompt": True, "min_p": 0.05}), None)

        assert payload["cache_prompt"] is True
        assert payload["min_p"] == 0.05

    def test_absent_extra_body_adds_nothing(self, monkeypatch):
        _patch_client(monkeypatch)
        payload = _run(_model(), None)

        assert "cache_prompt" not in payload
        # ``max_tokens`` is τ's own baseline field, not something extra_body added:
        # every Model declares one and the provider now sends it (see
        # tests/test_max_tokens.py — it used to be declared and never consulted).
        assert set(payload) == {
            "model",
            "messages",
            "stream",
            "stream_options",
            "max_tokens",
        }

    def test_per_call_option_beats_model_extra_body(self, monkeypatch):
        """Precedence: per-call options > Model.extra_body > τ defaults."""
        _patch_client(monkeypatch)
        payload = _run(
            _model(extra_body={"temperature": 0.1, "cache_prompt": True}),
            {"temperature": 0.9},
        )

        assert payload["temperature"] == 0.9  # per-call wins
        assert payload["cache_prompt"] is True  # non-conflicting static param survives

    @pytest.mark.parametrize(
        "reserved",
        [
            {"stream_options": {"include_usage": False}},  # would zero out token accounting
            {"messages": [{"role": "user", "content": "injected"}]},  # a hidden context channel
            {"stream": False},
            {"model": "some-other-model"},
        ],
    )
    def test_extra_body_may_not_clobber_the_transport_contract(self, monkeypatch, reserved):
        """Fail-Early: static config must not silently corrupt what τ sends.

        extra_body is for the server's decode/cache knobs. Letting it rewrite
        `messages` would make config a hidden context channel (violating
        tree-as-truth), and flipping `include_usage` off would silently zero token
        accounting for every call on that model.
        """
        _patch_client(monkeypatch)
        with pytest.raises(ValueError, match="may not set τ transport fields"):
            _run(_model(extra_body=reserved), None)
