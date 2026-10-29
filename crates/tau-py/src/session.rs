//! `Session`: the machine plus its optional store, behind one lock.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use pyo3::prelude::*;
use tau_core::machine::Machine;
use tau_core::tree;
use tau_store::Store;

use crate::error::Failure;
use crate::job::{Answer, Job};
use crate::values::{Author, Body, Config, ItemData};
use crate::view::{CursorState, Tree};

#[derive(Debug)]
pub(crate) struct Inner {
    pub(crate) machine: Machine,
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
}

/// The machine behind its one lock, shared by `Session` and the `Tree` view of it.
///
/// Every call takes the lock with the GIL released, so a thread waiting on the
/// lock never holds the interpreter. `None` is a closed session.
#[derive(Debug)]
pub(crate) struct Shared {
    inner: Mutex<Option<Inner>>,
}

impl Shared {
    fn lock(&self) -> Result<std::sync::MutexGuard<'_, Option<Inner>>, Failure> {
        self.inner.lock().map_err(|_| Failure::Poisoned)
    }

    /// A call that only reads.
    pub(crate) fn read<R: Send>(
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
}

/// One session: its tree, cursors, scratchpad, and every cursor's step.
///
/// `frozen`: the state lives behind a `Mutex`, so the class has no mutable
/// Python attributes and is shared across threads on free-threaded builds
/// without further locking. Each call that changes the session hands its
/// queued records to the store (when there is one) before it returns; Python
/// never saves. `tree` is the read view of the nodes; `cursor` snapshots one
/// cursor.
#[pyclass(frozen, module = "tau_next._core")]
#[derive(Debug)]
pub struct Session {
    shared: Arc<Shared>,
    #[pyo3(get)]
    tree: Py<Tree>,
}

