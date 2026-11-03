//! What the client knows of the session, built from the events and nothing else: the open
//! cursors' views, the nodes it has read, the first cursor's walk, the live layer over it.
//! No IO and no terminal: `hear` takes an event and answers what it now needs from the server,
//! `rows` says what the walk looks like. The rules for the live layer are model.md section 9's.

use std::collections::{BTreeMap, HashMap, VecDeque};

use crate::wire::{
    Author, Body, CursorView, Ending, Event, ItemData, Live, LiveDelta, Node, OpenQuestion,
    Outcome, Part, Prose, Stop,
};

/// What the model now needs from the server.
#[derive(Debug, PartialEq, Eq)]
pub enum Need {
    /// Hear this cursor's `Live`.
    Watch(u64),
    /// Read the walk to this node.
    Walk(u64),
    /// Open a new cursor where this closed one ended.
    Reopen(u64),
    /// Read the nodes from this id on, a page of them (for the tree).
    Page(u64),
}

/// How many nodes one `nodes` call asks for.
pub const PAGE: u64 = 500;

/// How a row of the transcript is drawn.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Mine,
    Other,
    Assistant,
    Thinking,
    Call,
    Result,
    Failure,
    Dim,
}

/// One logical line of the transcript: a `label`, then `body` (which may hold newlines).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row {
    pub label: String,
    pub body: String,
    pub kind: Kind,
}

impl Row {
    fn new(kind: Kind, label: impl Into<String>, body: impl Into<String>) -> Self {
        Self {
            label: label.into(),
            body: body.into(),
            kind,
        }
    }
}

/// What the sample in flight has said that the tree does not hold yet.
#[derive(Debug, Default)]
struct Layer {
    at: Option<u64>,
    text: String,
    thinking: String,
    /// The calls forming, by their index in the reply: (tool, argument text so far).
    calls: BTreeMap<u64, (String, String)>,
    /// Running calls' output, keyed by (batch node, call id), in the order they began.
    progress: Vec<((u64, String), String)>,
    /// The provider is waiting to try again: `reason: retry 1 of 3 in 2 s`, until the sample says
    /// anything else.
    retrying: Option<String>,
}

impl Layer {
    fn is_empty(&self) -> bool {
        self.text.is_empty()
            && self.thinking.is_empty()
            && self.calls.is_empty()
            && self.progress.is_empty()
    }
}

const NOTICES_KEPT: usize = 5;
const RESULT_LINES: usize = 12;

#[derive(Debug)]
pub struct Model {
    me: String,
    views: BTreeMap<u64, CursorView>,
    /// The closed cursors and the node each was last at.
    gone: BTreeMap<u64, u64>,
    labels: Vec<(String, u64)>,
    /// The cursor being shown.
    cursor: Option<u64>,
    nodes: HashMap<u64, Node>,
    /// Nodes 0..dense are all held (the tree needs them whole).
    dense: u64,
    reading: bool,
    walk: Vec<u64>,
    fetching: Option<u64>,
    layer: Layer,
    notices: VecDeque<String>,
    lost: Option<String>,
}

impl Model {
    /// A model of the session for the client `me`, before any event.
    pub fn new(me: &str) -> Self {
        Self {
            me: me.to_owned(),
            views: BTreeMap::new(),
            gone: BTreeMap::new(),
            labels: Vec::new(),
            cursor: None,
            nodes: HashMap::new(),
            dense: 0,
            reading: false,
            walk: Vec::new(),
            fetching: None,
            layer: Layer::default(),
            notices: VecDeque::new(),
            lost: None,
        }
    }

    /// The cursor being shown, once the snapshot has been heard.
    pub fn cursor(&self) -> Option<u64> {
        self.cursor
    }

    pub fn view(&self) -> Option<&CursorView> {
        self.views.get(&self.cursor?)
    }

    /// Every open cursor's view.
    pub fn views(&self) -> &BTreeMap<u64, CursorView> {
        &self.views
    }

    /// The closed cursors and the node each was last at.
    pub fn gone(&self) -> &BTreeMap<u64, u64> {
        &self.gone
    }

    /// Every label and the cursor it is on.
    pub fn labels(&self) -> &[(String, u64)] {
        &self.labels
    }

    pub fn node(&self, id: u64) -> Option<&Node> {
        self.nodes.get(&id)
    }

    /// How many nodes, from the root, are all held.
    pub fn dense(&self) -> u64 {
        self.dense
    }

