//! A reply's parts and the replay data a provider keeps on them.

use serde::de::{self, DeserializeSeed, Deserializer, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;

/// Data only the provider that wrote a part can send back (a signature, a
/// redacted block). `data` is the text of a JSON object with no repeated key,
/// kept exactly as given and opaque to core beyond that; `provider` names who
/// may send it back.
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
    /// The provider name is empty, blank, or holds a control character.
    BadProvider,
    /// `data` is not the text of a strict JSON object (NaN and Infinity are not
    /// JSON) or repeats a key; the parser's reason.
    NotAnObject(String),
}

impl std::fmt::Display for ReplayError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::BadProvider => write!(
                f,
                "replay needs the name of the provider that wrote it \
                 (not empty, blank, or holding a control character)"
            ),
            Self::NotAnObject(why) => {
                write!(f, "replay data must be the text of a JSON object: {why}")
            }
        }
    }
}

impl std::error::Error for ReplayError {}

impl Replay {
    /// # Errors
    /// [`ReplayError`] if `provider` is empty, blank, or holds a control
    /// character, or `data` is not the text of a strict JSON object with no
    /// repeated key (at any depth).
    pub fn new(provider: String, data: String) -> Result<Self, ReplayError> {
        if provider.trim().is_empty() || provider.chars().any(char::is_control) {
            return Err(ReplayError::BadProvider);
        }
        check_object(&data).map_err(|e| ReplayError::NotAnObject(e.to_string()))?;
        Ok(Self { provider, data })
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

/// Reads `data` once without building a tree: it must be one JSON object, and
/// no object in it may repeat a key.
fn check_object(data: &str) -> Result<(), serde_json::Error> {
    let mut de = serde_json::Deserializer::from_str(data);
    de.deserialize_map(Json)?;
    de.end()
}

/// Accepts a JSON object whose objects all have distinct keys, keeping nothing.
struct Json;

impl<'de> Visitor<'de> for Json {
    type Value = ();

    fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("a JSON object")
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<(), A::Error> {
        let mut seen = HashSet::new();
        while let Some(key) = map.next_key::<String>()? {
            if !seen.insert(key.clone()) {
                return Err(de::Error::custom(format!("repeated key {key:?}")));
            }
            map.next_value_seed(Nested)?;
        }
        Ok(())
    }
}

/// A value inside the object: any JSON, checked for repeated keys.
struct Nested;

impl<'de> DeserializeSeed<'de> for Nested {
    type Value = ();
    fn deserialize<D: Deserializer<'de>>(self, d: D) -> Result<(), D::Error> {
        d.deserialize_any(Inner)
    }
}

struct Inner;

impl<'de> Visitor<'de> for Inner {
    type Value = ();

    fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("any JSON value")
    }

    fn visit_bool<E>(self, _: bool) -> Result<(), E> {
        Ok(())
    }
    fn visit_i64<E>(self, _: i64) -> Result<(), E> {
        Ok(())
    }
    fn visit_u64<E>(self, _: u64) -> Result<(), E> {
        Ok(())
    }
    fn visit_f64<E>(self, _: f64) -> Result<(), E> {
        Ok(())
    }
    fn visit_str<E>(self, _: &str) -> Result<(), E> {
        Ok(())
    }
    fn visit_unit<E>(self) -> Result<(), E> {
        Ok(())
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<(), A::Error> {
        while seq.next_element_seed(Nested)?.is_some() {}
        Ok(())
    }
    fn visit_map<A: MapAccess<'de>>(self, map: A) -> Result<(), A::Error> {
        Json.visit_map(map)
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
    use serde::de::value::MapAccessDeserializer;
    use serde::de::{Deserializer, MapAccess, Visitor};
    use serde::{Deserialize, Serialize};

    /// A string is plain; an object is signed. Deserialized by hand because
    /// `untagged` would drop the reason a bad replay was refused.
    #[derive(Debug, Serialize)]
    #[serde(untagged)]
    pub enum Prose {
        Plain(String),
        Signed { text: String, replay: Replay },
    }

    #[derive(Deserialize)]
    struct SignedWire {
        text: String,
        replay: Replay,
    }

    impl<'de> Deserialize<'de> for Prose {
        fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
            struct V;
            impl<'de> Visitor<'de> for V {
                type Value = Prose;
                fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                    f.write_str("a string, or an object with `text` and `replay`")
                }
                fn visit_str<E>(self, s: &str) -> Result<Prose, E> {
                    Ok(Prose::Plain(s.to_owned()))
                }
                fn visit_string<E>(self, s: String) -> Result<Prose, E> {
                    Ok(Prose::Plain(s))
                }
                fn visit_map<A: MapAccess<'de>>(self, map: A) -> Result<Prose, A::Error> {
                    let SignedWire { text, replay } =
                        SignedWire::deserialize(MapAccessDeserializer::new(map))?;
                    Ok(Prose::Signed { text, replay })
                }
            }
            d.deserialize_any(V)
        }
    }

    /// A present `replay` must be one; `null` is refused like any non-replay.
    fn replay_if_present<'de, D: Deserializer<'de>>(d: D) -> Result<Option<Replay>, D::Error> {
        Replay::deserialize(d).map(Some)
    }

    #[derive(Debug, Serialize, Deserialize)]
    pub enum Part {
        Text(Prose),
        Thinking(Prose),
        Call {
            id: String,
            tool: String,
            args_raw: String,
            #[serde(
                default,
                skip_serializing_if = "Option::is_none",
                deserialize_with = "replay_if_present"
            )]
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

