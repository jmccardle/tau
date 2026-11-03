//! The tree plan (D23 section 6, in this client's terms): what the tree screen draws, as data,
//! built a level at a time. Pure: it knows node numbers, parents and a few counts, not the wire.
//!
//! The viewed cursor's walk is the **spine**: drawn straight down, one row per node, with each
//! turn (a user node and what follows it down the line) folded into one row. At a node with more
//! than one child the line goes on through one of them and every other child is a **stub**: one
//! folded row for the whole branch, with its node and turn counts and the cursors on it. A stub
//! unfolds to its own line, drawn the same way: from the stub's first node it goes on through the
//! child toward the lowest-numbered open cursor below it, else through the child with the longest
//! line below it (the lowest id on a tie); the other children are stubs again.
//!
//! A branch at the last node of a turn hangs level with the turn, so folding the turn does not
//! hide it; a branch in the middle of a turn hangs inside it.
//!
//! One difference from the Textual tree: where the spine ends (the viewed cursor's node) every
//! child below it is a stub, even an only child. Textual's plan drops a lone child there, so after
//! a go-to the nodes the cursor left are not in its tree at all.

use std::{
    cmp::Reverse,
    collections::{BTreeMap, HashMap, HashSet},
};

/// What a row is: a node that opens nothing, a user node (which opens the rest of its turn), or a
/// folded branch.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Kind {
    #[default]
    Node,
    Turn,
    Stub,
}

/// A row's identity across plans: its kind and its first node.
pub type Key = (Kind, usize);

/// What the plan needs to know of a node; the model reads these off the wire's nodes.
#[derive(Clone, Copy, Debug)]
pub struct Facts {
    pub parent: Option<usize>,
    pub user: bool,
    /// Tool calls in a reply.
    pub calls: usize,
    /// Characters the model reads of the node (≈ chars / 4 tokens).
    pub chars: usize,
}

/// One row to draw. A turn's `size` counts the nodes after its user node, a stub's all the nodes
/// of the branch and `turns` its user nodes; `calls` and `branches` are a turn's tool calls and the
/// stubs hung inside it. `cursors` are the open cursors on a stub's branch, or at or inside a turn,
/// and `closed` the closed ones. `opens` is whether a turn starts unfolded.
#[derive(Clone, Debug, Default)]
pub struct Row {
    pub kind: Kind,
    pub node: usize,
    pub opens: bool,
    pub expandable: bool,
    pub size: usize,
    pub turns: usize,
    pub calls: usize,
    pub branches: usize,
    pub cursors: Vec<u64>,
    pub closed: Vec<u64>,
    pub(crate) members: Vec<usize>,
    pub(crate) hung: Vec<(usize, usize)>,
}

impl Row {
    pub fn key(&self) -> Key {
        (self.kind, self.node)
    }

    fn plain(kind: Kind, node: usize) -> Self {
        Self {
            kind,
            node,
            ..Self::default()
        }
    }
}

/// More new nodes than this and the counts are made again in one pass, not by walking up from
/// each.
const BULK: usize = 64;

/// What nodes say that never changes, kept between plans: parents, children, whether a node is a
/// user node, its tool calls; how big, how many turns and how tall the branch below it is; and the
/// length of the walk to it.
#[derive(Debug, Default)]
pub struct Shape {
    parent: Vec<Option<usize>>,
    user: Vec<bool>,
    calls: Vec<usize>,
    kids: Vec<Vec<usize>>,
    size: Vec<usize>,
    turns: Vec<usize>,
    height: Vec<usize>,
    walk_nodes: Vec<usize>,
    walk_chars: Vec<usize>,
    roots: Vec<usize>,
}

impl Shape {
    pub fn len(&self) -> usize {
        self.parent.len()
    }

