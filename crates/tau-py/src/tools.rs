//! The base tools of `tau-tools`, as Python classes. Conversion only: each
//! `run`/`wait` releases the GIL for the whole of the work and answers an
//! `Outcome`. The `Tool` protocol's `async call` is Python's, in
//! `tau.base_tools`, which runs these off the event loop.

// Properties are methods in Python, whether or not they read `self`.
#![allow(clippy::unused_self)]

use std::path::PathBuf;
use std::sync::{PoisonError, RwLock};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use tau_tools::bash::{Bash as BashTool, BashConfig, BashRun as Run};
use tau_tools::grep::Grep as GrepTool;
use tau_tools::read::Read as ReadTool;
use tau_tools::write::Write as WriteTool;
use tau_tools::{Cancel as Flag, Limits, ToolError};

use crate::values::Outcome;

/// Whether a progress emitter may still attach to the interpreter. Under the stable ABI
/// (`abi3-py312`) `PyO3` cannot ask Python whether it is finalizing, and attaching then is
/// undefined behavior on 3.12 and 3.13; so `stop_progress` is registered with `atexit`, which
/// runs before finalization begins, and no emitter attaches after it.
static EMITTING: RwLock<bool> = RwLock::new(true);

/// Stop every progress emitter from attaching again (registered with `atexit`). Releases the
/// GIL while it waits for an emitter that holds the flag to finish its call.
#[pyfunction]
pub fn stop_progress(py: Python<'_>) {
    py.detach(|| *EMITTING.write().unwrap_or_else(PoisonError::into_inner) = false);
}

fn outcome(result: Result<String, ToolError>) -> Outcome {
    match result {
        Ok(value) => Outcome::Ok { value },
        Err(ToolError(error)) => Outcome::Err { error },
    }
}

fn value_error(e: ToolError) -> PyErr {
    PyValueError::new_err(e.0)
}

fn limits(
    max_output: usize,
    overflow_dir: Option<PathBuf>,
    max_overflow: usize,
) -> PyResult<Limits> {
    let dir = overflow_dir.unwrap_or_else(Limits::default_dir);
    Limits::new(max_output, max_overflow, dir).map_err(value_error)
}

fn parameters_json(schema: &serde_json::Value) -> String {
    schema.to_string()
}

/// A flag that stops a running `read` or `grep`.
#[pyclass(frozen, module = "tau._core")]
#[derive(Debug)]
pub struct Cancel {
    flag: Flag,
}

#[pymethods]
impl Cancel {
    #[new]
    fn new() -> Self {
        Self { flag: Flag::new() }
    }

    /// Set the flag; the call it was given to stops with an `Err`.
    fn cancel(&self) {
        self.flag.cancel();
    }
}

/// The `read` tool.
#[pyclass(frozen, module = "tau._core")]
#[derive(Debug)]
pub struct Read {
    inner: ReadTool,
}

#[pymethods]
impl Read {
    #[new]
    fn new(cwd: PathBuf, max_output: usize) -> PyResult<Self> {
        Ok(Self {
            inner: ReadTool::new(cwd, max_output).map_err(value_error)?,
        })
    }

    #[getter]
    fn name(&self) -> &'static str {
        "read"
    }

    #[getter]
    fn description(&self) -> String {
        self.inner.description()
    }

    #[getter]
    fn parameters_json(&self) -> String {
        parameters_json(&ReadTool::parameters())
    }

    /// Run the call; releases the GIL.
    fn run(&self, py: Python<'_>, args_raw: &str, cancel: &Cancel) -> Outcome {
        outcome(py.detach(|| self.inner.run(args_raw, &cancel.flag)))
    }
}

/// The `write` tool.
#[pyclass(frozen, module = "tau._core")]
#[derive(Debug)]
pub struct Write {
    inner: WriteTool,
}

#[pymethods]
impl Write {
    #[new]
    fn new(cwd: PathBuf) -> Self {
        Self {
            inner: WriteTool::new(cwd),
        }
    }

    #[getter]
    fn name(&self) -> &'static str {
        "write"
    }

    #[getter]
    fn description(&self) -> String {
        WriteTool::description()
    }

    #[getter]
    fn parameters_json(&self) -> String {
        parameters_json(&WriteTool::parameters())
    }

    /// Run the call; releases the GIL.
    fn run(&self, py: Python<'_>, args_raw: &str) -> Outcome {
        outcome(py.detach(|| self.inner.run(args_raw)))
    }
}

