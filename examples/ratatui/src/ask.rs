//! What a question's answer looks like, read from the JSON Schema it carries (`OpenQuestion.schema`),
//! and how the typed text becomes the value `answer` sends.

use serde_json::{Map, Value};

/// How the person answers.
#[derive(Debug, Clone, PartialEq)]
pub enum Shape {
    /// One of these values (`Literal["yes", "no"]`).
    Choices(Vec<Value>),
    Bool,
    Integer,
    Number,
    Text,
    /// Anything else: the typed text is JSON.
    Json,
}

/// The shape of `schema`. A nullable schema (`anyOf` with `null`) is the shape of its other half.
pub fn shape(schema: &Map<String, Value>) -> Shape {
    if let Some(Value::Array(options)) = schema.get("enum") {
        return Shape::Choices(options.clone());
    }
    if let Some(Value::Array(any)) = schema.get("anyOf") {
        let others: Vec<&Value> = any
            .iter()
            .filter(|s| s.get("type").and_then(Value::as_str) != Some("null"))
            .collect();
        if let [Value::Object(only)] = others.as_slice() {
            return shape(only);
        }
        return Shape::Json;
    }
    match schema.get("type").and_then(Value::as_str) {
        Some("boolean") => Shape::Bool,
        Some("integer") => Shape::Integer,
        Some("number") => Shape::Number,
        Some("string") => Shape::Text,
        _ => Shape::Json,
    }
}

impl Shape {
    /// The choices a person picks from, as they are shown; empty for a shape that is typed.
    pub fn choices(&self) -> Vec<Value> {
        match self {
            Self::Choices(options) => options.clone(),
            Self::Bool => vec![Value::Bool(true), Value::Bool(false)],
            _ => Vec::new(),
        }
    }

    /// The value for the text typed into the field, or why it is not one.
    pub fn parse(&self, text: &str) -> Result<Value, String> {
        match self {
            Self::Text => Ok(Value::String(text.to_owned())),
            Self::Integer => text
                .trim()
                .parse::<i64>()
                .map(Value::from)
                .map_err(|_| format!("{text:?} is not an integer")),
            Self::Number => text
                .trim()
                .parse::<f64>()
                .ok()
                .and_then(|n| serde_json::Number::from_f64(n).map(Value::Number))
                .ok_or_else(|| format!("{text:?} is not a number")),
            Self::Json | Self::Choices(_) | Self::Bool => {
                serde_json::from_str(text).map_err(|e| format!("{text:?} is not JSON ({e})"))
            }
        }
    }
}

/// A choice as a person reads it: a string bare, anything else as JSON.
pub fn show(choice: &Value) -> String {
    match choice {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn of(schema: &Value) -> Shape {
        shape(schema.as_object().unwrap())
    }

    #[test]
    fn a_literal_is_a_choice() {
        let got = of(&json!({"type": "string", "enum": ["yes", "no"]}));
        assert_eq!(got, Shape::Choices(vec![json!("yes"), json!("no")]));
        assert_eq!(show(&json!("yes")), "yes");
    }

    #[test]
    fn scalars_are_typed_and_checked() {
        assert_eq!(
            of(&json!({"type": "boolean"})).choices(),
            vec![json!(true), json!(false)]
        );
        assert_eq!(
            of(&json!({"type": "integer"})).parse(" 3 ").unwrap(),
            json!(3)
        );
        assert!(of(&json!({"type": "integer"})).parse("x").is_err());
        assert_eq!(
            of(&json!({"type": "string"})).parse("a b").unwrap(),
            json!("a b")
        );
        assert_eq!(
            of(&json!({"type": "number"})).parse("1.5").unwrap(),
            json!(1.5)
        );
    }

    #[test]
    fn nullable_is_the_shape_of_the_rest_and_a_union_is_json() {
        let nullable = json!({"anyOf": [{"type": "integer"}, {"type": "null"}]});
        assert_eq!(of(&nullable), Shape::Integer);
        let union = json!({"anyOf": [{"type": "integer"}, {"type": "string", "enum": ["new"]}]});
        assert_eq!(of(&union), Shape::Json);
        assert_eq!(of(&union).parse("\"new\"").unwrap(), json!("new"));
    }
}
