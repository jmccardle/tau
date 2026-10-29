//! The session tree: nodes, cursors, labels, the scratchpad, and the records a
//! store writes. The shape of model.md §1, minus copy relations.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

mod replay;

pub use replay::{load, reconcile};

pub type NodeId = u64;
pub type CursorId = u64;
pub type ItemId = u64;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Part {
    Text(String),
    Thinking(String),
    Call {
        id: String,
        tool: String,
        args_raw: String,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Stop {
    End,
    Tools,
    Length,
    Refused,
    Interrupted,
    Error,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Outcome {
    Ok(String),
    Err(String),
    Interrupted,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Body {
    System(String),
    User(String),
    Assistant {
        parts: Vec<Part>,
        stop: Stop,
        /// The provider's own finish string, verbatim; `None` when it sent none
        /// (an interrupted reply, a failed sample).
        finish_reason: Option<String>,
    },
    /// One `(call id, outcome, by)` per call, in call order. `by` is who
    /// produced that outcome: `Tools`, or the extension that answered or edited it.
    Results(Vec<(String, Outcome, Author)>),
    Config(Config),
    Custom {
        kind: String,
        data: String,
    },
}

/// A config snapshot. `data` is opaque to core and typed by the driver.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Config {
    pub data: String,
}

impl From<&str> for Config {
    fn from(data: &str) -> Self {
        Self { data: data.into() }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Author {
    Frontend(String),
    Provider,
    Tools,
    Core,
    Extension(String),
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Node {
    pub id: NodeId,
    pub parent: Option<NodeId>,
    pub time: u64,
    pub by: Author,
    pub body: Body,
    pub delivers: Vec<ItemId>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Move {
    pub cursor: CursorId,
    pub from: Option<NodeId>,
    pub to: NodeId,
    pub by: Author,
    pub time: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum ItemData {
    Input { client: String, text: String },
    Extension { name: String, data: String },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Item {
    pub id: ItemId,
    pub to: CursorId,
    pub data: ItemData,
}

/// A cursor's items live here, for every cursor, with the next item id. The
/// store keeps it as the scratchpad file, never in the log (D12).
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Scratchpad {
    pub items: Vec<Item>,
    pub next_item: ItemId,
}

/// What the store must make durable, in order. Core never writes; it queues.
/// The store may buffer everything up to a `Sync`; at a `Sync` it syncs the
/// log, then, if `pad` is set, rewrites the scratchpad file with it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Record {
    Write {
        node: Node,
        mv: Option<Move>,
    },
    Move(Move),
    /// Set or move a label; no cursor removes it.
    Label {
        label: String,
        cursor: Option<CursorId>,
    },
    /// A sync point. `pad`: the scratchpad as it stands here, if it changed
    /// since the last sync.
    Sync {
        pad: Option<Scratchpad>,
    },
}

#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    NoSuchNode(NodeId),
    NoSuchCursor(CursorId),
    NoSuchLabel(String),
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
    /// A feed that answers no job: none is out for the cursor, or the result
    /// is not of the kind the job asked for. Nothing changed.
    WrongFeed(CursorId),
}

/// The one wording of core's refusals: the Python exception message and a
/// step's `Failed` reason both come from here.
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NoSuchNode(n) => write!(f, "no node {n}"),
            Self::NoSuchCursor(c) => write!(f, "no cursor {c}"),
            Self::NoSuchLabel(l) => write!(f, "no label {l:?}"),
            Self::Busy(c) => write!(f, "cursor {c} has a step running"),
            Self::NotInHook(c) => write!(f, "no cursor hook is running on cursor {c}"),
            Self::NoSuchItem(i) => write!(f, "no item {i}"),
            Self::NotPending(i) => write!(f, "item {i} is not pending for that cursor"),
            Self::DuplicateCall => write!(f, "a node names a call id twice"),
            Self::WrongFeed(c) => write!(f, "that feed answers no job on cursor {c}"),
            Self::BadRecord(at) => write!(f, "record {at} does not replay"),
        }
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
        let _span = tracing::info_span!("core.append").entered();
        let node = self.tree.check(Some(parent), by, body, delivers, time)?;
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
        let _span = tracing::info_span!("core.write", cursor).entered();
        let from = self.at(cursor)?;
        if let Some(bad) = delivers
            .iter()
            .find(|d| !self.items(cursor).any(|i| i.id == **d))
        {
            return Err(Error::NotPending(*bad));
        }
        let node = self
            .tree
            .check(Some(from), by.clone(), body, delivers, time)?;
        let id = node.id;
        if !node.delivers.is_empty() {
            self.pad.items.retain(|i| !node.delivers.contains(&i.id));
            self.touch_pad();
        }
        let mv = Move {
            cursor,
            from: Some(from),
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

    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open.
    pub fn at(&self, cursor: CursorId) -> Result<NodeId, Error> {
        self.at
            .get(&cursor)
            .copied()
            .ok_or(Error::NoSuchCursor(cursor))
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
    ) -> Result<Node, Error> {
        if let Some(p) = parent {
            self.node(p)?;
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
                Part::Call { id, tool, args_raw } => {
                    Some((id.as_str(), tool.as_str(), args_raw.as_str()))
                }
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
