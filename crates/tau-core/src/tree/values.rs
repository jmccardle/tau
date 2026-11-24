//! The values of the tree: what a node holds, who wrote it, and the records a store writes.

use serde::{Deserialize, Serialize};

use super::{CursorId, Image, ItemId, NodeId, Part};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
pub enum Stop {
    End,
    Tools,
    Length,
    Interrupted,
    Error,
    Refused,
}

/// How a call ended. `Ok` carries the tool's text and its images (D30), which only `Ok` has.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(from = "OutcomeRepr", into = "OutcomeRepr")]
pub enum Outcome {
    Ok { value: String, images: Vec<Image> },
    Err(String),
    Interrupted,
}

impl Outcome {
    /// `Ok` with text and no images.
    #[must_use]
    pub fn ok(value: impl Into<String>) -> Self {
        Self::Ok {
            value: value.into(),
            images: vec![],
        }
    }
}

/// `Outcome` as the log and the wire encode it: `Ok` without images is a string, as before D30,
/// so a log written before images loads unchanged.
#[derive(Serialize, Deserialize)]
#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
#[cfg_attr(feature = "schema", schemars(rename = "Outcome"))]
enum OutcomeRepr {
    Ok(OkRepr),
    Err(String),
    Interrupted,
}

/// `Outcome::Ok`'s two encodings: the text alone, or the text with its images.
#[derive(Serialize)]
#[serde(untagged)]
#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
#[cfg_attr(feature = "schema", schemars(inline))]
enum OkRepr {
    Text(String),
    WithImages { value: String, images: Vec<Image> },
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WithImages {
    value: String,
    images: Vec<Image>,
}

/// By the JSON's shape, so a bad image says why (an untagged enum would only say no variant
/// matched).
impl<'de> Deserialize<'de> for OkRepr {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct Shape;
        impl<'de> serde::de::Visitor<'de> for Shape {
            type Value = OkRepr;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("a string, or an object with `value` and `images`")
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<OkRepr, E> {
                Ok(OkRepr::Text(v.to_owned()))
            }
            fn visit_string<E: serde::de::Error>(self, v: String) -> Result<OkRepr, E> {
                Ok(OkRepr::Text(v))
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(self, map: A) -> Result<OkRepr, A::Error> {
                let w = WithImages::deserialize(serde::de::value::MapAccessDeserializer::new(map))?;
                Ok(OkRepr::WithImages {
                    value: w.value,
                    images: w.images,
                })
            }
        }
        d.deserialize_any(Shape)
    }
}

impl From<OutcomeRepr> for Outcome {
    fn from(r: OutcomeRepr) -> Self {
        match r {
            OutcomeRepr::Ok(OkRepr::Text(value)) => Self::ok(value),
            OutcomeRepr::Ok(OkRepr::WithImages { value, images }) => Self::Ok { value, images },
            OutcomeRepr::Err(e) => Self::Err(e),
            OutcomeRepr::Interrupted => Self::Interrupted,
        }
    }
}

impl From<Outcome> for OutcomeRepr {
    fn from(o: Outcome) -> Self {
        match o {
            Outcome::Ok { value, images } if images.is_empty() => Self::Ok(OkRepr::Text(value)),
            Outcome::Ok { value, images } => Self::Ok(OkRepr::WithImages { value, images }),
            Outcome::Err(e) => Self::Err(e),
            Outcome::Interrupted => Self::Interrupted,
        }
    }
}

#[cfg(feature = "schema")]
impl schemars::JsonSchema for Outcome {
    fn schema_name() -> std::borrow::Cow<'static, str> {
        "Outcome".into()
    }

    fn json_schema(generator: &mut schemars::SchemaGenerator) -> schemars::Schema {
        OutcomeRepr::json_schema(generator)
    }
}

impl Body {
    /// The images this body holds: those of a `Results` node's `Ok` outcomes, in call order.
    pub fn images(&self) -> impl Iterator<Item = &Image> {
        let outcomes = match self {
            Self::Results(outcomes) => outcomes.as_slice(),
            _ => &[],
        };
        outcomes.iter().flat_map(|(_, o, _)| match o {
            Outcome::Ok { images, .. } => images.as_slice(),
            _ => &[],
        })
    }

    /// `images`, to change: what a store does to give a copy its bytes.
    pub fn images_mut(&mut self) -> impl Iterator<Item = &mut Image> {
        let outcomes = match self {
            Self::Results(outcomes) => outcomes.as_mut_slice(),
            _ => &mut [],
        };
        outcomes.iter_mut().flat_map(|(_, o, _)| match o {
            Outcome::Ok { images, .. } => images.as_mut_slice(),
            _ => &mut [],
        })
    }
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
