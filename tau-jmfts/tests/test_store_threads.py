"""The JMFTS store's in-memory mirror under a worker-thread append.

``append_at``/``finalize`` POST on a worker thread (docs/BLOCKING-PERSISTENCE.md
§1) while the head reads ``context``/``config``/``entries()`` on its loop thread.
Two properties, both measured in experiments/freethreading/RESULTS.md §8:

* a read takes ONE snapshot, so an append landing mid-read cannot pair a leaf
  from the new mirror with entries from the old one (that pairing returned an
  empty context, with and without the GIL);
* a read never waits on the POST, which is why the mirror has its own lock
  rather than borrowing ``_append_lock``.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

import tau_jmfts.store as store_mod
from tau_jmfts.store import JmftsSessionLog


class _Client:
    """Stand-in for the two client methods the write path calls."""

    def __init__(self) -> None:
        self._next_id = 1
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def create_document(self, **kwargs: Any) -> dict[str, Any]:
        if self.gate is not None:
            self.entered.set()
            assert self.gate.wait(10), "test never released the POST"
        doc = {"id": self._next_id, **kwargs}
        self._next_id += 1
        return doc

    def update_document(self, *args: Any, **kwargs: Any) -> None:
        return None


def _log_with_one_message() -> tuple[JmftsSessionLog, str]:
    log = JmftsSessionLog.create(_Client(), cwd=".", model="m", backend="b")  # type: ignore[arg-type]
    parent = log.entries()[-1]["id"]
    leaf = log._append_now(parent, "message", {"message": {"role": "user", "content": "hi"}})
    return log, leaf


def test_context_is_not_torn_by_an_append_landing_mid_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log, leaf = _log_with_one_message()
    before = log.context
    assert before, "fixture: one user message must give a non-empty context"
    real_default_leaf = store_mod.default_leaf
    landed: list[str] = []

    def append_then_pick_leaf(entries: list[dict[str, Any]]) -> str | None:
        if not landed:
            landed.append(
                log._append_now(leaf, "message", {"message": {"role": "user", "content": "2"}})
            )
        return real_default_leaf(entries)

    monkeypatch.setattr(store_mod, "default_leaf", append_then_pick_leaf)
    assert log.context == before
    assert landed, "the interleaving under test never happened"


def test_reads_do_not_wait_for_an_in_flight_post() -> None:
    log, leaf = _log_with_one_message()
    client: _Client = log.client  # type: ignore[assignment]
    client.gate = threading.Event()
    writer = threading.Thread(
        target=log._append_now,
        args=(leaf, "message", {"message": {"role": "user", "content": "slow"}}),
    )
    writer.start()
    try:
        assert client.entered.wait(10), "the append never reached its POST"
        results: list[Any] = []
        reader = threading.Thread(
            target=lambda: results.extend([log.context, log.config, log.entries()])
        )
        reader.start()
        reader.join(5)
        assert not reader.is_alive(), "a read blocked behind the in-flight POST"
        assert len(results) == 3
    finally:
        client.gate.set()
        writer.join(10)
    assert [e["message"]["content"] for e in log.entries()[1:]] == ["hi", "slow"]
