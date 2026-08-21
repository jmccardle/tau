"""Tests for the C0/C1 induction-loop runner (experiments/m3/induction.py).

Reference: docs/M3-DESIGN.md §4.4 (curriculum vs. measuring stick), §6 (C0/C1 conditions +
store isolation), §7/§8 (the material-margin metric). Everything runs under a FAKE LLM (a
greedy ``EngineAgent`` stand-in) against the in-memory ``memory_store`` backing — no GPU,
no server, fully deterministic. Sibling imports mirror the other experiments/m3 tests
(pytest puts the directory on sys.path).
"""

from __future__ import annotations

import json

import pytest

from driver import Agent, EngineAgent, GameRecord
from induction import (
    RunConfig,
    _DryRunDistiller,
    greedy_agent_factory,
    main,
    margin_trajectory,
    material_margin,
    measurement_suite,
    run,
)
from los_alamos import BLACK, WHITE, Board
from memory_store import InMemoryJmftsClient

# --- fakes ------------------------------------------------------------------------


class _SpyDistiller:
    """Counts distill calls; returns one canned lesson per game."""

    def __init__(self) -> None:
        self.calls = 0

    def distill(self, game_record: GameRecord, swung: list, board_start: Board) -> list[str]:
        self.calls += 1
        return ["lesson"]


class _RecordingFactory:
    """A greedy agent factory that records every strategy_context it is handed."""

    def __init__(self) -> None:
        self.contexts: list[str | None] = []

    def __call__(self, *, strategy_context: str | None, temperature: float) -> Agent:
        self.contexts.append(strategy_context)
        return EngineAgent(1)


# --- schedule firing --------------------------------------------------------------


def test_schedule_firing_distill_and_consolidate_counts() -> None:
    # k_train=6, log_every_games=2 → all 6 games distilled (one call each, threshold 0 so
    # every game has White swung moves). One lesson each → 6 logs. consolidate_every_logs=3
    # → consolidate fires at log 3 and log 6 → 2 consolidations.
    config = RunConfig(
        condition="C1",
        k_train=6,
        measure_suite_size=1,
        log_every_games=2,
        consolidate_every_logs=3,
        swing_threshold=0.0,
    )
    spy = _SpyDistiller()
    result = run(
        config,
        llm_endpoint="unused",
        distiller_endpoint="unused",
        agent_factory=greedy_agent_factory(),
        distiller=spy,
    )
    assert spy.calls == 6
    assert result.training["games"] == 6
    assert result.training["lessons_logged"] == 6
    assert result.training["consolidations"] == 2


# --- C0 vs C1 store + injection ---------------------------------------------------


def test_c0_writes_nothing_and_injects_none() -> None:
    client = InMemoryJmftsClient()
    factory = _RecordingFactory()
    config = RunConfig(condition="C0", k_train=0, measure_suite_size=3)
    result = run(
        config,
        llm_endpoint="unused",
        distiller_endpoint="unused",
        store_client=client,
        agent_factory=factory,
        distiller=_SpyDistiller(),
    )
    # C0 never touches the store, and injects None at every measurement game.
    assert client._docs == {}
    assert factory.contexts == [None, None, None]
    assert result.training == {"games": 0, "lessons_logged": 0, "consolidations": 0}


def test_c1_grows_store_and_injects_assembled_doc() -> None:
    client = InMemoryJmftsClient()
    factory = _RecordingFactory()
    config = RunConfig(
        condition="C1",
        k_train=2,
        measure_suite_size=2,
        log_every_games=1,
        consolidate_every_logs=100,  # no consolidation — lessons stay in the footer.
        swing_threshold=0.0,
    )
    run(
        config,
        llm_endpoint="unused",
        distiller_endpoint="unused",
        store_client=client,
        agent_factory=factory,
        distiller=_SpyDistiller(),
    )
    # The store grew: a root, a head, and the appended log children.
    assert len(client._docs) > 0
    log_docs = [d for d in client._docs.values() if d["usetype"] == "memory:strategy:log"]
    assert len(log_docs) == 2  # one lesson per training game.
    # The last measure_suite_size contexts are the measurement injections: the assembled
    # strategy doc, which is non-None and carries the induced lesson text.
    measure_contexts = factory.contexts[-config.measure_suite_size :]
    assert all(c is not None and "lesson" in c for c in measure_contexts)


