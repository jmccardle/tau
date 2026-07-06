"""Tests for tau_ai.abort — AbortSignal implementation.

Tests verify:
- AbortSignal can be instantiated
- is_aborted() returns False initially
- abort() sets is_aborted() to True
- abort() is idempotent (calling multiple times is safe)
- is_aborted() is thread-safe (using threading.Lock internally)
"""

import asyncio
import json
import threading
import time

import pytest

from tau_ai.abort import AbortSignal
from tau_ai.providers.openai import OpenAICompletionsProvider
from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import Model, TextContent, UserMessage


class TestAbortSignalBasic:
    """Tests for basic AbortSignal behavior."""

    def test_create_abort_signal(self):
        """AbortSignal can be instantiated."""
        signal = AbortSignal()
        assert signal is not None

    def test_is_aborted_initially_false(self):
        """is_aborted() returns False for a new AbortSignal."""
        signal = AbortSignal()
        assert signal.is_aborted() is False

    def test_abort_sets_is_aborted_true(self):
        """After abort(), is_aborted() returns True."""
        signal = AbortSignal()
        signal.abort()
        assert signal.is_aborted() is True


class TestAbortSignalIdempotent:
    """Tests for abort() idempotency."""

    def test_abort_twice_is_safe(self):
        """Calling abort() multiple times should not raise."""
        signal = AbortSignal()
        signal.abort()
        signal.abort()  # Should not raise
        signal.abort()  # Should not raise
        assert signal.is_aborted() is True

    def test_abort_then_is_aborted_checked_multiple_times(self):
        """is_aborted() should consistently return True after abort()."""
        signal = AbortSignal()
        signal.abort()
        for _ in range(100):
            assert signal.is_aborted() is True


class TestAbortSignalThreadSafety:
    """Tests for AbortSignal thread safety."""

    def test_abort_is_thread_safe(self):
        """Multiple threads calling abort() simultaneously should be safe."""
        signal = AbortSignal()
        errors = []

        def worker():
            try:
                for _ in range(100):
                    signal.abort()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Thread safety errors: {errors}"
        assert signal.is_aborted() is True

    def test_is_aborted_is_thread_safe(self):
        """Multiple threads reading is_aborted() simultaneously should be safe."""
        signal = AbortSignal()
        results = []
        errors = []
        lock = threading.Lock()

        def reader():
            try:
                for _ in range(100):
                    result = signal.is_aborted()
                    with lock:
                        results.append(result)
            except Exception as e:
                with lock:
                    errors.append(e)

        def writer():
            time.sleep(0.01)
            signal.abort()

        threads = []
        for _ in range(5):
            threads.append(threading.Thread(target=reader))
        threads.append(threading.Thread(target=writer))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Thread safety errors: {errors}"
        # All results should be bool
        assert all(isinstance(r, bool) for r in results)

    def test_abort_read_race_condition(self):
        """Reading is_aborted() during concurrent abort() should not crash."""
        signal = AbortSignal()
        errors = []
        stop_flag = threading.Event()

        def reader():
            try:
                for _ in range(200):
                    signal.is_aborted()
                    if stop_flag.is_set():
                        break
            except Exception as e:
                errors.append(e)

        def writer():
            import time
            time.sleep(0.01)  # Let readers start
            signal.abort()
            stop_flag.set()  # Signal readers to stop

        threads = []
        for _ in range(5):
            threads.append(threading.Thread(target=reader))
        threads.append(threading.Thread(target=writer))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0, f"Race condition errors: {errors}"


# ──────────────────────────────────────────────────────────────────────────
# Abort threaded into the LLM stream (cooperative mid-completion cancellation)
# ──────────────────────────────────────────────────────────────────────────


