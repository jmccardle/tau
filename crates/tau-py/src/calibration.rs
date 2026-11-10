//! The calibration functions and the calibration store (D26 section 6), for
//! the `tokens` built-in: conversion and error mapping only. The fit is
//! `tau_core::calibration`; the files are `tau_store::calibration`.

use std::collections::BTreeMap;
use std::path::PathBuf;

use pyo3::prelude::*;
use tau_core::calibration as core;
use tau_core::tree;
use tau_store::calibration as store;

use crate::error::{CalibrationError, CalibrationNewer, os_err};
use crate::values::Body;

/// The features of a node's body, as the fit counts it: for each role (system, user, assistant,
/// calls, results, thinking) its text's lowercase letters, uppercase letters, digits, other
/// ASCII, whitespace, bytes of non-ASCII characters and words, then how many nodes of each role
/// (1 for the body's own). 48 numbers; a span's features are the sum of its nodes'. A `Config`
/// or `Custom` body, which is never sent, is all zeros.
#[pyfunction]
pub fn calibration_features(body: Body) -> Vec<f64> {
    core::features(&tree::Body::from(body)).0
}

/// One model's calibration record: what the `tokens` built-in has learned of what its tokens
/// cost. A value: `CalibrationStore.observe` returns a new one.
#[pyclass(frozen, module = "tau._core")]
#[derive(Debug)]
pub struct Calibration {
    record: core::Record,
}

#[pymethods]
impl Calibration {
    /// The provider, base URL and model id the record is about.
    #[getter]
    fn key(&self) -> (String, Option<String>, String) {
        let key = &self.record.key;
        (
            key.provider.clone(),
            key.base_url.clone(),
            key.model.clone(),
        )
    }

    /// Replies observed.
    #[getter]
    fn observations(&self) -> u64 {
        self.record.observations
    }

    /// Proved spans learned from.
    #[getter]
    fn spans(&self) -> u64 {
        self.record.spans
    }

    /// Proved spans learned from since the record was last reset (a changed drift marker, a
    /// jump in what the model's tokens cost).
    #[getter]
    fn since_reset(&self) -> u32 {
        self.record.heuristic.since_reset
    }

    /// How many proved spans after a reset make the record ready.
    #[getter]
    #[allow(clippy::unused_self, clippy::cast_possible_truncation)]
    fn needed(&self) -> u32 {
        core::WARMUP + core::READY as u32
    }

    /// Whether the record answers a prediction.
    #[getter]
    fn ready(&self) -> bool {
        self.record.ready()
    }

    /// The relative error the bound is made from: the 95th percentile of the sizes of the
    /// record's last 200 residuals (their largest while there are fewer than 20). `None` while
    /// the record is not ready.
    #[getter]
    fn error(&self) -> Option<f64> {
        self.record.error()
    }

    /// The drift markers of the newest reply observed, by name.
    #[getter]
    fn markers(&self) -> BTreeMap<String, String> {
        self.record.markers.clone()
    }

    /// When the newest reply was observed, as it was given.
    #[getter]
    fn updated(&self) -> String {
        self.record.updated.clone()
    }

    /// The fitted tokens per unit of each class of text, for each role: 42 numbers, in the order
    /// of `calibration_features`.
    #[getter]
    fn weights(&self) -> Vec<f64> {
        self.record.heuristic.weights.clone()
    }

    /// The fitted tokens per node of each role: system, user, assistant, calls, results,
    /// thinking.
    #[getter]
    fn per_message(&self) -> Vec<u32> {
        self.record.heuristic.per_message.clone()
    }

    /// The count the record predicts for `features` (a node's, or a sum of nodes'), as
    /// `(tokens, bound)`: the estimate, and how many more it may be (D26 section 6.4). `None`
    /// while the record is not ready, or for features that hold no node.
    fn predict(&self, features: Vec<f64>) -> Option<(u64, u64)> {
        core::predict(&self.record, &core::Features(features)).map(|e| (e.tokens, e.bound))
    }

