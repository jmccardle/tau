//! Error mapping: every `tau_core::tree::Error` and `tau_store::Error` variant
//! becomes its own exception, subclassing the closest builtin. No catch-all.
//! `Io` is the one exception to "created here": it becomes the builtin
//! `OSError` (or its subclass for the errno), which already says what failed.

use pyo3::create_exception;
use pyo3::exceptions::{PyFileExistsError, PyLookupError, PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyModuleMethods;
use tau_core::tree;
use tau_store as store;

create_exception!(
    tau_next._core,
    NoSuchNode,
    PyLookupError,
    "A node id that is not in the session."
);
create_exception!(
    tau_next._core,
    NoSuchCursor,
    PyLookupError,
    "A cursor id that is not open."
);
create_exception!(
    tau_next._core,
    NoSuchLabel,
    PyLookupError,
    "A label that is not set."
);
create_exception!(
    tau_next._core,
    Busy,
    PyRuntimeError,
    "A step on a cursor whose step is running."
);
create_exception!(
    tau_next._core,
    NotInHook,
    PyRuntimeError,
    "A hook's move, write, or auto-step change when no cursor hook is running on that cursor."
);
create_exception!(
    tau_next._core,
    NoSuchItem,
    PyLookupError,
    "An item id that is not in the scratchpad."
);
create_exception!(
    tau_next._core,
    NotPending,
    PyLookupError,
    "A write that delivers an item not pending for that cursor."
);
create_exception!(
    tau_next._core,
    RootNotConfig,
    PyValueError,
    "A root node that is not a Config (V1)."
);
create_exception!(
    tau_next._core,
    DuplicateCall,
    PyValueError,
    "A Results body naming a call twice (V2)."
);
create_exception!(
    tau_next._core,
    BadRecord,
    PyValueError,
    "A logged record that replay cannot apply."
);
create_exception!(
    tau_next._core,
    Leased,
    PyOSError,
    "Another store holds the session's lease."
);
create_exception!(
    tau_next._core,
    Exists,
    PyFileExistsError,
    "A session already exists in that directory."
);
create_exception!(
    tau_next._core,
    NotASession,
    PyValueError,
    "Not a τ log or scratchpad file."
);
create_exception!(
    tau_next._core,
    Version,
    PyValueError,
    "A session file of a version this build does not read."
);
create_exception!(
    tau_next._core,
    Corrupt,
    PyValueError,
    "A damaged scratchpad file."
);
create_exception!(
    tau_next._core,
    Poisoned,
    PyRuntimeError,
    "A call panicked while it held the session; load the session again."
);

create_exception!(
    tau_next._core,
    WrongFeed,
    PyValueError,
    "A feed that answers no job; nothing changed."
);
create_exception!(
    tau_next._core,
    Closed,
    PyRuntimeError,
    "The session was closed."
);

/// Everything a session call can fail with.
#[derive(Debug)]
pub enum Failure {
    Tree(tree::Error),
    Store(store::Error),
    /// A call panicked inside the session's lock.
    Poisoned,
    /// `close` was called.
    Closed,
}

impl From<tree::Error> for Failure {
    fn from(e: tree::Error) -> Self {
        Self::Tree(e)
    }
}

impl From<store::Error> for Failure {
    fn from(e: store::Error) -> Self {
        Self::Store(e)
    }
}

impl From<Failure> for PyErr {
    fn from(f: Failure) -> Self {
        match f {
            Failure::Tree(e) => tree_err(&e),
            Failure::Store(e) => store_err(e),
            Failure::Closed => Closed::new_err("the session is closed"),
            Failure::Poisoned => Poisoned::new_err("a call panicked while it held the session"),
        }
    }
}

fn tree_err(e: &tree::Error) -> PyErr {
    // One wording: `Display` in core, shared with a step's `Failed` reason.
    let m = e.to_string();
    match e {
        tree::Error::NoSuchNode(_) => NoSuchNode::new_err(m),
        tree::Error::NoSuchCursor(_) => NoSuchCursor::new_err(m),
        tree::Error::NoSuchLabel(_) => NoSuchLabel::new_err(m),
        tree::Error::Busy(_) => Busy::new_err(m),
        tree::Error::NotInHook(_) => NotInHook::new_err(m),
        tree::Error::NoSuchItem(_) => NoSuchItem::new_err(m),
        tree::Error::NotPending(_) => NotPending::new_err(m),
        tree::Error::RootNotConfig => RootNotConfig::new_err(m),
        tree::Error::DuplicateCall => DuplicateCall::new_err(m),
        tree::Error::WrongFeed(_) => WrongFeed::new_err(m),
        tree::Error::BadRecord(_) => BadRecord::new_err(m),
    }
}

fn store_err(e: store::Error) -> PyErr {
    match e {
        store::Error::Io(e) => e.into(),
        store::Error::Leased => Leased::new_err(e.to_string()),
        store::Error::Exists => Exists::new_err(e.to_string()),
        store::Error::NotASession(_) => NotASession::new_err(e.to_string()),
        store::Error::Version(_) => Version::new_err(e.to_string()),
        store::Error::Corrupt(_) => Corrupt::new_err(e.to_string()),
        store::Error::Core(e) => tree_err(&e),
    }
}

/// Add every exception class to the module.
pub fn add_all(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("NoSuchNode", py.get_type::<NoSuchNode>())?;
    m.add("NoSuchCursor", py.get_type::<NoSuchCursor>())?;
    m.add("NoSuchLabel", py.get_type::<NoSuchLabel>())?;
    m.add("Busy", py.get_type::<Busy>())?;
    m.add("NotInHook", py.get_type::<NotInHook>())?;
    m.add("NoSuchItem", py.get_type::<NoSuchItem>())?;
    m.add("NotPending", py.get_type::<NotPending>())?;
    m.add("RootNotConfig", py.get_type::<RootNotConfig>())?;
    m.add("DuplicateCall", py.get_type::<DuplicateCall>())?;
    m.add("BadRecord", py.get_type::<BadRecord>())?;
    m.add("WrongFeed", py.get_type::<WrongFeed>())?;
    m.add("Closed", py.get_type::<Closed>())?;
    m.add("Leased", py.get_type::<Leased>())?;
    m.add("Exists", py.get_type::<Exists>())?;
    m.add("NotASession", py.get_type::<NotASession>())?;
    m.add("Version", py.get_type::<Version>())?;
    m.add("Corrupt", py.get_type::<Corrupt>())?;
    m.add("Poisoned", py.get_type::<Poisoned>())?;
    m.add("TraceError", py.get_type::<crate::trace::TraceError>())
}
