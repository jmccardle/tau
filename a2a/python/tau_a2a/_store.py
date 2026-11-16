"""What the A2A server keeps beside the SDK's task store, in a SQLite file of its own: which cursor
each context is (per owner), and for each task its cursor and the node it ended on (the tau-fork
extension's `at`). The τ session's log stays the truth for what was said."""

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
create table if not exists tau_contexts (
    owner text not null, context_id text not null, cursor integer not null, created real not null,
    primary key (owner, context_id));
create table if not exists tau_tasks (
    task_id text primary key, owner text not null, context_id text not null,
    cursor integer not null, end_node integer);
"""


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    owner: str
    context_id: str
    cursor: int
    end_node: int | None


class Store:
    """A small synchronous SQLite table pair: each call is one short statement."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
        self._db.execute("pragma journal_mode=wal")
        self._db.executescript(_SCHEMA)

    def cursor_of(self, owner: str, context_id: str) -> int | None:
        row = self._db.execute(
            "select cursor from tau_contexts where owner = ? and context_id = ?",
            (owner, context_id),
        ).fetchone()
        return None if row is None else int(row[0])

    def bind(self, owner: str, context_id: str, cursor: int) -> None:
        self._db.execute(
            "insert into tau_contexts values (?, ?, ?, ?)", (owner, context_id, cursor, time.time())
        )

    def add_task(self, task_id: str, owner: str, context_id: str, cursor: int) -> None:
        self._db.execute(
            "insert or ignore into tau_tasks values (?, ?, ?, ?, null)",
            (task_id, owner, context_id, cursor),
        )

    def ended(self, task_id: str, node: int) -> None:
        self._db.execute("update tau_tasks set end_node = ? where task_id = ?", (node, task_id))

    def task(self, task_id: str, owner: str) -> TaskRecord | None:
        """The task, if `owner` owns it: another owner's task is as good as missing."""
        row = self._db.execute(
            "select task_id, owner, context_id, cursor, end_node from tau_tasks"
            " where task_id = ? and owner = ?",
            (task_id, owner),
        ).fetchone()
        return None if row is None else TaskRecord(*row)

    def close(self) -> None:
        self._db.close()
