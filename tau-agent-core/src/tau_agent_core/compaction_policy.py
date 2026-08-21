"""Declared compaction policy for a measured run (SIM_SPEC_v2 §16.8 / H5).

**This module changes nothing about how τ compacts.** Auto-compaction stays on by
default (``compaction.py:76``), still fires at the tail of every ``prompt()``,
still summarises through the session's own model, and a failed summarisation still
raises :class:`~tau_agent_core.compaction.CompactionError` rather than degrading.
§16.8 is explicit that "this is not a τ defect" and that the raise-rather-than-
degrade choice is right. Nothing here makes compaction quieter or more forgiving;
everything here makes an *undeclared* compaction impossible to run a measurement
over by accident.

Why the harness needs it at all
-------------------------------

Two facts about the shipped behaviour, both deliberate, combine badly with a
measurement run:

1. Compaction is a **model call**. Under SIM_SPEC_v2 §11.1 a partition removes the
   bus, JMFTS *and* the LLM in one stroke, so an agent that crosses its context
   threshold while disconnected raises ``CompactionError`` and scenarios D and E
   die of harness physics while reporting it as the thing under test.
2. It is "routinely the most expensive single call in a session, and it fires
   automatically" (``compaction.py:108-113``), it goes through ``complete_simple``
   which emits no events, and it lands at the *tail of a prompt*. So one prompt in
   N carries a full-window summarisation, and §5.2's headline number — assembled
   turn latency, the primary output of the whole program — becomes a **bimodal
   population reported as one distribution**.

The remedy for both is the same and it is a declaration, not a code change: every
run states which of three admissible policies it is running under, the statement is
checked against the model it is bound to, and the statement is recorded in
``manifest.json`` (see :mod:`tau_agent_core.run_manifest`). "Leave the default" is
not one of the three — a default that nobody wrote down is not a partition key, and
a configuration that changes what a number means is a mandatory partition key
(the same argument as ``Trace.arm`` and as the link's ``spool_capacity``).

The three admissible policies
-----------------------------

``disabled``
    Compaction off, with a declared bound on user turns. Cheapest, and §16.8 calls
    it "the cleanest for a scripted scenario". Its weakness is that the bound is on
    turn *count* only: a short run whose tool results are large still overruns the
    model window, and the first thing that notices is the provider, mid-request,
    with an error that does not name the cause.

``local_summarizer``
    Compaction on, summarising through a **separate model** that is declared
    explicitly. Survives a partition only if that model actually runs on the near
    side of it. Note this is the only one of the three under which a compaction may
    still fire, so a run declaring it must partition its latency population
    (:mod:`tau_agent_core.latency`) rather than merely observe that the population
    is unimodal.

``turn_cap``
    Compaction **left exactly as shipped**, plus a declared budget — at most
    ``max_turns`` user turns, each adding at most ``max_tokens_per_turn`` — whose
    arithmetic is checked against the bound model's real context window at
    construction, and whose premise is checked at runtime at the one site
    auto-compaction fires. "Provably cannot reach the threshold" is then a proof
    rather than an estimate:

        P1. after *k* user turns, ``context_tokens <= k * max_tokens_per_turn``
            — enforced by :meth:`CompactionPolicy.observe_context`, which raises;
        P2. ``k <= max_turns``
            — enforced by :meth:`CompactionPolicy.admit_turn`, which raises;
        C.  therefore ``context_tokens <= max_turns * max_tokens_per_turn``
            ``<= context_window - reserve_tokens``
            — the last inequality checked by :meth:`CompactionPolicy.bind_to`,
            which refuses to construct a session whose budget does not close.

    So ``should_compact`` is False at every turn, by construction. If either
    premise fails the run is void and says so, loudly, at a named site with the
    numbers — instead of producing a latency figure with an un-priced full-window
    model call hidden in its tail.

Reference: SIM_SPEC_v2.md §16.8, §16.11 (H5), §5.2, §9 rule 1, §11.1, §7.6, §7.7.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from tau_llm.types import Model

from tau_agent_core.compaction import DEFAULT_COMPACTION_SETTINGS, CompactionSettings

#: The three admissible policies of §16.8. There is deliberately no fourth value
#: meaning "whatever the harness defaults to" — that is the option §16.8 removes.
PolicyMode = Literal["disabled", "local_summarizer", "turn_cap"]


class CompactionPolicyError(Exception):
    """A compaction policy declaration is inadmissible.

    Raised at declaration time or at bind time — never mid-run. Either the fields
    do not match the declared mode, or the budget does not close against the model
    the policy was bound to.
    """


class CompactionPolicyViolation(CompactionPolicyError):
    """A bound run exceeded the budget its policy declared.

    The run is void: a measurement taken past its own declared bound describes a
    configuration nobody wrote down. Raised at the site the bound was crossed, with
    the numbers, so the failure is legible rather than a plausible value.

    This is **not** a compaction failure and is deliberately not a
    :class:`~tau_agent_core.compaction.CompactionError` — compaction did not fail,
    the run's premise did.
    """


@dataclass(frozen=True)
class CompactionPolicy:
    """One run's declared answer to "what happens when the context fills up?".

    Construct through :meth:`disabled`, :meth:`local_summarizer` or
    :meth:`turn_cap` — the constructors are what make the mode/field pairing
    checkable. There is no default instance and no default mode.
    """

    mode: PolicyMode
    #: Bound on user turns (``prompt()`` calls). Required by ``disabled`` and
    #: ``turn_cap``; must be absent for ``local_summarizer``.
    max_turns: int | None = None
    #: Bound on the context each user turn may add. Required by ``turn_cap`` only.
    max_tokens_per_turn: int | None = None
    #: The summariser. Required by ``local_summarizer`` only.
    summarizer_model: Model | None = None
    #: The summariser's credential. Required (never inferred) by
    #: ``local_summarizer``; never serialised into a manifest.
    summarizer_api_key: str | None = None
    #: The thresholds this policy runs under. The reserve is what
    #: ``should_compact`` measures against, so it is part of the declaration and
    #: part of the arithmetic ``turn_cap`` proves.
    reserve_tokens: int = DEFAULT_COMPACTION_SETTINGS.reserve_tokens
    keep_recent_tokens: int = DEFAULT_COMPACTION_SETTINGS.keep_recent_tokens

    # ── declaration ───────────────────────────────────────────────────────

    def __post_init__(self) -> None:
        required: dict[PolicyMode, tuple[str, ...]] = {
            "disabled": ("max_turns",),
            "local_summarizer": ("summarizer_model", "summarizer_api_key"),
            "turn_cap": ("max_turns", "max_tokens_per_turn"),
        }
        if self.mode not in required:
            raise CompactionPolicyError(
                f"unknown compaction policy mode {self.mode!r}; "
                f"§16.8 admits exactly {sorted(required)}"
            )
        every = ("max_turns", "max_tokens_per_turn", "summarizer_model", "summarizer_api_key")
        for field_name in every:
            value = getattr(self, field_name)
            if field_name in required[self.mode]:
                if value is None:
                    raise CompactionPolicyError(
                        f"compaction policy mode {self.mode!r} requires {field_name}; "
                        "a policy with an unfilled parameter is not a declaration"
                    )
            elif value is not None:
                raise CompactionPolicyError(
                    f"compaction policy mode {self.mode!r} does not use {field_name}, "
                    f"but it was set to {value!r}; a field that is read nowhere "
                    "advertises a capability the mode does not have"
                )
        for positive in ("max_turns", "max_tokens_per_turn", "reserve_tokens"):
            value = getattr(self, positive)
            if value is not None and value <= 0:
                raise CompactionPolicyError(f"{positive} must be positive, got {value!r}")

    @classmethod
    def disabled(cls, *, max_turns: int) -> CompactionPolicy:
        """Compaction off, bounded by user-turn count (§16.8 option 1).

        ``max_turns`` is not decoration: with compaction off, nothing else stops
        the conversation growing past the model window, and the bound is the whole
        of what makes this policy safe. Exceeding it raises rather than letting the
        run continue into a provider-side overflow whose cause is not recoverable
        from the artifact.
        """
        return cls(mode="disabled", max_turns=max_turns)

    @classmethod
    def local_summarizer(cls, *, model: Model, api_key: str) -> CompactionPolicy:
        """Compaction on, through a separately declared model (§16.8 option 2).

        ``api_key`` is required rather than inherited from the session: a
        summariser on the near side of a partition is a different endpoint with
        different credentials, and silently reusing the session's key is the kind
        of guess that works in the lab and fails on the board. Pass the literal the
        local server expects (many ignore it) — but pass it.

        The key is never written to a manifest; see :meth:`to_manifest`.
        """
        return cls(mode="local_summarizer", summarizer_model=model, summarizer_api_key=api_key)

    @classmethod
    def turn_cap(cls, *, max_turns: int, max_tokens_per_turn: int) -> CompactionPolicy:
        """Compaction as shipped, under a budget proven to keep it out of reach.

        See the module docstring for the proof and for which half of it is checked
        where. Both numbers are the caller's to supply: this module will not invent
        a per-turn token bound, because an invented bound turns the proof back into
        the estimate §16.8 refuses.
        """
        return cls(mode="turn_cap", max_turns=max_turns, max_tokens_per_turn=max_tokens_per_turn)

    # ── what the session runs under ───────────────────────────────────────

    @property
    def compaction_settings(self) -> CompactionSettings:
        """The :class:`CompactionSettings` this policy puts in force.

        ``turn_cap`` leaves compaction **enabled**: the point of that policy is
        that the measured system stays the shipped system, and the threshold is
        kept out of reach by the budget rather than by switching the mechanism off.
        """
        return CompactionSettings(
            enabled=self.mode != "disabled",
            reserve_tokens=self.reserve_tokens,
            keep_recent_tokens=self.keep_recent_tokens,
        )

    def summarizer_for(self, session_model: Model) -> tuple[Model, str | None]:
        """The (model, api_key) a compaction under this policy summarises through.

        For every mode but ``local_summarizer`` that is the session's own model and
        the session's own key — which is exactly the shipped behaviour, restated
        rather than changed. Returning ``None`` for the key means "whatever the
        session was constructed with"; the session substitutes its own.
        """
        if self.mode == "local_summarizer":
            assert self.summarizer_model is not None  # guaranteed by __post_init__
            return self.summarizer_model, self.summarizer_api_key
        return session_model, None

    # ── binding, and the arithmetic half of the proof ─────────────────────

    def bind_to(self, model: Model) -> None:
        """Check this declaration against the model it will run under; raise if not.

        Called once, at session construction. Everything checkable before a token is
        spent is checked here rather than discovered at turn 40 of a scenario run.
        """
        context_window = model.context_window
        if context_window <= self.reserve_tokens:
            raise CompactionPolicyError(
                f"model {model.id!r} has context_window={context_window}, which is not "
                f"greater than reserve_tokens={self.reserve_tokens}; the compaction "
                "threshold is meaningless on this model and no policy can be proven "
                "against it"
            )

        if self.mode == "local_summarizer":
            assert self.summarizer_model is not None
            if self.summarizer_model.context_window < context_window:
                raise CompactionPolicyError(
                    f"summarizer {self.summarizer_model.id!r} has context_window="
                    f"{self.summarizer_model.context_window}, smaller than session model "
                    f"{model.id!r}'s {context_window}. Compaction summarises a full "
                    "session window, so this summariser cannot ingest what it would be "
                    "asked to summarise — the call fails at the provider, mid-partition, "
                    "as a CompactionError that looks like a scenario result"
                )
            return

        if self.mode == "turn_cap":
            assert self.max_turns is not None and self.max_tokens_per_turn is not None
            budget = context_window - self.reserve_tokens
            worst_case = self.max_turns * self.max_tokens_per_turn
            if worst_case > budget:
                raise CompactionPolicyError(
                    f"turn_cap budget does not close: {self.max_turns} turns x "
                    f"{self.max_tokens_per_turn} tokens = {worst_case} > "
                    f"{budget} (context_window {context_window} - reserve "
                    f"{self.reserve_tokens}). This policy claims compaction cannot be "
                    "reached and the arithmetic says it can"
                )

    # ── the runtime half of the proof ─────────────────────────────────────

    def admit_turn(self, turns_used: int) -> None:
        """Admit the ``turns_used``-th user turn, or raise (premise P2).

        ``turns_used`` is 1-based and counts ``prompt()`` calls, which is the unit a
        scripted scenario is written in.
        """
        if self.max_turns is not None and turns_used > self.max_turns:
            raise CompactionPolicyViolation(
                f"compaction policy {self.mode!r} declared max_turns={self.max_turns}; "
                f"this is user turn {turns_used}. The run is past its declared bound and "
                "any number taken from here describes a configuration that was never "
                "written down"
            )

    def observe_context(self, *, turns_used: int, context_tokens: int, context_window: int) -> None:
        """Check the run against its declared budget at the auto-compaction site.

        Called at the one place ``should_compact`` is evaluated, **before** the
        enabled/threshold gates, so a ``turn_cap`` run that has broken its premise
        dies here rather than making the full-window model call the policy exists to
        keep out of the measurement.

        Modes other than ``turn_cap`` declare no per-turn token bound, so there is
        nothing here for them to violate. That asymmetry is not an oversight: it is
        the reason ``turn_cap`` is the stronger declaration.
        """
        if self.mode != "turn_cap":
            return
        assert self.max_tokens_per_turn is not None
        allowed = turns_used * self.max_tokens_per_turn
        if context_tokens > allowed:
            raise CompactionPolicyViolation(
                f"turn_cap premise failed: after {turns_used} user turn(s) the context is "
                f"{context_tokens} tokens, above the declared bound of {turns_used} x "
                f"{self.max_tokens_per_turn} = {allowed}. The proof that compaction "
                "cannot be reached rested on this bound, so it no longer holds"
            )
        threshold = context_window - self.reserve_tokens
        if context_tokens > threshold:
            raise CompactionPolicyViolation(
                f"turn_cap claim failed: context is {context_tokens} tokens, above the "
                f"compaction threshold of {threshold} (context_window {context_window} - "
                f"reserve {self.reserve_tokens}). This policy asserted the threshold was "
                "unreachable; it was reached"
            )

    # ── the record ────────────────────────────────────────────────────────

    def to_manifest(self) -> dict[str, Any]:
        """The manifest fragment. JSON-serialisable, and it never carries a secret.

        Everything a later reader needs to know what population a number came from,
        and nothing that would make the artifact unsafe to keep.
        """
        declared: dict[str, Any] = {}
        if self.max_turns is not None:
            declared["max_turns"] = self.max_turns
        if self.max_tokens_per_turn is not None:
            declared["max_tokens_per_turn"] = self.max_tokens_per_turn
        if self.summarizer_model is not None:
            # Identity and endpoint only. The api key is deliberately absent — a
            # manifest is an artifact that gets copied, attached and shared.
            declared["summarizer_model"] = {
                "id": self.summarizer_model.id,
                "provider": self.summarizer_model.provider,
                "base_url": self.summarizer_model.base_url,
                "context_window": self.summarizer_model.context_window,
            }
        return {
            "mode": self.mode,
            "declared": declared,
            "compaction_enabled": self.compaction_settings.enabled,
            "reserve_tokens": self.reserve_tokens,
            "keep_recent_tokens": self.keep_recent_tokens,
        }


# ── the per-scenario declaration (§16.8 deliverable 1) ────────────────────
#
# The decision §16.11 says must be taken "before phase 3, not after" is *which
# mode* each scenario runs under. It is recorded here, in code, where a run has to
# go through it — not in prose where a run can skip it.
#
# All five scenarios declare `turn_cap`, and the argument is one argument:
#
#   * It is the only one of the three that leaves τ's compaction mechanism exactly
#     as shipped. §16.8 says outright that the automatic firing and the
#     raise-rather-than-degrade choice are deliberate and correct; `disabled` turns
#     that mechanism off, so the measured harness is no longer the shipped harness.
#     `turn_cap` changes the *run*, not the system under test.
#   * It is the only one of the three whose safety claim is checked rather than
#     assumed. `disabled` bounds turn COUNT and says nothing about turn SIZE, so a
#     six-turn run with three large tool results overruns the window and the first
#     thing to notice is the provider, mid-request. `turn_cap` bounds both, checks
#     the arithmetic at construction and the premise at runtime, and names the
#     numbers when it fails.
#   * It makes §5.2's headline population unimodal *by construction* rather than
#     merely partitioned after the fact. Tagging (see `tau_agent_core.latency`)
#     rescues a contaminated population; a proven cap means there is nothing to
#     rescue, and the tagging becomes the detector that says so — a measured zero
#     instead of an assumed one.
#   * `local_summarizer` is implemented and admissible, and is declared for none of
#     the five, because for D and E it would be a fabricated mechanism: §11.1's
#     backpack runs `praxis/backpack_audio.yaml` — ASR, accumulator, resolver, echo
#     gate, speech — and no LLM, and §7.7's precondition is that the preprocessor
#     MoE is *absent* because it needs over half of midlife's GPU. Declaring a
#     board-local summariser today would be declaring a model that does not exist.
#     For A/B/C it would run, but it is the one policy under which compaction can
#     still fire, so it reintroduces the bimodality into the primary output and adds
#     a second model as an uncontrolled variable.
#
# This disagrees with §16.8's parenthetical preference for `disabled` ("the
# cleanest for a scripted scenario"). The disagreement is narrow and deliberate:
# §16.8 lists all three as admissible and expresses a preference, not a
# requirement, and the preference was stated before anyone had to write down what
# "bounded turns" bounds. Recorded here rather than acted on silently.
#
# The NUMBERS are not here, and that is also the decision. `max_turns` and
# `max_tokens_per_turn` are per-run inputs the scenario harness must supply, because
# this module has no basis on which to choose them: §7's scenario specifications
# state preconditions, assertions and yields, and state no turn budget. Writing a
# plausible pair here would be exactly the estimate wearing a proof's clothes that
# §16.8's "provably means a test, not an estimate" refuses. What IS decided is that
# a run cannot proceed without them.
SCENARIO_POLICY_MODES: dict[str, PolicyMode] = {
    "A": "turn_cap",  # §7.3 verbal, connected, from idle
    "B": "turn_cap",  # §7.4 τ typed, connected
    "C": "turn_cap",  # §7.5 τ API from a timer
    "D": "turn_cap",  # §7.6 partition mid-task, complete offline, reconnect
    "E": "turn_cap",  # §7.7 verbal instruction, mid-task, disconnected
}


def policy_for_scenario(
    scenario: str, *, max_turns: int, max_tokens_per_turn: int
) -> CompactionPolicy:
    """Build the declared policy for a lettered scenario (§7.3-§7.7).

    Raises on an unknown scenario rather than falling back to anything: an
    unrecognised scenario letter is a harness bug, and the failure mode a default
    would produce is a run measured under a policy nobody chose.

    Args:
        scenario: the scenario letter, ``"A"``-``"E"``.
        max_turns: the run's declared bound on user turns.
        max_tokens_per_turn: the run's declared bound on per-turn context growth.

    Raises:
        CompactionPolicyError: unknown scenario, or the scenario's declared mode is
            not one this builder can construct from these arguments.
    """
    key = scenario.strip().upper()
    if key not in SCENARIO_POLICY_MODES:
        raise CompactionPolicyError(
            f"no compaction policy is declared for scenario {scenario!r}; "
            f"declared scenarios are {sorted(SCENARIO_POLICY_MODES)}. "
            "'leave the default' is not an option (§16.8)"
        )
    mode = SCENARIO_POLICY_MODES[key]
    if mode != "turn_cap":
        # Reached only if SCENARIO_POLICY_MODES is edited without editing this
        # builder. Raising keeps the table and the constructor from drifting apart
        # silently, which would hand back a policy of the wrong mode.
        raise CompactionPolicyError(
            f"scenario {key} declares mode {mode!r}, which this builder cannot construct "
            f"from (max_turns, max_tokens_per_turn); use CompactionPolicy.{mode}(...)"
        )
    return CompactionPolicy.turn_cap(max_turns=max_turns, max_tokens_per_turn=max_tokens_per_turn)