/// The `grep` tool.
#[pyclass(frozen, module = "tau._core")]
#[derive(Debug)]
pub struct Grep {
    inner: GrepTool,
}

#[pymethods]
impl Grep {
    #[new]
    fn new(
        cwd: PathBuf,
        max_output: usize,
        overflow_dir: Option<PathBuf>,
        max_overflow: usize,
    ) -> PyResult<Self> {
        let limits = limits(max_output, overflow_dir, max_overflow)?;
        Ok(Self {
            inner: GrepTool::new(cwd, limits),
        })
    }

    #[getter]
    fn name(&self) -> &'static str {
        "grep"
    }

    #[getter]
    fn description(&self) -> String {
        self.inner.description()
    }

    #[getter]
    fn parameters_json(&self) -> String {
        parameters_json(&GrepTool::parameters())
    }

    /// Run the call; releases the GIL.
    fn run(&self, py: Python<'_>, args_raw: &str, cancel: &Cancel) -> Outcome {
        outcome(py.detach(|| self.inner.run(args_raw, &cancel.flag)))
    }
}

/// The `bash` tool.
#[pyclass(frozen, module = "tau._core")]
#[derive(Debug)]
pub struct Bash {
    inner: BashTool,
}

#[pymethods]
impl Bash {
    #[new]
    fn new(
        cwd: PathBuf,
        max_output: usize,
        overflow_dir: Option<PathBuf>,
        max_overflow: usize,
        timeout_ms: u64,
        max_timeout_ms: u64,
        kill_grace_ms: u64,
    ) -> PyResult<Self> {
        let limits = limits(max_output, overflow_dir, max_overflow)?;
        let config = BashConfig {
            default_timeout_ms: timeout_ms,
            max_timeout_ms,
            kill_grace_ms,
        };
        let inner = BashTool::new(cwd, limits, config).map_err(value_error)?;
        Ok(Self { inner })
    }

    #[getter]
    fn name(&self) -> &'static str {
        "bash"
    }

    #[getter]
    fn description(&self) -> String {
        self.inner.description()
    }

    #[getter]
    fn parameters_json(&self) -> String {
        parameters_json(&BashTool::parameters())
    }

    /// Start the command. Never raises: a bad call is a `BashRun` whose `wait` answers `Err`.
    fn start(&self, py: Python<'_>, args_raw: &str) -> BashRun {
        BashRun {
            inner: py.detach(|| self.inner.start(args_raw)),
        }
    }
}

/// A started command, killable from any thread.
#[pyclass(frozen, module = "tau._core")]
#[derive(Debug)]
pub struct BashRun {
    inner: Run,
}

#[pymethods]
impl BashRun {
    /// Block, GIL released, until the command ends, is killed, or times out.
    ///
    /// With `progress`, a callable of one `str`, the output is also passed to it as it
    /// arrives, coalesced to at most one call per 100 ms, from a thread of its own (so the
    /// callable must be safe to call from any thread). The GIL is taken only for each call,
    /// never in the loop that reads the command's output. A slow `progress` never slows the
    /// command, but `wait` returns only after the call running and the final flush, so it
    /// delays the answer. Calls stop for good once the interpreter begins to exit.
    #[pyo3(signature = (progress=None))]
    fn wait(&self, py: Python<'_>, progress: Option<Py<PyAny>>) -> Outcome {
        let Some(progress) = progress else {
            return outcome(py.detach(|| self.inner.wait()));
        };
        let emit = |text: String| -> bool {
            // Held across the attach: `stop_progress` waits for the writers to finish.
            let emitting = EMITTING.read().unwrap_or_else(PoisonError::into_inner);
            if !*emitting {
                return false;
            }
            Python::try_attach(|py| {
                if let Err(e) = progress.call1(py, (text,)) {
                    e.write_unraisable(py, None);
                }
            })
            .is_some()
        };
        outcome(py.detach(|| self.inner.wait_with(Some(&emit))))
    }

    /// Ask for the command and its process group to end (SIGTERM, then SIGKILL
    /// after the grace period). Does not block; safe at any time, any number of times.
    fn kill(&self) {
        self.inner.kill();
    }
}
