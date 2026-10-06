"""The CPython alternatives to a Monty dump, for comparison (experiment, 2026-10-05).

Two ways to make a CPython ``exec`` namespace rebuildable from the tree:

- **Pickle the namespace** with dill or cloudpickle after each script.
- **Replay** the path's scripts into a fresh namespace, answering each nested tool
  call from its recorded result and raising when a call diverges from the record.

Neither is a sandbox (CODE-MODE.md §6.2: in-process ``exec`` escapes); both are
measured here only as state-lifetime strategies.
"""

from __future__ import annotations

import json
import pickle
import time
from typing import Any

import cloudpickle
import dill


class ReplayDivergence(RuntimeError):
    """A replayed script asked for a different call than the one recorded."""


def pickle_namespace(ns: dict[str, Any], lib: str) -> tuple[bytes | None, dict[str, str]]:
    """Pickle every user name in ``ns`` with ``lib``; report the names that fail.

    Returns:
        The pickle of the names that succeeded (``None`` if every name failed), and
        ``{name: error}`` for the ones that did not.
    """
    dumps = {"dill": dill.dumps, "cloudpickle": cloudpickle.dumps}[lib]
    good: dict[str, Any] = {}
    failed: dict[str, str] = {}
    for name, value in ns.items():
        if name.startswith("__"):
            continue
        try:
            dumps(value)
            good[name] = value
        except Exception as exc:
            failed[name] = f"{type(exc).__name__}: {str(exc)[:80]}"
    return (dumps(good) if good else None), failed


def unpickle_namespace(data: bytes) -> dict[str, Any]:
    """A namespace restored from :func:`pickle_namespace`'s bytes (either library)."""
    return dict(pickle.loads(data))


def run_recording(
    scripts: list[str], tools: dict[str, Any]
) -> tuple[dict[str, Any], list[list[dict[str, Any]]]]:
    """Run ``scripts`` in one namespace with live tools, recording each script's calls."""
    ns: dict[str, Any] = {"__name__": "__sandbox__"}
    records: list[list[dict[str, Any]]] = []
    for code in scripts:
        calls: list[dict[str, Any]] = []
        for name, fn in tools.items():
            ns[name] = _recorder(name, fn, calls)
        exec(compile(code, "<script>", "exec"), ns)
        records.append(calls)
    for name in tools:
        ns.pop(name, None)
    return ns, records


def _recorder(name: str, fn: Any, calls: list[dict[str, Any]]) -> Any:
    def call(*args: Any) -> Any:
        result = fn(*args)
        calls.append({"name": name, "args": json.loads(json.dumps(list(args))), "result": result})
        return result

    return call


def replay(scripts: list[str], records: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """Rebuild a namespace by re-running ``scripts``, answering calls from ``records``.

    Raises:
        ReplayDivergence: a script made a call that is not the next recorded one, or
            made fewer calls than were recorded. A divergence that never reaches a
            tool call (``random``, the clock) is NOT detected; see RESULTS.md §4.
    """
    ns: dict[str, Any] = {"__name__": "__sandbox__"}
    for index, (code, calls) in enumerate(zip(scripts, records, strict=True)):
        queue = list(calls)

        def answer(name: str) -> Any:
            def call(*args: Any) -> Any:
                if not queue:
                    raise ReplayDivergence(f"script {index}: extra call {name}{args}")
                expected = queue.pop(0)
                got = json.loads(json.dumps(list(args)))
                if expected["name"] != name or expected["args"] != got:
                    raise ReplayDivergence(
                        f"script {index}: called {name}{tuple(got)}, recorded "
                        f"{expected['name']}{tuple(expected['args'])}"
                    )
                return expected["result"]

            return call

        names = {c["name"] for c in calls}
        for name in names:
            ns[name] = answer(name)
        exec(compile(code, "<script>", "exec"), ns)
        if queue:
            raise ReplayDivergence(f"script {index}: {len(queue)} recorded call(s) never made")
        for name in names:
            ns.pop(name, None)
    return ns


def timed(fn: Any, *args: Any) -> tuple[Any, float]:
    """``fn(*args)`` and its wall time in milliseconds."""
    start = time.perf_counter()
    result = fn(*args)
    return result, (time.perf_counter() - start) * 1000
