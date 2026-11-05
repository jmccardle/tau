//! The tree browser's state and keys: the session's nodes as a spine and stubs (`plan`), the
//! rows that are open, the highlight, and whether it follows the viewed cursor. No drawing
//! (`treeui`) and no calls: a key answers what the person asked for (`Act`), and `run` makes it so.
//!
//! Only open rows have children, and only the rows on screen are given words, so a tree of tens
//! of thousands of nodes costs a pass over the open rows per redraw, not a layout of them.

use std::{
    cell::Cell,
    collections::{BTreeMap, HashMap},
    time::{Duration, Instant},
};

use ratatui::crossterm::event::{KeyCode, KeyEvent};

use crate::{
    model::Model,
    plan::{Key, Kind, Plan, Row, Shape},
    treetext::{self, Flags, Marks},
};

/// One row of the screen: a plan row, how deep it sits, and whether it is unfolded.
#[derive(Debug)]
pub struct Flat {
    pub row: Row,
    pub depth: usize,
    pub open: bool,
}

/// What a redraw found.
#[derive(Debug, PartialEq, Eq)]
pub enum Drawn {
    Done,
    /// A cursor is at a node the client has not read: the nodes must be read first.
    Missing(u64, usize),
}

/// What the person asked for, for `run` to do.
#[derive(Debug, PartialEq, Eq)]
pub enum Act {
    Nothing,
    Leave,
    /// Move `cursor` to `node`.
    Go {
        cursor: u64,
        node: u64,
    },
    /// Open a cursor at `node`.
    Open(u64),
    /// Show `cursor` (the screen then follows it).
    View(u64),
    /// Ask for a label to put on `cursor`.
    Label(u64),
    /// Say this; nothing else happens.
    Say(String),
}

pub struct Tree {
    shape: Shape,
    open_at: BTreeMap<u64, usize>,
    gone_at: BTreeMap<u64, usize>,
    viewed: u64,
    viewed_at: usize,
    folds: HashMap<Key, bool>,
    flat: Vec<Flat>,
    highlight: usize,
    top: Cell<usize>,
    height: Cell<usize>,
    following: bool,
    /// Where `[` or `]` put the highlight, until it moves again.
    wanted: Option<usize>,
    marks: Vec<usize>,
    /// How long the last redraw took (planning and placing the highlight).
    pub took: Duration,
    /// A word for the person (a key had nowhere to go), until the next key.
    pub said: Option<String>,
}

impl Tree {
    pub fn new(viewed: u64) -> Self {
        Self {
            shape: Shape::default(),
            open_at: BTreeMap::new(),
            gone_at: BTreeMap::new(),
            viewed,
            viewed_at: 0,
            folds: HashMap::new(),
            flat: Vec::new(),
            highlight: 0,
            top: Cell::new(0),
            height: Cell::new(1),
            following: true,
            wanted: None,
            marks: Vec::new(),
            took: Duration::ZERO,
            said: None,
        }
    }

    pub fn title(&self) -> String {
        let state = if self.following {
            "following"
        } else {
            "paused (f follows)"
        };
        match &self.said {
            Some(said) => format!("The session's tree · {state} · {said}"),
            None => format!("The session's tree · {state}"),
        }
    }

    pub fn flat(&self) -> &[Flat] {
        &self.flat
    }

    pub fn highlighted(&self) -> Option<&Flat> {
        self.flat.get(self.highlight)
    }

    pub fn highlight(&self) -> usize {
        self.highlight
    }

    /// The length and size of the viewed cursor's walk, as the detail pane says it.
    pub fn walk_line(&self) -> String {
        if self.shape.len() == 0 {
            return String::new();
        }
        let (nodes, tokens) = self.shape.walk_of(self.viewed_at);
        format!(
            "cursor {}: a walk of {}, about {} tokens\n\n",
            self.viewed,
            treetext::plural(nodes, "node", None),
            group(tokens)
        )
    }

    /// The marks of cursors for the rows the last plan drew: the cursors it had, so a row never
    /// names one the marks lack (one that closed since is still marked where it was, until the
    /// next plan); `model` says whether each is busy or asking.
    pub fn marks_of(&self, model: &Model) -> Marks {
        let open = self
            .open_at
            .iter()
            .map(|(cursor, at)| {
                let view = model.views().get(cursor);
                let flags = Flags {
                    at: *at,
                    busy: view.is_some_and(|v| v.busy),
                    asking: view.is_some_and(|v| !v.questions.is_empty()),
                };
                (*cursor, flags)
            })
            .collect();
        Marks::new(open, &self.gone_at, model.labels().to_vec(), self.viewed)
    }

