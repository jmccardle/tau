"""Tests for the M3 results plotter (experiments/m3/plot_results.py).

Reference: docs/M3-DESIGN.md §8 (final material margin is the headline metric).
Pure offline: no LLM, no server. The RunResult inputs are hand-written SYNTHETIC
dicts written to temp JSON — a C0 baseline hanging pieces (negative margins), a
C1 that hangs less material, and a C1 that actually converts a win — so both the
summary arithmetic and the Fail-Early validation paths are exercised without the
real runner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from plot_results import (
    format_summary,
    load_results,
    main,
    plot,
    summarize,
)

# --- Synthetic fixtures -----------------------------------------------------


def _game(
    start_id: str,
    llm_side: str,
    winner: str | None,
    reason: str,
    final_margin: float,
    trajectory: list[float],
) -> dict[str, Any]:
    return {
        "start_id": start_id,
        "llm_side": llm_side,
        "winner": winner,
        "reason": reason,
        "final_margin": final_margin,
        "margin_trajectory": trajectory,
    }


def _c0() -> dict[str, Any]:
    """Baseline: the model hangs pieces — margins are all negative, all losses."""
    return {
        "label": "baseline",
        "condition": "C0",
        "config": {"k_train": 0, "opponent_depth": 2, "temp_train": 0.0},
        "training": {"games": 0, "lessons_logged": 0, "consolidations": 0},
        "measurement": [
            _game("p1", "w", "b", "checkmate", -5.0, [0.0, -1.0, -3.0, -5.0]),
            _game("p2", "w", "b", "checkmate", -3.0, [0.0, -1.0, -3.0]),
            _game("p3", "w", "b", "checkmate", -7.0, [0.0, -2.0, -7.0]),
        ],
    }


def _c1_better() -> dict[str, Any]:
    """C1 induced: hangs less material (margins less negative), still 0 wins."""
    return {
        "label": "induced",
        "condition": "C1",
        "config": {"k_train": 40, "opponent_depth": 2, "temp_train": 0.7},
        "training": {"games": 40, "lessons_logged": 12, "consolidations": 3},
        "measurement": [
            _game("p1", "w", "b", "checkmate", -1.0, [0.0, 0.0, -1.0]),
            _game("p2", "w", None, "stalemate", 0.0, [0.0, 0.0, 0.0]),
            _game("p3", "w", "b", "checkmate", -2.0, [0.0, -1.0, -2.0]),
        ],
    }


def _c1_win() -> dict[str, Any]:
    """C1 curated: converts material into an actual win (W/D/L has a W)."""
    return {
        "label": "curated",
        "condition": "C1",
        "config": {"k_train": 40, "opponent_depth": 2, "temp_train": 0.7},
        "training": {"games": 40, "lessons_logged": 20, "consolidations": 5},
        "measurement": [
            _game("p1", "w", "w", "checkmate", 6.0, [0.0, 1.0, 3.0, 6.0]),
            _game("p2", "b", None, "insufficient-material", 0.0, [0.0, 0.0]),
            _game("p3", "w", "b", "checkmate", -1.0, [0.0, -1.0]),
        ],
    }


def _write(tmp_path: Path, name: str, data: dict[str, Any]) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


# --- load_results + summarize arithmetic ------------------------------------


def test_load_and_summarize_stats(tmp_path: Path) -> None:
    paths = [
        _write(tmp_path, "c0.json", _c0()),
        _write(tmp_path, "c1a.json", _c1_better()),
        _write(tmp_path, "c1b.json", _c1_win()),
    ]
    results = load_results(paths)
    assert [r["label"] for r in results] == ["baseline", "induced", "curated"]

    summary = summarize(results)

    # C0: three losses, mean of (-5,-3,-7) = -5, median -5.
    base = summary["baseline"]
    assert base["condition"] == "C0"
    assert base["n_games"] == 3
    assert base["mean_final_margin"] == pytest.approx(-5.0)
    assert base["median_final_margin"] == pytest.approx(-5.0)
    assert (base["wins"], base["draws"], base["losses"]) == (0, 0, 3)
    assert base["mean_lessons_logged"] == pytest.approx(0.0)

    # C1 induced: mean(-1,0,-2) = -1; one stalemate draw, two losses; less negative than C0.
    ind = summary["induced"]
    assert ind["mean_final_margin"] == pytest.approx(-1.0)
    assert (ind["wins"], ind["draws"], ind["losses"]) == (0, 1, 2)
    assert ind["mean_lessons_logged"] == pytest.approx(12.0)
    assert ind["mean_final_margin"] > base["mean_final_margin"]  # the C1 improvement

    # C1 curated: a win (p1), a draw (p2 insufficient-material), a loss (p3).
    cur = summary["curated"]
    assert (cur["wins"], cur["draws"], cur["losses"]) == (1, 1, 1)
    assert cur["mean_final_margin"] == pytest.approx((6.0 + 0.0 - 1.0) / 3)


def test_black_side_win_attribution(tmp_path: Path) -> None:
    # winner matches llm_side even when the LLM plays black => a win, not a loss.
    data = _c0()
    data["measurement"] = [_game("p1", "b", "b", "checkmate", 4.0, [0.0, 4.0])]
    results = load_results([_write(tmp_path, "blk.json", data)])
    s = summarize(results)["baseline"]
    assert (s["wins"], s["draws"], s["losses"]) == (1, 0, 0)


def test_format_summary_is_text_table(tmp_path: Path) -> None:
    results = load_results([_write(tmp_path, "c0.json", _c0())])
    text = format_summary(summarize(results))
    assert "baseline" in text
    assert "mean_marg" in text  # header present


# --- plot writes a non-empty PNG --------------------------------------------


def test_plot_writes_nonempty_png(tmp_path: Path) -> None:
    results = load_results(
        [
            _write(tmp_path, "c0.json", _c0()),
            _write(tmp_path, "c1a.json", _c1_better()),
            _write(tmp_path, "c1b.json", _c1_win()),
        ]
    )
    out = tmp_path / "plot.png"
    returned = plot(results, out)
    assert returned == out
    assert out.is_file()
    assert out.stat().st_size > 0
    # PNG magic bytes.
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_main_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p0 = _write(tmp_path, "c0.json", _c0())
    p1 = _write(tmp_path, "c1.json", _c1_better())
    out = tmp_path / "out.png"
    rc = main([str(p0), str(p1), "--out", str(out)])
    assert rc == 0
    assert out.is_file() and out.stat().st_size > 0
    captured = capsys.readouterr()
    assert "baseline" in captured.out
    assert str(out) in captured.out


# --- Fail-Early validation --------------------------------------------------


def test_missing_measurement_raises(tmp_path: Path) -> None:
    data = _c0()
    del data["measurement"]
    p = _write(tmp_path, "bad.json", data)
    with pytest.raises(ValueError, match="missing required key 'measurement'"):
        load_results([p])


def test_empty_measurement_raises(tmp_path: Path) -> None:
    data = _c0()
    data["measurement"] = []
    p = _write(tmp_path, "empty.json", data)
    with pytest.raises(ValueError, match="'measurement' is empty"):
        load_results([p])


def test_bad_winner_value_raises(tmp_path: Path) -> None:
    data = _c0()
    data["measurement"][0]["winner"] = "white"  # out of vocabulary
    p = _write(tmp_path, "bad_winner.json", data)
    with pytest.raises(ValueError, match="'winner' must be"):
        load_results([p])


def test_bad_condition_value_raises(tmp_path: Path) -> None:
    data = _c0()
    data["condition"] = "C2"
    p = _write(tmp_path, "bad_cond.json", data)
    with pytest.raises(ValueError, match="'condition' must be"):
        load_results([p])


def test_wrong_type_final_margin_raises(tmp_path: Path) -> None:
    data = _c0()
    data["measurement"][0]["final_margin"] = "not-a-number"
    p = _write(tmp_path, "bad_margin.json", data)
    with pytest.raises(ValueError, match="'final_margin' has wrong type"):
        load_results([p])


def test_trajectory_not_a_list_raises(tmp_path: Path) -> None:
    data = _c0()
    data["measurement"][0]["margin_trajectory"] = 3.0
    p = _write(tmp_path, "bad_traj.json", data)
    with pytest.raises(ValueError, match="'margin_trajectory' has wrong type"):
        load_results([p])


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="results file not found"):
        load_results([tmp_path / "does_not_exist.json"])


def test_non_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "junk.json"
    p.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_results([p])


def test_empty_paths_raises() -> None:
    with pytest.raises(ValueError, match="no result paths"):
        load_results([])