#[cfg(test)]
mod tests {
    use super::*;

    fn data(d: &str) -> Result<Replay, ReplayError> {
        Replay::new("p".into(), d.into())
    }

    #[test]
    fn repeated_keys_are_refused_at_any_depth() {
        assert!(data(r#"{"a":1,"b":2}"#).is_ok());
        assert!(data(r#"{"a":{"b":1},"c":{"b":2}}"#).is_ok());
        for bad in [
            r#"{"a":1,"a":2}"#,
            r#"{"x":{"a":1,"a":2}}"#,
            r#"{"x":[{"a":1,"a":2}]}"#,
        ] {
            let Err(ReplayError::NotAnObject(why)) = data(bad) else {
                panic!("{bad} was accepted");
            };
            assert!(why.contains("repeated key"), "{why}");
        }
    }

    #[test]
    fn data_must_be_one_object() {
        for bad in [
            "[]",
            "1",
            "\"s\"",
            "null",
            "",
            "{} {}",
            "{\"a\":1} x",
            "{\"a\":NaN}",
        ] {
            assert!(data(bad).is_err(), "{bad}");
        }
        let spaced = " { \"a\" : [1, 2.5e0, null] } ";
        assert_eq!(data(spaced).unwrap().data(), spaced);
    }

    #[test]
    fn provider_names_are_visible_text() {
        for bad in ["", " ", "\t\n", "a\0b", "a\nb", "\u{7f}"] {
            assert_eq!(
                Replay::new(bad.into(), "{}".into()),
                Err(ReplayError::BadProvider),
                "{bad:?}"
            );
        }
        assert!(Replay::new("anthropic".into(), "{}".into()).is_ok());
    }

    #[test]
    fn a_null_replay_is_refused_on_every_variant() {
        let r = r#"{"provider":"p","data":"{}"}"#;
        for ok in [
            format!(r#"{{"Text":{{"text":"t","replay":{r}}}}}"#),
            format!(r#"{{"Thinking":{{"text":"t","replay":{r}}}}}"#),
            format!(r#"{{"Call":{{"id":"i","tool":"t","args_raw":"{{}}","replay":{r}}}}}"#),
        ] {
            serde_json::from_str::<Part>(&ok).unwrap();
        }
        for bad in [
            r#"{"Text":{"text":"t","replay":null}}"#,
            r#"{"Thinking":{"text":"t","replay":null}}"#,
            r#"{"Call":{"id":"i","tool":"t","args_raw":"{}","replay":null}}"#,
        ] {
            assert!(serde_json::from_str::<Part>(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn a_bad_replay_in_a_log_names_the_reason() {
        let bad = r#"{"Text":{"text":"t","replay":{"provider":"p","data":"[1]"}}}"#;
        let why = serde_json::from_str::<Part>(bad).unwrap_err().to_string();
        assert!(why.contains("JSON object"), "{why}");
        let dup = r#"{"Call":{"id":"i","tool":"t","args_raw":"{}","replay":{"provider":"p","data":"{\"a\":1,\"a\":2}"}}}"#;
        let why = serde_json::from_str::<Part>(dup).unwrap_err().to_string();
        assert!(why.contains("repeated key"), "{why}");
    }
}
