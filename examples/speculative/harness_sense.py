"""Speculative: `harness_sense`, the awareness an agent needs to use a harness and change it.

Written from the agent's side: a model handed this harness knows its tools and nothing else. It
cannot tell where in the tree it is, what it is offered, what it is costing, why a hook stopped
it, or whether the extension it just edited loads. The tools carry the names a model would
guess: `whereami`, `tree`, `context_budget`, `hook_errors`, `note`, `probe`, `check_extension`.

The app wires it once, after building the driver (an extension is built before the driver it
lives in, so it cannot be given one):

    sense = HarnessSense()
    extensions = [sense, ...]
    driver = Driver(session, providers, catalog, tools=tools, extensions=extensions)
    sense.attach(driver, extensions, tools)
"""

import asyncio
import importlib.util
import inspect
import json
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from tau import (
    CURSOR_POINTS,
    TOOL_POINTS,
    Author,
    Body,
    CallDraft,
    Cursor,
    CursorView,
    Driver,
    Ending,
    Labels,
    Node,
    NodeHandle,
    Outcome,
    Part,
    Session,
    Subject,
    ToolError,
    model_config,
    read_models,
)

NOTE = "harness_sense.note"
STOPPED = "harness_sense.stopped"
TRUNCATED = re.compile(r"\[truncated[^\]]*\]")
TREE_LINES = 60


@dataclass(frozen=True)
class Options:
    """`harness_sense`'s options (`/config harness_sense`)."""

    # WISH: the window is a fact about the model (`ModelConfig.context_window`), not a setting.
    context_window: Annotated[int, "the model's window in tokens (0: not known)"] = 0
    probe_timeout: Annotated[float, "seconds a probe waits for its model"] = 120.0


def said(parts: Sequence[Part]) -> str:
    return "".join(p.text for p in parts if isinstance(p, Part.Text))


def called(parts: Sequence[Part]) -> list[Part.Call]:
    return [p for p in parts if isinstance(p, Part.Call)]


def gist(node: Node, width: int = 48) -> str:
    """One line for a node: its id, kind and the start of what it says."""
    body = node.body
    if isinstance(body, Body.User | Body.System):
        what = f"{type(body).__name__} {body.text[:width]!r}"
    elif isinstance(body, Body.Assistant):
        tools = [c.tool for c in called(body.parts)]
        what = f"Assistant {said(body.parts)[:width]!r}" + (f" calls {tools}" if tools else "")
    elif isinstance(body, Body.Results):
        what = "Results " + ",".join(type(o).__name__ for _, o, _ in body.outcomes)
    elif isinstance(body, Body.Config):
        what = f"Config {body.config.data[:width]}"
    elif isinstance(body, Body.Custom):
        what = f"Custom {body.kind} {body.data[:width]!r}"
    else:
        what = repr(body)[:width]
    return f"{node.id:>3} {what}"


def describe_extension(ext: object) -> str:
    """An extension in one line: name, source file, what it adds."""
    source = inspect.getsourcefile(ext if inspect.ismodule(ext) else type(ext))
    hooks = [p for p in (*CURSOR_POINTS, *TOOL_POINTS) if hasattr(ext, p)]
    names = {a: [f.__name__ for f in getattr(ext, a, ())] for a in ("tools", "commands")}
    options = hasattr(ext, "options")
    return (
        f"extension {getattr(ext, 'name', '?')} ({source}): {names}, hooks {hooks}, "
        f"options {options}"
    )


