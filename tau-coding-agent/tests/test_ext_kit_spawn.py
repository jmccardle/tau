"""Tests for ``examples/ext_kit/spawn.py`` — the S53 *isolated-agent* primitive.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §4 S53.

Two layers, mirroring ``test_delegate.py`` (from which the kit is distilled):

* **pure** — argv shape, usage/cost roll-up, stuck-signature, and the bounded
  :class:`WorkerPool` (its concurrency ceiling + input-order guarantee), all with
  no subprocess.
* **real spawn** — against a stdlib fake OpenAI-compatible server (no network, no
  API key), exercising the whole path the kit depends on: spawn → E-json stream →
  usage roll-up. Covers :func:`spawn_tau` (usage + cost), :func:`stream_tau` (live
  event iterator), :func:`spawn_all` (bounded fan-out), and abort propagation from
  a ``ctx.signal``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tau_ai import AbortSignal

# ── import the kit as a top-level package (examples/ on the path) ────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = str(_REPO_ROOT / "examples")
if _EXAMPLES not in sys.path:
    sys.path.insert(0, _EXAMPLES)

from ext_kit import spawn  # noqa: E402  (path insertion must precede the import)


# ── fake OpenAI-compatible provider (real HTTP server the child talks to) ────

_SSE_BODY = (
    'data: {"id":"cmpl-1","choices":[{"index":0,'
    '"delta":{"role":"assistant","content":"Spawned result: done."},'
    '"finish_reason":null}]}\n\n'
    'data: {"id":"cmpl-1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":11,"completion_tokens":5,"total_tokens":16}}\n\n'
    "data: [DONE]\n\n"
)


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return
        body = _SSE_BODY.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def fake_provider():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def fake_home(tmp_path, fake_provider, monkeypatch):
    """A temp ``$HOME`` whose ``~/.tau/config.json`` default model points at the
    fake provider; the spawned child inherits this env and resolves its model.
    """
    tau_dir = tmp_path / ".tau"
    tau_dir.mkdir()
    config = {
        "models": {
            "fake": {
                "backend": "openai",
                "model": "fake-model",
                "base_url": fake_provider,
                "api_key": "x",
                "tools": [],
            }
        },
        "default_model": "fake",
        "system_prompt": "You are a helpful subagent.",
    }
    (tau_dir / "config.json").write_text(json.dumps(config))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ── pure: argv shape ─────────────────────────────────────────────────────────


def test_build_child_args_is_headless_ephemeral_and_extension_free():
    args = spawn.build_child_args(
        prompt="audit the repo", model=None, tools=None, system_prompt_path=None
    )
    assert args[:5] == ["-p", "--mode", "json", "--no-session", "--no-extensions"]
    # The prompt is the single positional message, passed VERBATIM (no "Task:" prefix
    # — that is a 20_delegate flavour the neutral kit deliberately drops).
    assert args[-1] == "audit the repo"
    assert "--model" not in args and "--tools" not in args


def test_build_child_args_adds_optional_flags():
    args = spawn.build_child_args(
        prompt="scan", model="gpt-4o", tools=["read", "grep"], system_prompt_path="/tmp/sp.md"
    )
    assert args[args.index("--model") + 1] == "gpt-4o"
    assert args[args.index("--tools") + 1] == "read,grep"
    assert args[args.index("--append-system-prompt") + 1] == "/tmp/sp.md"


def test_tau_invocation_reruns_the_cli_module():
    command, argv = spawn.tau_invocation(["-p", "x"])
    assert command == sys.executable
    assert argv[:3] == ["-m", "tau_coding_agent.cli", "-p"]
    # Honours an explicit interpreter override.
    cmd2, _ = spawn.tau_invocation(["-p"], python_executable="/usr/bin/python3")
    assert cmd2 == "/usr/bin/python3"


# ── pure: usage / cost roll-up ───────────────────────────────────────────────


def _assistant_message(*, tokens=(11, 5, 0), stop="stop", text="hi", tool=None):
    content = [{"type": "text", "text": text}]
    if tool is not None:
        content.append({"type": "toolCall", "name": tool, "arguments": {}})
    return {
        "role": "assistant",
        "stop_reason": stop,
        "usage": {
            "input_tokens": tokens[0],
            "output_tokens": tokens[1],
            "cache_read_tokens": tokens[2],
            "total_tokens": sum(tokens),
        },
        "content": content,
    }


def test_roll_message_usage_accumulates_tokens_and_turns():
    usage = spawn.ChildUsage()
    spawn.roll_message_usage(usage, _assistant_message(tokens=(11, 5, 0)), None)
    spawn.roll_message_usage(usage, _assistant_message(tokens=(20, 7, 3)), None)
    assert usage.turns == 2
    assert usage.input == 31
    assert usage.output == 12
    assert usage.cache_read == 3
    assert usage.context_tokens == 30  # last message's total_tokens (running size)
    # Unpriced → cost stays unknown (Fail-Early: never a fabricated $0).
    assert usage.cost is None


def test_roll_message_usage_prices_when_seeded():
    cost = {"input": 1_000_000.0, "output": 2_000_000.0, "cache_read": 0.0}
    usage = spawn.ChildUsage(cost=0.0)  # seeded (spawn_tau does this when cost given)
    spawn.roll_message_usage(usage, _assistant_message(tokens=(10, 5, 0)), cost)
    # 10 * (1e6/1e6) + 5 * (2e6/1e6) = 10 + 10 = 20.0
    assert usage.cost == pytest.approx(20.0)


def test_price_increment_none_when_unpriced():
    assert (
        spawn.price_increment(None, input_tokens=100, output_tokens=100, cache_read_tokens=0)
        is None
    )


def test_tool_call_signature_and_message_text():
    with_tool = _assistant_message(tool="read", text="reading")
    without = _assistant_message(tool=None, text="done")
    assert spawn.tool_call_signature(with_tool) is not None
    assert spawn.tool_call_signature(without) is None
    assert spawn.message_text(with_tool) == "reading"


def test_child_result_failed_flag():
    ok = spawn.ChildResult(prompt="p", stop_reason="stop", exit_code=0)
    bad_reason = spawn.ChildResult(prompt="p", stop_reason="stuck", exit_code=0)
    bad_exit = spawn.ChildResult(prompt="p", stop_reason="stop", exit_code=1)
    assert not ok.failed
    assert bad_reason.failed
    assert bad_exit.failed


# ── pure: LimitEnforcer (the trip logic spawn_tau's consume loop runs) ────────


def test_limit_enforcer_no_trip_under_limits():
    usage = spawn.ChildUsage()
    enforcer = spawn.LimitEnforcer(spawn.SpawnLimits(max_turns=5), usage)
    message = _assistant_message(tool="read")
    spawn.roll_message_usage(usage, message, None)
    assert enforcer.observe(message) is None


def test_limit_enforcer_trips_stuck_on_repeated_signature():
    # stuck_limit=2 → the 3rd identical tool-call signature trips (repeat 0→1→2>=2).
    enforcer = spawn.LimitEnforcer(spawn.SpawnLimits(stuck_limit=2), spawn.ChildUsage())
    read = _assistant_message(tool="read")
    assert enforcer.observe(read) is None
    assert enforcer.observe(read) is None
    assert enforcer.observe(read) == "stuck"


def test_limit_enforcer_resets_stuck_run_on_signature_change():
    # A different signature (or a text-only turn) resets the run, so no false trip.
    enforcer = spawn.LimitEnforcer(spawn.SpawnLimits(stuck_limit=2), spawn.ChildUsage())
    read = _assistant_message(tool="read")
    grep = _assistant_message(tool="grep")
    assert enforcer.observe(read) is None
    assert enforcer.observe(grep) is None  # signature changed → run reset
    assert enforcer.observe(read) is None
    assert enforcer.observe(read) is None
    assert enforcer.observe(read) == "stuck"


def test_limit_enforcer_trips_max_turns():
    usage = spawn.ChildUsage()
    enforcer = spawn.LimitEnforcer(spawn.SpawnLimits(max_turns=2), usage)
    message = _assistant_message()  # text-only → no stuck signature to interfere
    spawn.roll_message_usage(usage, message, None)
    assert enforcer.observe(message) is None  # turn 1 of 2
    spawn.roll_message_usage(usage, message, None)
    assert enforcer.observe(message) == "max_turns"  # turn 2 >= 2


def test_limit_enforcer_trips_over_budget():
    cost = {"input": 1_000_000.0, "output": 0.0, "cache_read": 0.0}
    usage = spawn.ChildUsage(cost=0.0)  # seeded, as spawn_tau does when priced
    enforcer = spawn.LimitEnforcer(spawn.SpawnLimits(max_usd=5.0), usage)
    message = _assistant_message(tokens=(11, 0, 0))  # 11 in × $1/tok = $11 > $5
    spawn.roll_message_usage(usage, message, cost)
    assert usage.cost == pytest.approx(11.0)
    assert enforcer.observe(message) == "over_budget"


# ── pure: WorkerPool ─────────────────────────────────────────────────────────


def test_worker_pool_rejects_non_positive_concurrency():
    with pytest.raises(ValueError, match="concurrency must be >= 1"):
        spawn.WorkerPool(0)


async def test_worker_pool_bounds_concurrency_and_preserves_order():
    live = 0
    peak = 0
    lock = asyncio.Lock()

    async def _job(item: int, index: int) -> tuple[int, int]:
        nonlocal live, peak
        async with lock:
            live += 1
            peak = max(peak, live)
        await asyncio.sleep(0.02)
        async with lock:
            live -= 1
        return (index, item * 10)

    pool = spawn.WorkerPool(2)
    results = await pool.map(_job, [1, 2, 3, 4, 5])
    # Never more than `concurrency` workers in flight at once.
    assert peak <= 2
    # Results are in INPUT order despite out-of-order completion.
    assert results == [(0, 10), (1, 20), (2, 30), (3, 40), (4, 50)]


async def test_worker_pool_empty_items():
    pool = spawn.WorkerPool(4)
    assert await pool.map(lambda _i, _x: asyncio.sleep(0), []) == []


# ── real spawn: usage roll-up ────────────────────────────────────────────────


async def test_spawn_tau_rolls_usage(fake_home):
    result = await spawn.spawn_tau("summarize the plan", cwd=str(fake_home))
    assert not result.failed
    assert result.exit_code == 0
    assert result.stop_reason == "stop"
    assert result.usage.turns == 1
    assert result.usage.input == 11
    assert result.usage.output == 5
    assert result.usage.context_tokens == 16
    assert result.usage.cost is None  # no price supplied → unknown, not $0
    assert "Spawned result: done." in result.final_output


async def test_spawn_tau_rolls_cost_when_priced(fake_home):
    cost = {"input": 3_000_000.0, "output": 5_000_000.0, "cache_read": 0.0}
    result = await spawn.spawn_tau("price me", cwd=str(fake_home), cost=cost)
    # 11 in * (3e6/1e6) + 5 out * (5e6/1e6) = 33 + 25 = 58.0
    assert result.usage.cost == pytest.approx(58.0)


async def test_spawn_tau_rejects_double_timeout(fake_home):
    with pytest.raises(ValueError, match="not both"):
        await spawn.spawn_tau(
            "x", cwd=str(fake_home), timeout=5.0, limits=spawn.SpawnLimits(max_seconds=5.0)
        )


async def test_spawn_tau_rejects_budget_without_price(fake_home):
    # Fail-Early: a max_usd we could never enforce (no price) is refused up front,
    # not silently ignored (mirrors the double-timeout guard above).
    with pytest.raises(ValueError, match="refusing to silently not-enforce"):
        await spawn.spawn_tau("budgeted", cwd=str(fake_home), limits=spawn.SpawnLimits(max_usd=5.0))


# ── real spawn: live limit enforcement (trip → kill → imposed stop_reason) ────


async def test_spawn_tau_trips_max_turns(fake_home):
    # One child turn against the fake server; max_turns=1 trips after it, the child
    # is killed, and the imposed stop_reason overrides the child-reported "stop".
    result = await spawn.spawn_tau(
        "one and done", cwd=str(fake_home), limits=spawn.SpawnLimits(max_turns=1)
    )
    assert result.stop_reason == "max_turns"
    assert result.failed
    assert result.error_message == "child max_turns"
    assert result.usage.turns == 1


async def test_spawn_tau_trips_over_budget(fake_home):
    # Priced run whose first turn (11 in + 5 out) blows a $1 cap → over_budget.
    cost = {"input": 1_000_000.0, "output": 1_000_000.0, "cache_read": 0.0}
    result = await spawn.spawn_tau(
        "spend it", cwd=str(fake_home), cost=cost, limits=spawn.SpawnLimits(max_usd=1.0)
    )
    assert result.stop_reason == "over_budget"
    assert result.failed
    assert result.error_message == "child over_budget"
    assert result.usage.cost == pytest.approx(16.0)


# ── real spawn: streamed event iterator ──────────────────────────────────────


async def test_stream_tau_yields_message_end(fake_home):
    events = []
    async for event in spawn.stream_tau("stream me", cwd=str(fake_home)):
        events.append(event)
    assert events, "expected at least the session header + lifecycle events"
    message_ends = [
        e
        for e in events
        if e.get("type") == "message_end" and (e.get("message") or {}).get("role") == "assistant"
    ]
    assert message_ends, "expected an assistant message_end in the child stream"
    assert (message_ends[-1]["message"].get("usage") or {}).get("total_tokens") == 16


# ── real spawn: bounded fan-out ──────────────────────────────────────────────


async def test_spawn_all_runs_every_prompt_in_order(fake_home):
    results = await spawn.spawn_all(
        ["task A", "task B", "task C"], concurrency=2, cwd=str(fake_home)
    )
    assert [r.prompt for r in results] == ["task A", "task B", "task C"]
    assert all(not r.failed for r in results)
    assert all(r.usage.turns == 1 for r in results)


# ── real spawn: abort propagation from ctx.signal ────────────────────────────


async def test_spawn_tau_aborts_from_signal(fake_home):
    # A pre-aborted ctx.signal must propagate: the first child stdout line trips
    # the abort check, the child is killed, and stop_reason is "aborted".
    signal = AbortSignal()
    signal.abort()
    result = await spawn.spawn_tau("never mind", cwd=str(fake_home), signal=signal)
    assert result.stop_reason == "aborted"
    assert result.failed
    assert result.error_message == "child aborted"
