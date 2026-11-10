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
"""

import hashlib
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from tau._config import ModelConfig
from tau._core import Body, Calibration, CalibrationError, CalibrationStore, calibration_features
from tau._counts import Basis, Count, CounterAnswer, CountJob, Observation, ceiling_of
from tau._handles import Cursor, Subject, TreeHandle

NAME = "tokens"
INSTALL = "pip install 'ffwf-tau[tokens]'"
"""The command every message about the missing tokenizer wheel prints (D26 decision 2)."""
DIRECTORY = "calibration"
MARKERS = ("model", "system_fingerprint")
"""The drift markers read from a reply's `meta` itself; llama.cpp's are under `meta.server`."""


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
    `home` is `TAU_HOME`; `tokenizers` is the tokenizer wheel when it is installed."""

    name = NAME

    def __init__(self, home: Path, tokenizers: Tokenizers | None = None) -> None:
        self._store = CalibrationStore(home / DIRECTORY)
        self._tokenizers = tokenizers
        self._lock = threading.Lock()  # `count` and `observe` run in threads
        self._held: dict[str, _Held] = {}
        self._models: dict[str, str] = {}  # qualified model name -> its record's name
        self._news: list[str] = []  # what the next `post_reply` tells the clients
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
        if held.problem is not None:
            return CounterAnswer({}, held.problem)
        record = held.record
        if record is None or not record.ready:
            return CounterAnswer({}, describe(job.model, record, self._tokenizers))
        nodes: dict[int, Count] = {}
        wanted = set(job.unreported)
        for node in job.walk:
            if node.id not in wanted:
                continue
            if node.derived_from is not None and isinstance(node.body, Body.User):
                continue  # a summary: the meter counts it from its reply's own report
            estimate = record.predict(calibration_features(node.body))
            if estimate is None:
                continue
            most = ceiling_of(node.body).upper
            tokens = min(estimate[0], most)
            nodes[node.id] = Count(tokens, Basis.HEURISTIC, min(estimate[1], most - tokens))
        return CounterAnswer(nodes, self._why(job.model))

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
                    self._news.append(f"{observation.model}: {problem}")
                return
            self._held[name] = _Held(record)
            self._news += [_news(observation.model, event) for event in events]

    # The extension

    async def post_reply(self, cursor: Cursor, subject: Subject) -> str | None:
        """Tell the clients what the last observations found (a model that changed)."""
        with self._lock:
            news, self._news = self._news, []
        for text in news:
            cursor.tell(text)
        return None

    def tokens(self, tree: TreeHandle) -> str:
        """How each model of this session is counted before a request is sent."""
        with self._lock:
            models = dict(self._models)
            lines = [self._line(model, name) for model, name in sorted(models.items())]
        if not lines:
            return "no model has been counted or observed yet"
        return "\n".join(lines)

    def _line(self, model: str, name: str) -> str:
        held = self._read(name)
        if held.problem is not None:
            return f"{model}: {held.problem}"
        return describe(model, held.record, self._tokenizers)


def _news(model: str, event: tuple[str, str, str, str, float]) -> str:
    """What a fold found, as the clients are told it (D26 section 6.3)."""
    kind, name, was, now, by = event
    if kind == "marker":
        return f"{model}: the server's {name} changed ({was} → {now}); recalibrating"
    more = "more" if by > 0 else "fewer"
    return f"{model}: requests now cost {abs(by):.0%} {more} tokens than calibrated; recalibrating"
