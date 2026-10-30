//! The tree's value types as frozen Python classes. One class per enum
//! variant where variants carry different data, so `match` works in Python.
//! Each mirrors a `tau_core::tree` type; the `From` impls are the whole of the
//! conversion, in the directions a driver needs: core to Python for everything
//! it reads, Python to core for everything it builds.

use pyo3::prelude::*;
use pyo3::types::PyString;
use tau_core::tree;

/// `__repr__` is the Rust `Debug` text: mechanical, and readable in a failing
/// assert. (A class that already has a `#[pymethods]` block writes its own.)
macro_rules! debug_repr {
    ($($t:ty),+) => {$(
        #[pymethods]
        impl $t {
            fn __repr__(&self) -> String {
                format!("{self:?}")
            }
        }
    )+};
}
pub(crate) use debug_repr;

/// Python view of [`tree::Replay`]. Construction validates, so a `Part` never holds a bad one.
#[pyclass(frozen, eq, hash, from_py_object, module = "tau_next._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Replay {
    inner: tree::Replay,
}

#[pymethods]
impl Replay {
    #[new]
    fn new(provider: String, data: String) -> PyResult<Self> {
        let inner = tree::Replay::new(provider, data)
            .map_err(|e| crate::error::BadReplay::new_err(e.to_string()))?;
        Ok(Self { inner })
    }

    #[getter]
    fn provider(&self) -> &str {
        self.inner.provider()
    }

    #[getter]
    fn data(&self) -> &str {
        self.inner.data()
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        let quoted = |s: &str| PyString::new(py, s).repr().map(|r| r.to_string());
        Ok(format!(
            "Replay(provider={}, data={})",
            quoted(self.inner.provider())?,
            quoted(self.inner.data())?
        ))
    }
}

/// Python view of [`tree::Part`].
#[pyclass(frozen, eq, hash, from_py_object, module = "tau_next._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Part {
    #[pyo3(constructor = (text, replay = None))]
    Text {
        text: String,
        replay: Option<Replay>,
    },
    #[pyo3(constructor = (text, replay = None))]
    Thinking {
        text: String,
        replay: Option<Replay>,
    },
    #[pyo3(constructor = (id, tool, args_raw, replay = None))]
    Call {
        id: String,
        tool: String,
        args_raw: String,
        replay: Option<Replay>,
    },
}

impl From<tree::Part> for Part {
    fn from(p: tree::Part) -> Self {
        let own = |r: Option<tree::Replay>| r.map(|inner| Replay { inner });
        match p {
            tree::Part::Text { text, replay } => Self::Text {
                text,
                replay: own(replay),
            },
            tree::Part::Thinking { text, replay } => Self::Thinking {
                text,
                replay: own(replay),
            },
            tree::Part::Call {
                id,
                tool,
                args_raw,
                replay,
            } => Self::Call {
                id,
                tool,
                args_raw,
                replay: own(replay),
            },
        }
    }
}

impl From<Part> for tree::Part {
    fn from(p: Part) -> Self {
        let own = |r: Option<Replay>| r.map(|r| r.inner);
        match p {
            Part::Text { text, replay } => Self::Text {
                text,
                replay: own(replay),
            },
            Part::Thinking { text, replay } => Self::Thinking {
                text,
                replay: own(replay),
            },
            Part::Call {
                id,
                tool,
                args_raw,
                replay,
            } => Self::Call {
                id,
                tool,
                args_raw,
                replay: own(replay),
            },
        }
    }
}

/// Python view of [`tree::Stop`].
#[pyclass(frozen, eq, hash, from_py_object, module = "tau_next._core")]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Stop {
    End,
    Tools,
    Length,
    Refused,
    Interrupted,
    Error,
}

impl From<tree::Stop> for Stop {
    fn from(s: tree::Stop) -> Self {
        match s {
            tree::Stop::End => Self::End,
            tree::Stop::Tools => Self::Tools,
            tree::Stop::Length => Self::Length,
            tree::Stop::Refused => Self::Refused,
            tree::Stop::Interrupted => Self::Interrupted,
            tree::Stop::Error => Self::Error,
        }
    }
}

