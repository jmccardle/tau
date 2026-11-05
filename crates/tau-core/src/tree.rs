//! The session tree: nodes, cursors, labels, the scratchpad, and the records a
//! store writes. The shape of model.md §1, minus copy relations.

use std::collections::{BTreeMap, BTreeSet};

mod part;
mod replay;
mod values;

pub use part::{Part, Replay, ReplayError};
pub use replay::{load, reconcile};
pub use values::{
    Author, Body, Config, Item, ItemData, Move, Node, Outcome, Provenance, Record, Scratchpad, Stop,
};

pub type NodeId = u64;
pub type CursorId = u64;
pub type ItemId = u64;

#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    NoSuchNode(NodeId),
    NoSuchCursor(CursorId),
    NoSuchLabel(String),
    /// A call on a closed cursor (D17): the id is real, its last node is in
    /// `Session::closed`.
    Closed(CursorId),
    /// A frontend's `Session.step` on a busy cursor.
    Busy(CursorId),
    /// A hook's move, write, or auto-step change on a cursor with no cursor
    /// hook job out (a handle used after its hook returned).
    NotInHook(CursorId),
    /// Removing an item that is not in the scratchpad.
    NoSuchItem(ItemId),
    /// A write that delivers an item not pending for that cursor (not in the
    /// scratchpad, or addressed to another cursor).
    NotPending(ItemId),
    /// A node naming a call id twice: a `Results` node's outcomes, or an
    /// `Assistant` node's calls.
    DuplicateCall,
    /// A record replay cannot apply: a node id or a cursor out of sequence, a
    /// move from a node the cursor is not at. Its position in the log.
    BadRecord(usize),
    /// An answer to no job: none is out for the cursor, or the result
    /// is not of the kind the job asked for. Nothing changed.
    WrongAnswer(CursorId),
}

/// The one wording of core's refusals: the Python exception message and a
/// step's `Failed` reason both come from here.
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NoSuchNode(n) => write!(f, "no node {n}"),
            Self::NoSuchCursor(c) => write!(f, "no cursor {c}"),
            Self::NoSuchLabel(l) => write!(f, "no label {l:?}"),
            Self::Closed(c) => write!(f, "cursor {c} is closed"),
            Self::Busy(c) => write!(f, "cursor {c} has a step running"),
            Self::NotInHook(c) => write!(f, "no cursor hook is running on cursor {c}"),
            Self::NoSuchItem(i) => write!(f, "no item {i}"),
            Self::NotPending(i) => write!(f, "item {i} is not pending for that cursor"),
            Self::DuplicateCall => write!(f, "a node names a call id twice"),
            Self::WrongAnswer(c) => write!(f, "that answer answers no job on cursor {c}"),
            Self::BadRecord(at) => write!(f, "record {at} does not replay"),
        }
    }
}