    /// Take in the nodes that arrived, in id order (ids are dense, a parent first). `Err` for
    /// nodes that are not, and for a tree of more than one root.
    pub fn extend(&mut self, facts: &[Facts]) -> Result<(), String> {
        let start = self.len();
        for (id, f) in facts.iter().enumerate().map(|(i, f)| (start + i, f)) {
            if f.parent.is_some_and(|p| p >= id) {
                return Err(format!("node {id} is not after its parent"));
            }
            self.parent.push(f.parent);
            self.user.push(f.user);
            self.calls.push(f.calls);
            self.kids.push(Vec::new());
            match f.parent {
                None => {
                    self.roots.push(id);
                    self.walk_nodes.push(1);
                    self.walk_chars.push(f.chars);
                }
                Some(p) => {
                    self.kids[p].push(id);
                    self.walk_nodes.push(self.walk_nodes[p] + 1);
                    self.walk_chars.push(self.walk_chars[p] + f.chars);
                }
            }
        }
        if facts.len() > BULK {
            self.count_all();
        } else {
            for id in start..self.len() {
                self.count_up(id);
            }
        }
        if self.len() > 0 && self.roots.len() != 1 {
            return Err(format!("the tree has {} roots", self.roots.len()));
        }
        Ok(())
    }

    fn count_all(&mut self) {
        let count = self.len();
        self.size = vec![1; count];
        self.turns = self.user.iter().map(|&u| usize::from(u)).collect();
        self.height = vec![0; count];
        for i in (1..count).rev() {
            if let Some(p) = self.parent[i] {
                self.size[p] += self.size[i];
                self.turns[p] += self.turns[i];
                self.height[p] = self.height[p].max(self.height[i] + 1);
            }
        }
    }

    fn count_up(&mut self, i: usize) {
        self.size.push(1);
        self.turns.push(usize::from(self.user[i]));
        self.height.push(0);
        let add = self.turns[i];
        let mut up = self.parent[i];
        while let Some(p) = up {
            self.size[p] += 1;
            self.turns[p] += add;
            up = self.parent[p];
        }
        let (mut tall, mut up) = (1, self.parent[i]);
        while let Some(p) = up {
            if self.height[p] >= tall {
                break;
            }
            self.height[p] = tall;
            tall += 1;
            up = self.parent[p];
        }
    }

    /// The walk to `node`: its length in nodes and its size in tokens (characters over four).
    pub fn walk_of(&self, node: usize) -> (usize, usize) {
        (self.walk_nodes[node], self.walk_chars[node] / 4)
    }

    /// `node` and every node above it.
    pub fn ancestry(&self, node: usize) -> HashSet<usize> {
        let mut found = HashSet::new();
        let mut step = Some(node);
        while let Some(n) = step {
            found.insert(n);
            step = self.parent[n];
        }
        found
    }
}

/// Which row of a level shows each node of it (a node inside a turn is shown by the turn's row).
type Cover = HashMap<usize, usize>;

/// The rows of one picture of the tree.
pub struct Plan<'a> {
    shape: &'a Shape,
    open_at: &'a BTreeMap<u64, usize>,
    gone_at: &'a BTreeMap<u64, usize>,
    marked: HashSet<usize>,
    above: BTreeMap<u64, HashSet<usize>>,
    gone_above: BTreeMap<u64, HashSet<usize>>,
    next: HashMap<usize, usize>,
    spine: Option<(Vec<Row>, Cover)>,
}

impl<'a> Plan<'a> {
    /// A plan over `shape`. `open_at` and `gone_at` are the node each open and each closed cursor
    /// is at; `viewed` is the cursor whose walk is the spine (open, or closed and about to be
    /// left). `Err` for a viewed cursor that is neither, or a cursor at a node not in `shape`.
    pub fn new(
        shape: &'a Shape,
        open_at: &'a BTreeMap<u64, usize>,
        gone_at: &'a BTreeMap<u64, usize>,
        viewed: u64,
    ) -> Result<Self, String> {
        for (cursor, at) in open_at.iter().chain(gone_at) {
            if *at >= shape.len() {
                return Err(format!(
                    "cursor {cursor} is at node {at}, which is not read"
                ));
            }
        }
        let above: BTreeMap<_, _> = open_at
            .iter()
            .map(|(c, at)| (*c, shape.ancestry(*at)))
            .collect();
        let gone_above: BTreeMap<_, _> = gone_at
            .iter()
            .map(|(c, at)| (*c, shape.ancestry(*at)))
            .collect();
        let walk = above
            .get(&viewed)
            .or_else(|| gone_above.get(&viewed))
            .ok_or_else(|| format!("cursor {viewed} is neither open nor closed"))?;
        let next = walk
            .iter()
            .filter_map(|&n| shape.parent[n].map(|p| (p, n)))
            .collect();
        Ok(Self {
            shape,
            open_at,
            gone_at,
            marked: open_at.values().chain(gone_at.values()).copied().collect(),
            above,
            gone_above,
            next,
            spine: None,
        })
    }