impl From<Stop> for tree::Stop {
    fn from(s: Stop) -> Self {
        match s {
            Stop::End => Self::End,
            Stop::Tools => Self::Tools,
            Stop::Length => Self::Length,
            Stop::Refused => Self::Refused,
            Stop::Interrupted => Self::Interrupted,
            Stop::Error => Self::Error,
        }
    }
}

/// Python view of [`tree::Outcome`].
#[pyclass(frozen, eq, hash, from_py_object, module = "tau_next._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Outcome {
    Ok { value: String },
    Err { error: String },
    Interrupted {},
}

impl From<tree::Outcome> for Outcome {
    fn from(o: tree::Outcome) -> Self {
        match o {
            tree::Outcome::Ok(value) => Self::Ok { value },
            tree::Outcome::Err(error) => Self::Err { error },
            tree::Outcome::Interrupted => Self::Interrupted {},
        }
    }
}

impl From<Outcome> for tree::Outcome {
    fn from(o: Outcome) -> Self {
        match o {
            Outcome::Ok { value } => Self::Ok(value),
            Outcome::Err { error } => Self::Err(error),
            Outcome::Interrupted {} => Self::Interrupted,
        }
    }
}

/// Python view of [`tree::Author`].
#[pyclass(frozen, eq, hash, from_py_object, module = "tau_next._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Author {
    Frontend { client: String },
    Provider {},
    Tools {},
    Core {},
    Extension { name: String },
}

impl From<tree::Author> for Author {
    fn from(a: tree::Author) -> Self {
        match a {
            tree::Author::Frontend(client) => Self::Frontend { client },
            tree::Author::Provider => Self::Provider {},
            tree::Author::Tools => Self::Tools {},
            tree::Author::Core => Self::Core {},
            tree::Author::Extension(name) => Self::Extension { name },
        }
    }
}

impl From<Author> for tree::Author {
    fn from(a: Author) -> Self {
        match a {
            Author::Frontend { client } => Self::Frontend(client),
            Author::Provider {} => Self::Provider,
            Author::Tools {} => Self::Tools,
            Author::Core {} => Self::Core,
            Author::Extension { name } => Self::Extension(name),
        }
    }
}

/// Python view of [`tree::Config`].
#[pyclass(frozen, eq, hash, get_all, from_py_object, module = "tau_next._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Config {
    data: String,
}

#[pymethods]
impl Config {
    #[new]
    fn new(data: String) -> Self {
        Self { data }
    }

    fn __repr__(&self) -> String {
        format!("{self:?}")
    }
}

impl From<tree::Config> for Config {
    fn from(c: tree::Config) -> Self {
        Self { data: c.data }
    }
}

impl From<Config> for tree::Config {
    fn from(c: Config) -> Self {
        Self { data: c.data }
    }
}

/// Python view of [`tree::Body`].
#[pyclass(frozen, eq, hash, from_py_object, module = "tau_next._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Body {
    System {
        text: String,
    },
    User {
        text: String,
    },
    #[pyo3(constructor = (parts, stop, finish_reason = None, meta = None))]
    Assistant {
        parts: Vec<Part>,
        stop: Stop,
        finish_reason: Option<String>,
        meta: Option<String>,
    },
    Results {
        outcomes: Vec<(String, Outcome, Author)>,
    },
    Config {
        config: Config,
    },
    Custom {
        kind: String,
        data: String,
    },
}

impl From<tree::Body> for Body {
    fn from(b: tree::Body) -> Self {
        match b {
            tree::Body::System(text) => Self::System { text },
            tree::Body::User(text) => Self::User { text },
            tree::Body::Assistant {
                parts,
                stop,
                finish_reason,
                meta,
            } => Self::Assistant {
                parts: parts.into_iter().map(Into::into).collect(),
                stop: stop.into(),
                finish_reason,
                meta,
            },
            tree::Body::Results(outcomes) => Self::Results {
                outcomes: outcomes
                    .into_iter()
                    .map(|(c, o, by)| (c, o.into(), by.into()))
                    .collect(),
            },
            tree::Body::Config(config) => Self::Config {
                config: config.into(),
            },
            tree::Body::Custom { kind, data } => Self::Custom { kind, data },
        }
    }
}

