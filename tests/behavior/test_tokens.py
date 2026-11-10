"""`tokens` (D26 section 6): the built-in extension that offers the counter. It learns what a
model's tokens cost from the proved spans its replies report, and counts with that what no
report covers yet.

The server here bills by a rule the test knows (`tok`: a quarter of a token a character and 4 a
message, over a hidden overhead), and reports `cached` as a server that found the whole previous
request in its cache does, so every span is proved. Node ids: 0 the config, 1 the system text,
then message k is node 2k and its reply node 2k + 1.
"""

import hashlib
import json
from pathlib import Path

from fakes import FAKE_PROVIDER, Fail, FakeProvider, FakeTool, Reply
from harness import Harness, scenario

from tau import (
    Author,
    Basis,
    Body,
    CalibrationStore,
    Compaction,
    Count,
    Cursor,
    ModelCatalog,
    Outcome,
    Part,
    RequestCount,
    Stop,
    Subject,
    Tokens,
    Usage,
    calibration_features,
    model_config,
    read_models,
)

OVERHEAD = 700
INSTALL = "pip install 'ffwf-tau[tokens]'"
RECORD = f"{FAKE_PROVIDER}/{hashlib.sha256(b'\na').hexdigest()[:16]}.json"
"""The record of the model `a` of the fake provider, which has no base URL (D26 section 6.5)."""


def tok(text: str) -> int:
    """What the server bills for a message: a quarter of a token a character, and 4."""
    return round(len(text) / 4) + 4


def said(k: int) -> str:
    """The k-th message: prose whose length differs from turn to turn."""
    return f"question {k}: " + "lorem ipsum dolor sit amet " * (60 + (k * 7) % 23)


def answered(k: int) -> str:
    return f"answer {k}: " + "consectetur adipiscing elit sed do " * (40 + (k * 5) % 17)


def conversation(turns: int, fingerprint: dict[int, str] | None = None) -> list[Reply]:
    """The replies of `turns` turns, each billed by `tok` and reading the whole previous request
    from the cache. `fingerprint` is the `system_fingerprint` from a turn on (`fp1` at first)."""
    replies: list[Reply] = []
    billed, previous, current = OVERHEAD + tok("sys"), None, "fp1"
    for k in range(1, turns + 1):
        current = (fingerprint or {}).get(k, current)
        billed += tok(said(k))
        usage = {"input": billed, "output": tok(answered(k))}
        if previous is not None:
            usage["cached"] = previous
        meta = {"usage": usage, "system_fingerprint": current, "model": "a-2026"}
        replies.append(Reply([Part.Text(answered(k))], Stop.End, meta=json.dumps(meta)))
        previous = billed
        billed += tok(answered(k))
    return replies


class Probe:
    """An extension that counts the walk before every sample: the request about to go out."""

    name = "probe"

    def __init__(self) -> None:
        self.counted: list[RequestCount] = []

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> str | None:
        self.counted.append(await cursor.count())
        return None


def catalog(window: int = 10_000_000, **compaction: object) -> ModelCatalog:
    model = {"model": "a", "provider": FAKE_PROVIDER, "context_window": window}
    extensions = {"compaction": compaction} if compaction else {}
    return read_models(cli={"models": {"a": model}, "extensions": extensions})


async def talk(h: Harness, first: int, last: int) -> None:
    for k in range(first, last + 1):
        await h.send(said(k))
        await h.settle()


def told(h: Harness, word: str) -> list[str]:
    return [n for n in h.notices() if word in n]