    /// The rows to draw in a window `height` high, as a range of `flat`; the window keeps the
    /// highlight in view.
    pub fn window(&self, height: usize) -> std::ops::Range<usize> {
        let height = height.max(1);
        self.height.set(height);
        // The window ends at the last row at most: a terminal that grew shows more above.
        let mut top = self.top.get().min(self.flat.len().saturating_sub(height));
        if self.highlight < top {
            top = self.highlight;
        } else if self.highlight >= top + height {
            top = self.highlight + 1 - height;
        }
        self.top.set(top);
        top..(top + height).min(self.flat.len())
    }

    /// What a reconnect keeps: whether the highlight follows, and the node it is on.
    pub fn keep(&self) -> (bool, Option<usize>) {
        (self.following, self.highlighted().map(|f| f.row.node))
    }

    /// Come back after a reconnect: follow as before, or hold the node the highlight was on if
    /// it is still in the tree (the next redraw checks).
    pub fn resume(&mut self, following: bool, node: Option<usize>) {
        self.following = following;
        self.wanted = if following { None } else { node };
    }

    // Keeping up with the session

    /// Plan the tree again from `model` as it is now and put the highlight where it belongs: on
    /// the viewed cursor while following, where `[` or `]` put it, else where it was. A cursor at a
    /// node not read yet is `Drawn::Missing`: the caller reads the nodes.
    pub fn redraw(&mut self, model: &Model) -> Result<Drawn, String> {
        let started = Instant::now();
        let Some(viewed) = model.cursor() else {
            return Err("the tree has no viewed cursor".to_owned());
        };
        self.viewed = viewed;
        let dense = model.dense();
        let facts: Vec<_> = (self.shape.len() as u64..dense)
            .map(|id| treetext::facts(model.node(id).expect("nodes below dense are held")))
            .collect();
        self.shape.extend(&facts)?;
        let at = |n: u64| usize::try_from(n).expect("a node id fits");
        self.open_at = model.views().iter().map(|(c, v)| (*c, at(v.at))).collect();
        self.gone_at = model.gone().iter().map(|(c, n)| (*c, at(*n))).collect();
        let unread = (self.open_at.iter())
            .chain(self.gone_at.iter())
            .find(|(_, n)| **n >= self.shape.len());
        if let Some((&cursor, &node)) = unread {
            return Ok(Drawn::Missing(cursor, node));
        }
        let Some(&viewed_at) = self.open_at.get(&viewed) else {
            return Err(format!("the viewed cursor {viewed} has no view"));
        };
        self.viewed_at = viewed_at;
        let kept = self.highlighted().map(|f| (f.row.key(), f.row.node));
        let (flat, index, marks) = self.plan(|plan, folds| {
            let flat = flatten(plan, folds);
            let index = index_of(&flat);
            (flat, index, plan.marks())
        })?;
        self.flat = flat;
        self.marks = marks;
        let target = if self.following {
            Some((self.viewed_at, None))
        } else if let Some(wanted) = self.wanted.filter(|w| *w < self.shape.len()) {
            Some((wanted, None))
        } else {
            kept.map(|(key, node)| (node, Some(key)))
        };
        let placed = match target {
            Some((node, key)) => self.place(node, key, &index)?,
            None => None,
        };
        self.highlight = placed
            .unwrap_or(self.highlight)
            .min(self.flat.len().saturating_sub(1));
        self.took = started.elapsed();
        Ok(Drawn::Done)
    }

    fn plan<R>(&self, f: impl FnOnce(&mut Plan, &HashMap<Key, bool>) -> R) -> Result<R, String> {
        let mut plan = Plan::new(&self.shape, &self.open_at, &self.gone_at, self.viewed)?;
        Ok(f(&mut plan, &self.folds))
    }

    /// The index of the row that shows `node` (or has `key`), or of the nearest one on the way
    /// that is not folded away.
    fn place(
        &self,
        node: usize,
        key: Option<Key>,
        index: &HashMap<Key, usize>,
    ) -> Result<Option<usize>, String> {
        let chain = self.plan(|plan, _| plan.locate(node, key))??;
        let mut reached = None;
        for row in &chain {
            let Some(&at) = index.get(&row.key()) else {
                break;
            };
            reached = Some(at);
            if row.expandable && !self.flat[at].open {
                break;
            }
        }
        Ok(reached)
    }

