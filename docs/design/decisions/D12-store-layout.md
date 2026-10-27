# D12: One record log, plus a scratchpad file

Status: proposed (Phase 1); not yet spiked beyond `Session::outbox`

## Context

Several writes must be durable together: a commit and its move, a
`cursor.write` and its move. Two logs would need a protocol across files.
The scratchpad is modifiable, and a log of item changes would keep every
input's text twice.

## Decision

- **One record log per session**, append-only, with a versioned header. Each
  record is one durable write: a node with at most one move, a move, or a
  label change. Length-prefixed and checksummed; a torn final record is
  ignored on load.
- **One scratchpad file per session** holding the current items and the next
  item id, rewritten atomically (write, sync, rename, sync the directory).
- **Order:** an added item is durable before its cursor wakes; a delivering
  record is durable before its items are removed; nothing about a running
  batch is written (D4).
- **Load** replays the log, reads the scratchpad, and drops delivered items
  (`Session::reconcile`).
- Writes happen inside the Rust call (D11). The lease (`flock`) is on the log.
- **JMFTS (Phase 7) is a replica** fed from the local log outside any step;
  replay into it is idempotent by record position.
- The record encoding is Phase 3's.

## Alternatives

| option | why not |
|---|---|
| A node log and a move log | No atomic node-plus-move. |
| Item changes as log records | Every input stored twice. |
| JMFTS as the store of record | A network round trip on every write; no lease. |
| Labels in the scratchpad file | A crash must not lose a rename. |
| SQLite for the scratchpad | A second durability model for a small list. |

## Consequences

- A write and its move can never be split by a crash.
- Cost: each item change rewrites the scratchpad file.
- Enforced by the I7 proptests (Phase 3), including a torn tail and a crash
  between a delivering record and the removal.
