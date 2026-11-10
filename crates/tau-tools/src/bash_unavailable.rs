//! `bash` on a platform it does not run on (Windows): the same API as `bash.rs`, so
//! everything that names the tool builds, and [`Bash::new`] is the error, naming the
//! platform. No [`Bash`] is ever made, so nothing pretends to run a command.
//!
//! Which shell τ offers on Windows (Git Bash, a `powershell` tool, or none) is not
//! decided yet; see docs/plan.md, Phase 9.

use std::path::PathBuf;

use serde_json::Value;

use crate::{Limits, Result, ToolError};

/// Timeouts of the tool, in milliseconds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BashConfig {
    /// The timeout of a call that gives none.
    pub default_timeout_ms: u64,
    /// The longest timeout a call may ask for.
    pub max_timeout_ms: u64,
    /// Time between SIGTERM and SIGKILL.
    pub kill_grace_ms: u64,
}

/// Never made: [`Bash::new`] always fails here.
#[derive(Clone, Debug)]
pub struct Bash {
    _never: (),
}

fn unavailable() -> ToolError {
    ToolError(format!(
        "the bash tool is not available on {}: it runs on Linux and macOS",
        std::env::consts::OS
    ))
}

impl Bash {
    /// # Errors
    /// Always: the tool does not run on this platform.
    pub fn new(_cwd: PathBuf, _limits: Limits, _config: BashConfig) -> Result<Self> {
        Err(unavailable())
    }

    #[must_use]
    pub fn description(&self) -> String {
        unavailable().0
    }

    #[must_use]
    pub fn parameters() -> Value {
        Value::Null
    }

    #[must_use]
    pub fn start(&self, _args_raw: &str) -> BashRun {
        BashRun(unavailable())
    }
}

/// A command that was never started: its `wait` is the error.
#[derive(Debug)]
pub struct BashRun(ToolError);

impl BashRun {
    /// # Errors
    /// Always: the tool does not run on this platform.
    pub fn wait(&self) -> Result<String> {
        Err(self.0.clone())
    }

    /// # Errors
    /// Always: the tool does not run on this platform.
    pub fn wait_with(&self, _progress: Option<&(dyn Fn(String) -> bool + Sync)>) -> Result<String> {
        Err(self.0.clone())
    }

    /// Nothing runs, so there is nothing to kill.
    pub fn kill(&self) {}
}
