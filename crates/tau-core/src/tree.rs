//! The session tree: nodes, cursors, the scratchpad, and the records a store
//! writes. Spike: the shape of model.md §1, minus labels and copy relations.

use std::collections::BTreeMap;

pub type NodeId = u64;
pub type CursorId = u64;
pub type ItemId = u64;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Part {
    Text(String),
    Thinking(String),
    Call {
        id: String,
        tool: String,
        args_raw: String,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stop {
    End,
    Tools,
    Length,
    Refused,
    Interrupted,
    Error,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Outcome {
    Ok(String),
    Err(String),
    Interrupted,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Body {
    System(String),
    User(String),
    Assistant {
        parts: Vec<Part>,
        stop: Stop,
    },
    /// One `(call id, outcome, by)` per call, in call order. `by` is who
    /// produced that outcome: `Tools`, or the extension that answered or edited it.
    Results(Vec<(String, Outcome, Author)>),
    Config(String),
    Custom {
        kind: String,
        data: String,
    },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Author {
    Frontend(String),
    Provider,
    Tools,
    Core,
    Extension(String),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Node {
    pub id: NodeId,
    pub parent: Option<NodeId>,
    pub time: u64,
    pub by: Author,
    pub body: Body,
    pub delivers: Vec<ItemId>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Move {
    pub cursor: CursorId,
    pub from: Option<NodeId>,
    pub to: NodeId,
    pub by: Author,
    pub time: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ItemData {
    Input { client: String, text: String },
    Extension { name: String, data: String },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Item {
    pub id: ItemId,
    pub to: CursorId,
    pub data: ItemData,
}

/// What the store must make durable, in order. Core never writes; it queues.
/// The store may buffer everything up to a `Sync`; at a `Sync` it syncs the
/// log, then rewrites the scratchpad file.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Record {
    Write { node: Node, mv: Option<Move> },
    Move(Move),
    Scratchpad,
    Sync,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    NoSuchNode(NodeId),
    NoSuchCursor(CursorId),
    RootNotConfig,
    DuplicateCall,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Next {
    Wait,
    Sample,
    RunTools,
}

#[derive(Debug, Default)]
pub struct Session {
    nodes: Vec<Node>,
    at: BTreeMap<CursorId, NodeId>,
    pub auto: BTreeMap<CursorId, bool>,
    items: Vec<Item>,
    next_item: ItemId,
    /// Per cursor: bumped by every scratchpad change core did not make itself.
    pub changes: BTreeMap<CursorId, u64>,
    pub moves: Vec<Move>,
    pub outbox: Vec<Record>,
    /// Records queued since the last `Sync`.
    unsynced: bool,
    pub notices: Vec<(String, String)>,
}

impl Session {
    pub fn create(config: &str, system: &str, time: u64) -> (Self, CursorId) {
        let mut s = Self::default();
        let root = s
            .append(
                None,
                Author::Core,
                Body::Config(config.into()),
                vec![],
                time,
            )
            .unwrap();
        let sys = s
            .append(
                Some(root),
                Author::Core,
                Body::System(system.into()),
                vec![],
                time,
            )
            .unwrap();
        let cursor = s.open(sys, Author::Core, time).unwrap();
        (s, cursor)
    }

    pub fn open(&mut self, at: NodeId, by: Author, time: u64) -> Result<CursorId, Error> {
        self.node(at)?;
        let id = self.at.len() as CursorId;
        self.auto.insert(id, false);
        self.place(id, None, at, by, time);
        Ok(id)
    }

    pub fn append(
        &mut self,
        parent: Option<NodeId>,
        by: Author,
        body: Body,
        delivers: Vec<ItemId>,
        time: u64,
    ) -> Result<NodeId, Error> {
        let node = self.check(parent, by, body, delivers, time)?;
        let id = node.id;
        self.nodes.push(node.clone());
        self.queue(Record::Write { node, mv: None });
        Ok(id)
    }

    /// Append under `at` and move onto it, as one record; then drop delivered items.
    pub fn write(
        &mut self,
        cursor: CursorId,
        body: Body,
        delivers: Vec<ItemId>,
        by: Author,
        time: u64,
    ) -> Result<NodeId, Error> {
        let from = self.at(cursor)?;
        let node = self.check(Some(from), by.clone(), body, delivers.clone(), time)?;
        let mv = Move {
            cursor,
            from: Some(from),
            to: node.id,
            by,
            time,
        };
        self.nodes.push(node.clone());
        self.at.insert(cursor, node.id);
        self.moves.push(mv.clone());
        self.queue(Record::Write { node, mv: Some(mv) });
        if !delivers.is_empty() {
            self.items.retain(|i| !delivers.contains(&i.id));
            self.queue(Record::Scratchpad);
        }
        Ok(*self.at.get(&cursor).unwrap())
    }

    pub fn move_to(
        &mut self,
        cursor: CursorId,
        to: NodeId,
        by: Author,
        time: u64,
    ) -> Result<(), Error> {
        self.node(to)?;
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

    fn check(
        &self,
        parent: Option<NodeId>,
        by: Author,
        body: Body,
        delivers: Vec<ItemId>,
        time: u64,
    ) -> Result<Node, Error> {
        match (parent, &body) {
            (None, Body::Config(_)) => {}
            (None, _) => return Err(Error::RootNotConfig),
            (Some(p), _) => drop(self.node(p)?),
        }
        if let Body::Results(out) = &body {
            if (1..out.len()).any(|i| out[..i].iter().any(|(c, ..)| *c == out[i].0)) {
                return Err(Error::DuplicateCall); // V2
            }
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

    pub fn node(&self, id: NodeId) -> Result<&Node, Error> {
        self.nodes
            .get(usize::try_from(id).unwrap_or(usize::MAX))
            .ok_or(Error::NoSuchNode(id))
    }

    pub fn at(&self, cursor: CursorId) -> Result<NodeId, Error> {
        self.at
            .get(&cursor)
            .copied()
            .ok_or(Error::NoSuchCursor(cursor))
    }

    pub fn walk(&self, n: NodeId) -> Vec<&Node> {
        let mut out: Vec<&Node> = std::iter::successors(self.node(n).ok(), |x| {
            x.parent.and_then(|p| self.node(p).ok())
        })
        .collect();
        out.reverse();
        out
    }

    /// `next` from the end of the walk, without building it: only the last
    /// node that is not Config or Custom decides.
    pub fn next(&self, cursor: CursorId) -> Next {
        let last = self.at(cursor).ok().and_then(|a| {
            std::iter::successors(self.node(a).ok(), |x| {
                x.parent.and_then(|p| self.node(p).ok())
            })
            .find(|n| !matches!(n.body, Body::Config(_) | Body::Custom { .. }))
        });
        decide(last)
    }

    pub fn add_item(&mut self, to: CursorId, data: ItemData) -> ItemId {
        let id = self.next_item;
        self.next_item += 1;
        self.items.push(Item { id, to, data });
        self.queue(Record::Scratchpad);
        *self.changes.entry(to).or_default() += 1; // rules: allow(entry): BTreeMap API
        id
    }

    pub fn remove_item(&mut self, id: ItemId) {
        if let Some(i) = self.items.iter().position(|i| i.id == id) {
            let to = self.items.remove(i).to;
            self.queue(Record::Scratchpad);
            *self.changes.entry(to).or_default() += 1; // rules: allow(entry): BTreeMap API
        }
    }

    fn queue(&mut self, r: Record) {
        self.outbox.push(r);
        self.unsynced = true;
    }

    /// A sync point: queue a `Sync` if anything is queued since the last one.
    pub fn sync(&mut self) {
        if self.unsynced {
            self.outbox.push(Record::Sync);
            self.unsynced = false;
        }
    }

    /// True when every queued record is covered by a `Sync`.
    pub fn synced(&self) -> bool {
        !self.unsynced
    }

    pub fn items(&self, cursor: CursorId) -> impl Iterator<Item = &Item> {
        self.items.iter().filter(move |i| i.to == cursor)
    }

    /// What load does after replay (I7): every cursor at rest with auto-step
    /// off, and every item a node delivers dropped.
    pub fn reconcile(&mut self) {
        self.auto.values_mut().for_each(|a| *a = false);
        let delivered: Vec<ItemId> = self
            .nodes
            .iter()
            .flat_map(|n| n.delivers.iter().copied())
            .collect();
        self.items.retain(|i| !delivered.contains(&i.id));
    }
}

pub fn calls(body: &Body) -> Vec<(&str, &str, &str)> {
    match body {
        Body::Assistant {
            parts,
            stop: Stop::Tools,
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