class HarnessSense:
    name = "harness_sense"
    options = Options

    def __init__(self) -> None:
        self._driver: Driver | None = None
        self._extensions: list[object] = []
        self._tools: list[object] = []
        self._probes: set[int] = set()
        self.tools = [
            self.whereami,
            self.tree,
            self.context_budget,
            self.hook_errors,
            self.note,
            self.probe,
            self.check_extension,
        ]

    def attach(self, driver: Driver, extensions: list[object], tools: list[object]) -> None:
        """Give it what a tool cannot reach: the driver, and the lists the driver was built from."""
        self._driver, self._extensions, self._tools = driver, extensions, tools

    # WISH: `attach` should not exist. A handle that knew its harness would be enough:
    #     node.extensions()     # name, source file, tools, commands, hook points, options
    #     node.tools()          # what this sample was offered
    #     node.view(cursor)     # a CursorView: questions, ending and hook_error, as a client has
    #     node.model()          # the resolved ModelConfig, with its context window

    def _live(self) -> Driver:
        if self._driver is None:
            raise ToolError("harness_sense: the app did not call attach(driver, extensions, tools)")
        return self._driver

    async def _snapshot(self) -> tuple[dict[int, CursorView], dict[str, int]]:
        """Every cursor's state and the labels, as a client connecting now is told them."""
        views: dict[int, CursorView] = {}
        async with self._live().client("harness_sense:snapshot") as client:
            async for event in client:
                if isinstance(event, CursorView):
                    views[event.cursor] = event
                elif isinstance(event, Labels):
                    return views, dict(event.labels)
        raise ToolError("the driver stopped")

    # Where am I?

    async def whereami(self, node: NodeHandle) -> str:
        """Where you are in this session and what this harness gives you: your cursor and node,
        the branch around you, the model, the extensions (with their files, options and tools),
        other cursors, and notes you left earlier. Call this first in a new session."""
        views, labels = await self._snapshot()
        walk, here, mine = node.walk(), node.node, [node.cursor_id]
        siblings = (
            [c for c in node.tree.children(here.parent) if c != here.id] if here.parent else []
        )
        model = views[node.cursor_id].model or "?"
        called_by = {c: [n for n, at in labels.items() if at == c] for c in views}
        lines = [
            f"cursor {mine} at node {here.id}, depth {len(walk) - 1}, labels "
            f"{[n for c in mine for n in called_by[c]]}, model {model}",
            f"siblings of this node {siblings}, children {node.tree.children(here.id)}",
        ]
        lines += [
            f"other cursor {c} {called_by[c]} at node {v.at}, {'busy' if v.busy else 'at rest'}"
            for c, v in views.items()
            if c not in mine
        ]
        async with self._live().client("harness_sense:options") as client:
            for ext in self._extensions:
                lines.append(describe_extension(ext))
                if hasattr(ext, "options") and mine:
                    form = await client.options(mine[0], str(getattr(ext, "name")))  # noqa: B009
                    values = {k: v.get("default") for k, v in form.schema["properties"].items()}  # type: ignore[attr-defined]
                    lines.append(f"  options now: {values}")
        offered = [*self._tools, *(t for e in self._extensions for t in getattr(e, "tools", ()))]
        lines.append(
            "tools offered: " + ", ".join(getattr(t, "name", None) or t.__name__ for t in offered)
        )
        return "\n".join(lines + [f"note: {text}" for _, text in self._side(node, NOTE)])

    @staticmethod
    def _side(node: NodeHandle, kind: str) -> list[tuple[int, str]]:
        """What this extension left as side nodes (`kind`) hanging off the branch's walk."""
        found = (node.tree.node(c) for n in node.walk() for c in node.tree.children(n.id))
        return [
            (n.id, n.body.data)
            for n in found
            if isinstance(n.body, Body.Custom) and n.body.kind == kind
        ]

    def tree(self, node: NodeHandle, depth: Annotated[int, "levels above and below"] = 3) -> str:
        """The session tree around you: `depth` levels up (further, to reach the last branch
        point on your branch) and `depth * 2` down, a node per line. Marks cursors, and you."""
        walk = node.walk()
        at: dict[int, list[int]] = {}
        for c in node.cursors():
            at.setdefault(node.cursor(c).at, []).append(c)
        up = max(0, len(walk) - 1 - depth)

        def branches(n: Node) -> list[int]:  # side nodes (notes, stops) are not branch points
            return [
                c
                for c in node.tree.children(n.id)
                if not isinstance(node.tree.node(c).body, Body.Custom)
            ]

        splits = [i for i, n in enumerate(walk[:-1]) if len(branches(n)) > 1]
        top = min([i for i in splits if i >= up - 2 * depth][-1:], default=up)
        lines = [f"... {top} nodes above"] if top else []

        def show(id: int, level: int) -> None:
            here = "  <- you" if id == node.id else ""
            cursors = "".join(f" @{c}" for c in at.get(id, ()))
            lines.append(f"{'  ' * level}{gist(node.tree.node(id))}{here}{cursors}")
            children = node.tree.children(id)
            if len(lines) >= TREE_LINES or (level >= 3 * depth and children):
                lines.append(f"{'  ' * (level + 1)}... {len(children)} below")
                return
            for child in children:
                show(child, level + 1)

        show(walk[top].id, 0)
        return "\n".join(lines)

    # What is it costing me?

    def context_budget(self, node: NodeHandle) -> str:
        """How much of the model's window this branch uses, which nodes are biggest, and which
        tool outputs were cut short (and where the whole output is). Check before a big read."""
        walk = node.walk()
        size = {n.id: len(repr(n.body)) for n in walk}
        guess = sum(size.values()) // 4
        lines = [f"estimated {guess} tokens in {len(walk)} nodes (characters / 4)"]
        used = guess
        for i in reversed(range(len(walk))):
            body = walk[i].body
            usage = (
                json.loads(body.meta or "{}").get("usage")
                if isinstance(body, Body.Assistant)
                else None
            )
            if usage:
                since = sum(size[n.id] for n in walk[i + 1 :]) // 4
                used = usage["input"] + since
                lines.append(
                    f"reported {usage['input']} prompt tokens ({usage['cached']} cached) at node "
                    f"{walk[i].id}, about {since} more since"
                )
                break
        window = node.options().context_window
        lines.append(
            f"window {window}: {used / window:.0%} used"
            if window
            else "window unknown: /config harness_sense context_window=N"
        )
        biggest = sorted(walk, key=lambda n: size[n.id], reverse=True)[:3]
        lines.append(
            "biggest: " + "; ".join(f"{gist(n, 20)} ({size[n.id] // 4} tokens)" for n in biggest)
        )
        for n in walk:
            for call, outcome, _ in n.body.outcomes if isinstance(n.body, Body.Results) else ():
                cut = TRUNCATED.search(outcome.value) if isinstance(outcome, Outcome.Ok) else None
                if cut:
                    lines.append(f"cut: node {n.id} call {call}: {cut.group()}")
        return "\n".join(lines)

    # Why did it stop?

    async def post_step(self, cursor: Cursor, subject: Subject) -> str | None:
        """Write down each step that ended in a halt or a failure, as a side node: the next step
        erases it from the `CursorView`, and a model can only ask once that step has begun."""
        ending = subject.ending
        if isinstance(ending, Ending.Failed | Ending.Halted):
            failure = (await self._snapshot())[0][cursor.id].hook_error
            hook = f"{failure.point} of {failure.extension}" if failure else "no hook error"
            why = ending.error if isinstance(ending, Ending.Failed) else ending.reason
            at = cursor.cursor(cursor.id).at
            cursor.append(
                at, Body.Custom(STOPPED, f"{type(ending).__name__.lower()}: {why} ({hook})")
            )
        return None

    def hook_errors(self, node: NodeHandle) -> str:
        """Why steps stopped or tools were refused on your branch: halts and hook failures (which
        hook), and calls an extension answered with an error."""
        lines = [f"node {id}: {text}" for id, text in self._side(node, STOPPED)]
        names = {str(getattr(e, "name", "")) for e in self._extensions}
        for n in node.walk():
            for call, outcome, by in n.body.outcomes if isinstance(n.body, Body.Results) else ():
                if (
                    isinstance(outcome, Outcome.Err)
                    and isinstance(by, Author.Extension)
                    and by.name in names
                ):
                    lines.append(f"node {n.id} call {call}: {outcome.error} (by {by.name})")
        return "\n".join(lines) or "no hook has stopped a step or refused a call"

    # Leave a note for the next one

    def note(self, node: NodeHandle, text: str) -> str:
        """Leave a note for whoever works on this branch next (you, later); `whereami` shows it."""
        return f"noted as node {node.append(node.id, Body.Custom(NOTE, text))}"

    # Try it

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        """A probe branch runs no tools: it reports what the model tried to call."""
        if any(node.cursor(c).at == node.id for c in self._probes):
            return "probe: tools do not run on a probe branch"
        return None

    async def probe(self, node: NodeHandle, prompt: str, cursor: int | None = None) -> str:
        """Try a prompt on a scratch branch from just before your current reply and get the
        model's answer, without changing your own branch. Tools do not run there: you are told
        which it would call. Pass an earlier probe's `cursor` to continue that conversation."""
        driver = self._live()
        if cursor is not None and cursor not in self._probes:
            raise ToolError(f"cursor {cursor} is not a probe of yours; leave `cursor` out")
        async with driver.client("harness_sense:probe") as client:
            if cursor is None:
                cursor = await client.open(node.node.parent or node.id)
                self._probes.add(cursor)
                node.set_label(f"probe-{cursor}", cursor)
            await client.input(cursor, prompt)
            await driver.rest(cursor)  # the wake that opening gave it
            await client.step(cursor)  # one step: the reply, and its tool calls refused
            try:
                async with asyncio.timeout(node.options().probe_timeout):
                    await driver.rest(cursor)
            except TimeoutError:
                await client.abort(cursor)
                raise ToolError(f"probe {cursor}: no reply in time; aborted") from None
            walk = await client.walk(node.cursor(cursor).at)
        reply = next((n.body for n in reversed(walk) if isinstance(n.body, Body.Assistant)), None)
        if reply is None or not isinstance(walk[-1].body, Body.Assistant | Body.Results):
            raise ToolError(f"probe {cursor}: no reply; see hook_errors")
        tried = [f"{c.tool}({c.args_raw})" for c in called(reply.parts)]
        return f"probe {cursor} replied: {said(reply.parts)}" + (
            f"\nit would call: {', '.join(tried)}" if tried else ""
        )

    # Is my edit any good?

    def check_extension(self, path: Path) -> str:
        """Load an extension file you edited and check it the way a driver does, without
        running it: its tools, commands, options and hooks, or what is wrong. It does not replace
        the running one."""
        spec = importlib.util.spec_from_file_location(f"checked_{path.stem}", path)
        if spec is None or spec.loader is None:
            raise ToolError(f"{path} is not a Python file")
        try:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as scratch:
                session = Session.create(model_config("m"), "sys", 0, Path(scratch) / "s")
                try:
                    catalog = read_models(cli={"models": {"m": {"model": "m", "provider": "m"}}})
                    Driver(session, {}, catalog, extensions=[module])  # pyright: ignore[reportArgumentType]
                finally:
                    session.close()
        except Exception as e:  # what is wrong with the file is the answer
            raise ToolError(f"{type(e).__name__}: {e}") from e
        return "ok, not loaded: " + describe_extension(module)

    # WISH: `reload`, the tool that closes the loop (edit a file, then watch it work).
    #
    # `Driver.reload(extensions)` exists now, but it is refused while any effect is out, and a tool
    # call is an effect: a tool cannot reload the driver it runs in. The app that owns the driver
    # reloads between steps (`rest`, then `reload`); this tool can only `check_extension`.


extensions = [HarnessSense()]  # what `chat.py --extension` adds
