//! The read views of a `Session`: `Tree` (the nodes) and `CursorState` (one
//! cursor at one instant). Reads only; nothing here changes the session.

use std::sync::Arc;

use pyo3::prelude::*;
use tau_core::machine::Machine;
use tau_core::tree;

use crate::session::Shared;
use crate::values::{Config, Item, Move, Next, Node};

/// The session's nodes: `Session.tree`. It shares the session's lock, so a
/// read is as current as the call that made it, and a closed session raises
/// `Closed` here too.
#[pyclass(frozen, module = "tau._core")]
#[derive(Debug)]
pub struct Tree {
    pub(crate) shared: Arc<Shared>,
}

#[pymethods]
impl Tree {
    fn node(&self, py: Python<'_>, id: u64) -> PyResult<Node> {
        self.shared
            .read(py, tracing::info_span!("py.tree.node"), |i| {
                Ok(i.machine.session.tree.node(id)?.clone().into())
            })
    }

    /// The nodes root to `node`, inclusive.
    fn walk(&self, py: Python<'_>, node: u64) -> PyResult<Vec<Node>> {
        self.shared
            .read(py, tracing::info_span!("py.tree.walk"), |i| {
                let tree = &i.machine.session.tree;
                tree.node(node)?;
                Ok(tree
                    .walk(node)
                    .into_iter()
                    .cloned()
                    .map(Into::into)
                    .collect())
            })
    }

    /// The children of `node`, in append order.
    fn children(&self, py: Python<'_>, node: u64) -> PyResult<Vec<u64>> {
        self.shared
            .read(py, tracing::info_span!("py.tree.children"), |i| {
                Ok(i.machine.session.tree.children(node)?.to_vec())
            })
    }

    /// How many nodes there are; ids are `0..len(tree)`.
    fn __len__(&self, py: Python<'_>) -> PyResult<usize> {
        self.shared
            .read(py, tracing::info_span!("py.tree.len"), |i| {
                Ok(i.machine.session.tree.node_count())
            })
    }

    fn config_at(&self, py: Python<'_>, node: u64) -> PyResult<Config> {
        self.shared
            .read(py, tracing::info_span!("py.tree.config_at"), |i| {
                Ok(i.machine.session.tree.config_at(node)?.clone().into())
            })
    }

    /// The deepest node on both walks; `None` across trees.
    fn lca(&self, py: Python<'_>, a: u64, b: u64) -> PyResult<Option<u64>> {
        self.shared
            .read(py, tracing::info_span!("py.tree.lca"), |i| {
                Ok(i.machine.session.tree.lca(a, b)?)
            })
    }
}

/// One cursor as `Session.cursor` read it: every field from the same instant,
/// under one lock, so `busy` and `at` cannot disagree. Later changes do not
/// reach it. Building one costs the cursor's items and labels, not its moves:
/// a move list is append-only, so the snapshot keeps where it ended and reads
/// `moves` (under the lock, raising `Closed` if the session is closed) when
/// asked. A move made after the snapshot is not in it.
#[pyclass(frozen, module = "tau._core")]
#[derive(Clone)]
pub struct CursorState {
    shared: Arc<Shared>,
    id: u64,
    /// The node the cursor is on.
    #[pyo3(get)]
    at: u64,
    #[pyo3(get)]
    auto: bool,
    /// A step is running.
    #[pyo3(get)]
    busy: bool,
    /// What the cursor needs next, read from its walk alone.
    #[pyo3(get)]
    next: Next,
    items: Vec<tree::Item>,
    labels: Vec<String>,
    /// `Session::moves` held this many records when the snapshot was taken.
    moves_end: usize,
    /// ... and the running step began at this index.
    step_start: usize,
}

#[pymethods]
impl CursorState {
    fn __repr__(&self) -> String {
        format!("{self:?}")
    }

    /// Its scratchpad, oldest first.
    #[getter]
    fn items(&self) -> Vec<Item> {
        self.items.iter().cloned().map(Into::into).collect()
    }

    /// Its labels, in string order.
    #[getter]
    fn labels(&self) -> Vec<String> {
        self.labels.clone()
    }

    /// Every move of the cursor, oldest first.
    #[getter]
    fn moves(&self, py: Python<'_>) -> PyResult<Vec<Move>> {
        self.moves_from(py, 0, tracing::info_span!("py.cursor_state.moves"))
    }

    /// Its moves since the running step woke, other hooks' included; none at rest.
    #[getter]
    fn moves_since_step(&self, py: Python<'_>) -> PyResult<Vec<Move>> {
        let span = tracing::info_span!("py.cursor_state.moves_since_step");
        self.moves_from(py, self.step_start, span)
    }
}

/// The snapshot's own fields, never the session behind its handle.
impl std::fmt::Debug for CursorState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CursorState")
            .field("cursor", &self.id)
            .field("at", &self.at)
            .field("auto", &self.auto)
            .field("busy", &self.busy)
            .field("next", &self.next)
            .field("items", &self.items.len())
            .field("labels", &self.labels.len())
            .finish_non_exhaustive()
    }
}

impl CursorState {
    /// `NoSuchCursor` if `cursor` is not open.
    pub(crate) fn of(
        shared: &Arc<Shared>,
        machine: &Machine,
        cursor: u64,
    ) -> Result<Self, tree::Error> {
        let s = &machine.session;
        Ok(Self {
            shared: Arc::clone(shared),
            id: cursor,
            at: s.at(cursor)?,
            auto: s.auto[&cursor],
            busy: machine.busy(cursor),
            next: s.next(cursor).into(),
            items: s.items(cursor).cloned().collect(),
            labels: s.labels_of(cursor).map(String::from).collect(),
            moves_end: s.moves.len(),
            step_start: machine.step_start(cursor),
        })
    }

    /// The cursor's moves in `Session::moves[from..moves_end]`.
    fn moves_from(&self, py: Python<'_>, from: usize, span: tracing::Span) -> PyResult<Vec<Move>> {
        self.shared.read(py, span, |i| {
            let moves = &i.machine.session.moves[from.min(self.moves_end)..self.moves_end];
            Ok(moves
                .iter()
                .filter(|m| m.cursor == self.id)
                .cloned()
                .map(Into::into)
                .collect())
        })
    }
}
