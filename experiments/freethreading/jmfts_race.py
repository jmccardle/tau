"""Readers of JmftsSessionLog's mirror while a worker thread appends."""
import sys
import threading
import time
import traceback
from typing import Any

from tau_jmfts.store import JmftsSessionLog


class Client:
    def __init__(self) -> None:
        self._next = 1
        self._lock = threading.Lock()

    def create_document(self, **kw: Any) -> dict[str, Any]:
        time.sleep(float(sys.argv[2]))
        with self._lock:
            d = {"id": self._next, **kw}
            self._next += 1
            return d

    def update_document(self, *a: Any, **kw: Any) -> None:
        pass


log = JmftsSessionLog.create(Client(), cwd=".", model="m", backend="b")
stop = threading.Event()
started = threading.Event()
errors: list[str] = []


def writer() -> None:
    parent = log.entries()[-1]["id"]
    for i in range(int(sys.argv[1])):
        parent = log._append_now(parent, "message", {"message": {"role": "user", "content": f"m{i}"}})
        started.set()
    stop.set()


def reader() -> None:
    n = 0
    while not stop.is_set():
        try:
            ctx = log.context
            if not ctx and started.is_set():
                errors.append("EMPTY context while the log holds messages")
            log.config
            n += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
            if len(errors) == 1:
                traceback.print_exc()
    print("reads", n)


ts = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(4)]
for t in ts:
    t.start()
for t in ts:
    t.join()
print(f"gil={sys._is_gil_enabled()} entries={len(log.entries())} reader errors={len(errors)}", errors[:3])
