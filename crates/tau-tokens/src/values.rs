//! JSON bytes to the values a template reads, as Python's `json.loads` would make them: an
//! object's keys in the order written, an integer an integer and `1.0` a float.

use std::fmt;

use minijinja::value::Value;
use serde::de::{DeserializeSeed, Deserializer, MapAccess, SeqAccess, Visitor};

use crate::error::Error;

/// Past these, serde hands an integer over as a float, and nothing tells it from a float that
/// was written as one. Python prints the first with all its digits and the second as `1e+20`,
/// so neither can be printed with certainty and both are refused.
const EXACT: std::ops::RangeInclusive<f64> =
    -9_223_372_036_854_775_808.0..=18_446_744_073_709_551_615.0;

/// Reads one JSON value. `printed` is whether a template may print its numbers, which is when
/// one that cannot be printed with certainty is refused.
#[derive(Clone, Copy)]
struct Json {
    printed: bool,
}

impl<'de> DeserializeSeed<'de> for Json {
    type Value = Value;

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<Value, D::Error> {
        deserializer.deserialize_any(self)
    }
}

impl<'de> Visitor<'de> for Json {
    type Value = Value;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a JSON value")
    }

    fn visit_bool<E>(self, v: bool) -> Result<Value, E> {
        Ok(Value::from(v))
    }

    fn visit_i64<E>(self, v: i64) -> Result<Value, E> {
        Ok(Value::from(v))
    }

    fn visit_u64<E>(self, v: u64) -> Result<Value, E> {
        Ok(Value::from(v))
    }

    fn visit_f64<E: serde::de::Error>(self, v: f64) -> Result<Value, E> {
        if self.printed && v.fract() == 0.0 && !EXACT.contains(&v) {
            return Err(E::custom(format!(
                "the number {v:e} is too large to print as Python would"
            )));
        }
        Ok(Value::from(v))
    }

    fn visit_str<E>(self, v: &str) -> Result<Value, E> {
        Ok(Value::from(v))
    }

    fn visit_string<E>(self, v: String) -> Result<Value, E> {
        Ok(Value::from(v))
    }

    fn visit_unit<E>(self) -> Result<Value, E> {
        Ok(Value::from(()))
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Value, A::Error> {
        let mut items = Vec::new();
        while let Some(item) = seq.next_element_seed(self)? {
            items.push(item);
        }
        Ok(Value::from(items))
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Value, A::Error> {
        let mut pairs: Vec<(String, Value)> = Vec::new();
        while let Some(key) = map.next_key::<String>()? {
            let value = map.next_value_seed(self)?;
            // A repeated key keeps its first place and takes its last value, as a dict does.
            match pairs.iter_mut().find(|(k, _)| *k == key) {
                Some(pair) => pair.1 = value,
                None => pairs.push((key, value)),
            }
        }
        Ok(Value::from_pairs(pairs))
    }
}

fn read(bytes: &[u8], what: &str, printed: bool) -> Result<Value, Error> {
    let mut reader = serde_json::Deserializer::from_slice(bytes);
    let value = Json { printed }
        .deserialize(&mut reader)
        .and_then(|value| reader.end().map(|()| value))
        .map_err(|err| {
            Error::Template(format!("{what}: not the JSON a template is given: {err}"))
        })?;
    Ok(value)
}

/// The value a template is given for these JSON bytes; `what` names them in the error.
pub(crate) fn parse(bytes: &[u8], what: &str) -> Result<Value, Error> {
    read(bytes, what, true)
}

/// A file's JSON that is read for a few fields and never printed (a `tokenizer_config.json`,
/// whose `model_max_length` is often 1e30).
pub(crate) fn parse_file(bytes: &[u8], what: &str) -> Result<Value, Error> {
    read(bytes, what, false)
}

/// The pairs of a JSON object, in order.
pub(crate) fn pairs(object: &Value) -> Result<Vec<(String, Value)>, minijinja::Error> {
    let mut out = Vec::new();
    for key in object.try_iter()? {
        let value = object.get_item(&key)?;
        out.push((key.as_str().unwrap_or_default().to_owned(), value));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_keep_their_order_and_numbers_their_kind() {
        let value = parse(br#"{"zeta": 1, "alpha": 1.0, "zeta": 2}"#, "test").unwrap();
        let all = pairs(&value).unwrap();
        assert_eq!(all[0].0, "zeta");
        assert_eq!(all[1].0, "alpha");
        assert!(all[0].1.is_integer() && all[0].1.as_i64() == Some(2));
        assert!(!all[1].1.is_integer());
    }

    #[test]
    fn a_number_python_would_print_differently_is_refused() {
        assert!(parse(b"[123456789012345678901234567890]", "test").is_err());
        assert!(parse(b"[1e20]", "test").is_err());
        assert!(
            parse(
                b"[18446744073709551615, -9223372036854775808, 1.5e10]",
                "test"
            )
            .is_ok()
        );
    }
}
