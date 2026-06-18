"""Tests for tau_ai.abort — AbortSignal implementation.

Tests verify:
- AbortSignal can be instantiated
- is_aborted() returns False initially
- abort() sets is_aborted() to True
- abort() is idempotent (calling multiple times is safe)
- is_aborted() is thread-safe (using threading.Lock internally)
"""

import threading
import time

import pytest

from tau_ai.abort import AbortSignal


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
