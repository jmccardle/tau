"""`Tokens`: the built-in extension that offers the counter (D26 section 6).

It learns what each model's tokens cost from the proved spans its replies report (the driver's
`observe` call), keeps what it learned in a **calibration record** per model under
`$TAU_HOME/calibration/`, and counts with it what no report covers yet: a message just typed, a
tool result just returned, the nodes of a line not yet sent. Such a count is `heuristic`: a
fitted estimate with a bound, shown with `~`. Everything a report covers stays the report's.

Without the tokenizer wheel (`pip install 'ffwf-tau[tokens]'`, D26 section 7) nothing here is
ever `calibrated` or `rendered`: there is no tokenizer to count text exactly. And a record
answers only once it is **ready** (enough proved spans since the model last changed); until
then every count is the meter's ceiling and `why` says how far along it is. Nothing is fitted
from a shipped table: a model τ has never talked to is counted by its bytes.

Nothing is read until a count is asked for and nothing is written until a reply is observed, so
a session with `load: {tokens: false}` leaves no `calibration` directory.

`TokenCalls` (key `token_calls`, below) is the second built-in here, off by default: a count
call before a sample. A **count call** (D26 section 8) is shown to `Tokens` as an observation of
kind `count_call`: the server's own count of one request. It is kept in memory, not in the
record, and a count of the same walk with the same tools is then that number, `rendered`, as its
`total`. The option `tokenizers` (`auto`, `off`, `require`) says what to do with the tokenizer
wheel (`tau._tokenizer_wheel`).
"""

import hashlib
import importlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from types import ModuleType
from typing import Annotated, ClassVar, Literal, Protocol
from urllib.parse import quote

from tau._compaction import hard_limit
from tau._config import ConfigError, ModelConfig
from tau._core import Body, Calibration, CalibrationError, CalibrationStore, calibration_features
from tau._counts import (
    Basis,
    Count,
    CounterAnswer,
    CountJob,
    Observation,
    ceiling_of,
    image_rule,
)
from tau._events import NoticeLevel
from tau._handles import Cursor, Subject, TreeHandle
from tau._hook_order import GUARD, OBSERVE
from tau._tokenizer_wheel import Mode, discover, mode_of

NAME = "tokens"
INSTALL = "pip install 'ffwf-tau[tokens]'"
"""The command every message about the missing tokenizer wheel prints (D26 decision 2)."""
DIRECTORY = "calibration"
MARKERS = ("model", "system_fingerprint")
"""The drift markers read from a reply's `meta` itself; llama.cpp's are under `meta.server`."""


@dataclass(frozen=True)
class Options:
    """`Tokens`' options (`extensions.tokens`). `tokenizers` is read once, when the extension is
    built: a change takes effect at the next start."""

    tokenizers: Annotated[
        Mode,
        "the tokenizer wheel: auto (use it if installed; warn if broken or stale), off, or "
        "require (a start fails without it)",
    ] = "auto"


class Tokenizers(Protocol):
    """What `Tokens` asks of the tokenizer wheel (`ffwf-tau-tokens`, D26 section 7) when it is
    installed. Nothing in this wheel provides one: D26 T4 adapts `tau_tokens` to it and hands it
    to `Tokens`, and T5 is what counts with it. Without one (`Tokens(home)`), every count this
    extension makes is `heuristic` and its `why` names the install command."""

    @property
    def version(self) -> str:
        """The wheel's version, which must be this wheel's (D26 section 7.2, lockstep)."""
        ...

    def count(self, model: ModelConfig, text: str) -> int | None:
        """The exact number of tokens of `text` by the tokenizer confirmed for `model`, or
        `None` if none is confirmed for it. CPU only, no network."""
        ...


def record_name(model: ModelConfig) -> str:
    """The name of a model's calibration record in the store (D26 section 6.5):
    `<provider>/<the first 16 hex digits of sha256(base_url + "\\n" + model)>.json`. A provider's
    name is percent-encoded where it is not a plain file name."""
    provider = quote(model.provider, safe="") or "_"
    if provider.strip(".") == "":
        provider = quote(provider, safe="").replace(".", "%2E")
    identity = f"{model.base_url or ''}\n{model.model}"
    return f"{provider}/{hashlib.sha256(identity.encode()).hexdigest()[:16]}.json"


@dataclass(frozen=True)
class _Held:
    """What the process knows of one record: it, or why it cannot be used."""

    record: Calibration | None = None
    problem: str | None = None


def describe(model: str, held: Calibration | None, tokenizers: Tokenizers | None) -> str:
    """One line on how `model` is counted (`/tokens`, `tau tokens show`)."""
    extra = "" if tokenizers is not None else f" (no tokenizer: {INSTALL})"
    if held is None:
        return f"{model}: no reply observed yet; byte ceilings for what is not sent yet{extra}"
    seen = f"{held.observations} {'reply' if held.observations == 1 else 'replies'}"
    if not held.ready:
        return (
            f"{model}: learning from {seen} ({held.since_reset} of {held.needed} proved spans "
            f"since it last changed); byte ceilings until then{extra}"
        )
    error = held.error
    assert error is not None  # a ready record has one
    return f"{model}: heuristic ±{error:.0%} for new nodes, from {seen}{extra}"


