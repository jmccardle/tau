//! Load (I7): rebuild a `Session` from the records a store wrote. Pure; the
//! store reads the files.

use super::{Body, Error, ItemId, Move, Record, Scratchpad, Session};

/// Replay the log's records, take the scratchpad file, then `reconcile`.
/// Nothing is queued.
///
/// # Errors
///
/// `BadRecord` with its position for the first record replay cannot apply.
pub fn load(records: impl IntoIterator<Item = Record>, pad: Scratchpad) -> Result<Session, Error> {
    let mut s = Session::default();
    for (i, r) in records.into_iter().enumerate() {
        replay(&mut s, r).map_err(|_| Error::BadRecord(i))?;
    }
    s.pad = pad;
    reconcile(&mut s);
    s.outbox.clear();
    s.unsynced = false;
    Ok(s)
}

fn replay(s: &mut Session, r: Record) -> Result<(), Error> {
    match r {
        Record::Write { node, mv } => {
            if node.parent.is_none() && !matches!(node.body, Body::Config(_)) {
                return Err(Error::BadRecord(0)); // a root is a Config
            }
            let checked = s.tree.check(
                node.parent,
                node.by.clone(),
                node.body.clone(),
                node.delivers.clone(),
                node.time,
            )?;
            if checked != node {
                return Err(Error::BadRecord(0));
            }
            s.tree.push(node);
            mv.map_or(Ok(()), |m| replay_move(s, m))
        }
        Record::Move(m) => replay_move(s, m),
        Record::Label { label, cursor } => {
            match cursor {
                Some(c) => drop(s.labels.insert(label, s.at(c).map(|_| c)?)),
                None => drop(s.labels.remove(&label)),
            }
            Ok(())
        }
        Record::Close { cursor } => {
            s.at(cursor)?;
            s.auto.remove(&cursor);
            s.closed.insert(cursor);
            Ok(())
        }
        Record::Sync { .. } => Ok(()),
    }
}

fn replay_move(s: &mut Session, m: Move) -> Result<(), Error> {
    s.tree.node(m.to)?;
    let ok = match m.from {
        None => m.cursor == s.at.len() as u64,
        Some(f) => s.at(m.cursor) == Ok(f),
    };
    if !ok {
        return Err(Error::BadRecord(0));
    }
    s.auto.entry(m.cursor).or_insert(false); // rules: allow(entry): BTreeMap API
    s.at.insert(m.cursor, m.to);
    s.moves.push(m);
    Ok(())
}

/// What load does after replay (I7): every open cursor at rest with auto-step
/// off, and every item a node delivers, or addressed to a closed cursor, dropped.
pub fn reconcile(s: &mut Session) {
    s.auto.values_mut().for_each(|a| *a = false);
    let closed = s.closed.clone();
    s.pad.items.retain(|i| !closed.contains(&i.to));
    let delivered: Vec<ItemId> = s
        .tree
        .nodes
        .iter()
        .flat_map(|n| n.delivers.iter().copied())
        .collect();
    s.pad.items.retain(|i| !delivered.contains(&i.id));
}
