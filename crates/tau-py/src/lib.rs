//! Python bindings. Thin by rule: conversion and error mapping only. Behavior
//! lives in `tau-core`, where it can be tested without an interpreter.

use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use tau_core::scaffold;

create_exception!(
    tau_next._core,
    ProbeError,
    PyValueError,
    "A probe failed validation."
);

/// Python view of [`scaffold::Probe`].
///
/// `frozen`: no interior mutability, so instances are shareable across
/// threads on free-threaded builds without locking, and hashable.
#[pyclass(frozen, eq, hash, module = "tau_next._core", name = "Probe")]
#[derive(Debug, PartialEq, Eq, Hash)]
struct PyProbe(scaffold::Probe);

#[pymethods]
impl PyProbe {
    #[new]
    #[pyo3(signature = (id, label, parts=Vec::new()))]
    fn new(id: u64, label: String, parts: Vec<String>) -> PyResult<Self> {
        scaffold::Probe::new(id, label, parts)
            .map(Self)
            .map_err(|e| ProbeError::new_err(e.to_string()))
    }

    #[getter]
    fn id(&self) -> u64 {
        self.0.id
    }

    #[getter]
    fn label(&self) -> &str {
        &self.0.label
    }

    #[getter]
    fn parts(&self) -> Vec<String> {
        self.0.parts.clone()
    }

    fn digest(&self) -> u64 {
        self.0.digest()
    }

    fn __repr__(&self) -> String {
        format!(
            "Probe(id={}, label={:?}, parts={:?})",
            self.0.id, self.0.label, self.0.parts
        )
    }
}

/// Copy a probe through Rust ownership and back: proves the round trip
/// allocates a new value rather than handing back the same object.
#[pyfunction]
fn echo(probe: &PyProbe) -> PyProbe {
    PyProbe(probe.0.clone())
}

#[pymodule(gil_used = false)]
mod _core {
    #[pymodule_export]
    use super::{PyProbe, echo};

    #[pymodule_init]
    fn init(m: &pyo3::Bound<'_, pyo3::types::PyModule>) -> pyo3::PyResult<()> {
        use pyo3::types::PyModuleMethods;
        m.add("ProbeError", m.py().get_type::<super::ProbeError>())
    }
}
