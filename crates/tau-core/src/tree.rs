//! The session tree: nodes, cursors, labels, the scratchpad, and the records a
//! store writes. The shape of model.md §1, minus copy relations.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

mod resident;
use resident::slot;

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

/// A config snapshot. Core reads only its typed fields; `data` is the rest,
/// opaque to core and typed by the driver.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Config {
    pub max_steps: Option<u32>,
    pub data: String,
}

impl From<&str> for Config {
    fn from(data: &str) -> Self {
        Self {
            max_steps: None,
            data: data.into(),
        }
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
    /// A frontend's input, go-to, or step on a cursor with an open question.
    Locked(CursorId),
    /// A frontend's go-to while a cursor hook runs.
    HookRunning(CursorId),
    RootNotConfig,
    DuplicateCall,
    /// Bodies this call needs are on disk, not in memory: the store loads
    /// them (`Store::load`), then the call is made again. Root first.
    Stored(Vec<NodeId>),
    /// A record replay cannot apply: a node id or a cursor out of sequence, a
    /// move from a node the cursor is not at. Its position in the log.
    BadRecord(usize),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Next {
    Wait,
    Sample,
    RunTools,
}

/// Equal by state (I7): which bodies are in memory is not state, so
/// `cooled` is left out; a stored body equals only another stored one.
#[derive(Clone, Debug, Default)]
pub struct Session {
    /// Per node, by id: the node, or `None` while its body is stored.
    nodes: Vec<Option<Box<Node>>>,
    /// Per node, by id: its parent, resident or stored.
    parents: Vec<Option<NodeId>>,
    /// Per node, by id: the nearest `Config` on its walk (`config_at`).
    configs: Vec<NodeId>,
    /// A cursor moved, or a node was appended, since the last `evict`:
    /// something may be on no cursor's walk.
    cooled: bool,
    at: BTreeMap<CursorId, NodeId>,
    labels: BTreeMap<String, CursorId>,
    pub auto: BTreeMap<CursorId, bool>,
    pad: Scratchpad,
    /// Per cursor: bumped by every scratchpad change core did not make itself.
    pub changes: BTreeMap<CursorId, u64>,
    pub moves: Vec<Move>,
    pub outbox: Vec<Record>,
    /// Per cursor: committing steps since a frontend last acted (`max_steps`).
    pub taken: BTreeMap<CursorId, u32>,
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
    /// Never: that root and child pass V1 and V2.
    pub fn create(config: impl Into<Config>, system: &str, time: u64) -> (Self, CursorId) {
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

    /// Open a cursor at `at`.
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `at` is not a node; `Stored` for bodies on its walk
    /// not in memory.
    pub fn open(&mut self, at: NodeId, by: Author, time: u64) -> Result<CursorId, Error> {
        self.resident(at)?;
        let id = self.at.len() as CursorId;
        self.auto.insert(id, false);
        self.place(id, None, at, by, time);
        Ok(id)
    }

    /// Append a node; the only checks are V1 and V2 (I5).
    ///
    /// # Errors
    ///
    /// `RootNotConfig` for a root that is not `Config` (V1); `NoSuchNode`
    /// if `parent` is not a node; `DuplicateCall` for `Results` that name a
    /// call twice (V2).
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
        self.push(node.clone());
        self.cooled = true;
        self.queue(Record::Write { node, mv: None });
        Ok(id)
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
        let from = self.at(cursor)?;
        let node = self.check(Some(from), by.clone(), body, delivers, time)?;
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
        self.push(node.clone());
        self.at.insert(cursor, node.id);
        self.moves.push(mv.clone());
        self.queue(Record::Write { node, mv: Some(mv) });
        Ok(id)
    }

    /// Move `cursor` to `to`; no record when it is already there.
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `to` is not a node; `NoSuchCursor` if `cursor` is not
    /// open; `Stored` for bodies on `walk(to)` not in memory.
    pub fn move_to(
        &mut self,
        cursor: CursorId,
        to: NodeId,
        by: Author,
        time: u64,
    ) -> Result<(), Error> {
        self.resident(to)?;
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
        self.cooled = true;
        self.queue(Record::Move(mv));
    }

    /// Add a checked node, with its nearest `Config`.
    fn push(&mut self, node: Node) {
        let config = match (&node.body, node.parent) {
            (Body::Config(_), _) | (_, None) => node.id,
            (_, Some(p)) => self.configs[slot(p)],
        };
        self.configs.push(config);
        self.parents.push(node.parent);
        self.nodes.push(Some(Box::new(node)));
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
            (Some(p), _) => drop(self.parent(p)?),
        }
        if let Body::Results(out) = &body
            && (1..out.len()).any(|i| out[..i].iter().any(|(c, ..)| *c == out[i].0))
        {
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

    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `id` is not a node; `Stored` if its body is not in memory.
    pub fn node(&self, id: NodeId) -> Result<&Node, Error> {
        self.nodes
            .get(slot(id))
            .ok_or(Error::NoSuchNode(id))?
            .as_deref()
            .ok_or_else(|| Error::Stored(vec![id]))
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

    /// `walk(n)`: root to `n`. Every cursor's walk is in memory (`evict`).
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `n` is not a node; `Stored` as `resident`.
    pub fn walk(&self, n: NodeId) -> Result<Vec<&Node>, Error> {
        self.parent(n)?;
        let mut out = vec![];
        let mut at = Some(n);
        while let Some(i) = at {
            match self.nodes[slot(i)].as_deref() {
                Some(node) => out.push(node),
                None => return Err(self.resident(n).unwrap_err()),
            }
            at = self.parents[slot(i)];
        }
        out.reverse();
        Ok(out)
    }

    /// The nearest `Config` on `walk(n)`; always defined, since every root is
    /// one (V1). Constant time: each node records its own when added.
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `n` is not a node.
    ///
    /// # Panics
    ///
    /// If a root is not `Config`, which V1 rules out.
    pub fn config_at(&self, n: NodeId) -> Result<&Config, Error> {
        self.parent(n)?;
        match &self.node(self.configs[slot(n)])?.body {
            Body::Config(c) => Ok(c),
            _ => panic!("a root is Config (V1)"),
        }
    }

    /// The deepest node on both walks; `None` across trees.
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `a` or `b` is not a node. Bodies need not be in memory.
    pub fn lca(&self, a: NodeId, b: NodeId) -> Result<Option<NodeId>, Error> {
        self.parent(b)?;
        let up = |n| std::iter::successors(Some(n), |&x| self.parents[slot(x)]);
        let on_a: std::collections::BTreeSet<NodeId> = {
            self.parent(a)?;
            up(a).collect()
        };
        Ok(up(b).find(|id| on_a.contains(id)))
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

    /// `next` from the end of the walk, without building it: only the last
    /// node that is not Config or Custom decides.
    #[must_use]
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
        let id = self.pad.next_item;
        self.pad.next_item += 1;
        self.pad.items.push(Item { id, to, data });
        self.touch_pad();
        *self.changes.entry(to).or_default() += 1; // rules: allow(entry): BTreeMap API
        id
    }

    pub fn remove_item(&mut self, id: ItemId) {
        if let Some(i) = self.pad.items.iter().position(|i| i.id == id) {
            let to = self.pad.items.remove(i).to;
            self.touch_pad();
            *self.changes.entry(to).or_default() += 1; // rules: allow(entry): BTreeMap API
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

    /// Load (I7): replay the log's records, take the scratchpad file, then
    /// `reconcile`. Pure; the store reads the files. Nothing is queued.
    ///
    /// # Errors
    ///
    /// `BadRecord` with its position for the first record replay cannot apply.
    pub fn load(records: impl IntoIterator<Item = Record>, pad: Scratchpad) -> Result<Self, Error> {
        let mut s = Self::default();
        for (i, r) in records.into_iter().enumerate() {
            s.replay(r).map_err(|_| Error::BadRecord(i))?;
        }
        s.pad = pad;
        s.reconcile();
        s.outbox.clear();
        s.unsynced = false;
        s.cooled = true;
        Ok(s)
    }

    fn replay(&mut self, r: Record) -> Result<(), Error> {
        match r {
            Record::Write { node, mv } => {
                let checked = self.check(
                    node.parent,
                    node.by.clone(),
                    node.body.clone(),
                    node.delivers.clone(),
                    node.time,
                )?;
                if checked != node {
                    return Err(Error::BadRecord(0));
                }
                self.push(node);
                mv.map_or(Ok(()), |m| self.replay_move(m))
            }
            Record::Move(m) => self.replay_move(m),
            Record::Label { label, cursor } => {
                match cursor {
                    Some(c) => drop(self.labels.insert(label, self.at(c).map(|_| c)?)),
                    None => drop(self.labels.remove(&label)),
                }
                Ok(())
            }
            Record::Sync { .. } => Ok(()),
        }
    }

    fn replay_move(&mut self, m: Move) -> Result<(), Error> {
        self.node(m.to)?;
        let ok = match m.from {
            None => m.cursor == self.at.len() as CursorId,
            Some(f) => self.at(m.cursor) == Ok(f),
        };
        if !ok {
            return Err(Error::BadRecord(0));
        }
        self.auto.entry(m.cursor).or_insert(false); // rules: allow(entry): BTreeMap API
        self.at.insert(m.cursor, m.to);
        self.moves.push(m);
        Ok(())
    }

    /// True when every queued record is covered by a `Sync`.
    #[must_use]
    pub fn synced(&self) -> bool {
        !self.unsynced
    }

    pub fn items(&self, cursor: CursorId) -> impl Iterator<Item = &Item> {
        self.pad.items.iter().filter(move |i| i.to == cursor)
    }

    /// What load does after replay (I7): every cursor at rest with auto-step
    /// off, and every item a node delivers dropped. Reads every body, so it
    /// runs before any `evict`.
    pub fn reconcile(&mut self) {
        self.auto.values_mut().for_each(|a| *a = false);
        let delivered: Vec<ItemId> = self
            .nodes
            .iter()
            .flatten()
            .flat_map(|n| n.delivers.iter().copied())
            .collect();
        self.pad.items.retain(|i| !delivered.contains(&i.id));
    }
}

#[must_use]
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
