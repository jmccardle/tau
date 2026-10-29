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
pub mod write;

use std::fmt;
use std::path::{Path, PathBuf};

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

/// Where the tool's output is limited, and where the overflow goes.
#[derive(Clone, Debug)]
pub struct Limits {
    pub max_bytes: usize,
    pub dir: PathBuf,
}

impl Limits {
    /// `max_bytes` of kept text; the overflow goes to `<temp dir>/tau`.
    ///
    /// # Errors
    /// A limit of zero keeps nothing and is refused.
    pub fn new(max_bytes: usize) -> Result<Self> {
        Self::in_dir(max_bytes, std::env::temp_dir().join("tau"))
    }

    /// As [`Limits::new`], with the overflow directory chosen.
    ///
    /// # Errors
    /// A limit of zero keeps nothing and is refused.
    pub fn in_dir(max_bytes: usize, dir: PathBuf) -> Result<Self> {
        if max_bytes == 0 {
            return Err("the output limit must be at least 1 byte".into());
        }
        Ok(Self { max_bytes, dir })
    }
}