@scenario
async def test_it_learns_from_proved_spans_then_counts_what_is_not_sent_yet(
    tmp_path: Path,
) -> None:
    """D26 T3. Ten proved spans make the record ready; until then a new message is a ceiling,
    after it a heuristic count near the truth, whose bound narrows as replies gather; a changed
    `system_fingerprint` starts it over, and the clients are told."""
    home = tmp_path / "home"
    probe = Probe()
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(conversation(32, fingerprint={30: "fp2"})),
        extensions=[Tokens(home), probe],
        catalog=catalog(),
        config=model_config("a"),
    )
    assert not (home / "calibration").exists(), "nothing is written before a reply is observed"
    await talk(h, 1, 30)
    assert (home / "calibration" / RECORD).is_file()

    def before(k: int) -> tuple[Count, str | None]:
        """How message k was counted before its request went out, and why."""
        counted = probe.counted[k - 1]
        return counted.nodes[2 * k], counted.why

    for k in (1, 5, 11):  # reply k - 1 proved span k - 2
        count, why = before(k)
        assert count == Count(0, Basis.CEILING, len(said(k)) + 16), k
    _, why = before(5)
    assert why == (
        "a: learning from 4 replies (3 of 10 proved spans since it last changed); "
        f"byte ceilings until then (no tokenizer: {INSTALL})"
    )
    bounds: dict[int, float] = {}
    for k in range(12, 31):
        count, why = before(k)
        assert count.basis is Basis.HEURISTIC, k
        assert abs(count.tokens - tok(said(k))) <= count.bound, (k, count, tok(said(k)))
        assert count.upper < len(said(k)) // 2, "far under the ceiling of its bytes"
        assert why == f"no tokenizer for a: heuristic counts; {INSTALL}"
        assert probe.counted[k - 1].total.basis is Basis.HEURISTIC
        bounds[k] = count.bound / count.tokens
    assert bounds[30] < bounds[12], "the bound narrows over 30 replies"
    assert bounds[30] < 0.1

    # Reply 30 came from a server whose fingerprint changed.
    assert told(h, "recalibrating") == [
        "a: the server's system_fingerprint changed (fp1 → fp2); recalibrating"
    ]
    await talk(h, 31, 32)
    for k in (31, 32):
        count, why = before(k)
        assert count.basis is Basis.CEILING, "a record that was reset counts nothing"
        assert why is not None
        assert "learning" in why
    assert len(told(h, "recalibrating")) == 1, "told once"
    assert (await h.view()).hook_error is None
    await h.close()


@scenario
async def test_tokens_and_usage_say_how_each_model_is_counted(tmp_path: Path) -> None:
    home = tmp_path / "home"
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(conversation(13)),
        extensions=[Tokens(home), Usage()],
        catalog=catalog(),
        config=model_config("a"),
    )
    assert await h.run("tokens") == "no model has been counted or observed yet"
    await talk(h, 1, 3)
    assert await h.run("tokens") == (
        "a: learning from 3 replies (2 of 10 proved spans since it last changed); "
        f"byte ceilings until then (no tokenizer: {INSTALL})"
    )
    usage = await h.run("usage")
    assert usage is not None
    assert usage.splitlines()[-1] == (
        "counts: reported, and by the tokens extension for what is not sent yet"
    )
    await talk(h, 4, 13)
    line = await h.run("tokens")
    assert line is not None
    assert line.startswith("a: heuristic ±"), line
    assert line.endswith(f"% for new nodes, from 13 replies (no tokenizer: {INSTALL})"), line
    await h.close()


@scenario
async def test_without_tokens_nothing_is_written_and_counts_are_ceilings(tmp_path: Path) -> None:
    """D26 T3: with the extension not loaded (`load: {tokens: false}`), a session whose replies
    prove spans leaves no `calibration` directory, and what is not sent yet is a ceiling."""
    home = tmp_path / "home"
    probe = Probe()
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(conversation(14)),
        extensions=[probe],
        catalog=catalog(),
        config=model_config("a"),
    )
    await talk(h, 1, 14)
    assert not home.exists()
    assert not list(tmp_path.rglob("calibration"))
    assert probe.counted[13].nodes[28] == Count(0, Basis.CEILING, len(said(14)) + 16)
    assert probe.counted[13].why is None
    await h.close()


def result_of(text: str) -> Body:
    """The `Results` node of one call, `c`, that returned `text`."""
    return Body.Results([("c", Outcome.Ok(text), Author.Tools())])


def trained(home: Path, rate: float, noise: float = 0.0, spans: int = 60) -> CalibrationStore:
    """A store under `home` whose record for the model `a` has learned, from `spans` proved
    spans of a reply and a tool result, that a character costs `rate` tokens (give or take
    `noise`, alternately)."""
    store = CalibrationStore(home / "calibration")
    for i in range(spans):
        reply = Body.Assistant([Part.Text("w" * (40 + 7 * i))], Stop.End)
        result = result_of("r" * (500 + 300 * i))
        rows = [calibration_features(reply), calibration_features(result)]
        features = [a + b for a, b in zip(*rows, strict=True)]
        size = 40 + 7 * i + 1 + 500 + 300 * i  # the reply, the call id, the result
        off = noise if i % 2 else -noise
        cost = round(size * rate * (1 + off))
        store.observe(RECORD, (FAKE_PROVIDER, None, "a"), {}, (features, cost), "t")
    return store


