"""HTML to readable Markdown text, for the `fetch` tool (D24 section 9), with the standard library
only: a page is parsed into a small tree (`html.parser`), its main content is chosen, and the tree
is written as Markdown: headings, paragraphs, lists, block quotes, fenced code, tables as rows,
links as `[text](absolute-url)` and emphasis. Scripts, styles, navigation, footers, sidebars,
forms' controls, hidden elements (the `hidden` attribute, `aria-hidden`, and an inline
`display: none`) and anything that is not text are left out.

It does not run JavaScript: a page that is an empty shell until a script fills it gives no text,
and `readable` says so (`NoText`) rather than answering nothing.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


MAX_DEPTH = 80
"""Elements nested deeper than this are flattened into their parent: the tree stays shallow
enough to walk by recursion, whatever a page sends."""

_VOID = _words("area base br col embed hr img input link meta param source track wbr")  # no end tag
_SKIPPED = _words(
    "script style noscript template svg iframe canvas object embed head button select textarea "
    "input nav footer aside dialog audio video map"
)
_BLOCKS = _words(
    "address article body center details dd div dl dt fieldset figcaption figure form header "
    "html main p section summary"
)
_HEADINGS = {f"h{n}": n for n in range(1, 7)}
_SEPARATING = (
    _BLOCKS | _HEADINGS.keys() | _words("blockquote caption dt li ol pre table td th tr ul")
)  # inside a line of text these read as a space
_EMPHASIS = {"strong": "**", "b": "**", "em": "*", "i": "*", "del": "~~", "s": "~~"}
_SPACE = re.compile(r"\s+")
_DISPLAY_NONE = re.compile(r"(^|;)\s*display\s*:\s*none\b", re.I)
_LINK_SCHEMES = ("http", "https", "mailto")
# A tag that starts closes the open one of these before it, in the nearest enclosing list or table.
_CLOSES: dict[str, frozenset[str]] = {
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "tr": frozenset({"tr", "td", "th"}),
    "td": frozenset({"td", "th"}),
    "th": frozenset({"td", "th"}),
}
_CONTAINERS = frozenset({"ul", "ol", "dl", "table"})


class NoText(ValueError):
    """The page has no text a reader would see (a page built by script, or an empty one)."""


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=lambda: {})
    children: "list[_Node | str]" = field(default_factory=lambda: [])


class _Builder(HTMLParser):
    """The tree of a page. End tags close what they name (and what is open inside it); a tag that
    cannot nest in the open one (`li` in `li`, `p` in `p`) closes it first."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document")
        self._open = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._close_for(tag)
        node = _Node(tag, {k: v or "" for k, v in attrs})
        self._open[-1].children.append(node)
        if tag not in _VOID and len(self._open) < MAX_DEPTH:
            self._open.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._close_for(tag)
        self._open[-1].children.append(_Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self._open) - 1, 0, -1):
            if self._open[depth].tag == tag:
                del self._open[depth:]
                return

    def handle_data(self, data: str) -> None:
        self._open[-1].children.append(data)

    def _close_for(self, tag: str) -> None:
        if tag == "p" or tag in _HEADINGS or tag in ("ul", "ol", "pre", "table", "div"):
            if self._open[-1].tag == "p":
                self._open.pop()
            return
        closes = _CLOSES.get(tag)
        if closes is None:
            return
        for depth in range(len(self._open) - 1, 0, -1):
            here = self._open[depth].tag
            if here in closes:
                del self._open[depth:]
                return
            if here in _CONTAINERS:
                return


def _walk(node: _Node) -> Iterable[_Node]:
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _walk(child)


def _hidden(node: _Node) -> bool:
    if node.tag in _SKIPPED or "hidden" in node.attrs:
        return True
    if node.attrs.get("aria-hidden") == "true" or _DISPLAY_NONE.search(node.attrs.get("style", "")):
        return True
    if node.attrs.get("role") in ("navigation", "banner", "contentinfo", "complementary"):
        return True
    return "headerlink" in node.attrs.get("class", "").split()


def _text_of(node: _Node) -> str:
    """The text inside `node`, verbatim (`pre`), with `br` as a newline."""
    out: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            out.append(child)
        elif child.tag == "br":
            out.append("\n")
        elif child.tag not in _SKIPPED:
            out.append(_text_of(child))
    return "".join(out)


