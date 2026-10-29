"""The step's position graph, from the transitions the test suites made.

Input: the logs `scripts/diagrams.sh` collects (`TAU-TRANSITION` lines the core
writes when built with its `transitions` feature) and the shape of `Pos` from
the Rust source. A log named `rust-<test binary>.log` holds one `cargo test`
run; `python.log` holds the pytest run. Output: `step-positions.dot` and
`step-positions.md` in the given directory.

`ALLOWED` is the one hand-written table here: the changes the code in
`step.rs` can make, read from it once. A change the suites made that is not in
it stops the build (the table is stale); a change in it that no scenario made
is drawn dashed red.
"""

import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "crates" / "tau-core" / "tests"
ENDINGS = ("Done", "Halted", "Failed", "Aborted")

# What each position is, in words.
MEANING = {
    "PreStep": "pre_step hooks",
    "Input": "input phase: pre_input hooks, then commit a User node, per pending input",
    "PreSample": "pre_sample hooks (if a sample applies)",
    "Sample": "the provider sample",
    "PreReply": "pre_reply hooks on the reply draft, then commit the Assistant node",
    "PostReply": "post_reply hooks",
    "Tools": "decide whether a batch runs (no job of its own)",
    "Calls": "the tool calls run in the driver; outcomes arrive one by one",
    "PostResult": "post_result hooks, after the Results node is written",
    "PostStep": "post_step hooks, carrying the ending",
    "Rest": "the step has ended",
}

STOP_OR_FAIL = ("answer Hooks(stop)", "answer HookFailed")


def _allowed() -> set[tuple[str, str, str]]:
    a: set[tuple[str, str, str]] = set()
    for event in ("wake", "wake (asked)"):
        a.add(("(wake)", event, "PreStep"))
    # every hook position: a string halts, a raise fails; both go to post_step
    for pos in ("PreStep", "Input", "PreSample", "PreReply", "PostReply", "PostResult"):
        for event in STOP_OR_FAIL:
            a.add((pos, event, "PostStep"))
    a.add(("PreStep", "answer Hooks", "Input"))
    a.add(("Input", "no input to offer", "PreSample"))
    a.add(("Input", "answer Hooks", "Input"))  # one input committed; the next is offered
    a.add(("Input", "commit refused", "PostStep"))
    a.add(("PreSample", "answer Hooks", "Sample"))
    a.add(("PreSample", "sample does not apply", "Tools"))
    a.add(("Sample", "pre_sample draft answers the sample", "PreReply"))
    for reply in ("reply", "reply, error"):
        a.add(("Sample", f"answer Sampled({reply})", "PreReply"))
    for none in ("error", "none"):
        a.add(("Sample", f"answer Sampled({none})", "PostStep"))
    a.add(("PreReply", "answer Hooks", "PostReply"))
    a.add(("PreReply", "commit refused", "PostStep"))
    a.add(("PostReply", "answer Hooks", "Tools"))
    a.add(("PostReply", "answer Hooks", "PostStep"))  # the sample had failed after a reply
    a.add(("Tools", "tools apply", "Calls"))
    a.add(("Tools", "tools do not apply", "PostStep"))
    a.add(("Calls", "answer CallDone", "Calls"))
    a.add(("Calls", "answer CallDone", "PostResult"))
    a.add(("PostResult", "answer Hooks", "PostStep"))
    for ending in ENDINGS:
        a.add(("PostStep", f"ending {ending}", "Rest"))
    a.add(("PostStep", "ending Done", "PreStep"))
    return a


ALLOWED = _allowed()

# The job each position hands out.
EMITS = {
    "PreStep": ["Hooks(PreStep)"],
    "Input": ["Hooks(PreInput)"],
    "PreSample": ["Hooks(PreSample)"],
    "Sample": ["Sample"],
    "PreReply": ["Hooks(PreReply)"],
    "PostReply": ["Hooks(PostReply)"],
    "Tools": [],
    "Calls": ["RunCalls", "Pending"],
    "PostResult": ["Hooks(PostResult)"],
    "PostStep": [f"Hooks(PostStep, {e})" for e in ENDINGS],
    "Rest": ["Rest"],
}


def property_tests() -> set[str]:
    """Names of the tests written with `proptest!`, per test binary."""
    names: set[str] = set()
    for path in sorted(TESTS.glob("*.rs")):
        src = path.read_text()
        for m in re.finditer(r"proptest!\s*\{", src):
            depth, i = 1, m.end()
            while depth:
                depth += {"{": 1, "}": -1}.get(src[i], 0)
                i += 1
            names |= {f"rust/{path.stem}::{n}" for n in re.findall(r"fn (\w+)", src[m.end() : i])}
    return names


def read_logs(logs: list[Path]) -> list[tuple[str, str, str, str, str]]:
    """(scenario, kind, from, event, to) for every distinct line."""
    seen: set[tuple[str, str, str, str, str]] = set()
    for log in logs:
        prefix = "" if log.stem == "python" else f"rust/{log.stem.removeprefix('rust-')}::"
        for line in log.read_text().splitlines():
            if not line.startswith("TAU-TRANSITION\t"):
                continue
            _, scenario, kind, src, event, dst = line.split("\t")
            seen.add((prefix + scenario, kind, src, event, dst))
    return sorted(seen)


def normal(src: str, event: str) -> str:
    """At `PostStep` the answer's kind does not matter, only the ending."""
    found = re.search(r"\[ending (\w+)\]", event)
    if src == "PostStep" and found:
        return "ending " + found.group(1)
    return event


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class Graph:
    positions: list[str]
    edges: dict[tuple[str, str, str], set[str]]
    emits: dict[tuple[str, str], set[str]]
    prop: set[str]
    scenarios: set[str]


