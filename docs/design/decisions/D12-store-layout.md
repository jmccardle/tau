# D12: One record log, plus a scratchpad file

Status: proposed (Phase 1, sixth revision, 2026-10-27)

## Context

The plan gave each session a node log and a cursor-move log, and model.md
left the on-disk layout to a Phase 3 ADR. But several writes must be durable
together: a step's node and its move, a hook's `cursor.write` and its move,
an I6 restatement and the move onto it, a delivery and its move. With two
logs, "together" needs a two-phase protocol.

The sixth revision adds the scratchpad, which is modifiable: items are
updated and removed. A log of item changes would keep every input's text
forever, alongside the node that delivered it.

## Decision

- **One record log per session**, append-only, with a versioned header.
  Each **record** is one durable write: one or more nodes and at most one
  move. Each record is length-prefixed and checksummed, so a torn final
  record is detected exactly and ignored on load (I7).
- **One scratchpad file per session**, with a versioned header, holding the
  current items and the next item id. Each change rewrites it whole and
  atomically: write a new file, sync it, rename it over the old one, sync the
  directory. It is never torn, and a removed item is gone from disk.
- **Order of writes.**
  - Adding an item is durable before the cursor it wakes is woken.
  - A delivering write (nodes with `delivers`, and the move) is durable
    before its items are removed.
- **Load** replays the record log, then reads the scratchpad file and drops
  every item some node delivers. That rule is the whole reconciliation.
- Item ids are never reused, because the next id is stored with the items.
  A node's `delivers` therefore names one item forever.
- The lease (`flock`) is taken on the record log.
- The record encoding (JSON lines, or a binary frame per record) is decided
  in Phase 3. The trait boundary for JMFTS (Phase 7) is records in, records
  out, plus the scratchpad as a whole.

## Alternatives

| option | why not |
|---|---|
| A node log and a move log (plan) | Node and move cannot be durable together without a protocol across files. |
| Scratchpad changes as records in the same log | Atomic with nodes, but every input's text is stored twice: once in its add record, once in its delivered node. |
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
- Enforced by: the I7 proptests, including a crash injected between a
  delivering write and the item's removal, and a torn-tail test.