    /// The record as the JSON text its file holds.
    fn to_json(&self) -> String {
        serde_json::to_string(&self.record).expect("a record serializes")
    }
}

/// The calibration records under one directory (`$TAU_HOME/calibration`), one file per model.
/// Making one touches nothing: the directory is created by the first `observe`. A record's
/// `name` is a relative path ending in `.json` (`tau.Tokens` names them
/// `<provider>/<digest>.json`).
#[pyclass(frozen, module = "tau._core")]
#[derive(Debug)]
pub struct CalibrationStore {
    store: store::CalibrationStore,
}

type Span = (Vec<f64>, u64);
type Event = (String, String, String, String, f64);

#[pymethods]
impl CalibrationStore {
    #[new]
    fn new(directory: PathBuf) -> Self {
        Self {
            store: store::CalibrationStore::new(directory),
        }
    }

    /// The record named `name`, or `None` if there is none. `CalibrationNewer` for a record a
    /// newer tau wrote, `CalibrationError` for a file that is not a record this build reads or
    /// a name that is not a record's.
    fn read(&self, py: Python<'_>, name: &str) -> PyResult<Option<Calibration>> {
        let found = py.detach(|| self.store.read(name)).map_err(failed)?;
        Ok(found.map(|record| Calibration { record }))
    }

    /// Fold one reply into the record named `name` and return the record and what the fold
    /// found. The file is locked, read, folded into and replaced, so two processes that observe
    /// one model lose nothing of each other's. `key` is `(provider, base_url, model)`, for a
    /// record that does not exist yet; `markers` the reply's drift markers; `span` the reply's
    /// newest proved span as `(features, cost)`, or `None`; `time` when, as text the record
    /// keeps. Each event is `(kind, name, was, now, by)`: `("marker", name, was, now, 0.0)`, a
    /// drift marker that changed, or `("jump", "", "", "", by)`, a jump in what the model's
    /// tokens cost (`by` is the relative change); either resets the record. Raises as `read`
    /// does, leaving the file as it was.
    #[pyo3(signature = (name, key, markers, span, time))]
    fn observe(
        &self,
        py: Python<'_>,
        name: &str,
        key: (String, Option<String>, String),
        markers: BTreeMap<String, String>,
        span: Option<Span>,
        time: String,
    ) -> PyResult<(Calibration, Vec<Event>)> {
        let key = core::Key {
            provider: key.0,
            base_url: key.1,
            model: key.2,
        };
        let observation = core::Observation {
            markers,
            span: span.map(|(x, cost)| (core::Features(x), cost)),
            time,
        };
        let (record, events) = py
            .detach(|| self.store.observe(name, &key, &observation))
            .map_err(failed)?;
        let told = events
            .into_iter()
            .map(|event| match event {
                core::Event::Marker { name, was, now } => {
                    ("marker".to_owned(), name, was, now, 0.0)
                }
                core::Event::Jump { by } => {
                    let none = String::new;
                    ("jump".to_owned(), none(), none(), none(), by)
                }
            })
            .collect();
        Ok((Calibration { record }, told))
    }

    /// Remove the record named `name`, whatever it holds; whether there was one.
    fn remove(&self, py: Python<'_>, name: &str) -> PyResult<bool> {
        py.detach(|| self.store.remove(name)).map_err(failed)
    }

    /// The names of the records in the store, sorted; none if the directory does not exist.
    fn names(&self, py: Python<'_>) -> PyResult<Vec<String>> {
        py.detach(|| self.store.names()).map_err(failed)
    }
}

fn failed(e: store::Error) -> PyErr {
    match e {
        store::Error::Io(e) => os_err(&e, None),
        store::Error::Newer { .. } => CalibrationNewer::new_err(e.to_string()),
        store::Error::Damaged { .. } | store::Error::BadName(_) => {
            CalibrationError::new_err(e.to_string())
        }
    }
}