async def big_result(tmp_path: Path, home: Path, window: int, *, counter: bool) -> Harness:
    """A session whose first reply calls a tool that returns 40,000 characters, then goes on if
    the hard limit lets it: with compaction, a reserve of 2,000, and `window`."""
    call = Reply(
        [Part.Call("c", "big", "{}")],
        Stop.Tools,
        meta=json.dumps({"usage": {"input": 1000, "output": 50}}),
    )
    usage = {"usage": {"input": 11_000, "output": 5}}
    done = Reply([Part.Text("done")], Stop.End, meta=json.dumps(usage))
    script: list[Reply | Fail] = [call, done]
    h = await Harness.create(
        tmp_path / ("with" if counter else "without") / str(window),
        provider=FakeProvider(script),
        tools=[FakeTool("big", "r" * 40_000)],
        extensions=[*([Tokens(home)] if counter else []), Compaction()],
        catalog=catalog(window, reserve=2000, soft=window - 2000),
        config=model_config("a"),
    )
    await h.send("go")
    await h.settle()
    return h


@scenario
async def test_the_hard_limit_reads_the_upper_of_a_heuristic_count(tmp_path: Path) -> None:
    """D26 T3: a large tool result counted by the heuristic, where `upper` crosses the hard limit
    and `tokens` does not. The request is not sent; with a little more room it is."""
    home = tmp_path / "home"
    store = trained(home, rate=0.25, noise=0.1)
    record = store.read(RECORD)
    assert record is not None
    assert record.ready
    result = result_of("r" * 40_000)
    estimate = record.predict(calibration_features(result))
    assert estimate is not None
    tokens, bound = estimate
    assert abs(tokens - 10_000) < 300, "a quarter of a token a character"
    assert 500 < bound < 2500, "about the 10% the costs were off by, and its margin"
    request = 1000 + 50 + tokens  # the bill, the reply, the result
    between = request + bound // 2 + 2000  # a window whose hard limit is inside the bound
    h = await big_result(tmp_path, home, between, counter=True)
    assert len(h.provider.requests) == 1, "the upper count is over the hard limit: not sent"
    failed = (await h.view()).hook_error
    assert failed is not None
    assert "is an estimate (`~`), counted with the most it may be off by" in failed.error
    assert "by its bytes" not in failed.error
    await h.close()

    roomy = request + bound + 2000 + 100
    h = await big_result(tmp_path, home, roomy, counter=True)
    assert len(h.provider.requests) == 2, "under the limit by its upper count: sent"
    await h.close()

    h = await big_result(tmp_path, home, roomy, counter=False)
    assert len(h.provider.requests) == 1, "by its bytes the result is 40,016: not sent"
    failed = (await h.view()).hook_error
    assert failed is not None
    assert "counted by its bytes" in failed.error
    await h.close()


@scenario
async def test_a_heuristic_count_is_never_above_the_ceiling(tmp_path: Path) -> None:
    """A record that learned two tokens a character would count a node above its bytes; the
    ceiling wins (D26 section 6.4)."""
    home = tmp_path / "home"
    trained(home, rate=2.0)
    probe = Probe()
    call = Reply(
        [Part.Call("c", "big", "{}")],
        Stop.Tools,
        meta=json.dumps({"usage": {"input": 1000, "output": 50}}),
    )
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([call, Reply([Part.Text("done")], Stop.End)]),
        tools=[FakeTool("big", "r" * 4000)],
        extensions=[Tokens(home), probe],
        catalog=catalog(),
        config=model_config("a"),
    )
    await h.send("go")
    await h.settle()
    result = next(n for n in h.nodes() if isinstance(n.body, Body.Results))
    count = probe.counted[1].nodes[result.id]
    assert count == Count(4000 + 1 + 16, Basis.HEURISTIC, 0), "its bytes and 16, no more"
    await h.close()


@scenario
async def test_a_record_that_cannot_be_used_is_said_and_counts_stay_ceilings(
    tmp_path: Path,
) -> None:
    """A record a newer tau wrote is left alone and not used; `why`, `/tokens` and one notice say
    so, and the session goes on by its bytes."""
    home = tmp_path / "home"
    trained(home, rate=0.25)
    path = home / "calibration" / RECORD
    newer = path.read_text().replace('"version":1', '"version":2', 1)
    path.write_text(newer)
    probe = Probe()
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(conversation(3)),
        extensions=[Tokens(home), probe],
        catalog=catalog(),
        config=model_config("a"),
    )
    await talk(h, 1, 3)
    counted = probe.counted[2]
    assert counted.nodes[6].basis is Basis.CEILING
    assert counted.why is not None
    assert "from a newer tau" in counted.why
    assert "tau tokens reset" in counted.why
    assert len(told(h, "from a newer tau")) == 1, "told once, not at every reply"
    line = await h.run("tokens")
    assert line is not None
    assert line.startswith("a: ")
    assert "from a newer tau" in line
    assert path.read_text() == newer, "left as it was"
    assert (await h.view()).hook_error is None
    await h.close()
