//! The base tools of `tau-tools`, as Python classes. Conversion only: each
//! `run`/`wait` releases the GIL for the whole of the work and answers an
//! `Outcome`. The `Tool` protocol's `async call` is Python's, in
//! `tau_next.base_tools`, which runs these off the event loop.

// Properties are methods in Python, whether or not they read `self`.
#![allow(clippy::unused_self)]

use std::path::PathBuf;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use tau_tools::bash::{Bash as BashTool, BashRun as Run};
use tau_tools::grep::Grep as GrepTool;
use tau_tools::read::Read as ReadTool;
use tau_tools::write::Write as WriteTool;
use tau_tools::{DEFAULT_MAX_OUTPUT, Limits, ToolError};

use crate::values::Outcome;

fn outcome(result: Result<String, ToolError>) -> Outcome {
    match result {
        Ok(value) => Outcome::Ok { value },
        Err(ToolError(error)) => Outcome::Err { error },
    }
}

fn limits(max_output: usize, overflow_dir: Option<PathBuf>) -> PyResult<Limits> {
    let made = match overflow_dir {
        Some(dir) => Limits::in_dir(max_output, dir),
        None => Limits::new(max_output),
    };
    made.map_err(|e| PyValueError::new_err(e.0))
}

fn parameters_json(schema: &serde_json::Value) -> String {
    schema.to_string()
}

macro_rules! text_tool {
    ($(#[$doc:meta])* $class:ident, $inner:ty, $name:literal, $schema:expr, { $($methods:tt)* }) => {
        $(#[$doc])*
        #[pyclass(frozen, module = "tau_next._core")]
        #[derive(Debug)]
        pub struct $class {
            inner: $inner,
        }

        #[pymethods]
        impl $class {
            #[getter]
            fn name(&self) -> &'static str {
                $name
            }

            #[getter]
            fn parameters_json(&self) -> String {
                parameters_json(&$schema)
            }

            /// Run the call; releases the GIL.
            fn run(&self, py: Python<'_>, args_raw: &str) -> Outcome {
                outcome(py.detach(|| self.inner.run(args_raw)))
            }

            $($methods)*
        }
    };
}

text_tool!(
    /// The `read` tool.
    Read, ReadTool, "read", ReadTool::parameters(), {
        #[new]
        #[pyo3(signature = (cwd, max_output=DEFAULT_MAX_OUTPUT, overflow_dir=None))]
        fn new(cwd: PathBuf, max_output: usize, overflow_dir: Option<PathBuf>) -> PyResult<Self> {
            Ok(Self { inner: ReadTool::new(cwd, limits(max_output, overflow_dir)?) })
        }

        #[getter]
        fn description(&self) -> String {
            self.inner.description()
        }
    }
);
text_tool!(
    /// The `write` tool.
    Write, WriteTool, "write", WriteTool::parameters(), {
        #[new]
        fn new(cwd: PathBuf) -> Self {
            Self { inner: WriteTool::new(cwd) }
        }

        #[getter]
        fn description(&self) -> String {
            WriteTool::description()
        }
    }
);
text_tool!(
    /// The `grep` tool.
    Grep, GrepTool, "grep", GrepTool::parameters(), {
        #[new]
        #[pyo3(signature = (cwd, max_output=DEFAULT_MAX_OUTPUT, overflow_dir=None))]
        fn new(cwd: PathBuf, max_output: usize, overflow_dir: Option<PathBuf>) -> PyResult<Self> {
            Ok(Self { inner: GrepTool::new(cwd, limits(max_output, overflow_dir)?) })
        }

        #[getter]
        fn description(&self) -> String {
            self.inner.description()
        }
    }
);

/// The `bash` tool.
#[pyclass(frozen, module = "tau_next._core")]
#[derive(Debug)]
pub struct Bash {
    inner: BashTool,
}

#[pymethods]
impl Bash {
    #[new]
    #[pyo3(signature = (cwd, max_output=DEFAULT_MAX_OUTPUT, overflow_dir=None, timeout_ms=tau_tools::bash::DEFAULT_TIMEOUT_MS))]
    fn new(
        cwd: PathBuf,
        max_output: usize,
        overflow_dir: Option<PathBuf>,
        timeout_ms: u64,
    ) -> PyResult<Self> {
        let inner = BashTool::new(cwd, limits(max_output, overflow_dir)?, timeout_ms)
            .map_err(|e| PyValueError::new_err(e.0))?;
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
    fn start(&self, args_raw: &str) -> BashRun {
        BashRun {
            inner: self.inner.start(args_raw),
        }
    }
}

/// A started command, killable from any thread.
#[pyclass(frozen, module = "tau_next._core")]
#[derive(Debug)]
pub struct BashRun {
    inner: Run,
}

#[pymethods]
impl BashRun {
    /// Block, GIL released, until the command ends, is killed, or times out.
    fn wait(&self, py: Python<'_>) -> Outcome {
        outcome(py.detach(|| self.inner.wait()))
    }

    /// Kill the command and its process group. Safe at any time, any number of times.
    fn kill(&self, py: Python<'_>) {
        py.detach(|| self.inner.kill());
    }
}
