"""Live conformance checks against a llama.cpp server (marker ``llama``, opt-in).

Deselected by default (``addopts = ["-m", "not llama"]`` in the root
``pyproject.toml``). Run with::

    pytest -m llama                       # the LAN default, or $TAU_LLAMA_TEST_URL
    TAU_LLAMA_TEST_URL=http://h:8080 pytest -m llama

Re-run this ONLY for changes that touch the LLM provider interfaces — the
``tau_ai.providers`` modules, the ``Model`` fields that reach the request body
(``reasoning``, ``thinking_level_map``, ``extra_body``, ``grammar_dialect``,
``max_tokens``), or the ``agent_loop`` → ``stream_simple`` options seam. A TUI,
session, RPC, extension or docs change cannot alter what an endpoint honors, and
the default ``pytest`` run is the gate for those.

**These assertions encode MEASUREMENTS, not desired behavior.** Each one records
what ``http://192.168.1.100:8080`` (Qwen3-35B IQ4_XS) did on 2026-08-08; the full
report is ``docs/probe-results/llama-2026-08-08.json``. A failure here is
therefore ambiguous by design, and the ambiguity is the point: it means the
endpoint changed, and the change may be llama.cpp being FIXED. Check the server
build before changing τ. Each assertion below names what its own failure would
mean.

Skip/fail classification follows ``tau-jmfts/tests/conftest.py``: a transport
failure skips (the box is not promised available, and being disconnected is not a
test failure), while a server that connects and answers wrong fails loudly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.llama

_PROBE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "llama_conformance_probe.py"


def _load_probe() -> ModuleType:
    """Import the standalone probe by path.

    ``scripts/`` is deliberately not a package: the probe must stay runnable by
    someone holding the endpoint who does not have τ installed (that is why it
    imports no τ modules). Loading it by path here keeps the ONE implementation
    shared between the script and this test, rather than restating the request
    bodies in a second place where they could drift.
    """
    spec = importlib.util.spec_from_file_location("llama_conformance_probe", _PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def report() -> dict:
    """Run the full probe once for this module (~15 s of generation)."""
    probe_module = _load_probe()
    try:
        return probe_module.probe()
    except probe_module.Unreachable as exc:
        pytest.skip(f"llama.cpp endpoint unreachable ({exc}); set TAU_LLAMA_TEST_URL to override")


def test_the_endpoint_is_deterministic_so_every_other_verdict_is_sound(report):
    """Two identical greedy+seeded requests must produce identical output.

    Asserted FIRST and separately because every "ignored" verdict below is an
    argument from two matching generations. Against a sampling server that
    argument proves nothing, so a failure here invalidates this whole module
    rather than being one finding among several.
    """
    assert report["deterministic"], (
        "temperature=0 + seed did not reproduce; the knob verdicts in this module "
        "compare outputs and are unsound until this is fixed"
    )


def test_reasoning_effort_is_still_ignored(report):
    """τ's only wire representation for a thinking level is a no-op here.

    `high`, `low` and the invalid `banana` all return the baseline generation —
    the endpoint does not even validate the field, which is the evidence it never
    reads it.

    If this FAILS: the endpoint started honoring `reasoning_effort`. That is good
    news and it retires the reason τ needs any per-model thinking target at all
    for this server. Do not "fix" the test; check the llama.cpp build first.
    """
    assert report["verdicts"]["reasoning_effort"] == "ignored"


def test_thinking_budget_tokens_is_honored(report):
    """The representation this endpoint DOES implement.

    Reasoning length tracks the budget tightly and monotonically (1/64/256 →
    15/77/270 reasoning tokens against a 502-token baseline). This is the
    measurement that justifies letting a model entry target a field other than
    `reasoning_effort`.

    If this FAILS: the endpoint stopped honoring it, and any τ config mapping
    thinking levels onto this field is silently doing nothing.
    """
    assert report["verdicts"]["thinking_budget_tokens"] == "honored"


def test_thinking_budget_zero_is_a_floor_not_an_inversion(report):
    """`thinking_budget_tokens: 0` means "immediate end", as documented.

    Measured on this build: budget 0 → 14 reasoning tokens, BELOW budget 1's 15
    and far below the 502-token baseline. Confirmed on a hard prompt too, where
    the baseline reasons 2062 tokens and budget 0 still reasons 14.

    **This asserts a property of the running server, not of the build.** The same
    machine produced 10187 reasoning tokens in 60.4 s for budget 0, then 14 tokens
    in 7.0 s after a restart whose only change was the `--reasoning-budget-message`
    launch flag. No /props field, generation param, or endpoint reports that flag,
    so neither this test nor the probe can tell which launch it is talking to.

    That invisibility is the finding, and it is what τ has to act on: a client
    cannot ask which behavior it is about to get, and the two behaviors differ by
    three orders of magnitude in cost. `1` means the same thing under both launches
    (15 reasoning tokens, versus 14 for `0`), so τ mapping "off" onto 1 rather than
    0 costs one token and removes the dependence on state it cannot observe.

    If this FAILS with `inverted-unbounded`: the server in front of you was
    launched without that flag. That is not a regression to fix in τ — it is the
    other half of a coin τ is already guarding against, and the guard is the reason
    the failure is survivable.
    """
    assert report["verdicts"]["thinking_budget_zero"] == "floor"


def test_a_malformed_thinking_budget_is_silently_ignored(report):
    """Every wrong spelling of the budget degrades to unbounded, with HTTP 200.

    `"0"` as a JSON string, `null`, `-1`, and a misspelled key all return the
    baseline generation — identical token counts and timing to not sending the
    parameter at all. Only a JSON number is read.

    This is the reproduction of a reported "budget 0 is unbounded" that does not
    reproduce with an integer 0 (see the test above). It is also the more
    dangerous property: a guard against the literal value 0 never fires, because
    a client tripping this is sending `"0"`, not `0`.

    Asserted so the hazard stays measured rather than remembered. If this FAILS
    with `refused`, the server started validating the field, and a τ-side guard
    on the encoding is no longer the only thing standing between a stringified
    config value and an unbounded turn.
    """
    assert report["verdicts"]["thinking_budget_malformed"] == "silently-ignored"
    assert "string_zero" in report["measurements"]["thinking_budget_encodings_ignored"]


def test_chat_template_enable_thinking_is_honored(report):
    """`chat_template_kwargs: {"enable_thinking": false}` suppresses thinking entirely.

    0 reasoning tokens, versus 14 for the cheapest budget and 502 for the
    baseline — the only knob measured here that reaches a true zero.

    This is the knob PI_RPC_REPLACEMENT.md §3.3 has listed as unreachable from τ
    since the first review. It works, it is nested, and no flat "name the field"
    config shape can express it.
    """
    assert report["verdicts"]["chat_template_enable_thinking"] == "honored"
