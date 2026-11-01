//! The wire's coding of the core values (D21 section 4): the log's own JSON encoding, exposed to
//! `tau._protocol` as text, so there is one encoding of a node in the system. A value is encoded
//! by its class; a decode names the class it wants. The schema is derived from the same types
//! (`tau-core`'s `schema` feature), not written by hand.

use pyo3::BoundObject;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use schemars::generate::SchemaSettings;
use serde::Serialize;
use serde::de::DeserializeOwned;
use tau_core::{step, tree};

use crate::job::Ending;
use crate::values::{Author, Body, Item, ItemData, Node};

/// The classes that cross the wire in this encoding: `encode` accepts instances of these,
/// `decode` is told which.
const KINDS: &[&str] = &["Author", "Body", "Ending", "Item", "ItemData", "Node"];

fn json<T: Serialize>(value: &T) -> PyResult<String> {
    serde_json::to_string(value).map_err(|e| PyValueError::new_err(e.to_string()))
}

fn read<T: DeserializeOwned>(kind: &str, text: &str) -> PyResult<T> {
    serde_json::from_str(text).map_err(|e| PyValueError::new_err(format!("not a {kind}: {e}")))
}

fn python<'py, T>(py: Python<'py>, value: T) -> PyResult<Bound<'py, PyAny>>
where
    T: IntoPyObject<'py>,
{
    value
        .into_pyobject(py)
        .map(|v| v.into_any().into_bound())
        .map_err(Into::into)
}

/// The JSON text of a core value: a `Node`, `Body`, `Author`, `Item`, `ItemData` or `Ending`.
/// `TypeError` for anything else.
#[pyfunction]
pub fn wire_encode(value: &Bound<'_, PyAny>) -> PyResult<String> {
    if let Ok(v) = value.extract::<PyRef<'_, Node>>() {
        return json(&tree::Node::from(v.clone()));
    }
    if let Ok(v) = value.extract::<PyRef<'_, Item>>() {
        return json(&tree::Item::from(v.clone()));
    }
    if let Ok(v) = value.extract::<Body>() {
        return json(&tree::Body::from(v));
    }
    if let Ok(v) = value.extract::<Author>() {
        return json(&tree::Author::from(v));
    }
    if let Ok(v) = value.extract::<ItemData>() {
        return json(&tree::ItemData::from(v));
    }
    if let Ok(v) = value.extract::<Ending>() {
        return json(&step::Ending::from(v));
    }
    Err(PyTypeError::new_err(format!(
        "{} is not one of {KINDS:?}",
        value.get_type().name()?
    )))
}

/// The value of class `kind` that `text` (from `wire_encode`) encodes. `ValueError` if `text`
/// is not one, `TypeError` for a `kind` that is not a class that is encoded.
#[pyfunction]
pub fn wire_decode<'py>(py: Python<'py>, kind: &str, text: &str) -> PyResult<Bound<'py, PyAny>> {
    match kind {
        "Node" => python(py, Node::from(read::<tree::Node>(kind, text)?)),
        "Item" => python(py, Item::from(read::<tree::Item>(kind, text)?)),
        "Body" => python(py, Body::from(read::<tree::Body>(kind, text)?)),
        "Author" => python(py, Author::from(read::<tree::Author>(kind, text)?)),
        "ItemData" => python(py, ItemData::from(read::<tree::ItemData>(kind, text)?)),
        "Ending" => python(py, Ending::from(read::<step::Ending>(kind, text)?)),
        _ => Err(PyTypeError::new_err(format!(
            "cannot decode {kind:?}; the classes are {KINDS:?}"
        ))),
    }
}

/// The JSON Schema `$defs` of the encoded classes and everything they hold, as JSON object text,
/// one definition per type name. Their references are `#/$defs/<name>`.
#[pyfunction]
pub fn wire_schema() -> PyResult<String> {
    let mut generator = SchemaSettings::draft2020_12().into_generator();
    generator.subschema_for::<tree::Node>();
    generator.subschema_for::<tree::Item>();
    generator.subschema_for::<tree::Body>();
    generator.subschema_for::<tree::Author>();
    generator.subschema_for::<tree::ItemData>();
    generator.subschema_for::<step::Ending>();
    json(&generator.take_definitions(true))
}