def test_c1_persists_strategy_and_c0_does_not() -> None:
    config = RunConfig(
        condition="C1", k_train=2, measure_suite_size=2, log_every_games=1, swing_threshold=0.0
    )
    c1 = run(
        config,
        llm_endpoint="unused",
        distiller_endpoint="unused",
        store_client=InMemoryJmftsClient(),
        agent_factory=_RecordingFactory(),
        distiller=_SpyDistiller(),
    )
    # C1 preserves the induced strategy: the final doc plus the full immutable log with
    # provenance and temporal position — the raw material for later analysis.
    assert c1.strategy is not None
    assert c1.strategy["final_doc"]
    assert len(c1.strategy["log"]) == 2  # one lesson per training game
    entry = c1.strategy["log"][0]
    assert set(entry) == {"content", "source", "consolidated", "position"}
    assert entry["content"] and entry["source"] == "train-game-0"

    c0 = run(
        RunConfig(condition="C0", k_train=0, measure_suite_size=2),
        llm_endpoint="unused",
        distiller_endpoint="unused",
        store_client=InMemoryJmftsClient(),
        agent_factory=_RecordingFactory(),
        distiller=_SpyDistiller(),
    )
    assert c0.strategy is None  # C0 induces nothing.


# --- measurement suite ------------------------------------------------------------


def test_measurement_suite_is_reproducible_and_distinct() -> None:
    a = measurement_suite(5)
    b = measurement_suite(5)
    assert len(a) == 5
    keys_a = [board.position_key() for board in a]
    keys_b = [board.position_key() for board in b]
    assert keys_a == keys_b  # same positions every call (paired C0/C1 measurement).
    assert len(set(keys_a)) == 5  # distinct openings.
    assert all(board.side == WHITE for board in a)  # LLM (White) to move.


def test_measurement_suite_too_large_raises() -> None:
    opening_count = len(Board().legal_moves())
    with pytest.raises(ValueError, match="legal opening moves"):
        measurement_suite(opening_count + 1)


# --- material-margin metric -------------------------------------------------------


def test_material_margin_is_side_relative() -> None:
    # White has K + N, Black has only k → White is up a knight (3.0).
    board = Board.from_piece_map({"a1": "K", "f6": "k", "b1": "N"}, side=WHITE)
    assert material_margin(board, WHITE) == 3.0
    assert material_margin(board, BLACK) == -3.0


def test_margin_trajectory_starts_at_zero_and_ends_at_final() -> None:
    # From the standard start the material margin is 0; a capture-free opening keeps it 0.
    start = Board()
    record = GameRecord(winner=None, reason="max-plies", moves=[], ply_evals=[])
    traj = margin_trajectory(start, record, WHITE)
    assert traj == [0.0]  # no plies → just the starting margin.


# --- end-to-end under the fake LLM ------------------------------------------------


def test_run_end_to_end_dry_run_is_schema_valid() -> None:
    config = RunConfig(
        condition="C1",
        k_train=4,
        measure_suite_size=3,
        log_every_games=2,
        consolidate_every_logs=2,
        swing_threshold=0.0,
    )
    result = run(
        config,
        llm_endpoint="unused",
        distiller_endpoint="unused",
        agent_factory=greedy_agent_factory(),
        distiller=_DryRunDistiller(),
    )
    payload = result.to_dict()
    assert set(payload) == {"label", "condition", "config", "training", "measurement", "strategy"}
    assert payload["condition"] == "C1"
    assert set(payload["config"]) == {
        "condition",
        "k_train",
        "measure_suite_size",
        "log_every_games",
        "consolidate_every_logs",
        "swing_threshold",
        "temp_train",
        "temp_measure",
        "opponent_depth",
        "max_plies",
    }
    assert len(payload["measurement"]) == 3
    for game in payload["measurement"]:
        assert set(game) == {
            "start_id",
            "llm_side",
            "winner",
            "reason",
            "final_margin",
            "margin_trajectory",
            "moves",
            "ply_evals",
        }
        assert game["llm_side"] == "w"
        assert game["winner"] in ("w", "b", None)
        assert isinstance(game["margin_trajectory"], list)
        assert game["final_margin"] == game["margin_trajectory"][-1]
        # The persisted record enables offline move-quality metrics: one ply_eval per move.
        assert isinstance(game["moves"], list)
        assert len(game["ply_evals"]) == len(game["moves"])
        if game["ply_evals"]:
            assert set(game["ply_evals"][0]) == {"ply_index", "side", "move_uci", "eval_after"}
    json.dumps(payload)  # the whole result is JSON-serialisable.


def test_main_dry_run_writes_json(tmp_path) -> None:
    out = tmp_path / "result.json"
    rc = main(
        [
            "--condition",
            "C1",
            "--k-train",
            "4",
            "--measure-suite-size",
            "2",
            "--log-every-games",
            "2",
            "--consolidate-every-logs",
            "2",
            "--swing-threshold",
            "0.0",
            "--dry-run",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["condition"] == "C1"
    assert len(data["measurement"]) == 2