    /// Flatten again after a fold changed, keeping the highlight on the same row.
    fn reflatten(&mut self) -> Result<(), String> {
        let kept = self.highlighted().map(|f| (f.row.key(), f.row.node));
        let (flat, index) = self.plan(|plan, folds| {
            let flat = flatten(plan, folds);
            let index = index_of(&flat);
            (flat, index)
        })?;
        self.flat = flat;
        if let Some((key, node)) = kept {
            let at = match index.get(&key) {
                Some(&at) => Some(at),
                None => self.place(node, Some(key), &index)?,
            };
            self.highlight = at.unwrap_or(0);
        }
        Ok(())
    }

    // Keys

    fn moved(&mut self, to: usize) {
        self.following = false;
        self.wanted = None;
        self.highlight = to.min(self.flat.len().saturating_sub(1));
    }

    /// Take a key. `Err` is a plan that cannot be made (a tree that is not one).
    pub fn key(&mut self, key: KeyEvent, model: &Model) -> Result<Act, String> {
        let page = self.height.get().saturating_sub(1).max(1);
        let here = self.highlight;
        match key.code {
            KeyCode::Esc => return Ok(Act::Leave),
            KeyCode::Up => self.moved(here.saturating_sub(1)),
            KeyCode::Down => self.moved(here + 1),
            KeyCode::PageUp => self.moved(here.saturating_sub(page)),
            KeyCode::PageDown => self.moved(here + page),
            KeyCode::Home => self.moved(0),
            KeyCode::End => self.moved(usize::MAX),
            KeyCode::Left => self.collapse()?,
            KeyCode::Right => self.expand()?,
            KeyCode::Enter => return Ok(self.act(true)),
            KeyCode::Char('o') => return Ok(self.act(false)),
            KeyCode::Char('l') => return Ok(Act::Label(self.viewed)),
            KeyCode::Char('v') => return Ok(self.view(model)),
            KeyCode::Char('f') => self.follow()?,
            KeyCode::Char('p') => self.source(model)?,
            KeyCode::Char('[') => self.mark(false)?,
            KeyCode::Char(']') => self.mark(true)?,
            _ => {}
        }
        Ok(Act::Nothing)
    }

    /// Enter and `o`: go to, or open a cursor at, the highlighted node. A folded branch is not a
    /// node: it is asked to be unfolded first.
    fn act(&self, go: bool) -> Act {
        let Some(flat) = self.highlighted() else {
            return Act::Nothing;
        };
        if flat.row.kind == Kind::Stub {
            let what = if go { "go to" } else { "open a cursor at" };
            return Act::Say(format!("Unfold the branch (→) and pick a node to {what}"));
        }
        let node = flat.row.node as u64;
        if go {
            Act::Go {
                cursor: self.viewed,
                node,
            }
        } else {
            Act::Open(node)
        }
    }

    /// `v`: the cursor at the highlight (or on the branch it is on) becomes the viewed one.
    fn view(&self, model: &Model) -> Act {
        let Some(flat) = self.highlighted() else {
            return Act::Nothing;
        };
        let folded = flat.row.kind == Kind::Stub || (flat.row.kind == Kind::Turn && !flat.open);
        let here: Vec<u64> = if folded {
            flat.row.cursors.clone()
        } else {
            self.marks_of(model).open_at(flat.row.node).to_vec()
        };
        match here.iter().find(|c| **c != self.viewed).or(here.first()) {
            Some(&cursor) => Act::View(cursor),
            None => Act::Say("No open cursor here".to_owned()),
        }
    }

    /// The viewed cursor changed (the screen drew with it already): follow it.
    pub fn view_changed(&mut self) {
        self.following = true;
        self.wanted = None;
    }

    /// `f`: follow the viewed cursor again; the highlight goes to it now.
    fn follow(&mut self) -> Result<(), String> {
        self.following = true;
        self.wanted = None;
        let index = index_of(&self.flat);
        if let Some(at) = self.place(self.viewed_at, None, &index)? {
            self.highlight = at;
        }
        Ok(())
    }

