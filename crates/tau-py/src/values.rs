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

/// Data only the provider that wrote a part can send back (a thinking signature, for one).
/// `provider` names that provider; `data` is the text of a JSON object, kept exactly as given.
/// Construction raises `BadReplay` for an empty or blank provider name, or `data` that is not
/// a strict JSON object, so a `Part` never holds a bad one.
#[pyclass(frozen, eq, hash, from_py_object, module = "tau._core")]
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

    /// The name of the provider that wrote the part.
    #[getter]
    fn provider(&self) -> &str {
        self.inner.provider()
    }

    /// The text of the JSON object, exactly as the provider gave it.
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

/// One piece of a reply, in the order the provider streamed it: `Text`, `Thinking` or `Call`.
/// Each may carry a `Replay`.
#[pyclass(frozen, eq, hash, from_py_object, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Part {
    /// Words for the reader.
    #[pyo3(constructor = (text, replay = None))]
    Text {
        /// The words.
        text: String,
        /// Data only the writing provider can send back, or `None`.
        replay: Option<Replay>,
    },
    /// The model's reasoning. `text` may be empty when `replay` holds a redacted block.
    #[pyo3(constructor = (text, replay = None))]
    Thinking {
        /// The reasoning, possibly empty.
        text: String,
        /// Data only the writing provider can send back, or `None`.
        replay: Option<Replay>,
    },
    /// A request to run one tool. A call is never a node of its own.
    #[pyo3(constructor = (id, tool, args_raw, replay = None))]
    Call {
        /// The provider's id for the call, unique within its node.
        id: String,
        /// The name of the tool to run.
        tool: String,
        /// The arguments exactly as the model produced them, never re-serialised.
        args_raw: String,
        /// Data only the writing provider can send back, or `None`.
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

/// Why a reply ended. Only `Tools` makes its calls runnable. It is not an integer:
/// `Stop.Tools != 1`.
#[pyclass(frozen, eq, hash, from_py_object, module = "tau._core")]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Stop {
    /// The reply is complete (a refusal is an `End` with the provider's reason kept).
    End,
    /// The reply asks for tool calls.
    Tools,
    /// The reply hit a length limit.
    Length,
    /// An abort cut the reply off.
    Interrupted,
    /// The sample failed after producing output.
    Error,
}

impl From<tree::Stop> for Stop {
    fn from(s: tree::Stop) -> Self {
        match s {
            tree::Stop::End => Self::End,
            tree::Stop::Tools => Self::Tools,
            tree::Stop::Length => Self::Length,
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
            Stop::Interrupted => Self::Interrupted,
            Stop::Error => Self::Error,
        }
    }
}

/// How one tool call ended: `Ok`, `Err` or `Interrupted`.
#[pyclass(frozen, eq, hash, from_py_object, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Outcome {
    /// The call succeeded.
    Ok {
        /// What the tool returned.
        value: String,
    },
    /// The call failed.
    Err {
        /// Why, as the model will read it.
        error: String,
    },
    /// An abort stopped the call or kept it from starting; the tool may or may not have run.
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

/// Who wrote a node, or asked for a move. It is recorded and never used to decide anything.
#[pyclass(frozen, eq, hash, from_py_object, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Author {
    /// A frontend, named by its client name.
    Frontend {
        /// The client name the frontend gave.
        client: String,
    },
    /// The model's provider.
    Provider {},
    /// The tools that ran a call.
    Tools {},
    /// The core itself.
    Core {},
    /// An extension.
    Extension {
        /// The extension's name.
        name: String,
    },
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

/// How a step runs from this node on: the text of a JSON object with at most two keys, `"model"`
/// and `"extensions"` (the options). The constructor does not check the text, and core stores it
/// without reading it.
#[pyclass(frozen, eq, hash, get_all, from_py_object, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Config {
    /// The JSON object text, as given.
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

