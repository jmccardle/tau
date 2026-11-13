"""Export a walk as Markdown (D18 section 8).

`to_markdown(nodes, thinking=False, of=None)` is pure over the nodes: no session, no clock. The
output is deterministic, so two exports can be diffed. With `of` (the session's `SessionInfo`) it
adds the header a `tau export` writes: the title, the session's id and `cwd`, the node exported and
`forked_from`.

The layout, node by node: `System` under `## System` inside `<details>`; `User` as `## User`
(`## User (by gamemaster)` when an extension wrote it); `Assistant` with its text parts as written,
each call a line naming the tool and call id and then a fenced block of its `args_raw`, and a stop
other than `End` or `Tools` as an italic line; `Results` as one fenced block per outcome, headed
with the tool's name and `ok`, `error` or `interrupted`; `Config` as one italic line. `Custom`
nodes never reach a model and are left out; thinking is a blockquote and only with `thinking`.
Each fence is one backtick longer than the longest run of backticks in its content, so a tool's
output cannot end its own block.
"""

import re
from collections.abc import Iterable

from tau._core import Author, Body, Node, Outcome, Part, Stop
from tau._sessions import SessionInfo

_TICKS = re.compile(r"`+")
_STOPS = (
    (Stop.Length, "length"),
    (Stop.Interrupted, "interrupted"),
    (Stop.Error, "error"),
    (Stop.Refused, "refused"),
)


def _fence(text: str, language: str = "") -> str:
    longest = max((len(run) for run in _TICKS.findall(text)), default=0)
    ticks = "`" * max(3, longest + 1)
    body = f"{text}\n" if text else ""
    return f"{ticks}{language}\n{body}{ticks}"


def _by(author: Author) -> str:
    match author:
        case Author.Extension(name=name):
            return f" (by {name})"
        case _:
            return ""


def _quote(text: str) -> str:
    return "\n".join(f"> {line}".rstrip() for line in text.split("\n"))


def _assistant(body: Body.Assistant, thinking: bool) -> list[str]:
    blocks = ["## Assistant"]
    for part in body.parts:
        match part:
            case Part.Text(text=text):
                blocks.append(text)
            case Part.Thinking(text=text) if thinking:
                blocks.append(_quote(f"**Thinking**\n\n{text}"))
            case Part.Call(id=call, tool=tool, args_raw=args):
                blocks += [f"**Call:** {tool} ({call})", _fence(args, "json")]
            case _:
                pass
    for stop, word in _STOPS:
        if body.stop == stop:
            blocks.append(f"*Stopped: {word}*")
    return blocks


def _outcome(call: str, outcome: Outcome, author: Author, tools: dict[str, str]) -> list[str]:
    if call not in tools:
        raise LookupError(f"a result for call {call!r}, which no Assistant node here made")
    match outcome:
        case Outcome.Ok(value=text):
            status = "ok"
        case Outcome.Err(error=text):
            status = "error"
        case _:
            status, text = "interrupted", ""
    return [f"**{tools[call]}** {status}{_by(author)}", _fence(text)]


def _node(node: Node, thinking: bool, tools: dict[str, str]) -> list[str]:
    match node.body:
        case Body.System(text=text):
            return [
                "<details>\n<summary>System</summary>",
                "## System",
                _fence(text),
                "</details>",
            ]
        case Body.User(text=text):
            return [f"## User{_by(node.by)}", text]
        case Body.Assistant() as body:
            tools.update({p.id: p.tool for p in body.parts if isinstance(p, Part.Call)})
            return _assistant(body, thinking)
        case Body.Results(outcomes=outcomes):
            blocks = ["## Results"]
            for call, outcome, author in outcomes:
                blocks += _outcome(call, outcome, author, tools)
            return blocks
        case Body.Config(config=config):
            return ["*Config: " + " ".join(config.data.split()) + "*"]
        case _:
            return []


def to_markdown(
    nodes: Iterable[Node], *, thinking: bool = False, of: SessionInfo | None = None
) -> str:
    """`nodes` (a walk, root first) as Markdown. With `of`, the session's row, the document
    starts with the header `tau export` writes: the title, `of`'s id and `cwd`, the node exported
    (the walk's last) and `forked_from`. `LookupError` for a `Results` node whose call no earlier
    `Assistant` node in `nodes` made."""
    walk = list(nodes)
    tools: dict[str, str] = {}
    body = "\n\n".join(block for node in walk for block in _node(node, thinking, tools)) + "\n"
    if of is None:
        return body
    header = [f"# {of.title}", f"- id: {of.id}", f"- cwd: {of.cwd}", f"- node: {walk[-1].id}"]
    if of.forked_from is not None:
        header.append(f"- forked from: {of.forked_from.session} at node {of.forked_from.node}")
    return header[0] + "\n\n" + "\n".join(header[1:]) + "\n\n" + body