def _link(node: _Node, base: str) -> str | None:
    """The absolute URL an `a` points to, or `None` for a fragment, a script or a bad one."""
    href = node.attrs.get("href", "").strip()
    if not href or href.startswith("#"):
        return None
    try:
        url = urljoin(base, href)
        return url if urlsplit(url).scheme in _LINK_SCHEMES else None
    except ValueError:
        return None


def _inline(node: _Node | str, base: str) -> str:
    """`node` as one run of inline Markdown: blocks inside it read as spaces."""
    if isinstance(node, str):
        return _SPACE.sub(" ", node)
    if _hidden(node):
        return ""
    if node.tag == "br":
        return " "
    if node.tag == "img":
        alt = _SPACE.sub(" ", node.attrs.get("alt", "")).strip()
        src = urljoin(base, node.attrs.get("src", ""))
        return f"![{alt}]({src})" if alt and urlsplit(src).scheme in ("http", "https") else ""
    inner = "".join(_inline(c, base) for c in node.children)
    if node.tag in _SEPARATING:
        return f" {inner.strip()} "
    return _decorated(node, inner, base)


def _flat(node: _Node, base: str) -> str:
    """`_inline` of `node` as one trimmed line."""
    return _SPACE.sub(" ", _inline(node, base)).strip()


def _decorated(node: _Node, inner: str, base: str) -> str:
    """`inner` as the inline element `node` writes it: emphasis, code, a link."""
    text = inner.strip()
    if not text:
        return inner
    lead = " " if inner[:1] == " " else ""
    trail = " " if inner[-1:] == " " else ""
    mark = _EMPHASIS.get(node.tag)
    if mark is not None:
        return f"{lead}{mark}{text}{mark}{trail}"
    if node.tag == "code":
        return f"{lead}`{text}`{trail}"
    if node.tag == "a" and (url := _link(node, base)) is not None:
        return f"{lead}[{text}]({url}){trail}"
    return inner


@dataclass
class _Prefix:
    first: str  # what starts the first line written inside the quote or item
    rest: str  # what starts every later one