def main(logs: list[Path], types_json: Path, out: Path) -> None:
    pos_shape = next(t for t in json.loads(types_json.read_text()) if t["name"] == "Pos")
    positions = [v["name"] for v in pos_shape["variants"]]
    if set(positions) != set(MEANING) or set(positions) != set(EMITS):
        sys.exit(f"positions.py is stale: Pos has {positions}")
    prop = property_tests()

    rows = read_logs(logs)
    edges: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    emits: dict[tuple[str, str], set[str]] = defaultdict(set)
    for scenario, kind, src, event, dst in rows:
        if kind == "emit":
            emits[(src, event.removeprefix("emit "))].add(scenario)
        else:
            edges[(src, normal(src, event), dst)].add(scenario)
    stale = sorted(set(edges) - ALLOWED)
    if stale:
        sys.exit(f"changes the suites made that positions.py does not allow: {stale}")
    scenarios = {r[0] for r in rows}

    g = Graph(positions, edges, emits, prop, scenarios)
    write_dot(out / "step-positions.dot", g)
    write_md(out / "step-positions.md", g)


def write_dot(path: Path, g: Graph) -> None:
    positions, edges, emits, prop = g.positions, g.edges, g.emits, g.prop
    reached = {d for (_, _, d), w in edges.items() if w} | {"PreStep"}
    out = [
        "// Generated by scripts/diagrams.sh from the test suites' transitions. Do not edit.",
        "digraph step_positions {",
        '  graph [rankdir=TB, fontname="Helvetica", nodesep=0.5, ranksep=0.6, labelloc=t, '
        "label=<<b>One step: positions, the job each hands out, the answer each edge waits "
        "for</b><br/>"
        "edge label = what causes the move, then the number of scenarios that made it; "
        "dashed red = the code allows it, no scenario makes it; "
        "orange = only the property tests make it>];",
        '  node [shape=plain, fontname="Helvetica", fontsize=11];',
        '  edge [fontname="Helvetica", fontsize=10];',
        '  "(wake)" [shape=circle, label="wake", fontsize=10];',
    ]
    for pos in positions:
        lines = [f'<tr><td align="left"><b>{pos}</b></td></tr>']
        meaning = esc(MEANING[pos])
        lines.append(f'<tr><td align="left"><font point-size="9">{meaning}</font></td></tr>')
        for job in EMITS[pos]:
            who = emits.get((pos, job), set())
            colour = "black" if who else "red"
            tag = f"{len(who)}" if who else "never"
            lines.append(
                f'<tr><td align="left"><font color="{colour}" point-size="10">hands out {esc(job)} '
                f"({tag})</font></td></tr>"
            )
        fill = "white" if pos in reached else "mistyrose"
        out.append(
            f'  "{pos}" [label=<<table border="1" cellborder="0" cellspacing="0" bgcolor="{fill}">'
            + "".join(lines)
            + "</table>>];"
        )
    pairs: dict[tuple[str, str], list[tuple[str, set[str]]]] = defaultdict(list)
    for src, event, dst in sorted(ALLOWED):
        pairs[(src, dst)].append((event, edges.get((src, event, dst), set())))
    for (src, dst), items in sorted(pairs.items()):
        label: list[str] = []
        for event, who in items:
            if not who:
                label.append(f'<font color="red">{esc(event)} (never)</font>')
            elif who <= prop:
                label.append(f'<font color="darkorange">{esc(event)} ({len(who)})</font>')
            else:
                label.append(f"{esc(event)} ({len(who)})")
        style = "dashed" if all(not w for _, w in items) else "solid"
        if all(not w for _, w in items):
            colour = "red"
        elif all(w <= prop for _, w in items):
            colour = "darkorange"
        else:
            colour = "black"
        text = "<br/>".join(label)
        out.append(f'  "{src}" -> "{dst}" [label=<{text}>, color="{colour}", style={style}];')
    out.append("}")
    path.write_text("\n".join(out) + "\n")


def write_md(path: Path, g: Graph) -> None:
    positions, edges, emits, prop, scenarios = g.positions, g.edges, g.emits, g.prop, g.scenarios
    named = {s for s in scenarios if s not in prop}
    out = [
        "<!-- Generated by scripts/diagrams.sh. Do not edit. -->",
        "# Step position graph: coverage",
        "",
        f"{len(scenarios)} scenarios reached the step machine: {len(named)} named tests and "
        f"{len(scenarios) - len(named)} property tests (`proptest!`). "
        "A property test counts once, however many cases it runs.",
        "",
        "## Edges",
        "",
        "| from | cause | to | scenarios | named | property | covered by (up to 3 named) |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    order = {p: i for i, p in enumerate(["(wake)", *positions])}
    for src, event, dst in sorted(ALLOWED, key=lambda e: (order[e[0]], order[e[2]], e[1])):
        who = edges.get((src, event, dst), set())
        names = sorted(who - prop)
        flag = " **never reached**" if not who else (" *property tests only*" if not names else "")
        shown = ", ".join(f"`{n}`" for n in names[:3])
        out.append(
            f"| {src} | {event} | {dst} | {len(who)}{flag} | {len(names)} | "
            f"{len(who & prop)} | {shown} |"
        )
    out += ["", "## Jobs handed out", "", "| at | job | scenarios | named |", "|---|---|---:|---:|"]
    for pos in positions:
        for job in EMITS[pos]:
            who = emits.get((pos, job), set())
            flag = " **never**" if not who else ""
            out.append(f"| {pos} | {job} | {len(who)}{flag} | {len(who - prop)} |")
    path.write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    main([Path(a) for a in args[2:]], Path(args[0]), Path(args[1]))