/// What a node holds. `System`, `User`, `Assistant` and `Results` are what the model reads;
/// `Config` and `Custom` never reach it.
#[pyclass(frozen, eq, hash, from_py_object, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Body {
    /// The system text a session starts with.
    System {
        /// The system text.
        text: String,
    },
    /// A message from the person (or an extension) using the session.
    User {
        /// The message.
        text: String,
    },
    /// The model's reply.
    #[pyo3(constructor = (parts, stop, finish_reason = None, meta = None))]
    Assistant {
        /// The reply's pieces, in stream order.
        parts: Vec<Part>,
        /// Why the reply ended.
        stop: Stop,
        /// The provider's own finish string, or `None`.
        finish_reason: Option<String>,
        /// What the response carried besides its parts (usage, for one), as JSON object text,
        /// or `None`.
        meta: Option<String>,
    },
    /// The answer to a batch of calls: one `(call id, outcome, author)` per call, in call order.
    Results {
        /// The outcomes, one per call.
        outcomes: Vec<(String, Outcome, Author)>,
    },
    /// A change of how steps run from here on.
    Config {
        /// The new configuration.
        config: Config,
    },
    /// A record an extension (or core) keeps in the tree that the model never reads.
    Custom {
        /// What kind of record this is; core writes `sample_error`.
        kind: String,
        /// The record's data, as text.
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

/// One record of a session's tree, written once and never changed: its `id`, its `parent`
/// (`None` for a root), the `time` the driver gave it in milliseconds, who wrote it (`by`), what
/// it holds (`body`), the scratchpad items it consumed (`delivers`), and where its content came
/// from (`copied_from`, `derived_from`).
#[pyclass(frozen, eq, hash, get_all, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Node {
    /// The node's id; ids are dense from 0.
    id: u64,
    /// The node it follows, or `None` for a root.
    parent: Option<u64>,
    /// The time the driver gave it, in milliseconds.
    time: u64,
    /// Who wrote it.
    by: Author,
    /// What it holds.
    body: Body,
    /// The ids of the scratchpad items it consumed.
    delivers: Vec<u64>,
    /// The node this one repeats verbatim (a compaction or prune copy), or `None`.
    copied_from: Option<u64>,
    /// The node whose walk this one's content was computed from (a summary, a pruned
    /// transcript), or `None`.
    derived_from: Option<u64>,
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
            copied_from: n.copied_from,
            derived_from: n.derived_from,
        }
    }
}

impl From<Node> for tree::Node {
    fn from(n: Node) -> Self {
        Self {
            id: n.id,
            parent: n.parent,
            time: n.time,
            by: n.by.into(),
            body: n.body.into(),
            delivers: n.delivers,
            copied_from: n.copied_from,
            derived_from: n.derived_from,
        }
    }
}

/// The record of a cursor changing node. `from` is a Python keyword, so `from_`.
#[pyclass(frozen, eq, hash, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Move {
    /// The cursor that moved.
    #[pyo3(get)]
    cursor: u64,
    /// The node it left, or `None` when the move opened the cursor.
    #[pyo3(get, name = "from_")]
    from: Option<u64>,
    /// The node it moved to.
    #[pyo3(get)]
    to: u64,
    /// Who asked for the move.
    #[pyo3(get)]
    by: Author,
    /// The time the driver gave it, in milliseconds.
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

/// What a scratchpad item holds: an `Input`, or an `Extension`'s own data.
#[pyclass(frozen, eq, hash, from_py_object, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum ItemData {
    /// A line for the cursor to take as a `User` message.
    Input {
        /// Who wrote it.
        by: Author,
        /// The line.
        text: String,
    },
    /// Data an extension keeps in the scratchpad; core does not read it.
    Extension {
        /// The extension's name.
        name: String,
        /// The extension's data, as text.
        data: String,
    },
}

impl From<tree::ItemData> for ItemData {
    fn from(d: tree::ItemData) -> Self {
        match d {
            tree::ItemData::Input { by, text } => Self::Input {
                by: by.into(),
                text,
            },
            tree::ItemData::Extension { name, data } => Self::Extension { name, data },
        }
    }
}

impl From<ItemData> for tree::ItemData {
    fn from(d: ItemData) -> Self {
        match d {
            ItemData::Input { by, text } => Self::Input {
                by: by.into(),
                text,
            },
            ItemData::Extension { name, data } => Self::Extension { name, data },
        }
    }
}

/// One item in a cursor's scratchpad. Ids are never reused.
#[pyclass(frozen, eq, hash, get_all, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Item {
    /// The item's id.
    id: u64,
    /// The cursor it is addressed to.
    to: u64,
    /// What it holds.
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

impl From<Item> for tree::Item {
    fn from(i: Item) -> Self {
        Self {
            id: i.id,
            to: i.to,
            data: i.data.into(),
        }
    }
}

/// What a cursor's walk needs next.
#[pyclass(frozen, eq, hash, module = "tau._core")]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Next {
    /// Nothing: no `User` message or batch of calls is waiting.
    Wait,
    /// A sample: the last message is a `User` or `Results` node.
    Sample,
    /// The tools: the last message is a reply whose calls have not been answered.
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
