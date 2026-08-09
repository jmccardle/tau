"""M3 results plotter: RunResult JSON files -> a condition-comparison figure.

Reference: docs/M3-DESIGN.md §8 (Metrics). The headline this pipeline renders is
the per-condition **final material-margin** distribution across the measurement
suite (each measurement game is one point), so a C0-vs-C1 improvement is legible
even when C1 has not yet turned material into a single win. A second panel shows
the mean `margin_trajectory` per condition (material over game-ply), i.e. whether
C1 hangs material later / less than C0. Per §8 win-rate is the ordinal summary and
the margin curve is the powered test; §9's "curves sit above C0 outside the noise
floor" is exactly this figure read by eye plus the W/D/L annotation.

Pure offline: no LLM, no server, no inference. It consumes a *list* of RunResult
JSON files (the runner writes one per configuration) and emits a PNG + a text
table.

Fail-Early (repo rule): a results file that is missing a required key, or carries
a value of the wrong shape / an out-of-vocabulary `winner`, RAISES with a clear
message. No point is ever silently skipped and no margin is ever fabricated — a
malformed suite is a hard stop, not a hole in the plot.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
from pathlib import Path
from typing import Any, Literal, TypedDict

import matplotlib

matplotlib.use("Agg")  # headless: render to a file, never to a display.

import matplotlib.pyplot as plt  # noqa: E402  # must follow use("Agg")

# --- Schema -----------------------------------------------------------------

Condition = Literal["C0", "C1"]
Side = Literal["w", "b"]
Winner = Literal["w", "b"] | None


class MeasurementGame(TypedDict):
    start_id: str
    llm_side: Side
    winner: Winner
    reason: str
    final_margin: float
    margin_trajectory: list[float]


class Training(TypedDict):
    games: int
    lessons_logged: int
    consolidations: int


class RunResult(TypedDict):
    label: str
    condition: Condition
    config: dict[str, Any]
    training: Training
    measurement: list[MeasurementGame]


# --- Loading + validation (Fail-Early) --------------------------------------


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ValueError(f"{where}: missing required key {key!r}")
    return mapping[key]


def _as_tuple(types: type | tuple[type, ...]) -> tuple[type, ...]:
    return types if isinstance(types, tuple) else (types,)


def _type_name(types: type | tuple[type, ...]) -> str:
    return " or ".join(t.__name__ for t in _as_tuple(types))


def _require_type(value: Any, types: type | tuple[type, ...], key: str, where: str) -> Any:
    allowed = _as_tuple(types)
    # bool is an int subclass; a bool where a number/str is wanted is a schema error.
    if isinstance(value, bool) and bool not in allowed:
        raise ValueError(
            f"{where}: key {key!r} has wrong type: expected {_type_name(types)}, got bool"
        )
    if not isinstance(value, allowed):
        raise ValueError(
            f"{where}: key {key!r} has wrong type: expected {_type_name(types)}, "
            f"got {type(value).__name__}"
        )
    return value


def _validate_measurement_game(raw: Any, where: str) -> MeasurementGame:
    _require_type(raw, dict, "<game>", where)
    start_id = _require_type(_require(raw, "start_id", where), str, "start_id", where)
    llm_side = _require_type(_require(raw, "llm_side", where), str, "llm_side", where)
    if llm_side not in ("w", "b"):
        raise ValueError(f"{where}: 'llm_side' must be 'w' or 'b', got {llm_side!r}")

    winner = _require(raw, "winner", where)
    if winner not in ("w", "b", None):
        raise ValueError(f"{where}: 'winner' must be 'w', 'b', or null, got {winner!r}")

    reason = _require_type(_require(raw, "reason", where), str, "reason", where)
    final_margin = float(
        _require_type(_require(raw, "final_margin", where), (int, float), "final_margin", where)
    )

    trajectory_raw = _require_type(
        _require(raw, "margin_trajectory", where), list, "margin_trajectory", where
    )
    trajectory: list[float] = []
    for i, ply in enumerate(trajectory_raw):
        trajectory.append(float(_require_type(ply, (int, float), f"margin_trajectory[{i}]", where)))

    return MeasurementGame(
        start_id=start_id,
        llm_side=llm_side,  # type: ignore[typeddict-item]  # narrowed above
        winner=winner,  # type: ignore[typeddict-item]  # narrowed above
        reason=reason,
        final_margin=final_margin,
        margin_trajectory=trajectory,
    )


def _validate_run_result(raw: Any, where: str) -> RunResult:
    _require_type(raw, dict, "<run>", where)
    label = _require_type(_require(raw, "label", where), str, "label", where)
    condition = _require_type(_require(raw, "condition", where), str, "condition", where)
    if condition not in ("C0", "C1"):
        raise ValueError(f"{where}: 'condition' must be 'C0' or 'C1', got {condition!r}")

    config = _require_type(_require(raw, "config", where), dict, "config", where)

    training_raw = _require_type(_require(raw, "training", where), dict, "training", where)
    training = Training(
        games=_require_type(_require(training_raw, "games", where), int, "training.games", where),
        lessons_logged=_require_type(
            _require(training_raw, "lessons_logged", where), int, "training.lessons_logged", where
        ),
        consolidations=_require_type(
            _require(training_raw, "consolidations", where), int, "training.consolidations", where
        ),
    )

    measurement_raw = _require_type(_require(raw, "measurement", where), list, "measurement", where)
    if not measurement_raw:
        raise ValueError(f"{where}: 'measurement' is empty; nothing to plot")
    measurement = [
        _validate_measurement_game(g, f"{where} measurement[{i}]")
        for i, g in enumerate(measurement_raw)
    ]

    return RunResult(
        label=label,
        condition=condition,  # type: ignore[typeddict-item]  # narrowed above
        config=config,
        training=training,
        measurement=measurement,
    )


def load_results(paths: list[str | Path]) -> list[RunResult]:
    """Parse and validate a list of RunResult JSON files.

    Fail-Early: a missing file, non-JSON content, or any missing / mistyped /
    out-of-vocabulary field raises with a message that names the file and the key.
    """
    if not paths:
        raise ValueError("load_results: no result paths given")

    results: list[RunResult] = []
    for p in paths:
        path = Path(p)
        if not path.is_file():
            raise FileNotFoundError(f"results file not found: {path}")
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: not valid JSON: {exc}") from exc
        results.append(_validate_run_result(raw, str(path)))
    return results


# --- W/D/L bookkeeping ------------------------------------------------------


class WDL(TypedDict):
    wins: int
    draws: int
    losses: int


def _outcome(game: MeasurementGame) -> Literal["W", "D", "L"]:
    """Classify one measurement game from the LLM side's perspective.

    A decisive game has a `winner`; W iff it matches `llm_side`. A `winner` of
    null is a draw (the terminal `reason` says which flavour, e.g. stalemate) —
    an unfinished 'max-plies' game also has winner null and is counted as a draw
    for the W/D/L tally (it resolved to no decisive result).
    """
    winner = game["winner"]
    if winner is None:
        return "D"
    return "W" if winner == game["llm_side"] else "L"


def _wdl(games: list[MeasurementGame]) -> WDL:
    tally: WDL = {"wins": 0, "draws": 0, "losses": 0}
    for g in games:
        outcome = _outcome(g)
        if outcome == "W":
            tally["wins"] += 1
        elif outcome == "D":
            tally["draws"] += 1
        else:
            tally["losses"] += 1
    return tally


# --- Summary ----------------------------------------------------------------


class ConditionSummary(TypedDict):
    condition: Condition
    n_games: int
    mean_final_margin: float
    median_final_margin: float
    wins: int
    draws: int
    losses: int
    mean_lessons_logged: float


def summarize(results: list[RunResult]) -> dict[str, ConditionSummary]:
    """Per-label (one config = one bar) summary keyed by RunResult label.

    Each entry: n games, mean/median final_margin, W/D/L counts, and mean
    lessons_logged (training signal). Keyed by label so C0 and each distinct C1
    config are reported separately.
    """
    if not results:
        raise ValueError("summarize: no results")

    summary: dict[str, ConditionSummary] = {}
    for run in results:
        games = run["measurement"]
        margins = [g["final_margin"] for g in games]
        tally = _wdl(games)
        summary[run["label"]] = ConditionSummary(
            condition=run["condition"],
            n_games=len(games),
            mean_final_margin=statistics.fmean(margins),
            median_final_margin=statistics.median(margins),
            wins=tally["wins"],
            draws=tally["draws"],
            losses=tally["losses"],
            mean_lessons_logged=float(run["training"]["lessons_logged"]),
        )
    return summary


def format_summary(summary: dict[str, ConditionSummary]) -> str:
    """Render the summary dict as a fixed-width text table."""
    header = (
        f"{'label':<20} {'cond':<4} {'n':>3} {'mean_marg':>10} "
        f"{'med_marg':>9} {'W':>3} {'D':>3} {'L':>3} {'lessons':>8}"
    )
    lines = [header, "-" * len(header)]
    for label, s in summary.items():
        lines.append(
            f"{label:<20.20} {s['condition']:<4} {s['n_games']:>3} "
            f"{s['mean_final_margin']:>10.2f} {s['median_final_margin']:>9.2f} "
            f"{s['wins']:>3} {s['draws']:>3} {s['losses']:>3} "
            f"{s['mean_lessons_logged']:>8.1f}"
        )
    return "\n".join(lines)


# --- Plot -------------------------------------------------------------------


def _mean_trajectory(games: list[MeasurementGame]) -> list[float]:
    """Ply-wise mean material margin across a condition's games.

    Games have different lengths; the mean at ply k averages only the games that
    reached ply k (no zero-padding, which would fabricate even-material plies for
    games that already ended). Fail-Early: a game with an empty trajectory is a
    valid record (0 plies played) and simply contributes to no ply.
    """
    max_len = max((len(g["margin_trajectory"]) for g in games), default=0)
    means: list[float] = []
    for k in range(max_len):
        vals = [g["margin_trajectory"][k] for g in games if len(g["margin_trajectory"]) > k]
        if vals:
            means.append(statistics.fmean(vals))
    return means


def plot(results: list[RunResult], out_path: str | Path) -> Path:
    """Render the two-panel condition-comparison figure to a PNG.

    Reference: docs/M3-DESIGN.md §8 (final material margin is the headline).

    Left panel — per-condition distribution of `final_margin` (one point per
    measurement game, jittered over a box), with a mean marker and a W/D/L
    annotation under each condition. A dashed horizontal line at margin 0 marks
    even material.

    Right panel — mean `margin_trajectory` per condition (material vs. game-ply),
    showing whether C1 hangs material later / less than C0.
    """
    if not results:
        raise ValueError("plot: no results")

    out = Path(out_path)
    summary = summarize(results)

    fig, (ax_dist, ax_traj) = plt.subplots(1, 2, figsize=(13, 6))

    cmap = plt.get_cmap("tab10")
    colors = [cmap(i % 10) for i in range(len(results))]

    # --- Left: final-margin distribution -----------------------------------
    box_data = [[g["final_margin"] for g in run["measurement"]] for run in results]
    positions = list(range(1, len(results) + 1))
    ax_dist.boxplot(
        box_data,
        positions=positions,
        widths=0.5,
        showfliers=False,
        medianprops={"color": "black"},
    )

    lo = min((m for col in box_data for m in col), default=0.0)
    hi = max((m for col in box_data for m in col), default=0.0)
    span = max(hi - lo, 1.0)

    for pos, run, col in zip(positions, results, colors):
        margins = [g["final_margin"] for g in run["measurement"]]
        rng = random.Random(run["label"])  # label-seeded => stable jitter
        xs = [pos + (rng.random() - 0.5) * 0.3 for _ in margins]
        ax_dist.scatter(xs, margins, color=col, alpha=0.7, s=28, zorder=3, edgecolors="none")
        mean_m = summary[run["label"]]["mean_final_margin"]
        ax_dist.scatter(
            [pos],
            [mean_m],
            marker="D",
            color="black",
            s=55,
            zorder=4,
            label="mean" if pos == positions[0] else None,
        )
        s = summary[run["label"]]
        ax_dist.annotate(
            f"W{s['wins']} D{s['draws']} L{s['losses']}",
            xy=(pos, lo - span * 0.08),
            ha="center",
            va="top",
            fontsize=9,
        )

    ax_dist.axhline(0.0, color="green", linestyle="--", linewidth=1, label="even material")
    ax_dist.set_xticks(positions)
    ax_dist.set_xticklabels(
        [f"{run['label']}\n({run['condition']})" for run in results], fontsize=9
    )
    ax_dist.set_ylabel("final material margin (LLM side; + = ahead)")
    ax_dist.set_ylim(lo - span * 0.18, hi + span * 0.1)
    ax_dist.set_title("Final material margin by condition\n(each point = one measurement game)")
    ax_dist.legend(loc="upper left", fontsize=8)
    ax_dist.grid(axis="y", alpha=0.3)

    # --- Right: mean margin trajectory -------------------------------------
    for run, col in zip(results, colors):
        traj = _mean_trajectory(run["measurement"])
        if traj:
            ax_traj.plot(
                range(len(traj)),
                traj,
                color=col,
                label=f"{run['label']} ({run['condition']})",
                linewidth=1.8,
            )
    ax_traj.axhline(0.0, color="green", linestyle="--", linewidth=1)
    ax_traj.set_xlabel("game ply")
    ax_traj.set_ylabel("mean material margin (LLM side)")
    ax_traj.set_title(
        "Mean material trajectory by condition\n(does C1 hang material later / less?)"
    )
    ax_traj.legend(loc="best", fontsize=8)
    ax_traj.grid(alpha=0.3)

    fig.suptitle("M3 Los Alamos chess induction — C0 baseline vs C1 configs", fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out


# --- CLI --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plot M3 Los Alamos chess induction results (C0 vs C1). "
        "See docs/M3-DESIGN.md §8."
    )
    parser.add_argument("paths", nargs="+", help="RunResult JSON files (one per configuration)")
    parser.add_argument("--out", default="m3_results.png", help="output PNG path")
    args = parser.parse_args(argv)

    results = load_results(args.paths)
    summary = summarize(results)
    print(format_summary(summary))
    out = plot(results, args.out)
    print(f"\nwrote plot: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