    /// Whether the whole tree is still being read.
    pub fn reading(&self) -> bool {
        self.reading
    }

    /// Start reading every node (the tree browser needs them all): the first page to ask for.
    pub fn begin_read(&mut self) -> Vec<Need> {
        if self.reading {
            return Vec::new(); // a read is already on its way
        }
        self.reading = true;
        vec![Need::Page(self.dense)]
    }

    /// A page of nodes the server answered. An empty page ends the read.
    pub fn paged(&mut self, nodes: &[Node]) -> Vec<Need> {
        for node in nodes {
            self.insert(node.clone());
        }
        if nodes.is_empty() {
            self.reading = false;
            return Vec::new();
        }
        vec![Need::Page(self.dense)]
    }

    fn insert(&mut self, node: Node) {
        self.nodes.insert(node.id, node);
        while self.nodes.contains_key(&self.dense) {
            self.dense += 1;
        }
    }

    /// The most recent notices, oldest first.
    pub fn notices(&self) -> impl Iterator<Item = &String> {
        self.notices.iter()
    }

    pub fn notify(&mut self, text: impl Into<String>) {
        self.notices.push_back(text.into());
        while self.notices.len() > NOTICES_KEPT {
            self.notices.pop_front();
        }
    }

    /// Why the session cannot be shown any more (the cursor was closed under us, say).
    pub fn lost(&self) -> Option<&str> {
        self.lost.as_deref()
    }

    /// The questions this client may answer: the open ones for anyone, and the ones for it.
    pub fn questions(&self) -> Vec<&OpenQuestion> {
        let Some(view) = self.view() else {
            return Vec::new();
        };
        view.questions
            .iter()
            .filter(|q| q.to.as_ref().is_none_or(|to| *to == self.me))
            .collect()
    }

    /// Show cursor `cursor` from now on: hear its `Live` and read its walk.
    pub fn attach(&mut self, cursor: u64) -> Vec<Need> {
        if self.cursor != Some(cursor) {
            self.layer = Layer::default();
        }
        self.cursor = Some(cursor);
        let mut needs = vec![Need::Watch(cursor)];
        needs.extend(self.resync());
        needs
    }

    /// The nodes of a walk the server answered. They may also have settled what `resync` asked.
    pub fn walked(&mut self, nodes: Vec<Node>) -> Vec<Need> {
        self.fetching = None;
        for node in nodes {
            self.insert(node);
        }
        self.resync()
    }

    /// Take one event.
    pub fn hear(&mut self, event: Event) -> Vec<Need> {
        match event {
            Event::CursorView(view) => self.hear_view(view),
            Event::CursorGone(gone) => {
                self.views.remove(&gone.cursor);
                self.gone.insert(gone.cursor, gone.at);
                if self.cursor == Some(gone.cursor) {
                    return self.viewed_closed(gone.cursor);
                }
                Vec::new()
            }
            Event::Labels(labels) => {
                self.labels = labels.labels;
                self.hear_labels()
            }
            Event::Appended(appended) => {
                for node in appended.nodes {
                    self.settle(&node);
                    self.insert(node);
                }
                self.resync()
            }
            Event::Notice(notice) => {
                self.notify(notice.text);
                Vec::new()
            }
            Event::Live(live) => {
                self.hear_live(live);
                Vec::new()
            }
        }
    }

    /// The snapshot is whole once `Labels` arrives: pick the first cursor (the open one with the
    /// lowest id; failing that reopen the lowest closed one, as `tau` does).
    fn hear_labels(&mut self) -> Vec<Need> {
        if self.cursor.is_some() {
            return Vec::new();
        }
        if let Some(first) = self.views.keys().next().copied() {
            return self.attach(first);
        }
        if let Some(closed) = self.gone.keys().next().copied() {
            return vec![Need::Reopen(closed)];
        }
        self.lost = Some("the session has no cursor".to_owned());
        Vec::new()
    }

    /// The cursor being shown was closed (by another client): show the lowest open one, saying
    /// so; with none left the session cannot be shown.
    fn viewed_closed(&mut self, closed: u64) -> Vec<Need> {
        match self.views.keys().next().copied() {
            Some(next) => {
                self.notify(format!("cursor {closed} was closed; showing cursor {next}"));
                self.attach(next)
            }
            None => {
                self.lost = Some(format!("cursor {closed} was closed"));
                Vec::new()
            }
        }
    }

