//! Residency (D14): which bodies are in memory. Not state, so equality
//! leaves it out.

use super::{Body, Error, Node, NodeId, Session};

/// A node id as an index into the per-node vectors; past the end if it
/// cannot be one.
pub(super) fn slot(id: NodeId) -> usize {
    usize::try_from(id).unwrap_or(usize::MAX)
}

impl PartialEq for Session {
    fn eq(&self, o: &Self) -> bool {
        let Self {
            nodes,
            parents,
            configs,
            cooled: _,
            at,
            labels,
            auto,
            pad,
            changes,
            moves,
            outbox,
            taken,
            unsynced,
            pad_changed,
            notices,
        } = self;
        (nodes, parents, configs, at, labels, auto, pad)
            == (
                &o.nodes, &o.parents, &o.configs, &o.at, &o.labels, &o.auto, &o.pad,
            )
            && (
                changes,
                moves,
                outbox,
                taken,
                unsynced,
                pad_changed,
                notices,
            ) == (
                &o.changes,
                &o.moves,
                &o.outbox,
                &o.taken,
                &o.unsynced,
                &o.pad_changed,
                &o.notices,
            )
    }
}

impl Eq for Session {}

impl Session {
    /// A node's parent, whether its body is in memory or not.
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `id` is not a node.
    pub fn parent(&self, id: NodeId) -> Result<Option<NodeId>, Error> {
        self.parents
            .get(slot(id))
            .copied()
            .ok_or(Error::NoSuchNode(id))
    }

    /// Ok when every body on `walk(n)` is in memory.
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `n` is not a node; `Stored` with the ids of the bodies
    /// that are not, root first.
    pub fn resident(&self, n: NodeId) -> Result<(), Error> {
        self.parent(n)?;
        let mut stored = vec![];
        let mut at = Some(n);
        while let Some(i) = at {
            if self.nodes[slot(i)].is_none() {
                stored.push(i);
            }
            at = self.parents[slot(i)];
        }
        if stored.is_empty() {
            return Ok(());
        }
        stored.reverse();
        Err(Error::Stored(stored))
    }

    /// Drop from memory the body of every node below `durable` (the store
    /// has it synced) that is on no cursor's walk and is not `Config`. Only
    /// does work after a move or an append. Returns how many were dropped.
    pub fn evict(&mut self, durable: NodeId) -> usize {
        if !self.cooled {
            return 0;
        }
        let mut on = vec![false; self.nodes.len()];
        for &tip in self.at.values() {
            let mut at = Some(tip);
            while let Some(i) = at.filter(|&i| !on[slot(i)]) {
                on[slot(i)] = true;
                at = self.parents[slot(i)];
            }
        }
        let (mut dropped, mut later) = (0, false);
        for (i, held) in self.nodes.iter_mut().enumerate() {
            let cold = !on[i]
                && held
                    .as_ref()
                    .is_some_and(|n| !matches!(n.body, Body::Config(_)));
            if cold && (i as NodeId) < durable {
                *held = None;
                dropped += 1;
            } else if cold {
                later = true;
            }
        }
        self.cooled = later;
        dropped
    }

    /// Put a stored body back, as the store read it from the log.
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `node.id` is not a node; `BadRecord` if `node` does
    /// not have the parent the session holds for it.
    pub fn restore(&mut self, node: Node) -> Result<(), Error> {
        if self.parent(node.id)? != node.parent {
            return Err(Error::BadRecord(0));
        }
        let held = &mut self.nodes[slot(node.id)];
        if held.is_none() {
            *held = Some(Box::new(node));
        }
        Ok(())
    }
}
