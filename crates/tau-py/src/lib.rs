//! Python bindings. Thin by rule: conversion and error mapping only. Behavior
//! lives in `tau-core`, where it can be tested without an interpreter.

mod calibration;
mod error;
mod job;
mod session;
mod tools;
mod trace;
mod values;
mod view;
mod wire;

use pyo3::prelude::*;

#[pymodule(gil_used = false)]
mod _core {
    #[pymodule_export]
    use super::calibration::{Calibration, CalibrationStore, calibration_features};
    #[pymodule_export]
    use super::job::{Answer, Draft, Ending, Hook, Job, RunCall, SampleEnd};
    #[pymodule_export]
    use super::session::{HandleWrites, Session, first_message, is_leased};
    #[pymodule_export]
    use super::tools::{Bash, BashRun, Cancel, Edit, Find, Grep, Read, Write};
    #[pymodule_export]
    use super::trace::{stop_trace, trace_clock, trace_to};
    #[pymodule_export]
    use super::values::{
        Author, Body, Config, Item, ItemData, Move, Next, Node, Outcome, Part, Replay, Stop,
    };
    #[pymodule_export]
    use super::view::{CursorState, Tree};
    #[pymodule_export]
    use super::wire::{wire_decode, wire_encode, wire_schema};

    #[pymodule_init]
    fn init(m: &pyo3::Bound<'_, pyo3::types::PyModule>) -> pyo3::PyResult<()> {
        #[cfg(feature = "transitions")]
        {
            use pyo3::types::PyModuleMethods;
            m.add_function(pyo3::wrap_pyfunction!(super::transitions_scenario, m)?)?;
        }
        {
            use pyo3::types::PyAnyMethods;
            let stop = pyo3::wrap_pyfunction!(super::tools::stop_progress, m)?;
            m.py().import("atexit")?.call_method1("register", (stop,))?;
        }
        super::error::add_all(m)
    }
}

/// Name the scenario the logged transitions belong to (diagram builds only).
#[cfg(feature = "transitions")]
#[pyfunction]
fn transitions_scenario(name: &str) {
    tau_core::transitions::scenario(name);
}
