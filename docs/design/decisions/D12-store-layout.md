# D12: One record log, plus a scratchpad file

Status: proposed (Phase 1, sixth revision, 2026-10-27; updated in the seventh)

## Context

The plan gave each session a node log and a cursor-move log, and model.md
left the on-disk layout to a Phase 3 ADR. But several writes must be durable
together: a phase's commit and its move, a hook's `cursor.write` and its
move, a delivered input's `User` node and its move. With two logs,
"together" needs a two-phase protocol. The seventh revision adds labels,
which change.

The sixth revision adds the scratchpad, which is modifiable: items are
updated and removed. A log of item changes would keep every input's text
forever, alongside the node that delivered it.

## Decision

- **One record log per session**, append-only, with a versioned header.
  Each **record** is one durable write: one or more nodes with at most one
  move, or one label change (`Label {label, cursor?}`). Each record is
  length-prefixed and checksummed, so a torn final record is detected
  exactly and ignored on load (I7).
- **One scratchpad file per session**, with a versioned header, holding the
  current items and the next item id. Each change rewrites it whole and
  atomically: write a new file, sync it, rename it over the old one, sync the
  directory. It is never torn, and a removed item is gone from disk.
- **Order of writes.**
  - Adding an item is durable before the cursor it wakes is woken.
  - A delivering record (nodes with `delivers`, and the move) is durable
    before its items are removed.
  - A batch's outcomes are not written until the join: nothing about a
    running batch is stored (D4).
- **Load** replays the record log (nodes, moves, labels), then reads the
  scratchpad file and drops every item some node delivers. That rule is the
  whole reconciliation.
- Item ids are never reused, because the next id is stored with the items.
  A node's `delivers` therefore names one item forever.
- The lease (`flock`) is taken on the record log.
- **Writes happen inside the Rust call that makes them** (D11), and are
  durable before it returns. They are never a job for the Python driver.
- **JMFTS (Phase 7) is a replica**, fed from the local record log outside
  any step. The local log is always the store of record: a step never waits
  on the network, and a JMFTS outage never stops one. Replaying records into
  JMFTS is idempotent by record position. The lease stays local.
- The record encoding (JSON lines, or binary records) is decided in Phase 3.

## Alternatives

| option | why not |
|---|---|
| A node log and a move log (plan) | Node and move cannot be durable together without a protocol across files. |
| Scratchpad changes as records in the same log | Atomic with nodes, but every input's text is stored twice: once in its add record, once in its delivered node. |
| JMFTS as the store of record | Every write becomes a network round trip on the step's path. A record (nodes plus a move) would need a transactional batch write, a timed-out write is ambiguous, and JMFTS has no lease. As a replica, none of that is on the step's path. |
| Labels in the scratchpad file | Labels are names, not messages: nothing consumes them, and a crash must not lose a rename. Records keep them with the moves they name. |
| The scratchpad in a database (SQLite) | Another dependency and a second durability model for one small list. |
| Marking delivered items instead of removing them | A second write that a crash can lose; `delivers` already says it. |

## Consequences

- A step's write and its move can never be split by a crash.
- An input's content is stored once, in its node, after delivery.
- Load is a replay plus one fixed rule, with no guessing (I7).
- Cost: each item change rewrites the scratchpad file. It holds only open
  items, so it stays small; a frontend that adds items in a burst pays one
  rewrite each.
- Cost: an item consumed without a node may reappear after a crash before its
  removal (D2).
- Cost: a session on JMFTS alone cannot be opened and written from another
  machine; that needs its record log.
- Enforced by: the I7 proptests, including a crash injected between a
  delivering record and the items' removal, labels set and moved across a
  reload, and a torn-tail test.