/// A reply with `usage` taken out of its `meta` (the rest of `meta` is kept); other bodies, and a
/// `meta` that is not a JSON object with a `usage`, as they are.
fn without_usage(body: &Body) -> Body {
    let Body::Assistant {
        parts,
        stop,
        finish_reason,
        meta: Some(meta),
    } = body
    else {
        return body.clone();
    };
    let Ok(serde_json::Value::Object(mut fields)) = serde_json::from_str(meta) else {
        return body.clone();
    };
    if fields.remove("usage").is_none() {
        return body.clone();
    }
    Body::Assistant {
        parts: parts.clone(),
        stop: *stop,
        finish_reason: finish_reason.clone(),
        meta: Some(serde_json::Value::Object(fields).to_string()),
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Next {
    Wait,
    Sample,
    RunTools,
}

/// The append-only forest of nodes and the reads over it: no cursors, labels,
/// or scratchpad. Every node was checked when it was added.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Tree {
    nodes: Vec<Node>,
    /// Per node, by id: the nearest `Config` on its walk (`config_at`).
    configs: Vec<NodeId>,
    /// Per node, by id: its children in append order.
    kids: Vec<Vec<NodeId>>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Session {
    pub tree: Tree,
    at: BTreeMap<CursorId, NodeId>,
    /// Cursors closed (D17): still in `at`, so ids stay dense; in no other map.
    closed: BTreeSet<CursorId>,
    labels: BTreeMap<String, CursorId>,
    pub auto: BTreeMap<CursorId, bool>,
    pad: Scratchpad,
    /// Per cursor: bumped by every item added (`add_item`); no other change.
    pub changes: BTreeMap<CursorId, u64>,
    pub moves: Vec<Move>,
    pub outbox: Vec<Record>,
    /// Records queued, or the scratchpad changed, since the last `Sync`.
    unsynced: bool,
    /// The scratchpad changed since the last `Sync`.
    pad_changed: bool,
    pub notices: Vec<(String, String)>,
}

impl Session {
    /// A session with a `Config` root, a `System` under it, and one cursor there.
    ///
    /// # Panics
    ///
    /// Never: a root has nothing to refuse, and that child passes V2.
    pub fn create(config: impl Into<Config>, system: &str, time: u64) -> (Self, CursorId) {
        let mut s = Self::default();
        let root = s.append_root(Author::Core, config.into(), time);
        let sys = s
            .append(
                root,
                Author::Core,
                Body::System(system.into()),
                vec![],
                time,
            )
            .unwrap();
        let cursor = s.open(sys, Author::Core, time).unwrap();
        (s, cursor)
    }

    /// A new session holding a copy of `walk(node)`: every node as a record (body,
    /// `by` and `time` as they were, `delivers` emptied: item ids belong to one session),
    /// parents rebuilt, so node *i* of the copy is node *i* of the walk. Cursor 0 is opened
    /// on the copied tip at `time`, auto-step off. Nothing else is copied: other branches,
    /// cursors, labels, pending items.
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `node` is not a node.
    ///
    /// # Panics
    ///
    /// Never: a walk starts at a `Config` root, and each copied node passed V2 once.
    pub fn fork(&self, node: NodeId, time: u64) -> Result<(Self, CursorId), Error> {
        self.tree.node(node)?;
        let mut copy = Self::default();
        let mut tip = 0;
        // Provenance is kept where its source is in the walk too, renumbered; a source
        // outside the walk is not in the copy, so the field is empty there.
        let mut renumbered: BTreeMap<NodeId, NodeId> = BTreeMap::new();
        for n in self.tree.walk(node) {
            let from = Provenance {
                copied_from: n.copied_from.and_then(|c| renumbered.get(&c).copied()),
                derived_from: n.derived_from.and_then(|d| renumbered.get(&d).copied()),
            };
            // A copy's usage is its source's: where the source is not in the walk, the copy
            // would otherwise count as a reply of its own.
            let cut_off = (n.copied_from.is_some() && from.copied_from.is_none())
                || (n.derived_from.is_some() && from.derived_from.is_none());
            let body = if cut_off {
                without_usage(&n.body)
            } else {
                n.body.clone()
            };
            tip = match (&body, n.parent) {
                (Body::Config(c), None) => copy.append_root(n.by.clone(), c.clone(), n.time),
                (body, Some(_)) => copy
                    .append_from(tip, n.by.clone(), body.clone(), vec![], n.time, from)
                    .expect("a node that passed V2 passes it again"),
                (_, None) => unreachable!("a root is a Config"),
            };
            renumbered.insert(n.id, tip);
        }
        let cursor = copy.open(tip, Author::Core, time)?;
        Ok((copy, cursor))
    }

    /// Open a cursor at `at`.
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `at` is not a node.
    pub fn open(&mut self, at: NodeId, by: Author, time: u64) -> Result<CursorId, Error> {
        self.tree.node(at)?;
        let id = self.at.len() as CursorId;
        self.auto.insert(id, false);
        self.place(id, None, at, by, time);
        Ok(id)
    }

    /// Append a node under `parent`; the only check is V2 (I5).
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `parent` is not a node; `DuplicateCall` for a `Results`
    /// or `Assistant` node that names a call id twice (V2).
    pub fn append(
        &mut self,
        parent: NodeId,
        by: Author,
        body: Body,
        delivers: Vec<ItemId>,
        time: u64,
    ) -> Result<NodeId, Error> {
        self.append_from(parent, by, body, delivers, time, Provenance::default())
    }

    /// `append`, recording where the node's content came from.
    ///
    /// # Errors
    ///
    /// As `append`; `NoSuchNode` too if `from` names a node that is not there.
    pub fn append_from(
        &mut self,
        parent: NodeId,
        by: Author,
        body: Body,
        delivers: Vec<ItemId>,
        time: u64,
        from: Provenance,
    ) -> Result<NodeId, Error> {
        let _span = tracing::info_span!("core.append").entered();
        let node = self
            .tree
            .check(Some(parent), by, body, delivers, time, from)?;
        let id = node.id;
        self.tree.push(node.clone());
        self.queue(Record::Write { node, mv: None });
        Ok(id)
    }

    /// Append a root: a node with no parent, which is always a `Config`. Cannot
    /// be refused.
    pub fn append_root(&mut self, by: Author, config: Config, time: u64) -> NodeId {
        let _span = tracing::info_span!("core.append_root").entered();
        let node = Node {
            id: self.tree.node_count() as NodeId,
            parent: None,
            time,
            by,
            body: Body::Config(config),
            delivers: vec![],
            copied_from: None,
            derived_from: None,
            typed: None,
        };
        let id = node.id;
        self.tree.push(node.clone());
        self.queue(Record::Write { node, mv: None });
        id
    }

    /// Append under `at` and move onto it, as one record; then drop delivered items.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open; else as `append`.
    pub fn write(
        &mut self,
        cursor: CursorId,
        body: Body,
        delivers: Vec<ItemId>,
        by: Author,
        time: u64,
    ) -> Result<NodeId, Error> {
        self.write_from(cursor, body, delivers, by, time, Provenance::default())
    }

    /// `write`, recording where the node's content came from.
    ///
    /// # Errors
    ///
    /// As `write`; `NoSuchNode` too if `from` names a node that is not there.
    pub fn write_from(
        &mut self,
        cursor: CursorId,
        body: Body,
        delivers: Vec<ItemId>,
        by: Author,
        time: u64,
        from: Provenance,
    ) -> Result<NodeId, Error> {
        let _span = tracing::info_span!("core.write", cursor).entered();
        let at = self.at(cursor)?;
        if let Some(bad) = delivers
            .iter()
            .find(|d| !self.items(cursor).any(|i| i.id == **d))
        {
            return Err(Error::NotPending(*bad));
        }
        let mut node = self
            .tree
            .check(Some(at), by.clone(), body, delivers, time, from)?;
        node.typed = self.typed(&node);
        let id = node.id;
        if !node.delivers.is_empty() {
            self.pad.items.retain(|i| !node.delivers.contains(&i.id));
            self.touch_pad();
        }
        let mv = Move {
            cursor,
            from: Some(at),
            to: node.id,
            by,
            time,
        };
        self.tree.push(node.clone());
        self.at.insert(cursor, node.id);
        self.moves.push(mv.clone());
        self.queue(Record::Write { node, mv: Some(mv) });
        Ok(id)
    }

    /// What a person typed, if `node` is the `User` node an input became and a hook changed
    /// its text: the delivered `Input` item's text.
    fn typed(&self, node: &Node) -> Option<String> {
        let Body::User(text) = &node.body else {
            return None;
        };
        self.pad
            .items
            .iter()
            .filter(|i| node.delivers.contains(&i.id))
            .find_map(|i| match &i.data {
                ItemData::Input { text: typed, .. } if typed != text => Some(typed.clone()),
                _ => None,
            })
    }

    /// Move `cursor` to `to`; no record when it is already there.
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `to` is not a node; `NoSuchCursor` if `cursor` is not open.
    pub fn move_to(
        &mut self,
        cursor: CursorId,
        to: NodeId,
        by: Author,
        time: u64,
    ) -> Result<(), Error> {
        self.tree.node(to)?;
        let from = self.at(cursor)?;
        if from != to {
            self.place(cursor, Some(from), to, by, time);
        }
        Ok(())
    }

    fn place(&mut self, cursor: CursorId, from: Option<NodeId>, to: NodeId, by: Author, time: u64) {
        let mv = Move {
            cursor,
            from,
            to,
            by,
            time,
        };
        self.at.insert(cursor, to);
        self.moves.push(mv.clone());
        self.queue(Record::Move(mv));
    }

    /// Where `cursor` is. Every call on a cursor goes through here, so this is
    /// the one place a closed cursor is refused (D17).
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` was never opened; `Closed` if it was closed.
    pub fn at(&self, cursor: CursorId) -> Result<NodeId, Error> {
        match self.at.get(&cursor) {
            None => Err(Error::NoSuchCursor(cursor)),
            Some(_) if self.closed.contains(&cursor) => Err(Error::Closed(cursor)),
            Some(node) => Ok(*node),
        }
    }

    /// Close `cursor`: its auto-step and pending items go, a `Close` record is
    /// queued, and its id, last node and labels stay. The caller has checked it
    /// is at rest.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` was never opened; `Closed` if already closed.
    pub fn close(&mut self, cursor: CursorId) -> Result<(), Error> {
        self.at(cursor)?;
        self.auto.remove(&cursor);
        if self.items(cursor).next().is_some() {
            self.pad.items.retain(|i| i.to != cursor);
            self.touch_pad();
        }
        self.closed.insert(cursor);
        self.queue(Record::Close { cursor });
        Ok(())
    }

    /// Each closed cursor with its last node, in id order.
    pub fn closed(&self) -> impl Iterator<Item = (CursorId, NodeId)> + '_ {
        self.closed.iter().map(|c| (*c, self.at[c]))
    }

    /// Set a label on a cursor; a label already set elsewhere moves.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open.
    pub fn set_label(&mut self, label: &str, cursor: CursorId) -> Result<(), Error> {
        self.at(cursor)?;
        if self.labels.get(label) != Some(&cursor) {
            self.labels.insert(label.into(), cursor);
            self.queue(Record::Label {
                label: label.into(),
                cursor: Some(cursor),
            });
        }
        Ok(())
    }

    /// Remove a label.
    ///
    /// # Errors
    ///
    /// `NoSuchLabel` if `label` is not set.
    pub fn remove_label(&mut self, label: &str) -> Result<(), Error> {
        self.labels
            .remove(label)
            .ok_or_else(|| Error::NoSuchLabel(label.into()))?;
        self.queue(Record::Label {
            label: label.into(),
            cursor: None,
        });
        Ok(())
    }

    /// `next` from the end of the walk, without building it: only the last
    /// node that is not Config or Custom decides.
    #[must_use]
    pub fn next(&self, cursor: CursorId) -> Next {
        self.deciding(cursor).map_or(Next::Wait, |(next, _)| next)
    }

    /// `next`, with the node that decided it: the last node of the walk that is
    /// not `Config` or `Custom`. `None` when there is none (then `next` is `Wait`).
    /// A batch is read from this node, not from `at`.
    #[must_use]
    pub fn deciding(&self, cursor: CursorId) -> Option<(Next, NodeId)> {
        self.tree.deciding(self.at(cursor).ok()?)
    }

    /// The cursor `label` is on.
    ///
    /// # Errors
    ///
    /// `NoSuchLabel` if `label` is not set.
    pub fn label(&self, label: &str) -> Result<CursorId, Error> {
        self.labels
            .get(label)
            .copied()
            .ok_or_else(|| Error::NoSuchLabel(label.into()))
    }

    /// A cursor's labels, in string order.
    pub fn labels_of(&self, cursor: CursorId) -> impl Iterator<Item = &str> {
        self.labels
            .iter()
            .filter(move |(_, c)| **c == cursor)
            .map(|(l, _)| l.as_str())
    }

    /// `cursor`'s moves from index `since` in `Session::moves` on.
    pub fn moves_of(&self, cursor: CursorId, since: usize) -> impl Iterator<Item = &Move> {
        self.moves[since.min(self.moves.len())..]
            .iter()
            .filter(move |m| m.cursor == cursor)
    }

    /// Every label with its cursor, in string order.
    pub fn labels(&self) -> impl Iterator<Item = (&str, CursorId)> {
        self.labels.iter().map(|(l, c)| (l.as_str(), *c))
    }

    pub fn add_item(&mut self, to: CursorId, data: ItemData) -> ItemId {
        let id = self.pad.next_item;
        self.pad.next_item += 1;
        self.pad.items.push(Item { id, to, data });
        self.touch_pad();
        *self.changes.entry(to).or_default() += 1; // rules: allow(entry): BTreeMap API
        id
    }

    pub fn remove_item(&mut self, id: ItemId) {
        if let Some(i) = self.pad.items.iter().position(|i| i.id == id) {
            self.pad.items.remove(i);
            self.touch_pad();
        }
    }

    fn queue(&mut self, r: Record) {
        self.outbox.push(r);
        self.unsynced = true;
    }

    fn touch_pad(&mut self) {
        self.pad_changed = true;
        self.unsynced = true;
    }

    /// A sync point: queue a `Sync` if anything changed since the last one,
    /// carrying the scratchpad if it changed.
    pub fn sync(&mut self) {
        if self.unsynced {
            let pad = self.pad_changed.then(|| self.pad.clone());
            self.outbox.push(Record::Sync { pad });
            self.unsynced = false;
            self.pad_changed = false;
        }
    }

    /// The scratchpad as it stands.
    #[must_use]
    pub fn scratchpad(&self) -> &Scratchpad {
        &self.pad
    }

    /// True when every queued record is covered by a `Sync`.
    #[must_use]
    pub fn synced(&self) -> bool {
        !self.unsynced
    }

    pub fn items(&self, cursor: CursorId) -> impl Iterator<Item = &Item> {
        self.pad.items.iter().filter(move |i| i.to == cursor)
    }
}

impl Tree {
    /// Add a checked node, with its nearest `Config`.
    fn push(&mut self, node: Node) {
        let config = match (&node.body, node.parent) {
            (Body::Config(_), _) | (_, None) => node.id,
            (_, Some(p)) => self.configs[usize::try_from(p).expect("a checked parent")],
        };
        self.configs.push(config);
        self.kids.push(vec![]);
        if let Some(p) = node.parent {
            self.kids[usize::try_from(p).expect("a checked parent")].push(node.id);
        }
        self.nodes.push(node);
    }

    /// The node `push` would add: V2 (I5) is the only check.
    fn check(
        &self,
        parent: Option<NodeId>,
        by: Author,
        body: Body,
        delivers: Vec<ItemId>,
        time: u64,
        from: Provenance,
    ) -> Result<Node, Error> {
        if let Some(p) = parent {
            self.node(p)?;
        }
        for source in [from.copied_from, from.derived_from].into_iter().flatten() {
            self.node(source)?;
        }
        let ids: Vec<&str> = match &body {
            Body::Results(out) => out.iter().map(|(c, ..)| c.as_str()).collect(),
            Body::Assistant { parts, .. } => parts
                .iter()
                .filter_map(|p| match p {
                    Part::Call { id, .. } => Some(id.as_str()),
                    _ => None,
                })
                .collect(),
            _ => vec![],
        };
        if (1..ids.len()).any(|i| ids[..i].contains(&ids[i])) {
            return Err(Error::DuplicateCall); // V2
        }
        Ok(Node {
            id: self.nodes.len() as NodeId,
            parent,
            time,
            by,
            body,
            delivers,
            copied_from: from.copied_from,
            derived_from: from.derived_from,
            typed: None,
        })
    }

    /// The node `id`.
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `id` is not a node.
    pub fn node(&self, id: NodeId) -> Result<&Node, Error> {
        self.nodes
            .get(usize::try_from(id).unwrap_or(usize::MAX))
            .ok_or(Error::NoSuchNode(id))
    }

    /// The nodes root to `n`, inclusive; empty if `n` is not a node.
    #[must_use]
    pub fn walk(&self, n: NodeId) -> Vec<&Node> {
        let mut out: Vec<&Node> = std::iter::successors(self.node(n).ok(), |x| {
            x.parent.and_then(|p| self.node(p).ok())
        })
        .collect();
        out.reverse();
        out
    }

    /// The nearest `Config` on `walk(n)`; always defined, since every root is
    /// one. Constant time: each node records its own when added.
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `n` is not a node.
    ///
    /// # Panics
    ///
    /// If a root is not `Config`, which `append_root` and replay rule out.
    pub fn config_at(&self, n: NodeId) -> Result<&Config, Error> {
        self.node(n)?;
        let config = self.configs[usize::try_from(n).expect("a node id")];
        match &self.node(config)?.body {
            Body::Config(c) => Ok(c),
            _ => panic!("a root is a Config"),
        }
    }

    /// The deepest node on both walks; `None` across trees.
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `a` or `b` is not a node.
    pub fn lca(&self, a: NodeId, b: NodeId) -> Result<Option<NodeId>, Error> {
        self.node(b)?;
        let on_a: std::collections::BTreeSet<NodeId> =
            self.walk(self.node(a)?.id).iter().map(|n| n.id).collect();
        Ok(self
            .walk(b)
            .iter()
            .rev()
            .map(|n| n.id)
            .find(|id| on_a.contains(id)))
    }

    /// The children of `n`, in append order.
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `n` is not a node.
    pub fn children(&self, n: NodeId) -> Result<&[NodeId], Error> {
        self.kids
            .get(usize::try_from(n).unwrap_or(usize::MAX))
            .map(Vec::as_slice)
            .ok_or(Error::NoSuchNode(n))
    }

    /// How many nodes the tree has; ids are `0..node_count`.
    #[must_use]
    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }

    /// `next`, with the node that decided it, for the walk ending at `at`: the
    /// last node that is not `Config` or `Custom`.
    #[must_use]
    pub fn deciding(&self, at: NodeId) -> Option<(Next, NodeId)> {
        let last = std::iter::successors(self.node(at).ok(), |x| {
            x.parent.and_then(|p| self.node(p).ok())
        })
        .find(|n| !matches!(n.body, Body::Config(_) | Body::Custom { .. }))?;
        Some((decide(Some(last)), last.id))
    }
}

#[must_use]
pub fn calls(body: &Body) -> Vec<(&str, &str, &str)> {
    match body {
        Body::Assistant {
            parts,
            stop: Stop::Tools,
            ..
        } => parts
            .iter()
            .filter_map(|p| match p {
                Part::Call {
                    id, tool, args_raw, ..
                } => Some((id.as_str(), tool.as_str(), args_raw.as_str())),
                _ => None,
            })
            .collect(),
        _ => vec![],
    }
}

#[must_use]
pub fn next(walk: &[&Node]) -> Next {
    decide(
        walk.iter()
            .rev()
            .find(|n| !matches!(n.body, Body::Config(_) | Body::Custom { .. }))
            .copied(),
    )
}

fn decide(last: Option<&Node>) -> Next {
    match last.map(|n| &n.body) {
        Some(Body::User(_) | Body::Results(_)) => Next::Sample,
        Some(b) if !calls(b).is_empty() => Next::RunTools,
        _ => Next::Wait,
    }
}