class Tokens:
    """The built-in extension `tokens` (the module docstring): the counter, and `/tokens`.
    `home` is `TAU_HOME`; `tokenizers` is the tokenizer wheel's adapter when there is one.
    `mode` is the option `tokenizers` (a start reads it from the config): under `auto` and
    `require` the wheel is found when this is built,
    through `importer` (`importlib.import_module`; a test gives its own). `require` with no
    usable wheel raises an `ImportError` naming the command that installs it; `auto` with a
    broken or stale one warns the clients once and does not use it."""

    name = NAME
    order: ClassVar[Mapping[str, int]] = {"post_reply": OBSERVE, "pre_step": OBSERVE}
    """Telling what the observations found is an observer (D28)."""
    options = Options

    @classmethod
    def configured(cls, home: Path, extensions: Mapping[str, Mapping[str, object]]) -> "Tokens":
        """`Tokens` as a start builds it: with the `tokenizers` option the config layers set in
        `extensions` (`ConfigError` for a value that is no mode)."""
        return cls(home, mode=mode_of(extensions))

    def __init__(
        self,
        home: Path,
        tokenizers: Tokenizers | None = None,
        mode: Literal["auto", "off", "require"] = "auto",
        importer: Callable[[str], ModuleType] = importlib.import_module,
    ) -> None:
        self._store = CalibrationStore(home / DIRECTORY)
        self._tokenizers = tokenizers
        self._wheel = discover(mode, version("ffwf-tau"), importer)
        self._mode = mode
        # record name -> the walk, tool digest and count of its newest count call
        self._called: dict[str, tuple[tuple[int, ...], str, int]] = {}
        self._lock = threading.Lock()  # `count` and `observe` run in threads
        self._held: dict[str, _Held] = {}
        self._models: dict[str, str] = {}  # qualified model name -> its record's name
        # what the next `post_reply` tells the clients, each with its level
        self._news: list[tuple[NoticeLevel, str]] = []
        if self._wheel.problem is not None:
            problem = self._wheel.problem
            self._news.append(("warning", f"the tokenizer wheel is not used: {problem}"))
        self._told: set[str] = set()  # problems already told
        self.counter = self
        self.commands = (self.tokens,)

    # The counter

    def count(self, job: CountJob) -> CounterAnswer:
        """A `heuristic` count for each node of `job.unreported` the model's record can count,
        never more than the node's ceiling; none while the record is not ready. `why` says what
        would make the counts stronger."""
        name = record_name(job.resolved)
        with self._lock:
            self._models[job.model] = name
            held = self._read(name)
            called = self._called.get(name)
        total = None
        if called is not None and called[:2] == (tuple(n.id for n in job.walk), job.digest):
            total = Count(called[2], Basis.RENDERED)
        if held.problem is not None:
            return CounterAnswer({}, held.problem, total)
        record = held.record
        if record is None or not record.ready:
            return CounterAnswer({}, describe(job.model, record, self._tokenizers), total)
        nodes: dict[int, Count] = {}
        wanted = set(job.unreported)
        images = image_rule(job.resolved)
        for node in job.walk:
            if node.id not in wanted:
                continue
            if node.derived_from is not None and isinstance(node.body, Body.User):
                continue  # a summary: the meter counts it from its reply's own report
            estimate = record.predict(calibration_features(node.body))
            if estimate is None:
                continue
            most = ceiling_of(node.body, images).upper
            tokens = min(estimate[0], most)
            nodes[node.id] = Count(tokens, Basis.HEURISTIC, min(estimate[1], most - tokens))
        return CounterAnswer(nodes, self._why(job.model), total)

    def _why(self, model: str) -> str | None:
        if self._tokenizers is not None:
            return None
        return f"no tokenizer for {model}: heuristic counts; {INSTALL}"

    def _read(self, name: str) -> _Held:
        """The record named `name` as this process last knew it, read from the store the first
        time. A record that cannot be used is a problem, said in `why` and not raised: counting
        goes on with ceilings."""
        if name not in self._held:
            try:
                self._held[name] = _Held(self._store.read(name))
            except CalibrationError as e:
                self._held[name] = _Held(problem=f"{e}; `tau tokens reset` removes it")
        return self._held[name]

    def observe(self, observation: Observation) -> None:
        """Fold one reply into its model's record on disk (under the file's lock, so another
        process observing the same model loses nothing) and keep the record that results. A
        changed drift marker or a jump in what the model's tokens cost resets the record; the
        clients are told at the next `post_reply`."""
        name = record_name(observation.resolved)
        if observation.kind == "count_call":
            with self._lock:
                self._models[observation.model] = name
                self._called[name] = (
                    observation.walk,
                    observation.tools,
                    observation.usage["input"],
                )
            return
        span = None
        if observation.span is not None:
            rows = [calibration_features(n.body) for n in observation.span.nodes]
            span = ([sum(column) for column in zip(*rows, strict=True)], observation.span.cost)
        key = (
            observation.resolved.provider,
            observation.resolved.base_url,
            observation.resolved.model,
        )
        when = datetime.fromtimestamp(observation.time / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self._models[observation.model] = name
            try:
                record, events = self._store.observe(
                    name, key, dict(observation.markers), span, when
                )
            except CalibrationError as e:
                problem = f"{e}; `tau tokens reset` removes it"
                self._held[name] = _Held(problem=problem)
                if problem not in self._told:
                    self._told.add(problem)
                    self._news.append(("warning", f"{observation.model}: {problem}"))
                return
            self._held[name] = _Held(record)
            self._news.extend(("info", _news(observation.model, event)) for event in events)

    # The extension

    async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
        """Tell the clients what is waiting to be told (a tokenizer wheel not used) before the
        first step goes on."""
        await self.post_reply(cursor, subject)

    async def post_reply(self, cursor: Cursor, subject: Subject) -> str | None:
        """Tell the clients what the last observations found (a model that changed)."""
        with self._lock:
            news, self._news = self._news, []
        for level, text in news:
            cursor.tell(text, level=level)
        return None

    def tokens(self, tree: TreeHandle) -> str:
        """How each model of this session is counted before a request is sent."""
        with self._lock:
            models = dict(self._models)
            lines = [self._line(model, name) for model, name in sorted(models.items())]
        if not lines:
            lines.append("no model has been counted or observed yet")
        if self._wheel.problem is not None:
            lines.append(f"tokenizers: not used: {self._wheel.problem}")
        return "\n".join(lines)

    def _line(self, model: str, name: str) -> str:
        held = self._read(name)
        called = self._called.get(name)
        server = "" if called is None else f"; the server counted its last request: {called[2]}"
        if held.problem is not None:
            return f"{model}: {held.problem}{server}"
        return describe(model, held.record, self._tokenizers) + server


def _news(model: str, event: tuple[str, str, str, str, float]) -> str:
    """What a fold found, as the clients are told it (D26 section 6.3)."""
    kind, name, was, now, by = event
    if kind == "marker":
        return f"{model}: the server's {name} changed ({was} → {now}); recalibrating"
    more = "more" if by > 0 else "fewer"
    return f"{model}: requests now cost {abs(by):.0%} {more} tokens than calibrated; recalibrating"


TOKEN_CALLS = "token_calls"
CALL_ORDER = GUARD // 2
"""Before compaction's `pre_sample` (0), whose hard limit reads what the call counted; after the
guards, so a halted step asks for nothing."""


@dataclass(frozen=True)
class CallOptions:
    """`TokenCalls`' options (`extensions.token_calls`)."""

    when: Annotated[
        Literal["straddle", "always"],
        "straddle: ask the server only when an estimate cannot say whether the request fits; "
        "always: before every sample",
    ] = "straddle"


class TokenCalls:
    """The built-in extension `token_calls` (D26 section 8), off by default (`tau ext enable
    token_calls`).

    Before a sample (`pre_sample`, ahead of compaction's hard limit) it asks the model's
    provider for a count call (`Cursor.count_call`): the server's own count of the request the
    walk makes. The driver shows the answer to the counter (`Tokens`), which gives it as the
    `rendered` total of the walk's request count, so the hard limit reads the server's number
    instead of an estimate. Only some providers count (`OpenAIProvider` on llama.cpp:
    `/apply-template` and `/tokenize`); elsewhere a call answers nothing. A failed call is told
    to the clients and the sample goes on with the counter's own count. It needs a counter: the
    driver refuses it without one.

    Option `when`: `straddle` (the default) calls only when the count's interval straddles the
    hard limit (`tokens <= hard < upper`), an estimate that cannot say whether the request fits;
    `always` calls before every sample."""

    name = TOKEN_CALLS
    needs_counter = True
    """The driver refuses this extension without a counter: it has nowhere to put its counts."""
    order: ClassVar[Mapping[str, int]] = {"pre_sample": CALL_ORDER}
    options = CallOptions

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> None:
        """Ask the server to count the request about to be sent, when `when` says to."""
        if cursor.options().when == "straddle" and not await _straddles(cursor):
            return
        try:
            await cursor.count_call()
        except Exception as e:  # the step goes on with the counter's own count
            cursor.tell(f"the count call failed: {type(e).__name__}: {e}", level="warning")


async def _straddles(cursor: Cursor) -> bool:
    """Whether the count of the cursor's walk cannot say if it fits under the hard limit
    (compaction's, else the model's window)."""
    try:
        hard = hard_limit(cursor)
    except ConfigError:
        hard = cursor.resolved_model().context_window
    if hard is None:
        return False
    total = (await cursor.count()).total
    return total.tokens <= hard < total.upper
