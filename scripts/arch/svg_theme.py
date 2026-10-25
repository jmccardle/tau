"""Rewrite a graphviz SVG so it repaints with a page's theme tokens.

Graphviz writes literal colours and fonts. This strips them and assigns the
class vocabulary of the FFWF ``Diagram`` component (``stroke``, ``stroke-soft``,
``fill-ground``, ``label`` …), so the figure follows light and dark themes when
inlined into a page that defines those classes.

Classes set in the DOT source on a node or edge (``class="mark"``) select the
variant drawn:

    node  mark      red outline, the one load-bearing element
    node  muted     ash outline and ash label
    node  durable   45-degree hatch fill (durable storage only)
    edge  mark      red stroke
    edge  lazy      dashed ink stroke (function-local import, deferred call)
    edge  soft      dashed ash stroke (TYPE_CHECKING import, weak relation)

Usage:
    python scripts/arch/svg_theme.py in.svg out.svg --title "..." --desc "..."
    dot -Tsvg graph.dot | python scripts/arch/svg_theme.py - out.svg --title T --desc D

Output: one ``<svg>`` element with no XML prolog, ``role="img"`` and a
``<title>``/``<desc>`` pair, suitable for inlining into HTML.
"""

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
SHAPES = {"polygon", "ellipse", "path", "polyline", "rect", "circle"}
STRIP = ("fill", "stroke", "font-family", "font-size", "font-weight", "stroke-width")


def _tag(el: ET.Element) -> str:
    """Return the element's tag without its namespace."""
    return el.tag.rsplit("}", 1)[-1]


def _classes(el: ET.Element) -> set[str]:
    """Return the element's class attribute as a set."""
    return set((el.get("class") or "").split())


def _style_shape(shape: ET.Element, group: set[str], is_arrowhead: bool, uid: str) -> None:
    """Assign stroke and fill classes to one shape inside a node, edge or cluster."""
    dashed = shape.get("stroke-dasharray") is not None
    for attr in STRIP + ("stroke-dasharray",):
        shape.attrib.pop(attr, None)
    if "edge" in group:
        if is_arrowhead:
            cls = "fill-red" if "mark" in group else ("fill-ash" if "soft" in group else "fill-ink")
        else:
            stroke = (
                "stroke-red"
                if "mark" in group
                else ("stroke-soft" if "soft" in group else "stroke-hair")
            )
            cls = f"{stroke} fill-none"
            if dashed or "lazy" in group or "soft" in group:
                cls += " stroke-dashed"
    elif "cluster" in group:
        cls = "stroke-soft fill-none stroke-dashed" if "muted" in group else "stroke-soft fill-none"
    else:
        stroke = (
            "stroke-red" if "mark" in group else ("stroke-soft" if "muted" in group else "stroke")
        )
        if "durable" in group:
            shape.set("fill", f"url(#{uid}-hatch)")
            cls = stroke
        else:
            cls = f"{stroke} fill-ground"
    shape.set("class", cls)


def _style_text(text: ET.Element, group: set[str], size: float) -> None:
    """Assign a label class to one text element, by its group and font size."""
    for attr in STRIP:
        text.attrib.pop(attr, None)
    if "edge" in group or "cluster" in group or "muted" in group or size < 12:
        text.set("class", "label-soft")
    else:
        text.set("class", "label")


def theme(svg_text: str, title: str, desc: str, uid: str) -> str:
    """Return themed SVG markup for a graphviz SVG document."""
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    root = ET.fromstring(svg_text)
    for attr in ("width", "height"):
        root.attrib.pop(attr, None)
    root.set("role", "img")
    root.set("aria-labelledby", f"{uid}-t {uid}-d")
    graph = root.find(f"{{{SVG_NS}}}g")
    if graph is None:
        raise SystemExit("svg_theme: input has no top-level <g>; is it graphviz output?")
    for child in list(graph):
        if _tag(child) == "polygon" and child.get("fill") == "white":
            graph.remove(child)
        elif _tag(child) == "title":
            graph.remove(child)
        elif _tag(child) == "text":
            _style_text(child, {"cluster"}, float(child.get("font-size", "14") or 14))
    for group in graph.iter(f"{{{SVG_NS}}}g"):
        gcls = _classes(group)
        if not gcls & {"node", "edge", "cluster"}:
            continue
        shapes = [c for c in group if _tag(c) in SHAPES]
        for i, shape in enumerate(shapes):
            arrow = "edge" in gcls and _tag(shape) == "polygon" and i > 0
            _style_shape(shape, gcls, arrow, uid)
        for text in group.iter(f"{{{SVG_NS}}}text"):
            size = float(text.get("font-size", "14") or 14)
            _style_text(text, gcls, size)
        for t in group.findall(f"{{{SVG_NS}}}title"):
            group.remove(t)
    defs = ET.Element(f"{{{SVG_NS}}}defs")
    pattern = ET.SubElement(
        defs,
        f"{{{SVG_NS}}}pattern",
        {
            "id": f"{uid}-hatch",
            "width": "8",
            "height": "8",
            "patternUnits": "userSpaceOnUse",
            "patternTransform": "rotate(45)",
        },
    )
    ET.SubElement(
        pattern, f"{{{SVG_NS}}}line", {"x1": "4", "y1": "0", "x2": "4", "y2": "8", "class": "hatch"}
    )
    t = ET.Element(f"{{{SVG_NS}}}title", {"id": f"{uid}-t"})
    t.text = title
    d = ET.Element(f"{{{SVG_NS}}}desc", {"id": f"{uid}-d"})
    d.text = desc
    root.insert(0, defs)
    root.insert(0, d)
    root.insert(0, t)
    out = ET.tostring(root, encoding="unicode")
    return re.sub(r"\s*<!--.*?-->", "", out, flags=re.S)


def main() -> int:
    """Parse arguments, theme the input SVG and write the result."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", help="graphviz SVG path, or - for stdin")
    ap.add_argument("dst", help="output path")
    ap.add_argument("--title", required=True, help="accessible title")
    ap.add_argument("--desc", required=True, help="accessible description of the arrangement")
    ap.add_argument("--id", default=None, help="document-unique id prefix (default: output stem)")
    args = ap.parse_args()
    text = sys.stdin.read() if args.src == "-" else Path(args.src).read_text()
    uid = args.id or re.sub(r"[^A-Za-z0-9_-]", "-", Path(args.dst).stem)
    Path(args.dst).write_text(theme(text, args.title, args.desc, uid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