class _Writer:
    """The lines of the Markdown, with the prefixes of the quotes and list items they are in."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._line = ""
        self._prefixes: list[_Prefix] = []
        self._gap = False

    def text(self, text: str) -> None:
        if self._line == "" or self._line.endswith(" "):
            text = text.lstrip()
        self._line += text

    def flush(self) -> None:
        """End the line being written, if it says anything."""
        line, self._line = self._line.rstrip(), ""
        if line:
            self.put(line)

    def put(self, line: str) -> None:
        """A whole line with the prefixes of the quotes and items it is in; a blank line first if
        a block came before it."""
        if self._gap and self.lines and self.lines[-1] != "":
            self.lines.append("")
        self._gap = False
        self.lines.append("".join(f.first for f in self._prefixes) + line)
        for prefix in self._prefixes:
            prefix.first = prefix.rest

    def block(self) -> None:
        """A block ends: what follows is set apart by a blank line."""
        self.flush()
        self._gap = True

    def set_apart(self) -> None:
        """`block`, except inside a quote or an item, where a nested list follows its line
        directly."""
        if self._prefixes:
            self.flush()
        else:
            self.block()

    def push(self, first: str, rest: str) -> None:
        self._prefixes.append(_Prefix(first, rest))

    def pop(self) -> None:
        self.flush()
        self._prefixes.pop()


def _render(node: _Node, out: _Writer, base: str) -> None:
    """Write the children of `node` into `out`."""
    for child in node.children:
        if isinstance(child, str):
            out.text(_SPACE.sub(" ", child))
        elif not _hidden(child):
            _element(child, out, base)


_INDENTED = {"blockquote": ("> ", "> "), "dt": ("", ""), "dd": (": ", "  ")}


def _element(node: _Node, out: _Writer, base: str) -> None:
    tag = node.tag
    if tag in _HEADINGS:
        out.block()
        text = _flat(node, base)
        if text:
            out.put(f"{'#' * _HEADINGS[tag]} {text}")
        out.block()
    elif tag == "pre":
        _code_block(node, out)
    elif tag in ("ul", "ol"):
        _list(node, out, base)
    elif tag == "table":
        _table(node, out, base)
    elif tag in _INDENTED:
        _indented(node, out, base)
    elif tag in ("hr", "br"):
        out.flush()
        if tag == "hr":
            out.block()
            out.put("---")
            out.block()
    elif tag in _BLOCKS or tag in ("li", "tr", "caption"):
        out.block()
        _render(node, out, base)
        out.block()
    else:
        _inline_here(node, out, base)


def _indented(node: _Node, out: _Writer, base: str) -> None:
    """A quote, or a term and what it says (on adjacent lines): its lines start with a prefix."""
    if node.tag == "dd":
        out.flush()
    else:
        out.block()
    out.push(*_INDENTED[node.tag])
    _render(node, out, base)
    out.pop()
    if node.tag == "blockquote":
        out.block()


def _inline_here(node: _Node, out: _Writer, base: str) -> None:
    """An inline element in the flow of a block: its inline text, or, when a block sits inside it
    (a link around a card), its children in place."""
    if any(
        isinstance(c, _Node) and (c.tag in _BLOCKS or c.tag in _HEADINGS) for c in node.children
    ):
        _render(node, out, base)
    else:
        out.text(_SPACE.sub(" ", _inline(node, base)))


def _code_block(node: _Node, out: _Writer) -> None:
    out.block()
    language = ""
    for candidate in (node, *(c for c in node.children if isinstance(c, _Node))):
        for name in candidate.attrs.get("class", "").split():
            if name.startswith("language-"):
                language = name.removeprefix("language-")
    body = _text_of(node).strip("\n")
    fence = "```"
    while fence in body:
        fence += "`"
    out.put(fence + language)
    for line in body.split("\n"):
        out.put(line.rstrip())
    out.put(fence)
    out.block()


def _list(node: _Node, out: _Writer, base: str) -> None:
    out.set_apart()
    number = _start_of(node)
    for item in node.children:
        if isinstance(item, str):
            if item.strip():
                out.text(_SPACE.sub(" ", item))
            continue
        if item.tag != "li":
            _element(item, out, base)
            continue
        marker = f"{number}. " if node.tag == "ol" else "- "
        number += 1
        out.flush()
        out.push(marker, " " * len(marker))
        _render(item, out, base)
        out.pop()
    out.set_apart()


def _start_of(node: _Node) -> int:
    try:
        return int(node.attrs.get("start", "1"))
    except ValueError:
        return 1


def _rows(node: _Node) -> Iterable[_Node]:
    """The rows of a table: its `tr`, however the sections nest them; cells with no `tr` around
    them (an author left it out) are one row."""
    loose: list[_Node | str] = []
    for child in node.children:
        if not isinstance(child, _Node) or _hidden(child):
            continue
        if child.tag in ("td", "th"):
            loose.append(child)
        elif child.tag == "tr":
            yield child
        elif child.tag != "table":
            yield from _rows(child)
    if loose:
        yield _Node("tr", {}, loose)


def _table(node: _Node, out: _Writer, base: str) -> None:
    out.block()
    for row in _rows(node):
        cells = [c for c in row.children if isinstance(c, _Node) and c.tag in ("td", "th")]
        if not cells:
            continue
        texts = [_flat(c, base).replace("|", "\\|") for c in cells]
        if not any(texts):  # a picture with no description, a spacer
            continue
        out.put("| " + " | ".join(texts) + " |")
        if all(c.tag == "th" for c in cells):
            out.put("|" + " --- |" * len(cells))
    out.block()


def _find(root: _Node, wanted: Callable[[_Node], bool]) -> list[_Node]:
    return [n for n in _walk(root) if wanted(n)]


def _main(root: _Node) -> _Node:
    """The node holding the page's content: its `main` (or `role=main`), else its only
    `article`, else its `body`, else the whole document."""
    mains = _find(root, lambda n: n.tag == "main" or n.attrs.get("role") == "main")
    articles = _find(root, lambda n: n.tag == "article")
    bodies = _find(root, lambda n: n.tag == "body")
    if mains:
        return mains[0]
    if len(articles) == 1:
        return articles[0]
    return bodies[0] if bodies else root


def readable(html: str, base_url: str) -> tuple[str, str]:
    """The `(title, markdown)` of an HTML document whose address is `base_url` (which links are
    made absolute against). `NoText` if the page has no text to read."""
    builder = _Builder()
    builder.feed(html)
    builder.close()
    titles = _find(builder.root, lambda n: n.tag == "title")
    title = _SPACE.sub(" ", _text_of(titles[0])).strip() if titles else ""
    out = _Writer()
    _render(_main(builder.root), out, base_url)
    out.block()
    markdown = "\n".join(out.lines).strip()
    if not markdown:
        raise NoText(
            "it has no text to read; it may be built by JavaScript, which fetch does not run"
        )
    return title, markdown