    /// The rows at the top: the spine from the root to the viewed cursor, its branches hung.
    pub fn top(&mut self) -> &[Row] {
        &self.top_and_cover().0
    }

    fn top_and_cover(&mut self) -> &(Vec<Row>, Cover) {
        if self.spine.is_none() {
            let made = match self.shape.roots.first() {
                Some(&root) => self.line(root, &|n| self.next.get(&n).copied()),
                None => (Vec::new(), Cover::new()),
            };
            self.spine = Some(made);
        }
        self.spine.as_ref().expect("made just above")
    }

    /// What unfolding `row` shows: a turn's remaining nodes and the stubs hung in it; a stub's own
    /// line.
    pub fn children(&self, row: &Row) -> Vec<Row> {
        if row.kind == Kind::Stub {
            return self.line(row.node, &|n| self.pick(n)).0;
        }
        let mut hung: HashMap<usize, Vec<usize>> = HashMap::new();
        for &(at, root) in &row.hung {
            hung.entry(at).or_default().push(root); // rules: allow(entry): HashMap API
        }
        let mut out = Vec::new();
        for &member in &row.members {
            if member != row.node {
                out.push(Row::plain(Kind::Node, member));
            }
            for &root in hung.get(&member).into_iter().flatten() {
                out.push(self.stub(root));
            }
        }
        out
    }

    /// The rows from the top down to the one that shows `node` (a node or turn row), or to the row
    /// with `key` on the way (a stub); each is the one to unfold to reach the next. `Err` for a
    /// node not in the plan.
    pub fn locate(&mut self, node: usize, key: Option<Key>) -> Result<Vec<Row>, String> {
        if node >= self.shape.len() {
            return Err(format!("node {node} is not in the tree"));
        }
        let mut chain = Vec::new();
        let (mut rows, mut cover) = self.top_and_cover().clone();
        loop {
            let mut step = Some(node);
            while let Some(n) = step {
                if cover.contains_key(&n) {
                    break;
                }
                step = self.shape.parent[n];
            }
            let step = step.ok_or_else(|| format!("no row shows node {node}"))?;
            let row = rows[cover[&step]].clone();
            chain.push(row.clone());
            if Some(row.key()) == key || (row.kind != Kind::Stub && row.node == node) {
                return Ok(chain);
            }
            if row.kind == Kind::Stub {
                (rows, cover) = self.line(row.node, &|n| self.pick(n));
            } else {
                rows = self.children(&row);
                cover = rows.iter().enumerate().map(|(i, r)| (r.node, i)).collect();
            }
        }
    }

    /// The nodes with a cursor at them, open or closed, in id order.
    pub fn marks(&self) -> Vec<usize> {
        let mut marks: Vec<usize> = self.marked.iter().copied().collect();
        marks.sort_unstable();
        marks
    }

    // Making rows

    fn open_below(&self, node: usize) -> Vec<u64> {
        let found = self.above.iter();
        found
            .filter(|(_, above)| above.contains(&node))
            .map(|(c, _)| *c)
            .collect()
    }

    fn closed_below(&self, node: usize) -> Vec<u64> {
        let found = self.gone_above.iter();
        found
            .filter(|(_, above)| above.contains(&node))
            .map(|(c, _)| *c)
            .collect()
    }

