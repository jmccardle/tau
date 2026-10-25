"""Assemble the τ as-built review page from head.html, body.html and the rendered figures.

Markers in body.html (and in any file it includes), paths relative to the
``arch/`` directory that holds this ``page/`` directory:

    <!--include:page/parts/01.html-->   splice another template file in place
    <!--svg:deps/package_graph.themed.svg-->   inline one SVG
    <!--md:turn/data/errors.md-->   inline a Markdown file (headings, tables, paragraphs)
    <!--txt:turn/data/base/calltree.txt-->   inline a text file, escaped, in <pre><code>

Every inlined SVG gets a per-figure id prefix (ids, url(#…), href="#…" and
aria-labelledby are rewritten together). The build fails on a duplicate id,
on a literal fill="white"/fill="black"/stroke="black"/font-family= left in an
inlined SVG, on a missing marker target, or when the page exceeds 8 MB.

Usage:
    python page/build.py [--out page/tau-as-built.html]
"""

import argparse
import html
import re
import sys
from pathlib import Path

PAGE = Path(__file__).resolve().parent
ARCH = PAGE.parent
LIMIT = 8 * 1024 * 1024
MARKER = re.compile(r"<!--(include|svg|md|txt):([^>]+?)-->")
FORBIDDEN = re.compile(r'fill="white"|fill="black"|stroke="black"|font-family=')
ID_ATTR = re.compile(r'\bid="([^"]+)"')


class BuildError(Exception):
    """A check failed; the message names the file and the fault."""


def read_target(rel: str) -> str:
    """Return the text of a marker target, failing if it does not exist."""
    path = (ARCH / rel.strip()).resolve()
    if not path.is_file():
        raise BuildError(f"marker target missing: {rel}")
    return path.read_text(encoding="utf-8")


def strip_prolog(svg: str) -> str:
    """Drop an XML declaration, DOCTYPE and leading comments before <svg."""
    start = svg.find("<svg")
    if start < 0:
        raise BuildError("no <svg element")
    return svg[start:].strip()


def prefix_ids(svg: str, prefix: str) -> str:
    """Rewrite every id in one SVG and every reference to it with ``prefix``."""
    ids = set(ID_ATTR.findall(svg))
    if not ids:
        return svg

    def sub_id(m: re.Match[str]) -> str:
        """Prefix one id attribute."""
        return f'id="{prefix}{m.group(1)}"'

    svg = ID_ATTR.sub(sub_id, svg)
    for old in sorted(ids, key=len, reverse=True):
        esc = re.escape(old)
        svg = re.sub(rf"url\(#{esc}\)", f"url(#{prefix}{old})", svg)
        svg = re.sub(rf'href="#{esc}"', f'href="#{prefix}{old}"', svg)

    def sub_labelled(m: re.Match[str]) -> str:
        """Prefix each token of an aria-labelledby list."""
        toks = [f"{prefix}{t}" if t in ids else t for t in m.group(1).split()]
        return f'aria-labelledby="{" ".join(toks)}"'

    return re.sub(r'aria-labelledby="([^"]*)"', sub_labelled, svg)


def inline_markdown(md: str) -> str:
    """Convert headings, pipe tables and paragraphs of a Markdown file to HTML."""

    def inline(text: str) -> str:
        """Escape text and turn `code` spans into <code>."""
        parts = text.split("`")
        out = []
        for i, part in enumerate(parts):
            esc = html.escape(part, quote=False)
            out.append(f"<code>{esc}</code>" if i % 2 else esc)
        return "".join(out)

    def cells(row: str) -> list[str]:
        """Split one pipe-table row into cell texts."""
        row = row.strip().strip("|")
        return [c.strip() for c in re.split(r"(?<!\\)\|", row)]

    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            out.append(f"<h4>{inline(line.lstrip('#').strip())}</h4>" if level > 1 else "")
            i += 1
        elif line.startswith("|"):
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                block.append(lines[i])
                i += 1
            head, body = cells(block[0]), [cells(r) for r in block[2:]]
            th = "".join(f"<th>{inline(c)}</th>" for c in head)
            rows = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body)
            out.append(f'<div class="tablewrap"><table><thead><tr>{th}</tr></thead><tbody>{rows}</tbody></table></div>')
        elif line.strip():
            para = []
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", "|")):
                para.append(lines[i].strip())
                i += 1
            out.append(f"<p>{inline(' '.join(para))}</p>")
        else:
            i += 1
    return "\n".join(x for x in out if x)


def expand(text: str, state: dict[str, int], depth: int = 0) -> str:
    """Replace every marker in ``text``, recursing into included templates."""
    if depth > 4:
        raise BuildError("include nesting deeper than 4")

    def sub(m: re.Match[str]) -> str:
        """Expand one marker."""
        kind, rel = m.group(1), m.group(2).strip()
        body = read_target(rel)
        if kind == "include":
            return expand(body, state, depth + 1)
        if kind == "md":
            return inline_markdown(body)
        if kind == "txt":
            return f"<pre><code>{html.escape(body, quote=False)}</code></pre>"
        state["n"] += 1
        svg = strip_prolog(body)
        bad = FORBIDDEN.findall(svg)
        if bad:
            raise BuildError(f"{rel}: literal presentation attributes left: {sorted(set(bad))}")
        return prefix_ids(svg, f"f{state['n']}-")

    return MARKER.sub(sub, text)


def check_ids(page: str) -> None:
    """Fail on any id used twice in the assembled page."""
    seen: dict[str, int] = {}
    for found in ID_ATTR.findall(page):
        seen[found] = seen.get(found, 0) + 1
    dup = sorted(k for k, v in seen.items() if v > 1)
    if dup:
        raise BuildError(f"duplicate ids: {dup[:20]}{' …' if len(dup) > 20 else ''}")


def main() -> int:
    """Build the page and report its size and figure count."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=PAGE / "tau-as-built.html")
    args = ap.parse_args()
    state = {"n": 0}
    try:
        head = (PAGE / "head.html").read_text(encoding="utf-8")
        body = expand((PAGE / "body.html").read_text(encoding="utf-8"), state)
        page = f'{head}\n<div class="ffwf">\n{body}\n</div>\n'
        left = MARKER.findall(page)
        if left:
            raise BuildError(f"unexpanded markers: {left[:5]}")
        check_ids(page)
        size = len(page.encode("utf-8"))
        if size > LIMIT:
            raise BuildError(f"page is {size} bytes, over {LIMIT}")
    except BuildError as err:
        print(f"build.py: {err}", file=sys.stderr)
        return 1
    args.out.write_text(page, encoding="utf-8")
    print(f"{args.out}: {size} bytes, {state['n']} figures inlined, checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