class _AbortStreamCM:
    """Async CM mimicking ``httpx.AsyncClient.stream(...)``."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _AbortingResponse:
    """A 200 SSE response that trips ``signal`` just before yielding line N."""

    def __init__(self, lines, signal, abort_before):
        self.status_code = 200
        self._lines = lines
        self._signal = signal
        self._abort_before = abort_before

    def json(self):
        return {}

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for i, line in enumerate(self._lines):
            if i == self._abort_before:
                self._signal.abort()
            yield line


class _AbortingClient:
    def __init__(self, response):
        self._response = response

    def stream(self, *args, **kwargs):
        return _AbortStreamCM(self._response)


def _abort_model() -> Model:
    return Model(
        id="m", name="m", api="openai-completions", provider="openai",
        base_url="http://localhost/v1", context_window=8192, max_tokens=1024,
    )


def _content_chunks(words):
    chunks = [{"id": "x", "choices": [{"delta": {"content": w}}]} for w in words]
    return ["data: " + json.dumps(c) for c in chunks] + ["data: [DONE]"]


class TestAbortSignalStopsStream:
    """The abort signal, threaded into ``stream_chat`` via ``options``, stops the
    LLM stream mid-completion (cooperative cancellation) and finalizes whatever
    streamed so far with an ``aborted`` stop_reason — instead of draining the
    whole response."""

    def test_abort_midstream_stops_and_finalizes_aborted(self):
        signal = AbortSignal()
        lines = _content_chunks(["one", "two", "three", "four", "five"])
        # Trip the signal just before the 3rd SSE line; the provider checks at the
        # top of the loop, so "three" onward are never processed.
        resp = _AbortingResponse(lines, signal, abort_before=2)

        provider = OpenAICompletionsProvider(api_key="sk-test")
        provider._get_client = lambda: _AbortingClient(resp)  # type: ignore[method-assign]

        async def go():
            stream = await provider.stream_chat(
                model=_abort_model(),
                messages=[UserMessage(content=[TextContent(text="go")], timestamp=0)],
                options={"abort_signal": signal},
            )
            return [e async for e in stream]

        events = asyncio.run(go())
        text = [e for e in events if isinstance(e, TextDeltaEvent)]
        done = [e for e in events if isinstance(e, DoneEvent)]

        # Only the first two deltas streamed before the abort took effect.
        assert [e.delta for e in text] == ["one", "two"]
        assert len(done) == 1
        assert done[0].final.stop_reason == "aborted"
        joined = "".join(c.text for c in done[0].final.content if isinstance(c, TextContent))
        assert joined == "onetwo"

    def test_abort_signal_is_not_sent_in_request_body(self):
        """The AbortSignal is a τ-internal handle — it must be stripped from the
        JSON payload (a non-serializable object would otherwise break the POST)."""
        signal = AbortSignal()
        captured: dict = {}

        class _CapturingClient:
            def __init__(self, response):
                self._response = response

            def stream(self, *args, **kwargs):
                captured["json"] = kwargs.get("json")
                return _AbortStreamCM(self._response)

        resp = _AbortingResponse(_content_chunks(["a"]), signal, abort_before=99)
        provider = OpenAICompletionsProvider(api_key="sk-test")
        provider._get_client = lambda: _CapturingClient(resp)  # type: ignore[method-assign]

        async def go():
            stream = await provider.stream_chat(
                model=_abort_model(),
                messages=[UserMessage(content=[TextContent(text="go")], timestamp=0)],
                options={"abort_signal": signal},
            )
            return [e async for e in stream]

        asyncio.run(go())
        assert "abort_signal" not in captured["json"]

    def test_no_abort_streams_to_completion(self):
        """Without an abort the full stream completes (control)."""
        signal = AbortSignal()  # never aborted
        resp = _AbortingResponse(_content_chunks(["a", "b"]), signal, abort_before=99)
        provider = OpenAICompletionsProvider(api_key="sk-test")
        provider._get_client = lambda: _AbortingClient(resp)  # type: ignore[method-assign]

        async def go():
            stream = await provider.stream_chat(
                model=_abort_model(),
                messages=[UserMessage(content=[TextContent(text="go")], timestamp=0)],
                options={},
            )
            return [e async for e in stream]

        events = asyncio.run(go())
        done = [e for e in events if isinstance(e, DoneEvent)]
        assert len(done) == 1
        assert done[0].final.stop_reason == "stop"
