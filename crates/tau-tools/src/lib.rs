//! The base tools: `read`, `write`, `edit`, `grep`, `find`, `bash`.
//!
//! Filesystem and process IO, no `PyO3`, no async runtime. Every tool takes the
//! call's raw JSON arguments and answers `Ok(text)` or `Err(message)` for the
//! model; nothing panics or raises for an expected failure (bad arguments,
//! missing files, a command that exits nonzero).
//!
//! `edit` (its diff), `grep`, `find` and `bash` limit their output through [`limit::Sink`]:
//! past the limit the full output goes to an overflow file and the model gets the kept
//! text plus a note. `read` uses a sink that saves nothing (the file it reads is on disk).
//!
//! Platforms. Everything builds on Linux, macOS and Windows. `bash` runs on Linux and
//! macOS; elsewhere [`bash::Bash::new`] is an error naming the platform (`bash_unavailable.rs`).

#[cfg(any(target_os = "linux", target_os = "macos"))]
pub mod bash;
#[cfg(not(any(target_os = "linux", target_os = "macos")))]
#[path = "bash_unavailable.rs"]
pub mod bash;
pub(crate) mod diff;
pub mod edit;
pub mod find;
pub mod grep;
pub mod limit;
pub mod progress;
pub mod read;
#[cfg(any(target_os = "linux", target_os = "macos"))]
pub(crate) mod session;
pub(crate) mod walk;
pub mod write;

use std::fmt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use serde::de::DeserializeOwned;

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

/// Where the tool's output is limited, and where the overflow goes.
#[derive(Clone, Debug)]
pub struct Limits {
    pub max_bytes: usize,
    pub dir: PathBuf,
    /// The most bytes the overflow file holds; the rest is counted, not saved.
    pub max_overflow: usize,
}

impl Limits {
    /// `max_bytes` of kept text; the full output goes to files in `dir`, each
    /// holding at most `max_overflow` bytes. The directory is checked when a
    /// file is first needed, not here.
    ///
    /// # Errors
    /// A limit of zero keeps nothing; a cap below the limit would save less than
    /// the model is shown.
    pub fn new(max_bytes: usize, max_overflow: usize, dir: PathBuf) -> Result<Self> {
        if max_bytes == 0 {
            return Err("the output limit must be at least 1 byte".into());
        }
        if max_overflow < max_bytes {
            return Err(format!(
                "the overflow cap ({max_overflow} bytes) must be at least the output limit \
                 ({max_bytes} bytes)"
            )
            .into());
        }
        Ok(Self {
            max_bytes,
            dir,
            max_overflow,
        })
    }

    /// The overflow directory when none is chosen: `$XDG_RUNTIME_DIR/tau` (private
    /// to the user, cleared at logout) if that is set, else `<temp dir>/tau-<euid>`.
    /// On Windows `<temp dir>\tau`: the temp directory there is the user's own.
    #[must_use]
    pub fn default_dir() -> PathBuf {
        #[cfg(unix)]
        let dir = match std::env::var_os("XDG_RUNTIME_DIR").filter(|dir| !dir.is_empty()) {
            Some(runtime) => PathBuf::from(runtime).join("tau"),
            None => std::env::temp_dir().join(format!("tau-{}", nix::unistd::Uid::effective())),
        };
        #[cfg(windows)]
        let dir = std::env::temp_dir().join("tau");
        dir
    }

    /// A limit with a cap that no test reaches by accident.
    #[cfg(test)]
    pub(crate) fn in_dir(max_bytes: usize, dir: PathBuf) -> Result<Self> {
        Self::new(max_bytes, 64 * 1024 * 1024, dir)
    }
}