    fn hear_view(&mut self, view: CursorView) -> Vec<Need> {
        let cursor = view.cursor;
        self.gone.remove(&cursor);
        if self.cursor == Some(cursor) {
            // The layer is drawn over the cursor's node while it works: another node, or rest,
            // and the tree holds what the layer said.
            if !view.busy || self.layer.at.is_some_and(|at| at != view.at) {
                self.layer = Layer::default();
            }
        }
        self.views.insert(cursor, view);
        if self.cursor == Some(cursor) {
            self.resync()
        } else {
            Vec::new()
        }
    }

    /// A node arrived: the call output it answers is no longer progress.
    fn settle(&mut self, node: &Node) {
        if let (Body::Results(_), Some(parent)) = (&node.body, node.parent) {
            self.layer
                .progress
                .retain(|((batch, _), _)| *batch != parent);
        }
    }

    fn hear_live(&mut self, live: Live) {
        if self.cursor != Some(live.cursor) {
            return;
        }
        let at = self.view().map(|v| v.at);
        self.layer.at = self.layer.at.or(at);
        if !matches!(
            live.delta,
            LiveDelta::Retrying(_) | LiveDelta::ToolProgress(_)
        ) {
            self.layer.retrying = None; // the sample is going again
        }
        match live.delta {
            LiveDelta::Retrying(d) => {
                self.layer.retrying = Some(format!(
                    "{}: retry {} of {} in {} s",
                    d.reason, d.attempt, d.attempts, d.seconds
                ));
            }
            LiveDelta::TextDelta(d) => self.layer.text.push_str(&d.text),
            LiveDelta::ThinkingDelta(d) => self.layer.thinking.push_str(&d.text),
            LiveDelta::CallNamed(d) => {
                self.layer.calls.insert(d.index, (d.tool, String::new()));
            }
            LiveDelta::CallArgs(d) => {
                // Arguments of a call whose name was never heard are ignored.
                if let Some((_, args)) = self.layer.calls.get_mut(&d.index) {
                    args.push_str(&d.text);
                }
            }
            LiveDelta::ToolProgress(d) => {
                let key = (d.node, d.call_id);
                match self.layer.progress.iter_mut().find(|(k, _)| *k == key) {
                    Some((_, text)) => text.push_str(&d.text),
                    None => self.layer.progress.push((key, d.text)),
                }
            }
        }
    }

    /// Make the walk follow the cursor: from the nodes held if the chain to the root is whole,
    /// else ask for it (once at a time).
    fn resync(&mut self) -> Vec<Need> {
        let Some(at) = self.view().map(|v| v.at) else {
            return Vec::new();
        };
        if self.walk.last() == Some(&at) {
            return Vec::new();
        }
        let mut chain = Vec::new();
        let mut next = Some(at);
        while let Some(id) = next {
            match self.nodes.get(&id) {
                Some(node) => {
                    chain.push(id);
                    next = node.parent;
                }
                None => {
                    if self.fetching == Some(at) {
                        return Vec::new();
                    }
                    self.fetching = Some(at);
                    return vec![Need::Walk(at)];
                }
            }
        }
        chain.reverse();
        self.walk = chain;
        Vec::new()
    }

    // The picture

    /// The transcript: the walk, then the live layer over it.
    pub fn rows(&self) -> Vec<Row> {
        let mut rows = Vec::new();
        for id in &self.walk {
            self.node_rows(&self.nodes[id], &mut rows);
        }
        self.layer_rows(&mut rows);
        rows
    }

    fn author(&self, by: &Author) -> (Kind, String) {
        match by {
            Author::Frontend(id) if *id == self.me => (Kind::Mine, "you".to_owned()),
            Author::Frontend(id) => (Kind::Other, id.clone()),
            Author::Extension(name) => (Kind::Other, format!("{name} (extension)")),
            Author::Provider => (Kind::Other, "provider".to_owned()),
            Author::Tools => (Kind::Other, "tools".to_owned()),
            Author::Core => (Kind::Other, "core".to_owned()),
        }
    }

