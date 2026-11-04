# D12: One record log, plus a scratchpad file

Status: accepted (Phase 1); built in `crates/tau-store` (3b)

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
- **Sync points** (D11): records are buffered up to a `Sync`; at a `Sync`
  the log is synced first, then the scratchpad file rewritten from the
  `Sync`'s own copy (`Record::Sync { pad }`), so the file is never newer
  than the synced log. The log never holds items. A save that finds several
  `Sync`s queued writes through the last with one log sync, then the newest
  pad among them: one sync per save, not per `Sync`.
- **Order:** an added item is durable before its cursor wakes; a delivering
  record is durable before its items are removed (the log sync precedes the
  scratchpad rewrite); nothing about a running
  batch is written (D4).
- **Load** replays the log, reads the scratchpad, and drops delivered items
  (`Session::load`, pure; the store only reads the files).
- Writes happen inside the Rust call (D11). The lease (`flock`) is on the log.
- **JMFTS (Phase 7) is a replica** fed from the local log outside any step;
  replay into it is idempotent by record position.
- **Encoding:** each file starts with an 8-byte magic and a `u32` version. The
  version marks a released format: it changes between tagged releases, never
  per commit (2026-10-31). A build refuses any other version with `Version`. A
  record is its length, a CRC-32 of length and payload, and the payload as
  JSON. A new session is written under a private name and hard-linked into
  place, so a log always holds its first `Sync` and `create` never replaces
  one.
- **Load cuts back to the last good `Sync`.** A record cut short or failing
  its checksum ends the scan; whatever follows the last `Sync` before it is
  cut (made durable, so a session goes on from there) and kept aside as
  `dropped-<offset>`. Damage mid-log is indistinguishable from a torn tail by
  checksum alone, so it is cut too, never believed, and the bytes are kept.
  A record whose length and checksum agree is intact: a crash cannot make
  one that does not decode, so load fails with `Undecodable` (file and record
  position) and cuts nothing.

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
- Cost: a stored plain turn is ~3 ms here, about ten fsyncs (log, scratchpad
  file, directory); opening 300 turns takes ~3 ms. `benches/tree.rs` in
  `crates/tau-store` measures the rest (a 1M-token session opens in
  ~7–27 ms; creating one from memory is one sync).
- Enforced by the I7 proptests in `crates/tau-core/tests/replay.rs`
  (including a crash between the log sync and the scratchpad rewrite) and
  `a_torn_tail_opens_as_the_last_sync_before_it` in
  `crates/tau-store/tests/store.rs` (a log cut anywhere, plus garbage, opens
  as the last sync before the cut, and the session goes on from there).

## Note: provenance fields (8E-3)

A `Write` record's node gains `copied_from` and `derived_from` (D24 M1), each a node id, written
only when set. A record without them loads as none, so `VERSION` stays 1: this is the first
release of the format and a log written before the fields existed is a log with none.