    /// Left: fold the turn or branch the highlight is on, else step out to the row around it.
    fn collapse(&mut self) -> Result<(), String> {
        let Some(flat) = self.highlighted() else {
            return Ok(());
        };
        if flat.row.expandable && flat.open {
            self.folds.insert(flat.row.key(), false);
            return self.reflatten();
        }
        let depth = flat.depth;
        if depth > 0
            && let Some(parent) = self.flat[..self.highlight]
                .iter()
                .rposition(|f| f.depth < depth)
        {
            self.moved(parent);
        }
        Ok(())
    }

    /// Right: unfold the turn or branch the highlight is on.
    fn expand(&mut self) -> Result<(), String> {
        match self.highlighted() {
            Some(flat) if flat.row.expandable && !flat.open => {
                self.folds.insert(flat.row.key(), true);
                self.reflatten()
            }
            _ => Ok(()),
        }
    }

    /// `p`: highlight the node the highlighted one was derived or copied from, unfolding what
    /// hides it. A node that came from none says so.
    fn source(&mut self, model: &Model) -> Result<(), String> {
        let Some(flat) = self.highlighted() else {
            return Ok(());
        };
        let id = flat.row.node;
        let node = model
            .node(id as u64)
            .ok_or(format!("node {id} is not held"))?;
        let from = node.derived_from.or(node.copied_from);
        match from.map(usize::try_from) {
            Some(Ok(source)) if source < self.shape.len() => self.reveal(source),
            Some(_) => Err(format!(
                "node {id} comes from a node that is not in the tree"
            )),
            None => {
                self.said = Some(format!("Node {id} was not copied or derived from another"));
                Ok(())
            }
        }
    }

    /// `[` and `]`: highlight the previous or next node with a cursor at it, open or closed, in
    /// node order, around the ends; a branch folded over it is unfolded.
    fn mark(&mut self, forward: bool) -> Result<(), String> {
        let Some(here) = self.highlighted().map(|f| f.row.node) else {
            return Ok(());
        };
        if self.marks.is_empty() {
            self.said = Some("No cursors to jump to".to_owned());
            return Ok(());
        }
        let target = if forward {
            self.marks
                .iter()
                .find(|m| **m > here)
                .or(self.marks.first())
        } else {
            self.marks
                .iter()
                .rev()
                .find(|m| **m < here)
                .or(self.marks.last())
        };
        self.reveal(*target.expect("marks is not empty"))
    }

    /// Highlight `target`, unfolding what hides it, and stop following the viewed cursor.
    fn reveal(&mut self, target: usize) -> Result<(), String> {
        self.following = false;
        let chain = self.plan(|plan, _| plan.locate(target, None))??;
        for row in &chain[..chain.len() - 1] {
            self.folds.insert(row.key(), true);
        }
        self.reflatten()?;
        let index = index_of(&self.flat);
        self.wanted = Some(target);
        if let Some(at) = self.place(target, None, &index)? {
            self.highlight = at;
        }
        Ok(())
    }

    /// The ids of the nodes on the path to the highlighted one, for the gutter.
    pub fn path(&self) -> std::collections::HashSet<usize> {
        match self.highlighted() {
            Some(flat) => self.shape.ancestry(flat.row.node),
            None => std::collections::HashSet::new(),
        }
    }
}

/// `1,234`.
fn group(n: usize) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

fn index_of(flat: &[Flat]) -> HashMap<Key, usize> {
    flat.iter()
        .enumerate()
        .map(|(i, f)| (f.row.key(), i))
        .collect()
}

/// The rows that are shown: the top rows, and under each that is unfolded its children, by the
/// folds the person made, else by the plan's default.
fn flatten(plan: &mut Plan, folds: &HashMap<Key, bool>) -> Vec<Flat> {
    fn go(
        plan: &Plan,
        rows: Vec<Row>,
        depth: usize,
        folds: &HashMap<Key, bool>,
        out: &mut Vec<Flat>,
    ) {
        for row in rows {
            let open = row.expandable && folds.get(&row.key()).copied().unwrap_or(row.opens);
            let children = open.then(|| plan.children(&row));
            out.push(Flat { row, depth, open });
            if let Some(children) = children {
                go(plan, children, depth + 1, folds, out);
            }
        }
    }
    let mut out = Vec::new();
    let top = plan.top().to_vec();
    go(plan, top, 0, folds, &mut out);
    out
}

#[cfg(test)]
mod tests;