    fn node_rows(&self, node: &Node, rows: &mut Vec<Row>) {
        match &node.body {
            // The system prompt, config snapshots and extensions' own nodes are not conversation.
            Body::System(_) | Body::Config(_) | Body::Custom { .. } => {}
            Body::User(text) => {
                let (kind, who) = self.author(&node.by);
                rows.push(Row::new(kind, who, text.clone()));
            }
            Body::Assistant { parts, stop, .. } => {
                for part in parts {
                    rows.push(match part {
                        Part::Text(p) => Row::new(Kind::Assistant, "assistant", prose(p)),
                        Part::Thinking(p) => Row::new(Kind::Thinking, "thinking", prose(p)),
                        Part::Call { tool, args_raw, .. } => {
                            Row::new(Kind::Call, "call", format!("{tool} {args_raw}"))
                        }
                    });
                }
                match stop {
                    Stop::End | Stop::Tools => {}
                    other => rows.push(Row::new(Kind::Failure, "stopped", other.to_string())),
                }
            }
            Body::Results(results) => {
                for (call_id, outcome, by) in results {
                    rows.push(self.result_row(node, call_id, outcome, by));
                }
            }
        }
    }

    fn result_row(&self, node: &Node, call_id: &str, outcome: &Outcome, by: &Author) -> Row {
        let tool = node
            .parent
            .and_then(|p| self.nodes.get(&p))
            .and_then(|batch| match &batch.body {
                Body::Assistant { parts, .. } => parts.iter().find_map(|part| match part {
                    Part::Call { id, tool, .. } if id == call_id => Some(tool.clone()),
                    _ => None,
                }),
                _ => None,
            })
            .unwrap_or_else(|| call_id.to_owned());
        let (kind, text) = match outcome {
            Outcome::Ok(text) => (Kind::Result, clip(text)),
            Outcome::Err(text) => (Kind::Failure, clip(text)),
            Outcome::Interrupted => (Kind::Failure, "interrupted".to_owned()),
        };
        let label = match by {
            Author::Tools => tool,
            other => format!("{tool}, by {}", self.author(other).1),
        };
        Row::new(kind, label, text)
    }

    fn layer_rows(&self, rows: &mut Vec<Row>) {
        if self.layer.is_empty() {
            return;
        }
        if !self.layer.thinking.is_empty() {
            rows.push(Row::new(
                Kind::Thinking,
                "thinking",
                self.layer.thinking.clone(),
            ));
        }
        if !self.layer.text.is_empty() {
            rows.push(Row::new(
                Kind::Assistant,
                "assistant",
                self.layer.text.clone(),
            ));
        }
        for (tool, args) in self.layer.calls.values() {
            rows.push(Row::new(Kind::Call, "call", format!("{tool} {args}")));
        }
        for (_, text) in &self.layer.progress {
            rows.push(Row::new(Kind::Result, "running", clip(text)));
        }
    }

    /// The pending inputs of the cursor, as rows: lines sent and not yet delivered.
    pub fn pending(&self) -> Vec<Row> {
        let Some(view) = self.view() else {
            return Vec::new();
        };
        view.items
            .iter()
            .filter_map(|item| match &item.data {
                ItemData::Input { by, text } => {
                    let who = self.author(by).1;
                    Some(Row::new(
                        Kind::Dim,
                        format!("{who} (waiting)"),
                        text.clone(),
                    ))
                }
                ItemData::Extension { .. } => None,
            })
            .collect()
    }

    /// What the provider is waiting for, if it is waiting to try the sample again.
    pub fn retrying(&self) -> Option<&str> {
        self.layer.retrying.as_deref()
    }

    /// How the last step ended, if it did not end well.
    pub fn trouble(&self) -> Option<String> {
        let view = self.view()?;
        if let Some(hook) = &view.hook_error {
            return Some(format!(
                "hook {} failed at {}: {}",
                hook.extension, hook.point, hook.error
            ));
        }
        match view.ending.as_ref()? {
            Ending::Failed(why) => Some(format!("the step failed: {why}")),
            Ending::Halted(why) => Some(format!("the step was halted: {why}")),
            Ending::Done | Ending::Aborted => None,
        }
    }
}

pub fn prose(p: &Prose) -> String {
    match p {
        Prose::String(text) | Prose::Object { text, .. } => text.clone(),
    }
}

/// A tool's output, cut to the lines worth a screen.
fn clip(text: &str) -> String {
    let lines: Vec<&str> = text.lines().collect();
    if lines.len() <= RESULT_LINES {
        return text.trim_end().to_owned();
    }
    let more = lines.len() - RESULT_LINES;
    format!(
        "{}\n... ({more} more lines)",
        lines[..RESULT_LINES].join("\n")
    )
}

#[cfg(test)]
mod tests;