/// A `Session` over `inner`, with the `Tree` that shares its lock.
fn open_session(py: Python<'_>, inner: Inner) -> PyResult<Session> {
    let shared = Arc::new(Shared {
        inner: Mutex::new(Some(inner)),
    });
    let tree = Py::new(
        py,
        Tree {
            shared: Arc::clone(&shared),
        },
    )?;
    Ok(Session { shared, tree })
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
        open_session(py, inner)
    }

    /// Open the session in `directory`, taking its lease. Every cursor loads
    /// with auto-step off, woken (load is a wake).
    #[staticmethod]
    fn load(py: Python<'_>, directory: PathBuf) -> PyResult<Self> {
        let (store, session) = py
            .detach(move || Store::open(&directory))
            .map_err(Failure::from)?;
        open_session(
            py,
            Inner {
                machine: Machine::loaded(session),
                store: Some(store),
            },
        )
    }

    /// Drop the store and release the lease without writing what is not yet
    /// synced, as a crash would. Every later call raises `Closed`. A second
    /// close is `Closed` too.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        Ok(py.detach(|| {
            self.shared.lock()?.take().ok_or(Failure::Closed)?;
            Ok::<(), Failure>(())
        })?)
    }

    // Frontend calls.

    fn input(&self, py: Python<'_>, cursor: u64, client: String, text: &str) -> PyResult<u64> {
        self.shared.write(py, tracing::info_span!("py.input"), |m| {
            Ok(m.input(cursor, client, text)?)
        })
    }

    fn step(&self, py: Python<'_>, cursor: u64) -> PyResult<()> {
        self.shared
            .write(py, tracing::info_span!("py.step"), |m| Ok(m.step(cursor)?))
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
        self.shared.write(py, tracing::info_span!("py.go_to"), |m| {
            Ok(m.go_to(cursor, to, client, now)?)
        })
    }

    fn set_auto(&self, py: Python<'_>, cursor: u64, on: bool) -> PyResult<()> {
        self.shared
            .write(py, tracing::info_span!("py.set_auto"), |m| {
                Ok(m.set_auto(cursor, on)?)
            })
    }

    /// Returns whether the driver must cancel the effect in flight.
    fn abort(&self, py: Python<'_>, cursor: u64) -> PyResult<bool> {
        self.shared.write(
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
        self.shared.write(py, tracing::info_span!("py.open"), |m| {
            Ok(m.open(at, by.into(), now, hook)?)
        })
    }

    /// `hook` as for `open`.
    #[pyo3(signature = (to, data, hook = None))]
    fn add_item(
        &self,
        py: Python<'_>,
        to: u64,
        data: ItemData,
        hook: Option<u64>,
    ) -> PyResult<u64> {
        self.shared
            .write(py, tracing::info_span!("py.add_item"), |m| {
                Ok(m.add_item(to, data.into(), hook)?)
            })
    }

    /// `hook` as for `open`.
    #[pyo3(signature = (id, hook = None))]
    fn remove_item(&self, py: Python<'_>, id: u64, hook: Option<u64>) -> PyResult<()> {
        self.shared
            .write(py, tracing::info_span!("py.remove_item"), |m| {
                Ok(m.remove_item(id, hook)?)
            })
    }

    /// Append a node under `parent` without moving any cursor. `hook` as for
    /// `open`.
    #[pyo3(signature = (parent, body, by, now, hook = None))]
    fn append(
        &self,
        py: Python<'_>,
        parent: u64,
        body: Body,
        by: Author,
        now: u64,
        hook: Option<u64>,
    ) -> PyResult<u64> {
        self.shared
            .write(py, tracing::info_span!("py.append"), |m| {
                Ok(m.append(parent, body.into(), by.into(), now, hook)?)
            })
    }

    /// Append a root `Config` (a detached tree) without moving any cursor.
    /// `hook` as for `open`.
    #[pyo3(signature = (config, by, now, hook = None))]
    fn append_root(
        &self,
        py: Python<'_>,
        config: Config,
        by: Author,
        now: u64,
        hook: Option<u64>,
    ) -> PyResult<u64> {
        self.shared
            .write(py, tracing::info_span!("py.append_root"), |m| {
                Ok(m.append_root(config.into(), by.into(), now, hook))
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
        self.shared
            .write(py, tracing::info_span!("py.hook_move"), |m| {
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
        self.shared
            .write(py, tracing::info_span!("py.hook_write"), |m| {
                Ok(m.hook_write(cursor, body.into(), delivers, by.into(), now)?)
            })
    }

    fn hook_set_auto(&self, py: Python<'_>, cursor: u64, on: bool) -> PyResult<()> {
        self.shared
            .write(py, tracing::info_span!("py.hook_set_auto"), |m| {
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
        self.shared
            .write(py, tracing::info_span!("py.set_label"), |m| {
                Ok(m.set_label(label, cursor, hook)?)
            })
    }

    /// `hook` as for `open`.
    #[pyo3(signature = (label, hook = None))]
    fn remove_label(&self, py: Python<'_>, label: &str, hook: Option<u64>) -> PyResult<()> {
        self.shared
            .write(py, tracing::info_span!("py.remove_label"), |m| {
                Ok(m.remove_label(label, hook)?)
            })
    }

    // Driving.

    /// The cursor's next job; `Job.Rest` when it has no step.
    fn next_job(&self, py: Python<'_>, cursor: u64) -> PyResult<Job> {
        self.shared
            .write(py, tracing::info_span!("py.next_job"), |m| {
                Ok(m.next_job(cursor)?.into())
            })
    }

    /// Answer the job last handed out for `cursor`.
    fn answer(&self, py: Python<'_>, cursor: u64, answer: Answer, now: u64) -> PyResult<()> {
        self.shared
            .write(py, tracing::info_span!("py.answer"), |m| {
                Ok(m.answer(cursor, answer.into(), now)?)
            })
    }

    // Views.

    /// Every open cursor, by id.
    fn cursors(&self, py: Python<'_>) -> PyResult<Vec<u64>> {
        self.shared
            .read(py, tracing::info_span!("py.cursors"), |i| {
                Ok(i.machine.session.auto.keys().copied().collect())
            })
    }

    /// One cursor as it stands, all read under one lock.
    fn cursor(&self, py: Python<'_>, cursor: u64) -> PyResult<CursorState> {
        self.shared.read(py, tracing::info_span!("py.cursor"), |i| {
            Ok(CursorState::of(&self.shared, &i.machine, cursor)?)
        })
    }

    /// Every label with its cursor, in string order.
    fn labels(&self, py: Python<'_>) -> PyResult<Vec<(String, u64)>> {
        self.shared.read(py, tracing::info_span!("py.labels"), |i| {
            Ok(i.machine
                .session
                .labels()
                .map(|(l, c)| (l.to_owned(), c))
                .collect())
        })
    }

    /// The cursor `label` is on.
    fn label(&self, py: Python<'_>, label: &str) -> PyResult<u64> {
        self.shared.read(py, tracing::info_span!("py.label"), |i| {
            Ok(i.machine.session.label(label)?)
        })
    }

    /// Drain the `(client, text)` messages to clients, oldest first.
    fn take_notices(&self, py: Python<'_>) -> PyResult<Vec<(String, String)>> {
        self.shared
            .write(py, tracing::info_span!("py.take_notices"), |m| {
                Ok(std::mem::take(&mut m.session.notices))
            })
    }
}
