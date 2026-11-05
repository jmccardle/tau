//! The values of the tree: what a node holds, who wrote it, and the records a store writes.

use serde::{Deserialize, Serialize};

use super::{CursorId, ItemId, NodeId, Part};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
pub enum Stop {
    End,
    Tools,
    Length,
    Interrupted,
    Error,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
pub enum Outcome {
    Ok(String),
    Err(String),
    Interrupted,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
pub enum Body {
    System(String),
    User(String),
    Assistant {
        parts: Vec<Part>,
        stop: Stop,
        /// The provider's own finish string, verbatim; `None` when it sent none
        /// (an interrupted reply, a failed sample).
        finish_reason: Option<String>,
        /// What the response carried besides its parts (model, id, usage...), as JSON
        /// object text. Opaque to core; `None` when the provider sent none.
        #[serde(default)]
        meta: Option<String>,
    },
    /// One `(call id, outcome, by)` per call, in call order. `by` is who
    /// produced that outcome: `Tools`, or the extension that answered or edited it.
    Results(Vec<(String, Outcome, Author)>),
    Config(Config),
    Custom {
        kind: String,
        data: String,
    },
}

/// A config snapshot. `data` is opaque to core and typed by the driver.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
pub struct Config {
    pub data: String,
}

impl From<&str> for Config {
    fn from(data: &str) -> Self {
        Self { data: data.into() }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
pub enum Author {
    Frontend(String),
    Provider,
    Tools,
    Core,
    Extension(String),
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
pub struct Node {
    pub id: NodeId,
    pub parent: Option<NodeId>,
    pub time: u64,
    pub by: Author,
    pub body: Body,
    pub delivers: Vec<ItemId>,
    /// The node this one repeats verbatim (a compaction or prune copy). A log written
    /// before this field existed has none.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub copied_from: Option<NodeId>,
    /// The node whose walk this one's content was computed from (a summary, a pruned
    /// transcript). A log written before this field existed has none.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub derived_from: Option<NodeId>,
    /// What a person typed, for a `User` node whose text a `pre_input` hook changed (the
    /// attachments extension inlines files). Set only when it differs from the text: the
    /// node's `body` is what the model sees, this is what to put back in an editor. A log
    /// written before this field existed has none.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub typed: Option<String>,
}

/// Where a new node's content came from: the two optional fields of a `Node`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Provenance {
    pub copied_from: Option<NodeId>,
    pub derived_from: Option<NodeId>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Move {
    pub cursor: CursorId,
    pub from: Option<NodeId>,
    pub to: NodeId,
    pub by: Author,
    pub time: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
pub enum ItemData {
    Input { by: Author, text: String },
    Extension { name: String, data: String },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
pub struct Item {
    pub id: ItemId,
    pub to: CursorId,
    pub data: ItemData,
}

/// A cursor's items live here, for every cursor, with the next item id. The
/// store keeps it as the scratchpad file, never in the log (D12).
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Scratchpad {
    pub items: Vec<Item>,
    pub next_item: ItemId,
}

/// What the store must make durable, in order. Core never writes; it queues.
/// The store may buffer everything up to a `Sync`; at a `Sync` it syncs the
/// log, then, if `pad` is set, rewrites the scratchpad file with it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[allow(
    clippy::large_enum_variant,
    reason = "a record is queued and written once; boxing the node would touch every writer for nothing"
)]
pub enum Record {
    Write {
        node: Node,
        mv: Option<Move>,
    },
    Move(Move),
    /// Set or move a label; no cursor removes it.
    Label {
        label: String,
        cursor: Option<CursorId>,
    },
    /// A tombstone: the cursor is closed (D17). It keeps its id, its last node
    /// and its labels; nothing may name it again.
    Close {
        cursor: CursorId,
    },
    /// A sync point. `pad`: the scratchpad as it stands here, if it changed
    /// since the last sync.
    Sync {
        pad: Option<Scratchpad>,
    },
}
