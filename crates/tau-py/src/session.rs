//! `Session`: the machine plus its optional store, behind one lock.

use std::path::PathBuf;
use std::sync::Mutex;

use pyo3::prelude::*;
use tau_core::machine::Machine;
use tau_core::tree;
use tau_store::Store;

use crate::error::Failure;
use crate::job::{Fed, Job};
use crate::values::{Author, Body, Config, Item, ItemData, Move, Next, Node, Scratchpad};

#[derive(Debug)]
struct Inner {
    machine: Machine,
    store: Option<Store>,
}

impl Inner {
    /// Hand the queued records to the store, or drop them when there is none:
    /// an in-memory session has nowhere to make them durable. A failed save
    /// consumes the session (`Store::save`): the caller is left with none.
    fn save(mut self) -> Result<Self, Failure> {
        if let Some(store) = self.store.take() {
            self.store = Some(store.save(&mut self.machine.session)?);
        } else {
            self.machine.session.outbox.clear();
        }
        Ok(self)
    }

    /// Cursor arguments are checked, so an unknown id is `NoSuchCursor` rather
    /// than an empty answer or a phantom record.
    fn cursor(&self, cursor: u64) -> Result<(), Failure> {
        self.machine.session.at(cursor)?;
        Ok(())
    }
}

/// One session: its tree, cursors, scratchpad, and every cursor's step.
///
/// `frozen`: the state lives in a `Mutex`, so the class has no mutable Python
/// attributes and is shared across threads on free-threaded builds without
/// further locking. Every call takes the lock with the GIL released, so
/// a thread waiting on the lock never holds the interpreter. Each call that
/// changes the session hands its queued records to the store (when there is
/// one) before it returns; Python never saves.
#[pyclass(frozen, module = "tau_next._core")]
#[derive(Debug)]
pub struct Session {
    inner: Mutex<Option<Inner>>,
}

impl Session {
    fn lock(&self) -> Result<std::sync::MutexGuard<'_, Option<Inner>>, Failure> {
        self.inner.lock().map_err(|_| Failure::Poisoned)
    }

    /// A call that only reads.
    fn read<R: Send>(
        &self,
        py: Python<'_>,
        span: tracing::Span,
        f: impl FnOnce(&Inner) -> Result<R, Failure> + Send,
    ) -> PyResult<R> {
        let _span = span.entered();
        Ok(py.detach(|| {
            let guard = tracing::info_span!("py.lock").in_scope(|| self.lock())?;
            f(guard.as_ref().ok_or(Failure::Closed)?)
        })?)
    }

    /// A call that changes the session: then the store gets what it queued.
    /// A failed save is a crash: the OS error is raised and the session is
    /// closed, as `close` leaves it; reopen it from disk. Otherwise a failure
    /// of the call itself is raised.
    fn write<R: Send>(
        &self,
        py: Python<'_>,
        span: tracing::Span,
        f: impl FnOnce(&mut Machine) -> Result<R, Failure> + Send,
    ) -> PyResult<R> {
        let _span = span.entered();
        Ok(py.detach(|| {
            let mut guard = tracing::info_span!("py.lock").in_scope(|| self.lock())?;
            let mut inner = guard.take().ok_or(Failure::Closed)?;
            let result = tracing::info_span!("py.machine").in_scope(|| f(&mut inner.machine));
            *guard = Some(tracing::info_span!("py.save").in_scope(|| inner.save())?);
            result
        })?)
    }

    fn new(inner: Inner) -> Self {
        Self {
            inner: Mutex::new(Some(inner)),
        }
    }
}

#[pymethods]
impl Session {
    /// A new session with a `Config` root, a `System` node, and cursor 0 on
    /// it. In `directory` (created if missing) when given, else in memory.
    #[staticmethod]
    #[pyo3(signature = (config, system, now, directory = None))]
    fn create(
        py: Python<'_>,
        config: Config,
        system: &str,
        now: u64,
        directory: Option<PathBuf>,
    ) -> PyResult<Self> {
        let inner = py.detach(|| {
            let (mut session, _) = tree::Session::create(tree::Config::from(config), system, now);
            let store = directory
                .map(|dir| Store::create(&dir, &mut session))
                .transpose()?;
            Inner {
                machine: Machine::new(session),
                store,
            }
            .save()
        })?;
        Ok(Self::new(inner))
    }