    /// The child a stub's line goes on through.
    fn pick(&self, node: usize) -> Option<usize> {
        let kids = &self.shape.kids[node];
        for above in self.above.values() {
            if let Some(&kid) = kids.iter().find(|k| above.contains(k)) {
                return Some(kid);
            }
        }
        kids.iter()
            .copied()
            .max_by_key(|&k| (self.shape.height[k], Reverse(k)))
    }

    fn stub(&self, root: usize) -> Row {
        Row {
            expandable: true,
            size: self.shape.size[root],
            turns: self.shape.turns[root],
            cursors: self.open_below(root),
            closed: self.closed_below(root),
            ..Row::plain(Kind::Stub, root)
        }
    }

    /// The rows of the line from `start` through `follow`: nodes before the first turn, then a row
    /// for each turn, and the stubs hanging level with them; and which row shows each node of the
    /// line (and each stub hung in a turn).
    fn line(&self, start: usize, follow: &dyn Fn(usize) -> Option<usize>) -> (Vec<Row>, Cover) {
        let shape = self.shape;
        let mut out = Vec::new();
        let mut cover = Cover::new();
        let mut turn: Option<Vec<usize>> = None;
        let mut hung: Vec<(usize, usize)> = Vec::new();
        let mut cur = Some(start);
        while let Some(c) = cur {
            let nxt = follow(c);
            let stubs: Vec<usize> = shape.kids[c]
                .iter()
                .copied()
                .filter(|&k| Some(k) != nxt)
                .collect();
            if shape.user[c] {
                turn = Some(Vec::new());
                hung.clear();
            } else if turn.is_none() {
                cover.insert(c, out.len());
                out.push(Row::plain(Kind::Node, c));
                self.hoist(&mut out, &mut cover, &stubs);
                cur = nxt;
                continue;
            }
            let members = turn.as_mut().expect("a user node opened the turn");
            members.push(c);
            if nxt.is_none_or(|n| shape.user[n]) {
                let at = out.len();
                cover.extend(members.iter().map(|&m| (m, at)));
                cover.extend(hung.iter().map(|&(_, root)| (root, at)));
                out.push(self.turn(members, &hung));
                self.hoist(&mut out, &mut cover, &stubs);
                turn = None;
            } else {
                hung.extend(stubs.into_iter().map(|s| (c, s)));
            }
            cur = nxt;
        }
        (out, cover)
    }

    fn hoist(&self, out: &mut Vec<Row>, cover: &mut Cover, stubs: &[usize]) {
        for &root in stubs {
            cover.insert(root, out.len());
            out.push(self.stub(root));
        }
    }

    fn turn(&self, members: &[usize], hung: &[(usize, usize)]) -> Row {
        let touched = members.iter().any(|m| self.marked.contains(m));
        let mut shown: HashSet<u64> = HashSet::new();
        let mut gone: HashSet<u64> = HashSet::new();
        if touched {
            shown.extend(
                self.open_at
                    .iter()
                    .filter(|(_, at)| members.contains(at))
                    .map(|(c, _)| *c),
            );
            gone.extend(
                self.gone_at
                    .iter()
                    .filter(|(_, at)| members.contains(at))
                    .map(|(c, _)| *c),
            );
        }
        let mut opens = touched;
        for &(_, root) in hung {
            let below = self.open_below(root);
            opens = opens || !below.is_empty();
            shown.extend(below);
            gone.extend(self.closed_below(root));
        }
        let sorted = |set: HashSet<u64>| {
            let mut v: Vec<u64> = set.into_iter().collect();
            v.sort_unstable();
            v
        };
        Row {
            opens,
            expandable: members.len() > 1 || !hung.is_empty(),
            size: members.len() - 1,
            calls: members.iter().map(|&m| self.shape.calls[m]).sum(),
            branches: hung.len(),
            cursors: sorted(shown),
            closed: sorted(gone),
            members: members.to_vec(),
            hung: hung.to_vec(),
            ..Row::plain(Kind::Turn, members[0])
        }
    }
}

#[cfg(test)]
mod tests;
