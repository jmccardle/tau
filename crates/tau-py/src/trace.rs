//! Tracing: one global subscriber, installed the first time a trace starts,
//! that writes a Chrome trace file while a trace is running and filters every
//! span out the rest of the time. The spans themselves live in the crates they
//! measure; this module only chooses where they go.

use std::fs::File;
use std::io::{self, Write};
use std::path::PathBuf;
use std::sync::mpsc::{Receiver, Sender, channel};
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant};

use pyo3::create_exception;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use tracing_chrome::{ChromeLayerBuilder, FlushGuard, TraceStyle};
use tracing_subscriber::filter::LevelFilter;
use tracing_subscriber::prelude::*;
use tracing_subscriber::{Registry, reload};

/// How long `stop_trace` waits for the writer thread to finish the file.
const FINISH: Duration = Duration::from_secs(10);

create_exception!(
    tau_next._core,
    TraceError,
    PyRuntimeError,
    "A trace started while one runs, stopped while none runs, or that cannot be installed."
);

/// A file that says so when the writer thread is done with it and drops it.
struct Finished<W: Write> {
    inner: W,
    done: Sender<()>,
}

impl<W: Write> Write for Finished<W> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.inner.write(buf)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

impl<W: Write> Drop for Finished<W> {
    fn drop(&mut self) {
        // The receiver is gone only if `stop_trace` gave up waiting.
        let _ = self.done.send(());
    }
}

struct Tracer {
    guard: FlushGuard,
    filter: reload::Handle<LevelFilter, Registry>,
    /// Zero of `trace_clock`: taken right after the layer's own zero.
    origin: Instant,
    /// `Some` while a trace runs: the signal that its file is complete.
    running: Option<(PathBuf, Receiver<()>)>,
}

static TRACER: Mutex<Option<Tracer>> = Mutex::new(None);

fn tracer() -> Result<MutexGuard<'static, Option<Tracer>>, PyErr> {
    TRACER
        .lock()
        .map_err(|_| TraceError::new_err("the tracer panicked"))
}

fn install() -> Result<Tracer, PyErr> {
    let (chrome, guard) = ChromeLayerBuilder::new()
        .writer(io::sink())
        .include_args(true)
        .include_locations(false)
        .trace_style(TraceStyle::Threaded)
        .build();
    let origin = Instant::now();
    let (filter, handle) = reload::Layer::new(LevelFilter::OFF);
    tracing::subscriber::set_global_default(Registry::default().with(chrome.with_filter(filter)))
        .map_err(|e| TraceError::new_err(format!("another tracing subscriber is installed: {e}")))?;
    Ok(Tracer {
        guard,
        filter: handle,
        origin,
        running: None,
    })
}

/// Start recording spans to a Chrome trace file at `path`.
#[pyfunction]
pub fn trace_to(path: PathBuf) -> PyResult<()> {
    let mut slot = tracer()?;
    if slot.is_none() {
        *slot = Some(install()?);
    }
    let t = slot.as_mut().expect("installed above");
    if let Some((running, _)) = &t.running {
        return Err(TraceError::new_err(format!(
            "already tracing to {}",
            running.display()
        )));
    }
    let (done, finished) = channel();
    let file = File::create(&path)?;
    t.guard
        .start_new(Some(Box::new(Finished { inner: file, done })));
    t.filter
        .modify(|f| *f = LevelFilter::INFO)
        .map_err(|e| TraceError::new_err(e.to_string()))?;
    t.running = Some((path, finished));
    Ok(())
}

/// Stop recording and return once the trace file is complete.
#[pyfunction]
pub fn stop_trace(py: Python<'_>) -> PyResult<()> {
    let mut slot = tracer()?;
    let t = slot
        .as_mut()
        .filter(|t| t.running.is_some())
        .ok_or_else(|| TraceError::new_err("no trace is running"))?;
    t.filter
        .modify(|f| *f = LevelFilter::OFF)
        .map_err(|e| TraceError::new_err(e.to_string()))?;
    let (_, finished) = t.running.take().expect("checked above");
    // Closing the current file is starting the next one, which we do not use.
    let (done, _unused) = channel();
    t.guard.start_new(Some(Box::new(Finished {
        inner: io::sink(),
        done,
    })));
    py.detach(move || finished.recv_timeout(FINISH))
        .map_err(|_| TraceError::new_err("the trace file was not finished"))
}

/// Microseconds since the first `trace_to`, on the clock the spans use.
#[pyfunction]
pub fn trace_clock() -> PyResult<f64> {
    let slot = tracer()?;
    let t = slot
        .as_ref()
        .ok_or_else(|| TraceError::new_err("no trace has been started"))?;
    Ok(t.origin.elapsed().as_secs_f64() * 1e6)
}
