//! A reply's parts and the replay data a provider keeps on them.

use serde::{Deserialize, Serialize};

/// Data only the provider that wrote a part can send back, byte for byte (a
/// signature, a redacted block). `data` is the text of a JSON object, opaque to
/// core beyond that; `provider` names who may send it back.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(try_from = "ReplayWire")]
pub struct Replay {
    provider: String,
    data: String,
}

#[derive(Deserialize)]
struct ReplayWire {
    provider: String,
    data: String,
}

impl TryFrom<ReplayWire> for Replay {
    type Error = ReplayError;
    fn try_from(w: ReplayWire) -> Result<Self, ReplayError> {
        Self::new(w.provider, w.data)
    }
}

/// Why a [`Replay`] was refused.
#[derive(Debug, PartialEq, Eq)]
pub enum ReplayError {
    EmptyProvider,
    /// `data` is not the text of a strict JSON object (NaN and Infinity are not
    /// JSON); the parser's reason.
    NotAnObject(String),
}

impl std::fmt::Display for ReplayError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmptyProvider => write!(f, "replay needs the name of the provider that wrote it"),
            Self::NotAnObject(why) => {
                write!(f, "replay data must be the text of a JSON object: {why}")
            }
        }
    }
}

impl std::error::Error for ReplayError {}

impl Replay {
    /// # Errors
    /// [`ReplayError`] if `provider` is empty or `data` is not the text of a
    /// strict JSON object.
    pub fn new(provider: String, data: String) -> Result<Self, ReplayError> {
        if provider.is_empty() {
            return Err(ReplayError::EmptyProvider);
        }
        match serde_json::from_str::<serde_json::Value>(&data) {
            Ok(serde_json::Value::Object(_)) => Ok(Self { provider, data }),
            Ok(_) => Err(ReplayError::NotAnObject("not an object".into())),
            Err(e) => Err(ReplayError::NotAnObject(e.to_string())),
        }
    }

    #[must_use]
    pub fn provider(&self) -> &str {
        &self.provider
    }

    #[must_use]
    pub fn data(&self) -> &str {
        &self.data
    }
}

/// One piece of a reply. `replay` is `None` unless the provider kept data to
/// send back. Logs written before replay existed load as `None`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(from = "wire::Part", into = "wire::Part")]
pub enum Part {
    Text {
        text: String,
        replay: Option<Replay>,
    },
    Thinking {
        text: String,
        replay: Option<Replay>,
    },
    Call {
        id: String,
        tool: String,
        args_raw: String,
        replay: Option<Replay>,
    },
}

impl Part {
    #[must_use]
    pub fn text(text: impl Into<String>) -> Self {
        Self::Text {
            text: text.into(),
            replay: None,
        }
    }

    #[must_use]
    pub fn thinking(text: impl Into<String>) -> Self {
        Self::Thinking {
            text: text.into(),
            replay: None,
        }
    }

    #[must_use]
    pub fn call(
        id: impl Into<String>,
        tool: impl Into<String>,
        args_raw: impl Into<String>,
    ) -> Self {
        Self::Call {
            id: id.into(),
            tool: tool.into(),
            args_raw: args_raw.into(),
            replay: None,
        }
    }
}

/// The log's spelling of a part. A part without replay is written as it was
/// before replay existed, so old logs load and unsigned parts are unchanged.
mod wire {
    use super::Replay;
    use serde::{Deserialize, Serialize};

    #[derive(Debug, Serialize, Deserialize)]
    #[serde(untagged)]
    pub enum Prose {
        Plain(String),
        Signed { text: String, replay: Replay },
    }

    #[derive(Debug, Serialize, Deserialize)]
    pub enum Part {
        Text(Prose),
        Thinking(Prose),
        Call {
            id: String,
            tool: String,
            args_raw: String,
            #[serde(default, skip_serializing_if = "Option::is_none")]
            replay: Option<Replay>,
        },
    }

    fn prose(text: String, replay: Option<Replay>) -> Prose {
        match replay {
            None => Prose::Plain(text),
            Some(replay) => Prose::Signed { text, replay },
        }
    }

    fn unprose(p: Prose) -> (String, Option<Replay>) {
        match p {
            Prose::Plain(text) => (text, None),
            Prose::Signed { text, replay } => (text, Some(replay)),
        }
    }

    impl From<super::Part> for Part {
        fn from(p: super::Part) -> Self {
            match p {
                super::Part::Text { text, replay } => Self::Text(prose(text, replay)),
                super::Part::Thinking { text, replay } => Self::Thinking(prose(text, replay)),
                super::Part::Call {
                    id,
                    tool,
                    args_raw,
                    replay,
                } => Self::Call {
                    id,
                    tool,
                    args_raw,
                    replay,
                },
            }
        }
    }

    impl From<Part> for super::Part {
        fn from(p: Part) -> Self {
            match p {
                Part::Text(p) => {
                    let (text, replay) = unprose(p);
                    Self::Text { text, replay }
                }
                Part::Thinking(p) => {
                    let (text, replay) = unprose(p);
                    Self::Thinking { text, replay }
                }
                Part::Call {
                    id,
                    tool,
                    args_raw,
                    replay,
                } => Self::Call {
                    id,
                    tool,
                    args_raw,
                    replay,
                },
            }
        }
    }
}
