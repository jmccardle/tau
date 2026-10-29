//! The base tools: `read`, `write`, `grep`, `bash`.
//!
//! Filesystem and process IO, no `PyO3`, no async runtime. Every tool takes the
//! call's raw JSON arguments and answers `Ok(text)` or `Err(message)` for the
//! model; nothing panics or raises for an expected failure (bad arguments,
//! missing files, a command that exits nonzero).
//!
//! `read`, `grep` and `bash` limit their output through [`limit::Sink`]: past
//! the limit the full output goes to an overflow file and the model gets the
//! kept text plus a note.

pub mod bash;
pub mod grep;
pub mod limit;
pub mod read;
pub(crate) mod session;
pub mod write;

use std::fmt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use serde::de::DeserializeOwned;

/// Default output limit, in bytes of kept text.
pub const DEFAULT_MAX_OUTPUT: usize = 10_000;

/// Why a call failed, in words for the model.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ToolError(pub String);

impl fmt::Display for ToolError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ToolError {}

impl From<String> for ToolError {
    fn from(message: String) -> Self {
        Self(message)
    }
}

impl From<&str> for ToolError {
    fn from(message: &str) -> Self {
        Self(message.to_owned())
    }
}

pub type Result<T> = std::result::Result<T, ToolError>;

/// Decode the call's raw JSON arguments; unknown and missing fields are errors.
pub(crate) fn parse_args<T: DeserializeOwned>(args_raw: &str) -> Result<T> {
    serde_json::from_str(args_raw).map_err(|e| ToolError(format!("invalid arguments: {e}")))
}

/// `path` as the tool reads it: relative paths resolve against `cwd`.
pub(crate) fn resolve(cwd: &Path, path: &str) -> PathBuf {
    cwd.join(path)
}

/// A flag a caller sets to stop a tool that is running on another thread.
/// Clones share the flag.
#[derive(Clone, Debug, Default)]
pub struct Cancel(Arc<AtomicBool>);

impl Cancel {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn cancel(&self) {
        self.0.store(true, Ordering::SeqCst);
    }

    #[must_use]
    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }

    /// # Errors
    /// The flag is set.
    pub(crate) fn check(&self) -> Result<()> {
        if self.is_cancelled() {
            Err("the call was cancelled".into())
        } else {
            Ok(())
        }
    }
}

/// Lock a mutex whether or not a thread panicked holding it: the data under
/// every lock in this crate is plain state that stays consistent.
pub(crate) fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// The most the overflow file holds, in bytes, unless the caller says otherwise.
pub const DEFAULT_MAX_OVERFLOW: usize = 64 * 1024 * 1024;

/// Where the tool's output is limited, and where the overflow goes.
#[derive(Clone, Debug)]
pub struct Limits {
    pub max_bytes: usize,
    pub dir: PathBuf,
    /// The most bytes the overflow file holds; the rest is counted, not saved.
    pub max_overflow: usize,
}

impl Limits {
    /// `max_bytes` of kept text; the overflow goes to `<temp dir>/tau-<euid>`.
    ///
    /// # Errors
    /// A limit of zero keeps nothing and is refused.
    pub fn new(max_bytes: usize) -> Result<Self> {
        let dir = std::env::temp_dir().join(format!("tau-{}", nix::unistd::Uid::effective()));
        Self::in_dir(max_bytes, dir)
    }

    /// As [`Limits::new`], with the overflow directory chosen.
    ///
    /// # Errors
    /// A limit of zero keeps nothing and is refused; so is an existing directory that
    /// is a symlink, not ours, or open to others.
    pub fn in_dir(max_bytes: usize, dir: PathBuf) -> Result<Self> {
        if max_bytes == 0 {
            return Err("the output limit must be at least 1 byte".into());
        }
        limit::check_existing_dir(&dir)?;
        Ok(Self {
            max_bytes,
            dir,
            max_overflow: DEFAULT_MAX_OVERFLOW.max(max_bytes),
        })
    }

    /// As given, with the overflow file capped at `max_overflow` bytes.
    ///
    /// # Errors
    /// A cap below the output limit would save less than the model is shown.
    pub fn with_max_overflow(mut self, max_overflow: usize) -> Result<Self> {
        if max_overflow < self.max_bytes {
            return Err(format!(
                "the overflow cap ({max_overflow} bytes) must be at least the output limit ({} bytes)",
                self.max_bytes
            )
            .into());
        }
        self.max_overflow = max_overflow;
        Ok(self)
    }
}
