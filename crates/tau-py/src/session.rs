//! `Session`: the machine plus its optional store, behind one lock.

use std::path::PathBuf;
use std::sync::Mutex;

use pyo3::prelude::*;
use tau_core::machine::Machine;
use tau_core::tree;
use tau_store::Store;

use crate::error::Failure;
use crate::job::{Fed, Job};
use crate::values::{Author, Config, Item, ItemData, Move, Next, Node, Scratchpad};

#[derive(Debug)]
struct Inner {
    machine: Machine,
    store: Option<Store>,
}

impl Inner {
    /// Hand the queued records to the store, or drop them when there is none:
    /// an in-memory session has nowhere to make them durable.
    fn save(&mut self) -> Result<(), Failure> {
        match &mut self.store {
            Some(store) => store.save(&mut self.machine.session)?,
            None => self.machine.session.outbox.clear(),
        }
        Ok(())
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
        f: impl FnOnce(&Inner) -> Result<R, Failure> + Send,
    ) -> PyResult<R> {
        Ok(py.detach(|| f(self.lock()?.as_ref().ok_or(Failure::Closed)?))?)
    }

    /// A call that changes the session: then the store gets what it queued.
    /// A failure of the call itself is reported before a failure to save; the
    /// store refuses every call after its own failure.
    fn write<R: Send>(
        &self,
        py: Python<'_>,
        f: impl FnOnce(&mut Machine) -> Result<R, Failure> + Send,
    ) -> PyResult<R> {
        Ok(py.detach(|| {
            let mut guard = self.lock()?;
            let inner = guard.as_mut().ok_or(Failure::Closed)?;
            let result = f(&mut inner.machine);
            let saved = inner.save();
            let result = result?;
            saved?;
            Ok::<R, Failure>(result)
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
            let mut inner = Inner {
                machine: Machine::new(session),
                store,
            };
            inner.save()?;
            Ok::<Inner, Failure>(inner)
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
        self.read(py, |i| Ok(i.store.as_ref().map_or(0, |s| s.dropped)))
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
        self.write(py, |m| Ok(m.input(cursor, client, text, now)?))
    }

    fn send(
        &self,
        py: Python<'_>,
        cursor: u64,
        client: String,
        text: &str,
        now: u64,
    ) -> PyResult<u64> {
        self.write(py, |m| Ok(m.send(cursor, client, text, now)?))
    }

    fn step(&self, py: Python<'_>, cursor: u64, client: String, now: u64) -> PyResult<()> {
        self.write(py, |m| Ok(m.step(cursor, client, now)?))
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
        self.write(py, |m| Ok(m.go_to(cursor, to, client, now)?))
    }

    fn set_auto(
        &self,
        py: Python<'_>,
        cursor: u64,
        on: bool,
        client: String,
        now: u64,
    ) -> PyResult<()> {
        self.write(py, |m| Ok(m.set_auto(cursor, on, client, now)?))
    }

    /// Returns whether the driver must cancel the effect in flight.
    fn abort(&self, py: Python<'_>, cursor: u64) -> PyResult<bool> {
        self.write(py, |m| {
            m.session.at(cursor)?;
            Ok(m.abort(cursor))
        })
    }

    // Extension and driver calls.

    fn open(&self, py: Python<'_>, at: u64, by: Author, now: u64) -> PyResult<u64> {
        self.write(py, |m| Ok(m.open(at, by.into(), now)?))
    }

    fn add_item(&self, py: Python<'_>, to: u64, data: ItemData, now: u64) -> PyResult<u64> {
        self.write(py, |m| Ok(m.add_item(to, data.into(), now)?))
    }

    fn ask(&self, py: Python<'_>, cursor: u64) -> PyResult<()> {
        self.write(py, |m| {
            m.session.at(cursor)?;
            m.ask(cursor);
            Ok(())
        })
    }

    fn answered(&self, py: Python<'_>, cursor: u64) -> PyResult<()> {
        self.write(py, |m| {
            m.session.at(cursor)?;
            m.answered(cursor);
            Ok(())
        })
    }

    fn set_label(&self, py: Python<'_>, label: &str, cursor: u64) -> PyResult<()> {
        self.write(py, |m| Ok(m.set_label(label, cursor)?))
    }

    fn remove_label(&self, py: Python<'_>, label: &str) -> PyResult<()> {
        self.write(py, |m| Ok(m.remove_label(label)?))
    }

    // Driving.

    /// The cursor's next job; `Job.Rest` when it has no step.
    fn next_job(&self, py: Python<'_>, cursor: u64) -> PyResult<Job> {
        self.write(py, |m| {
            m.session.at(cursor)?;
            Ok(m.next_job(cursor)?.into())
        })
    }

    /// Feed the result of the job last handed out for `cursor`.
    fn feed(&self, py: Python<'_>, cursor: u64, fed: Fed, now: u64) -> PyResult<()> {
        self.write(py, |m| {
            m.session.at(cursor)?;
            Ok(m.feed(cursor, fed.into(), now)?)
        })
    }

    fn busy(&self, py: Python<'_>, cursor: u64) -> PyResult<bool> {
        self.read(py, |i| {
            i.cursor(cursor)?;
            Ok(i.machine.busy(cursor))
        })
    }

    fn locked(&self, py: Python<'_>, cursor: u64) -> PyResult<bool> {
        self.read(py, |i| {
            i.cursor(cursor)?;
            Ok(i.machine.locked(cursor))
        })
    }

    fn moves_since_step(&self, py: Python<'_>, cursor: u64) -> PyResult<Vec<Move>> {
        self.read(py, |i| {
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
        self.read(py, |i| Ok(i.machine.session.auto.keys().copied().collect()))
    }

    /// The node `cursor` is on.
    fn at(&self, py: Python<'_>, cursor: u64) -> PyResult<u64> {
        self.read(py, |i| Ok(i.machine.session.at(cursor)?))
    }

    fn node(&self, py: Python<'_>, id: u64) -> PyResult<Node> {
        self.read(py, |i| Ok(i.machine.session.node(id)?.clone().into()))
    }

    /// The nodes root to `node`, inclusive.
    fn walk(&self, py: Python<'_>, node: u64) -> PyResult<Vec<Node>> {
        self.read(py, |i| {
            let s = &i.machine.session;
            s.node(node)?;
            Ok(s.walk(node).into_iter().cloned().map(Into::into).collect())
        })
    }

    fn config_at(&self, py: Python<'_>, node: u64) -> PyResult<Config> {
        self.read(py, |i| {
            Ok(i.machine.session.config_at(node)?.clone().into())
        })
    }

    /// The deepest node on both walks; `None` across trees.
    fn lca(&self, py: Python<'_>, a: u64, b: u64) -> PyResult<Option<u64>> {
        self.read(py, |i| Ok(i.machine.session.lca(a, b)?))
    }

    /// What the cursor needs next, read from its walk alone.
    fn next(&self, py: Python<'_>, cursor: u64) -> PyResult<Next> {
        self.read(py, |i| {
            i.cursor(cursor)?;
            Ok(i.machine.session.next(cursor).into())
        })
    }

    fn auto(&self, py: Python<'_>, cursor: u64) -> PyResult<bool> {
        self.read(py, |i| {
            i.cursor(cursor)?;
            Ok(i.machine.session.auto[&cursor])
        })
    }

    /// The cursor's scratchpad, oldest first.
    fn items(&self, py: Python<'_>, cursor: u64) -> PyResult<Vec<Item>> {
        self.read(py, |i| {
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
        self.read(py, |i| Ok(i.machine.session.scratchpad().clone().into()))
    }

    /// The cursor `label` is on.
    fn label(&self, py: Python<'_>, label: &str) -> PyResult<u64> {
        self.read(py, |i| Ok(i.machine.session.label(label)?))
    }

    /// The cursor's labels, in string order.
    fn labels_of(&self, py: Python<'_>, cursor: u64) -> PyResult<Vec<String>> {
        self.read(py, |i| {
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
        self.read(py, |i| {
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
        self.write(py, |m| Ok(m.session.take_notices()))
    }
}