    /// Open the session in `directory`, taking its lease. Every cursor loads
    /// with auto-step off, woken at `now` (load is a wake).
    #[staticmethod]
    fn load(py: Python<'_>, directory: PathBuf, now: u64) -> PyResult<Self> {
        let (store, session) = py
            .detach(move || Store::open(&directory))
            .map_err(Failure::from)?;
        Ok(Self::new(Inner {
            machine: Machine::loaded(session, now),
            store: Some(store),
        }))
    }

    /// Drop the store and release the lease without writing what is not yet
    /// synced, as a crash would. Every later call raises `Closed`. A second
    /// close is `Closed` too.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        Ok(py.detach(|| {
            self.lock()?.take().ok_or(Failure::Closed)?;
            Ok::<(), Failure>(())
        })?)
    }

    /// Bytes cut from the log's tail when this session was loaded; 0 in memory.
    fn dropped(&self, py: Python<'_>) -> PyResult<u64> {
        self.read(py, tracing::info_span!("py.dropped"), |i| {
            Ok(i.store.as_ref().map_or(0, |s| s.dropped))
        })
    }

    // Frontend calls.

    fn input(
        &self,
        py: Python<'_>,
        cursor: u64,
        client: String,
        text: &str,
        now: u64,
    ) -> PyResult<u64> {
        self.write(py, tracing::info_span!("py.input"), |m| {
            Ok(m.input(cursor, client, text, now)?)
        })
    }

    fn send(
        &self,
        py: Python<'_>,
        cursor: u64,
        client: String,
        text: &str,
        now: u64,
    ) -> PyResult<u64> {
        self.write(py, tracing::info_span!("py.send"), |m| {
            Ok(m.send(cursor, client, text, now)?)
        })
    }

    fn step(&self, py: Python<'_>, cursor: u64, client: String, now: u64) -> PyResult<()> {
        self.write(py, tracing::info_span!("py.step"), |m| {
            Ok(m.step(cursor, client, now)?)
        })
    }

    /// Returns whether the driver must cancel the effect in flight.
    fn go_to(
        &self,
        py: Python<'_>,
        cursor: u64,
        to: u64,
        client: String,
        now: u64,
    ) -> PyResult<bool> {
        self.write(py, tracing::info_span!("py.go_to"), |m| {
            Ok(m.go_to(cursor, to, client, now)?)
        })
    }

    fn set_auto(
        &self,
        py: Python<'_>,
        cursor: u64,
        on: bool,
        client: String,
        now: u64,
    ) -> PyResult<()> {
        self.write(py, tracing::info_span!("py.set_auto"), |m| {
            Ok(m.set_auto(cursor, on, client, now)?)
        })
    }

    /// Returns whether the driver must cancel the effect in flight.
    fn abort(&self, py: Python<'_>, cursor: u64) -> PyResult<bool> {
        self.write(
            py,
            tracing::info_span!("py.abort"),
            |m| Ok(m.abort(cursor)?),
        )
    }

    // Extension and driver calls.

    /// `hook`: the cursor whose hook makes this call, which then rides that
    /// phase's sync while its hook job is out; none for a frontend's or the
    /// driver's call, which syncs.
    #[pyo3(signature = (at, by, now, hook = None))]
    fn open(
        &self,
        py: Python<'_>,
        at: u64,
        by: Author,
        now: u64,
        hook: Option<u64>,
    ) -> PyResult<u64> {
        self.write(py, tracing::info_span!("py.open"), |m| {
            Ok(m.open(at, by.into(), now, hook)?)
        })
    }

    /// `hook` as for `open`.
    #[pyo3(signature = (to, data, now, hook = None))]
    fn add_item(
        &self,
        py: Python<'_>,
        to: u64,
        data: ItemData,
        now: u64,
        hook: Option<u64>,
    ) -> PyResult<u64> {
        self.write(py, tracing::info_span!("py.add_item"), |m| {
            Ok(m.add_item(to, data.into(), now, hook)?)
        })
    }

    /// `hook` as for `open`.
    #[pyo3(signature = (id, hook = None))]
    fn remove_item(&self, py: Python<'_>, id: u64, hook: Option<u64>) -> PyResult<()> {
        self.write(py, tracing::info_span!("py.remove_item"), |m| {
            Ok(m.remove_item(id, hook)?)
        })
    }

    /// Append a node under `parent` (none: a root) without moving any cursor.
    /// `hook` as for `open`.
    #[pyo3(signature = (parent, body, by, now, hook = None))]
    fn append(
        &self,
        py: Python<'_>,
        parent: Option<u64>,
        body: Body,
        by: Author,
        now: u64,
        hook: Option<u64>,
    ) -> PyResult<u64> {
        self.write(py, tracing::info_span!("py.append"), |m| {
            Ok(m.append(parent, body.into(), by.into(), now, hook)?)
        })
    }

    // A cursor hook's calls on its own cursor: refused (`NotInHook`) unless a
    // cursor hook job is out for it.

    fn hook_move(
        &self,
        py: Python<'_>,
        cursor: u64,
        to: u64,
        by: Author,
        now: u64,
    ) -> PyResult<()> {
        self.write(py, tracing::info_span!("py.hook_move"), |m| {
            Ok(m.hook_move(cursor, to, by.into(), now)?)
        })
    }

    fn hook_write(
        &self,
        py: Python<'_>,
        cursor: u64,
        body: Body,
        delivers: Vec<u64>,
        by: Author,
        now: u64,
    ) -> PyResult<u64> {
        self.write(py, tracing::info_span!("py.hook_write"), |m| {
            Ok(m.hook_write(cursor, body.into(), delivers, by.into(), now)?)
        })
    }

    fn hook_set_auto(&self, py: Python<'_>, cursor: u64, on: bool) -> PyResult<()> {
        self.write(py, tracing::info_span!("py.hook_set_auto"), |m| {
            Ok(m.hook_set_auto(cursor, on)?)
        })
    }

    /// `hook` as for `open`.
    #[pyo3(signature = (label, cursor, hook = None))]
    fn set_label(
        &self,
        py: Python<'_>,
        label: &str,
        cursor: u64,
        hook: Option<u64>,
    ) -> PyResult<()> {
        self.write(py, tracing::info_span!("py.set_label"), |m| {
            Ok(m.set_label(label, cursor, hook)?)
        })
    }

    /// `hook` as for `open`.
    #[pyo3(signature = (label, hook = None))]
    fn remove_label(&self, py: Python<'_>, label: &str, hook: Option<u64>) -> PyResult<()> {
        self.write(py, tracing::info_span!("py.remove_label"), |m| {
            Ok(m.remove_label(label, hook)?)
        })
    }

    // Driving.

    /// The cursor's next job; `Job.Rest` when it has no step.
    fn next_job(&self, py: Python<'_>, cursor: u64) -> PyResult<Job> {
        self.write(py, tracing::info_span!("py.next_job"), |m| {
            Ok(m.next_job(cursor)?.into())
        })
    }

    /// Feed the result of the job last handed out for `cursor`.
    fn feed(&self, py: Python<'_>, cursor: u64, fed: Fed, now: u64) -> PyResult<()> {
        self.write(py, tracing::info_span!("py.feed"), |m| {
            Ok(m.feed(cursor, fed.into(), now)?)
        })
    }

    fn busy(&self, py: Python<'_>, cursor: u64) -> PyResult<bool> {
        self.read(py, tracing::info_span!("py.busy"), |i| {
            i.cursor(cursor)?;
            Ok(i.machine.busy(cursor))
        })
    }

    fn moves_since_step(&self, py: Python<'_>, cursor: u64) -> PyResult<Vec<Move>> {
        self.read(py, tracing::info_span!("py.moves_since_step"), |i| {
            i.cursor(cursor)?;
            Ok(i.machine
                .moves_since_step(cursor)
                .cloned()
                .map(Into::into)
                .collect())
        })
    }

    // Views.

    /// Every open cursor, by id.
    fn cursors(&self, py: Python<'_>) -> PyResult<Vec<u64>> {
        self.read(py, tracing::info_span!("py.cursors"), |i| {
            Ok(i.machine.session.auto.keys().copied().collect())
        })
    }

    /// The node `cursor` is on.
    fn at(&self, py: Python<'_>, cursor: u64) -> PyResult<u64> {
        self.read(py, tracing::info_span!("py.at"), |i| {
            Ok(i.machine.session.at(cursor)?)
        })
    }

    fn node(&self, py: Python<'_>, id: u64) -> PyResult<Node> {
        self.read(py, tracing::info_span!("py.node"), |i| {
            Ok(i.machine.session.node(id)?.clone().into())
        })
    }

    /// The nodes root to `node`, inclusive.
    fn walk(&self, py: Python<'_>, node: u64) -> PyResult<Vec<Node>> {
        self.read(py, tracing::info_span!("py.walk"), |i| {
            let s = &i.machine.session;
            s.node(node)?;
            Ok(s.walk(node).into_iter().cloned().map(Into::into).collect())
        })
    }

    /// The children of `node`, in append order.
    fn children(&self, py: Python<'_>, node: u64) -> PyResult<Vec<u64>> {
        self.read(py, tracing::info_span!("py.children"), |i| {
            Ok(i.machine.session.children(node)?.to_vec())
        })
    }

    /// How many nodes there are; ids are `0..node_count()`.
    fn node_count(&self, py: Python<'_>) -> PyResult<usize> {
        self.read(py, tracing::info_span!("py.node_count"), |i| {
            Ok(i.machine.session.node_count())
        })
    }

    /// Every label with its cursor, in string order.
    fn labels(&self, py: Python<'_>) -> PyResult<Vec<(String, u64)>> {
        self.read(py, tracing::info_span!("py.labels"), |i| {
            Ok(i.machine
                .session
                .labels()
                .map(|(l, c)| (l.to_owned(), c))
                .collect())
        })
    }

    fn config_at(&self, py: Python<'_>, node: u64) -> PyResult<Config> {
        self.read(py, tracing::info_span!("py.config_at"), |i| {
            Ok(i.machine.session.config_at(node)?.clone().into())
        })
    }

    /// The deepest node on both walks; `None` across trees.
    fn lca(&self, py: Python<'_>, a: u64, b: u64) -> PyResult<Option<u64>> {
        self.read(py, tracing::info_span!("py.lca"), |i| {
            Ok(i.machine.session.lca(a, b)?)
        })
    }

    /// What the cursor needs next, read from its walk alone.
    fn next(&self, py: Python<'_>, cursor: u64) -> PyResult<Next> {
        self.read(py, tracing::info_span!("py.next"), |i| {
            i.cursor(cursor)?;
            Ok(i.machine.session.next(cursor).into())
        })
    }

    fn auto(&self, py: Python<'_>, cursor: u64) -> PyResult<bool> {
        self.read(py, tracing::info_span!("py.auto"), |i| {
            i.cursor(cursor)?;
            Ok(i.machine.session.auto[&cursor])
        })
    }

    /// The cursor's scratchpad, oldest first.
    fn items(&self, py: Python<'_>, cursor: u64) -> PyResult<Vec<Item>> {
        self.read(py, tracing::info_span!("py.items"), |i| {
            i.cursor(cursor)?;
            Ok(i.machine
                .session
                .items(cursor)
                .cloned()
                .map(Into::into)
                .collect())
        })
    }

    /// The whole scratchpad, every cursor's items.
    fn scratchpad(&self, py: Python<'_>) -> PyResult<Scratchpad> {
        self.read(py, tracing::info_span!("py.scratchpad"), |i| {
            Ok(i.machine.session.scratchpad().clone().into())
        })
    }

    /// The cursor `label` is on.
    fn label(&self, py: Python<'_>, label: &str) -> PyResult<u64> {
        self.read(py, tracing::info_span!("py.label"), |i| {
            Ok(i.machine.session.label(label)?)
        })
    }

    /// The cursor's labels, in string order.
    fn labels_of(&self, py: Python<'_>, cursor: u64) -> PyResult<Vec<String>> {
        self.read(py, tracing::info_span!("py.labels_of"), |i| {
            i.cursor(cursor)?;
            Ok(i.machine
                .session
                .labels_of(cursor)
                .map(String::from)
                .collect())
        })
    }

    /// Every move of `cursor` from index `since` of the session's moves on.
    #[pyo3(signature = (cursor, since = 0))]
    fn moves_of(&self, py: Python<'_>, cursor: u64, since: usize) -> PyResult<Vec<Move>> {
        self.read(py, tracing::info_span!("py.moves_of"), |i| {
            i.cursor(cursor)?;
            Ok(i.machine
                .session
                .moves_of(cursor, since)
                .cloned()
                .map(Into::into)
                .collect())
        })
    }

    /// Drain the `(client, text)` messages to clients, oldest first.
    fn take_notices(&self, py: Python<'_>) -> PyResult<Vec<(String, String)>> {
        self.write(py, tracing::info_span!("py.take_notices"), |m| {
            Ok(m.session.take_notices())
        })
    }
}
