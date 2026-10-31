//! Error mapping: every `tau_core::tree::Error` and `tau_store::Error` variant
//! becomes its own exception, subclassing the closest builtin. No catch-all.
//! `Io` is the one exception to "created here": it becomes the builtin
//! `OSError` (or its subclass for the errno), which already says what failed.

use std::io;
use std::path::Path;

use pyo3::create_exception;
use pyo3::exceptions::{PyFileExistsError, PyLookupError, PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyModuleMethods, PyType};
use tau_core::tree;
use tau_store as store;

/// The one table of exception classes: each is created here and listed in
/// `CLASSES` under its own name, which [`add_all`] registers.
macro_rules! exceptions {
    ($($name:ident: $base:ty = $doc:literal;)*) => {
        $(create_exception!(tau_next._core, $name, $base, $doc);)*

        /// Python name and class of every exception created above.
        const CLASSES: &[(&str, fn(Python<'_>) -> Bound<'_, PyType>)] =
            &[$((stringify!($name), |py| py.get_type::<$name>())),*];
    };
}

exceptions! {
    NoSuchNode: PyLookupError = "A node id that is not in the session.";
    NoSuchCursor: PyLookupError = "A cursor id that is not open.";
    NoSuchLabel: PyLookupError = "A label that is not set.";
    Busy: PyRuntimeError = "A step on a cursor whose step is running.";
    NotInHook: PyRuntimeError = "A hook's move, write, or auto-step change when no cursor hook is running on that cursor.";
    NoSuchItem: PyLookupError = "An item id that is not in the scratchpad.";
    NotPending: PyLookupError = "A write that delivers an item not pending for that cursor.";
    DuplicateCall: PyValueError = "A Results or Assistant body naming a call id twice (V2).";
    BadRecord: PyValueError = "A logged record that replay cannot apply.";
    BadReplay: PyValueError = "Replay data that is not the text of a JSON object, or a replay with no provider name.";
    Leased: PyOSError = "Another store holds the session's lease.";
    Exists: PyFileExistsError = "A session already exists in that directory.";
    NotASession: PyValueError = "Not a τ log or scratchpad file.";
    Version: PyValueError = "A session file of a version this build does not read.";
    Corrupt: PyValueError = "A damaged scratchpad file.";
    Undecodable: PyValueError = "A log record whose bytes are intact but do not decode; the log is not cut.";
    TooLarge: PyValueError = "A record of 4 GiB or more, which the log cannot hold; the session is closed, as after a failed save.";
    Poisoned: PyRuntimeError = "A call panicked while it held the session (that call raised PanicException); load the session again.";
    WrongAnswer: PyValueError = "An answer to no job; nothing changed.";
    Closed: PyRuntimeError = "The session was closed.";
    TraceError: PyRuntimeError = "A trace started while one runs, stopped while none runs, or that cannot be installed.";
}

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

impl Failure {
    /// As `PyErr`, naming `dir` as the file of an OS error: the session
    /// directory a `create` or `load` was given.
    pub fn into_py_in(self, dir: Option<&Path>) -> PyErr {
        match self {
            Failure::Store(store::Error::Io(e)) => os_err(&e, dir),
            f => f.into(),
        }
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
        tree::Error::DuplicateCall => DuplicateCall::new_err(m),
        tree::Error::WrongAnswer(_) => WrongAnswer::new_err(m),
        tree::Error::BadRecord(_) => BadRecord::new_err(m),
    }
}

/// An `OSError` with `errno` (so the interpreter picks the subclass), its message, and
/// `file` when known.
fn os_err(e: &io::Error, file: Option<&Path>) -> PyErr {
    let Some(errno) = e.raw_os_error() else {
        return PyOSError::new_err(e.to_string());
    };
    let text = e.to_string();
    // `Display` appends " (os error N)", which `OSError` says by `errno`.
    let message = text.split(" (os error").next().unwrap_or(&text).to_owned();
    match file {
        Some(file) => PyOSError::new_err((errno, message, file.to_string_lossy().into_owned())),
        None => PyOSError::new_err((errno, message)),
    }
}

fn store_err(e: store::Error) -> PyErr {
    match e {
        store::Error::Io(e) => os_err(&e, None),
        store::Error::TooLarge(_) => TooLarge::new_err(e.to_string()),
        store::Error::Leased => Leased::new_err(e.to_string()),
        store::Error::Exists => Exists::new_err(e.to_string()),
        store::Error::NotASession(_) => NotASession::new_err(e.to_string()),
        store::Error::Version(_) => Version::new_err(e.to_string()),
        store::Error::Corrupt(_) => Corrupt::new_err(e.to_string()),
        store::Error::Undecodable { .. } => Undecodable::new_err(e.to_string()),
        store::Error::Core(e) => tree_err(&e),
    }
}

/// Add every exception class to the module.
pub fn add_all(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    for (name, class) in CLASSES {
        m.add(*name, class(py))?;
    }
    Ok(())
}
