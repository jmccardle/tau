#!/usr/bin/env python3
"""Fit a heuristic prior from the recorded provider fixtures (D26 section 6.1).

    .venv/bin/python scripts/fit_prior.py [--fixtures DIR] [--out FILE]

It replays every `turns` case under `tests/fixtures/providers/<family>/` (offline: no network, no
key), takes the proved spans of each, folds them into one calibration record per family with the
fit the `tokens` built-in uses, and prints what was fitted as JSON:

    {"version": 1, "fitted": "YYYY-MM-DD", "tau": "<version>",
     "families": {"<family>": {"cases": 2, "spans": 11, "ready": true, "error": 0.04,
                               "weights": [42 numbers], "per_message": [6 integers]}}}

`weights` are tokens per unit of each class of text for each role and `per_message` tokens per
node of each role, in the order of `tau.calibration_features`. `error` is the relative error the
fit's bound is made from (null while the family has fewer than ten spans).

**Nothing ships a fitted prior today.** D26 has `tokens` start from one "fitted by a script from
the T2 fixtures"; no fixture is recorded yet, so there is nothing honest to fit, and `tokens`
starts every model from the ceiling's assumption (one token per byte) and answers only once that
model's own replies have proved ten spans. This script is the fitting half, so that the day the
fixtures exist a prior is one command; reading it (a record that starts from a family's weights,
with the 35% bound D26 section 6.4 gives a prior) is the half still to build, and to decide.

With no fixture it writes nothing and exits 1: it never invents a number. Exit 2: usage.
"""

import argparse
import asyncio
import datetime
import json
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import record_provider as recorder

import tau
from tau import CalibrationStore, Cursor, Driver, Session, Span, SystemClock, model_config

FIXTURES = recorder.OUT
FAMILIES = tuple(recorder.FAMILIES)


class Spans:
    """An extension whose `/spans` keeps the proved spans of the cursor's walk."""

    name = "fit_prior_spans"

    def __init__(self) -> None:
        self.found: list[Span] = []
        self.commands = (self.spans,)

    def spans(self, cursor: Cursor) -> None:
        """Keep the proved spans of this walk."""
        self.found = cursor.spans()


async def spans_of(case_dir: Path) -> list[Span]:
    """The proved spans of a recorded `turns` case, replayed offline."""
    case = json.loads((case_dir / "case.json").read_text())
    transport = recorder.Replay(case_dir)
    name = case["model"].get("api_key_env")
    provider = recorder.PROVIDERS[case["provider"]](
        transport=transport, environ={} if name is None else {name: recorder.REPLAY_KEY}
    )
    probe = Spans()
    clock = SystemClock()
    with tempfile.TemporaryDirectory() as tmp:
        session = Session.create(
            model_config(recorder.ALIAS), str(case["system"]), clock.now(), Path(tmp) / "s"
        )
        driver = Driver(
            session,
            {provider.name: provider},
            tau.read_models(cli={"models": {recorder.ALIAS: dict(case["model"])}}),
            tools=[recorder.TableTool(spec) for spec in case["tools"]],
            extensions=[probe],
            clock=clock,
        )
        driver.start()
        cursor = session.cursors()[0]
        try:
            for text in case["turns"]:
                await driver.client("fit").send(cursor, text)
                await driver.rest(cursor)
            if transport.mismatch is not None:
                raise recorder.Failed(f"{case_dir}: {transport.mismatch}")
            await driver.client("fit").run(cursor, "spans", {})
        finally:
            await driver.stop()
            session.close()
            await provider.aclose()
    return probe.found


def fit(family: str, spans: Sequence[Span]) -> Mapping[str, object]:
    """One family's fit of `spans`: the record the `tokens` built-in would hold after them."""
    with tempfile.TemporaryDirectory() as tmp:
        store = CalibrationStore(tmp)
        name = f"{family}/prior.json"
        record = None
        for span in spans:
            rows = [tau.calibration_features(n.body) for n in span.nodes]
            features = [sum(column) for column in zip(*rows, strict=True)]
            record, _ = store.observe(name, (family, None, "prior"), {}, (features, span.cost), "")
        assert record is not None
        return {
            "spans": record.spans,
            "ready": record.ready,
            "error": record.error,
            "weights": record.weights,
            "per_message": record.per_message,
        }


async def fitted(root: Path) -> dict[str, dict[str, object]]:
    families: dict[str, dict[str, object]] = {}
    for family in FAMILIES:
        directory = root / family
        cases = (
            sorted(p for p in directory.iterdir() if (p / "case.json").is_file())
            if (directory.is_dir())
            else []
        )
        turns = [c for c in cases if json.loads((c / "case.json").read_text())["kind"] == "turns"]
        spans = [span for case in turns for span in await spans_of(case)]
        if spans:
            families[family] = {"cases": len(turns), **fit(family, spans)}
    return families


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument(
        "--fixtures", default=str(FIXTURES), metavar="DIR", help=f"default: {FIXTURES}"
    )
    parser.add_argument("--out", metavar="FILE", help="write the JSON here (default: print it)")
    args = parser.parse_args()
    try:
        families = asyncio.run(fitted(Path(args.fixtures)))
    except recorder.Failed as e:
        print(f"{parser.prog}: {e}", file=sys.stderr)
        return 1
    if not families:
        print(
            f"{parser.prog}: no proved span in any fixture under {args.fixtures}: nothing to fit "
            "(record fixtures with scripts/record_provider.py)",
            file=sys.stderr,
        )
        return 1
    text = json.dumps(
        {
            "version": 1,
            "fitted": datetime.datetime.now(datetime.UTC).date().isoformat(),
            "tau": tau.__version__,
            "families": families,
        },
        indent=2,
    )
    if args.out is None:
        print(text)
    else:
        Path(args.out).write_text(text + "\n")
        print(f"{args.out}: {', '.join(f'{k} ({v["spans"]} spans)' for k, v in families.items())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
