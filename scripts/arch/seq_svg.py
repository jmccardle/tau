"""Render a mermaid ``sequenceDiagram`` subset as theme-classed inline SVG.

The output uses the FFWF ``Diagram`` class vocabulary only (no literal colours
or fonts), so it repaints with the page theme. Supported mermaid lines:

    participant Id as Label         (or ``participant Id``; ``actor`` is the same)
    A->>B: message                  call, solid arrow
    A-->>B: message                 return, dashed arrow
    A-xB: message                   failure, red arrow (the marked element)
    Note over A: text               note on one lifeline
    Note over A,B: text             note spanning lifelines
    Note right of A: text           note beside a lifeline
    %% comment                      ignored

Everything else (``loop``, ``alt``, ``activate``…) is rejected with the line
number, so a diagram never renders with steps silently missing.

Usage:
    python scripts/arch/seq_svg.py sequence.mmd out.svg --title T --desc D
"""

import argparse
import html
import re
import sys
from pathlib import Path

COL_W = 150
TOP = 46
ROW_H = 26
NOTE_H = 22
CHAR_W = 6.6

PART_RE = re.compile(r"^(?:participant|actor)\s+(\S+)(?:\s+as\s+(.+))?$")
MSG_RE = re.compile(r"^([^\s:>-]+)\s*(-->>|->>|-x|--x)\s*([^\s:]+)\s*:\s*(.*)$")
NOTE_RE = re.compile(r"^Note\s+(over|right of|left of)\s+([^:]+):\s*(.*)$", re.I)


def parse(text: str) -> tuple[list[tuple[str, str]], list[dict]]:
    """Parse mermaid text into participants and steps; raise on unsupported lines."""
    parts: list[tuple[str, str]] = []
    steps: list[dict] = []
    seen: set[str] = set()

    def need(pid: str) -> None:
        """Register an implicitly declared participant."""
        if pid not in seen:
            seen.add(pid)
            parts.append((pid, pid))

    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if (
            not line
            or line.startswith("%%")
            or line == "sequenceDiagram"
            or line.startswith("autonumber")
        ):
            continue
        if m := PART_RE.match(line):
            if m.group(1) not in seen:
                seen.add(m.group(1))
                parts.append((m.group(1), (m.group(2) or m.group(1)).strip()))
            continue
        if m := MSG_RE.match(line):
            src, arrow, dst, label = m.groups()
            need(src)
            need(dst)
            kind = {"->>": "call", "-->>": "return"}.get(arrow, "fail")
            steps.append({"kind": kind, "src": src, "dst": dst, "label": label})
            continue
        if m := NOTE_RE.match(line):
            where, who, label = m.groups()
            ids = [w.strip() for w in who.split(",")]
            for i in ids:
                need(i)
            steps.append({"kind": "note", "where": where.lower(), "ids": ids, "label": label})
            continue
        raise SystemExit(f"seq_svg: line {n}: unsupported mermaid syntax: {line!r}")
    return parts, steps


def render(parts: list[tuple[str, str]], steps: list[dict], title: str, desc: str, uid: str) -> str:
    """Return SVG markup for parsed participants and steps."""
    x = {pid: 20 + COL_W * i + COL_W // 2 for i, (pid, _) in enumerate(parts)}
    width = 40 + COL_W * len(parts)
    body: list[str] = []
    y = TOP + 20
    for s in steps:
        if s["kind"] == "note":
            xs = [x[i] for i in s["ids"]]
            label = html.escape(s["label"])
            tw = len(s["label"]) * CHAR_W + 14
            if s["where"] == "over":
                cx = (min(xs) + max(xs)) / 2
                w = max(tw, max(xs) - min(xs) + 40)
                x0 = cx - w / 2
            elif s["where"] == "right of":
                x0, w = xs[0] + 8, tw
            else:
                x0, w = xs[0] - 8 - tw, tw
            width = max(width, int(x0 + w + 20))
            body.append(
                f'<rect x="{x0:.0f}" y="{y - 14}" width="{w:.0f}" height="{NOTE_H - 4}" class="fill-surface stroke-soft"/>'
            )
            body.append(f'<text x="{x0 + 7:.0f}" y="{y}" class="label-soft">{label}</text>')
            y += NOTE_H
            continue
        x1, x2 = x[s["src"]], x[s["dst"]]
        label = html.escape(s["label"])
        stroke = {
            "call": "stroke-hair",
            "return": "stroke-soft stroke-dashed",
            "fail": "stroke-red",
        }[s["kind"]]
        head = {"call": "fill-ink", "return": "fill-ash", "fail": "fill-red"}[s["kind"]]
        text_cls = "label-mark" if s["kind"] == "fail" else "label-soft"
        if x1 == x2:
            body.append(
                f'<path d="M{x1} {y - 4} h28 v12 h-24" class="{stroke} fill-none"/>'
                f'<path d="M{x1} {y + 8} l8 -4 l0 8 Z" class="{head}"/>'
                f'<text x="{x1 + 34}" y="{y + 4}" class="{text_cls}">{label}</text>'
            )
            width = max(width, int(x1 + 40 + len(s["label"]) * CHAR_W))
            y += ROW_H + 6
            continue
        d = 1 if x2 > x1 else -1
        tip = x2 - d * 2
        body.append(f'<line x1="{x1}" y1="{y}" x2="{tip - d * 7}" y2="{y}" class="{stroke}"/>')
        body.append(f'<path d="M{tip} {y} l{-d * 9} -4 l0 8 Z" class="{head}"/>')
        tx = min(x1, x2) + 6
        body.append(f'<text x="{tx}" y="{y - 5}" class="{text_cls}">{label}</text>')
        width = max(width, int(tx + len(s["label"]) * CHAR_W + 10))
        y += ROW_H
    height = y + 10
    heads: list[str] = []
    for pid, label in parts:
        cx = x[pid]
        w = max(COL_W - 16, len(label) * 7.4 + 16)
        heads.append(
            f'<line x1="{cx}" y1="{TOP}" x2="{cx}" y2="{height - 6}" class="stroke-soft stroke-dashed"/>'
        )
        heads.append(
            f'<rect x="{cx - w / 2:.0f}" y="8" width="{w:.0f}" height="30" rx="15" class="fill-ground stroke"/>'
        )
        heads.append(
            f'<text x="{cx}" y="27" text-anchor="middle" class="label">{html.escape(label)}</text>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" '
        f'aria-labelledby="{uid}-t {uid}-d"><title id="{uid}-t">{html.escape(title)}</title>'
        f'<desc id="{uid}-d">{html.escape(desc)}</desc>' + "".join(heads) + "".join(body) + "</svg>"
    )


def main() -> int:
    """Parse arguments, render the diagram and write it."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", help="mermaid sequenceDiagram file, or - for stdin")
    ap.add_argument("dst", help="output SVG path")
    ap.add_argument("--title", required=True, help="accessible title")
    ap.add_argument("--desc", required=True, help="accessible description of the arrangement")
    ap.add_argument("--id", default=None, help="document-unique id prefix (default: output stem)")
    args = ap.parse_args()
    text = sys.stdin.read() if args.src == "-" else Path(args.src).read_text()
    parts, steps = parse(text)
    uid = args.id or re.sub(r"[^A-Za-z0-9_-]", "-", Path(args.dst).stem)
    Path(args.dst).write_text(render(parts, steps, args.title, args.desc, uid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