impl From<Body> for tree::Body {
    fn from(b: Body) -> Self {
        match b {
            Body::System { text } => Self::System(text),
            Body::User { text } => Self::User(text),
            Body::Assistant {
                parts,
                stop,
                finish_reason,
                meta,
            } => Self::Assistant {
                parts: parts.into_iter().map(Into::into).collect(),
                stop: stop.into(),
                finish_reason,
                meta,
            },
            Body::Results { outcomes } => Self::Results(
                outcomes
                    .into_iter()
                    .map(|(c, o, by)| (c, o.into(), by.into()))
                    .collect(),
            ),
            Body::Config { config } => Self::Config(config.into()),
            Body::Custom { kind, data } => Self::Custom { kind, data },
        }
    }
}

/// Python view of [`tree::Node`].
#[pyclass(frozen, eq, hash, get_all, module = "tau_next._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Node {
    id: u64,
    parent: Option<u64>,
    time: u64,
    by: Author,
    body: Body,
    delivers: Vec<u64>,
}

impl From<tree::Node> for Node {
    fn from(n: tree::Node) -> Self {
        Self {
            id: n.id,
            parent: n.parent,
            time: n.time,
            by: n.by.into(),
            body: n.body.into(),
            delivers: n.delivers,
        }
    }
}

/// Python view of [`tree::Move`]. `from` is a Python keyword, so `from_`.
#[pyclass(frozen, eq, hash, module = "tau_next._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Move {
    #[pyo3(get)]
    cursor: u64,
    #[pyo3(get, name = "from_")]
    from: Option<u64>,
    #[pyo3(get)]
    to: u64,
    #[pyo3(get)]
    by: Author,
    #[pyo3(get)]
    time: u64,
}

impl From<tree::Move> for Move {
    fn from(m: tree::Move) -> Self {
        Self {
            cursor: m.cursor,
            from: m.from,
            to: m.to,
            by: m.by.into(),
            time: m.time,
        }
    }
}

/// Python view of [`tree::ItemData`].
#[pyclass(frozen, eq, hash, from_py_object, module = "tau_next._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum ItemData {
    Input { client: String, text: String },
    Extension { name: String, data: String },
}

impl From<tree::ItemData> for ItemData {
    fn from(d: tree::ItemData) -> Self {
        match d {
            tree::ItemData::Input { client, text } => Self::Input { client, text },
            tree::ItemData::Extension { name, data } => Self::Extension { name, data },
        }
    }
}

impl From<ItemData> for tree::ItemData {
    fn from(d: ItemData) -> Self {
        match d {
            ItemData::Input { client, text } => Self::Input { client, text },
            ItemData::Extension { name, data } => Self::Extension { name, data },
        }
    }
}

/// Python view of [`tree::Item`].
#[pyclass(frozen, eq, hash, get_all, module = "tau_next._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Item {
    id: u64,
    to: u64,
    data: ItemData,
}

impl From<tree::Item> for Item {
    fn from(i: tree::Item) -> Self {
        Self {
            id: i.id,
            to: i.to,
            data: i.data.into(),
        }
    }
}

/// Python view of [`tree::Next`].
#[pyclass(frozen, eq, hash, module = "tau_next._core")]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Next {
    Wait,
    Sample,
    RunTools,
}

impl From<tree::Next> for Next {
    fn from(n: tree::Next) -> Self {
        match n {
            tree::Next::Wait => Self::Wait,
            tree::Next::Sample => Self::Sample,
            tree::Next::RunTools => Self::RunTools,
        }
    }
}

debug_repr!(Part, Outcome, Author, Body, Node, Move, ItemData, Item);
