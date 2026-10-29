//! Python bindings. Thin by rule: conversion and error mapping only. Behavior
//! lives in `tau-core`, where it can be tested without an interpreter.

mod error;
mod job;
mod session;
mod tools;
mod trace;
mod values;

use pyo3::prelude::*;

#[pymodule(gil_used = false)]
mod _core {
    #[pymodule_export]
    use super::job::{Draft, Ending, Fed, Hook, Job, RunCall};
    #[pymodule_export]
    use super::session::Session;
    #[pymodule_export]
    use super::tools::{Bash, BashRun, Cancel, Grep, Read, Write};
    #[pymodule_export]
    use super::trace::{stop_trace, trace_clock, trace_to};
    #[pymodule_export]
    use super::values::{
        Author, Body, Config, Item, ItemData, Move, Next, Node, Outcome, Part, Scratchpad, Stop,
    };

    #[pymodule_init]
    fn init(m: &pyo3::Bound<'_, pyo3::types::PyModule>) -> pyo3::PyResult<()> {
        super::error::add_all(m)
    }
}
