//! A form built from a JSON Schema: an extension's options (`options(cursor, ext)`), filled in
//! and sent as `configure`. Pure state and keys; `formui` draws it.
//!
//! It supports the subset of JSON Schema that the Textual TUI's form renderer (D23 section 3)
//! does, and says so when handed a schema outside it, rather than leaving a field out:
//!
//! | the schema | the field | its value |
//! |---|---|---|
//! | `enum` | a row of choices | the chosen value |
//! | `boolean` | a switch | `true` or `false` |
//! | `array` of `enum` | a multi-select | the chosen values |
//! | `string`, `integer`, `number` | a text field | the text read as the type |
//! | `array` of those | a text field | comma-separated items, or a JSON array |
//! | `anyOf` | the one non-null half; with more, a text field read by each in turn | |
//!
//! A `T | None` is the `T` field, left empty for `None`. `required` and `default` apply: a
//! required field left empty is refused, and a default is what the field shows until it is
//! changed. Anything else (`object`, `$ref`, `oneOf`, `allOf`, `const`, a missing `type`) is
//! refused with a message that names the option.

use ratatui::crossterm::event::{KeyCode, KeyEvent};
use serde_json::{Map, Value};

use crate::editor::Editor;

type Schema = Map<String, Value>;

/// How a field is filled in.
#[derive(Debug)]
pub enum Widget {
    Choice {
        options: Vec<Value>,
        picked: Option<usize>,
    },
    Switch(bool),
    Multi {
        options: Vec<Value>,
        on: Vec<bool>,
        at: usize,
    },
    Text(Editor),
}

#[derive(Debug)]
pub struct Field {
    pub name: String,
    pub description: Option<String>,
    pub required: bool,
    /// The schema of the value (a `T | None` is the `T`).
    shape: Schema,
    /// The option's default: the value it has now.
    pub default: Option<Value>,
    pub widget: Widget,
    pub error: Option<String>,
    /// The value the form opened with: what was sent only if it differs from this.
    opened: Option<Value>,
}

/// What a key did to the form.
#[derive(Debug, PartialEq)]
pub enum Done {
    /// Nothing that ends it.
    Open,
    Cancel,
    /// Apply: the options to set, and whether to save them for every session.
    Submit(Schema, bool),
}

/// Where the keyboard is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Focus {
    Field(usize),
    Save,
    Apply,
    Cancel,
}

#[derive(Debug)]
pub struct Form {
    /// Which form this is, among those opened in a run (a late answer names it).
    pub serial: u64,
    pub title: String,
    pub extension: String,
    pub fields: Vec<Field>,
    pub save: bool,
    focus: usize,
    /// Why the server refused the last `configure`.
    pub error: Option<String>,
}

fn kind(schema: &Schema) -> Option<&str> {
    schema.get("type").and_then(Value::as_str)
}

fn is_null(schema: &Value) -> bool {
    schema.get("type").and_then(Value::as_str) == Some("null")
}

/// The schema of the value, and whether it may be left empty: a `T | None` is the `T`.
fn shape_of(schema: &Schema) -> (Schema, bool) {
    let Some(Value::Array(alternatives)) = schema.get("anyOf") else {
        return (schema.clone(), false);
    };
    let present: Vec<&Value> = alternatives.iter().filter(|a| !is_null(a)).collect();
    let optional = present.len() < alternatives.len();
    match present.as_slice() {
        [Value::Object(only)] => (only.clone(), optional),
        _ => (schema.clone(), optional),
    }
}

fn enum_of(schema: &Schema) -> Option<Vec<Value>> {
    match schema.get("enum") {
        Some(Value::Array(options)) => Some(options.clone()),
        _ => None,
    }
}

/// Whether a text field can read a value of `schema`; why not.
fn supported(schema: &Schema) -> Result<(), String> {
    if let Some(options) = enum_of(schema) {
        return if options.is_empty() {
            Err("its `enum` has no values to choose".to_owned())
        } else {
            Ok(())
        };
    }
    if let Some(Value::Array(alternatives)) = schema.get("anyOf") {
        let present: Vec<&Value> = alternatives.iter().filter(|a| !is_null(a)).collect();
        if present.is_empty() {
            return Err("it allows only null".to_owned());
        }
        return present.iter().try_for_each(|a| match a {
            Value::Object(alternative) => supported(alternative),
            _ => Err("an alternative is not a schema".to_owned()),
        });
    }
    match kind(schema) {
        Some("string" | "integer" | "number" | "boolean") => Ok(()),
        Some("array") => match schema.get("items") {
            Some(Value::Object(items)) => supported(items),
            _ => Err("an array with no `items`".to_owned()),
        },
        Some(other) => Err(format!("type {other:?} is not one a form shows")),
        None => Err("it has no `type`, `enum` or `anyOf`".to_owned()),
    }
}

/// The value `text` stands for under `schema` (`commandline.value`): an `anyOf` is tried
/// alternative by alternative, an array is a JSON array if the text starts with `[` and else
/// comma-separated items, a scalar is read as its type and held to its `enum`.
pub fn read_value(text: &str, schema: &Schema) -> Result<Value, String> {
    if let Some(Value::Array(alternatives)) = schema.get("anyOf") {
        let mut problems = Vec::new();
        for alternative in alternatives.iter().filter(|a| !is_null(a)) {
            let Value::Object(alternative) = alternative else {
                continue;
            };
            match read_value(text, alternative) {
                Ok(value) => return Ok(value),
                Err(why) => problems.push(why),
            }
        }
        return Err(problems.join(" and "));
    }
    if kind(schema) == Some("array") {
        if text.starts_with('[') {
            return match serde_json::from_str::<Value>(text) {
                Ok(array @ Value::Array(_)) => Ok(array),
                Ok(_) => Err(format!("{text:?} is not a JSON array")),
                Err(e) => Err(format!(
                    "{text:?} is not a JSON array: {e}; text starting with [ is read as a JSON \
                     array"
                )),
            };
        }
        let Some(Value::Object(items)) = schema.get("items") else {
            return Err("an array with no `items`".to_owned());
        };
        if text.is_empty() {
            return Ok(Value::Array(Vec::new()));
        }
        return text
            .split(',')
            .map(|word| read_value(word.trim(), items))
            .collect::<Result<Vec<_>, _>>()
            .map(Value::Array);
    }
    scalar(text, schema)
}

fn scalar(word: &str, schema: &Schema) -> Result<Value, String> {
    let value = match kind(schema) {
        Some("integer") => word
            .trim()
            .parse::<i64>()
            .map(Value::from)
            .map_err(|_| format!("{word:?} is not an integer"))?,
        Some("number") => word
            .trim()
            .parse::<f64>()
            .ok()
            .and_then(serde_json::Number::from_f64)
            .map(Value::Number)
            .ok_or_else(|| format!("{word:?} is not a number"))?,
        Some("boolean") => match word.to_lowercase().as_str() {
            "true" | "yes" | "on" | "1" => Value::Bool(true),
            "false" | "no" | "off" | "0" => Value::Bool(false),
            _ => return Err(format!("{word:?} is not true or false")),
        },
        _ => Value::String(word.to_owned()),
    };
    match enum_of(schema) {
        Some(allowed) if !allowed.iter().any(|a| same(a, &value)) => {
            Err(format!("{word:?} is not one of {}", Value::Array(allowed)))
        }
        _ => Ok(value),
    }
}

/// JSON equality with `1` and `1.0` the same number.
pub fn same(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => x.as_f64() == y.as_f64(),
        _ => a == b,
    }
}

/// A choice as a person reads it: a string bare, anything else as JSON.
pub fn show(choice: &Value) -> String {
    match choice {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

impl Field {
    fn new(name: &str, outer: &Schema, required: bool) -> Result<Self, String> {
        let (shape, optional) = shape_of(outer);
        supported(&shape).map_err(|why| {
            format!(
                "option {name:?}: {why} (schema {}); this form shows enums, booleans, strings, \
                 numbers, integers and lists of them",
                Value::Object(outer.clone())
            )
        })?;
        let default = outer
            .get("default")
            .or_else(|| shape.get("default"))
            .cloned();
        let items = match shape.get("items") {
            Some(Value::Object(items)) => Some(items),
            _ => None,
        };
        let widget = if let Some(options) = enum_of(&shape) {
            let picked = default
                .as_ref()
                .and_then(|d| options.iter().position(|o| same(o, d)));
            Widget::Choice { options, picked }
        } else if kind(&shape) == Some("boolean") {
            Widget::Switch(default.as_ref().is_some_and(|d| d.as_bool() == Some(true)))
        } else if let Some(options) = kind(&shape)
            .filter(|k| *k == "array")
            .and(items)
            .and_then(enum_of)
        {
            let chosen = match &default {
                Some(Value::Array(chosen)) => chosen.clone(),
                _ => Vec::new(),
            };
            let on = options
                .iter()
                .map(|o| chosen.iter().any(|c| same(c, o)))
                .collect();
            Widget::Multi { options, on, at: 0 }
        } else {
            Widget::Text(Editor::default())
        };
        let description = outer
            .get("description")
            .or_else(|| shape.get("description"))
            .and_then(Value::as_str)
            .map(str::to_owned);
        let mut field = Self {
            name: name.to_owned(),
            description,
            required: required && !optional,
            shape,
            default,
            widget,
            error: None,
            opened: None,
        };
        // A text field opens showing its default; the others open holding their value.
        field.opened = match field.widget {
            Widget::Text(_) => field.default.clone(),
            _ => field.value().ok().flatten(),
        };
        Ok(field)
    }

    /// The value shown before anything is typed, for a text field.
    pub fn placeholder(&self) -> String {
        self.default.as_ref().map(show).unwrap_or_default()
    }

    /// What the field holds: `None` if left empty and allowed to be. `Err` says what is wrong.
    fn value(&self) -> Result<Option<Value>, String> {
        let empty = |why: &str| {
            if self.required {
                Err(why.to_owned())
            } else {
                Ok(None)
            }
        };
        match &self.widget {
            Widget::Switch(on) => Ok(Some(Value::Bool(*on))),
            Widget::Choice { options, picked } => match picked {
                Some(i) => Ok(Some(options[*i].clone())),
                None => empty("choose one"),
            },
            Widget::Multi { options, on, .. } => Ok(Some(Value::Array(
                options
                    .iter()
                    .zip(on)
                    .filter(|(_, on)| **on)
                    .map(|(o, _)| o.clone())
                    .collect(),
            ))),
            Widget::Text(editor) => {
                let text = editor.text();
                let text = text.trim();
                if text.is_empty() {
                    return empty("a value is needed");
                }
                read_value(text, &self.shape).map(Some)
            }
        }
    }

    fn key(&mut self, key: KeyEvent) {
        match (&mut self.widget, key.code) {
            (Widget::Text(editor), _) => {
                editor.handle(key);
            }
            (Widget::Switch(on), KeyCode::Char(' ')) => *on = !*on,
            (Widget::Switch(on), KeyCode::Left) => *on = false,
            (Widget::Switch(on), KeyCode::Right) => *on = true,
            (Widget::Choice { options, picked }, KeyCode::Left) => {
                *picked = Some(picked.map_or(options.len() - 1, |p| p.saturating_sub(1)));
            }
            (Widget::Choice { options, picked }, KeyCode::Right | KeyCode::Char(' ')) => {
                *picked = Some(picked.map_or(0, |p| (p + 1).min(options.len() - 1)));
            }
            (Widget::Multi { at, .. }, KeyCode::Left) => *at = at.saturating_sub(1),
            (Widget::Multi { options, at, .. }, KeyCode::Right) => {
                *at = (*at + 1).min(options.len() - 1);
            }
            (Widget::Multi { on, at, .. }, KeyCode::Char(' ')) => on[*at] = !on[*at],
            _ => {}
        }
    }
}

impl Form {
    /// The form for `extension`'s options, whose JSON Schema is `schema`. `Err` names the first
    /// option a form cannot show, and nothing is built.
    pub fn new(serial: u64, extension: &str, schema: &Schema) -> Result<Self, String> {
        let required: Vec<&str> = match schema.get("required") {
            Some(Value::Array(names)) => names.iter().filter_map(Value::as_str).collect(),
            _ => Vec::new(),
        };
        let properties = match schema.get("properties") {
            Some(Value::Object(properties)) => properties.clone(),
            _ => Map::new(),
        };
        let mut fields = Vec::new();
        for (name, property) in &properties {
            let Value::Object(property) = property else {
                return Err(format!("option {name:?}: its schema is not an object"));
            };
            fields.push(Field::new(
                name,
                property,
                required.contains(&name.as_str()),
            )?);
        }
        if fields.is_empty() {
            return Err(format!("{extension} has no options to show"));
        }
        Ok(Self {
            serial,
            title: format!("/config {extension}"),
            extension: extension.to_owned(),
            fields,
            save: false,
            focus: 0,
            error: None,
        })
    }

    pub fn focus(&self) -> Focus {
        let n = self.fields.len();
        match self.focus {
            i if i < n => Focus::Field(i),
            i if i == n => Focus::Save,
            i if i == n + 1 => Focus::Apply,
            _ => Focus::Cancel,
        }
    }

    fn move_focus(&mut self, by: isize) {
        let last = self.fields.len() + 2;
        self.focus = self.focus.saturating_add_signed(by).min(last);
    }

    /// Take a key.
    pub fn key(&mut self, key: KeyEvent) -> Done {
        self.error = None;
        match (key.code, self.focus()) {
            (KeyCode::Esc, _) | (KeyCode::Enter, Focus::Cancel) => return Done::Cancel,
            (KeyCode::Enter, Focus::Apply) => return self.submit(),
            (KeyCode::Tab | KeyCode::Down, _) => self.move_focus(1),
            (KeyCode::BackTab | KeyCode::Up, _) => self.move_focus(-1),
            (KeyCode::Enter, _) => self.move_focus(1),
            (KeyCode::Char(' ') | KeyCode::Left | KeyCode::Right, Focus::Save) => {
                self.save = !self.save;
            }
            (_, Focus::Field(i)) => {
                self.fields[i].error = None;
                self.fields[i].key(key);
            }
            _ => {}
        }
        Done::Open
    }

    /// Read every field. Each that cannot be read shows why and keeps the form open.
    fn submit(&mut self) -> Done {
        let mut values = Map::new();
        let mut problems = 0;
        for field in &mut self.fields {
            field.error = None;
            match field.value() {
                Ok(Some(value)) => {
                    values.insert(field.name.clone(), value);
                }
                Ok(None) => {}
                Err(why) => {
                    field.error = Some(why);
                    problems += 1;
                }
            }
        }
        if problems > 0 {
            return Done::Open;
        }
        // Only what differs from the value the form opened with; what "save" writes is the
        // server's rule.
        let save = self.save;
        let wanted =
            |field: &Field, value: &Value| field.opened.as_ref().is_none_or(|o| !same(o, value));
        let chosen: Schema = self
            .fields
            .iter()
            .filter_map(|f| values.get(&f.name).map(|v| (f, v)))
            .filter(|(f, v)| wanted(f, v))
            .map(|(f, v)| (f.name.clone(), v.clone()))
            .collect();
        Done::Submit(chosen, save)
    }
}

/// What a `configure` answer does, given which form is open now and which one sent it.
#[derive(Debug, PartialEq, Eq)]
pub enum Landing {
    /// The form it was sent from is open: close it (saved) or show the reason in it.
    Form,
    /// That form is gone (cancelled, or another opened since): say it as a notice, touching
    /// nothing now open.
    Notice,
}

pub fn landing(open: Option<u64>, sent_from: u64) -> Landing {
    if open == Some(sent_from) {
        Landing::Form
    } else {
        Landing::Notice
    }
}

/// A list to pick one of, for the extensions that have options.
#[derive(Debug)]
pub struct Pick {
    pub title: String,
    pub items: Vec<String>,
    pub at: usize,
}

impl Pick {
    /// `Some(Some(item))` picked, `Some(None)` cancelled, `None` still open.
    pub fn key(&mut self, key: KeyEvent) -> Option<Option<String>> {
        match key.code {
            KeyCode::Esc => return Some(None),
            KeyCode::Up => self.at = self.at.saturating_sub(1),
            KeyCode::Down => self.at = (self.at + 1).min(self.items.len() - 1),
            KeyCode::Enter => return Some(Some(self.items[self.at].clone())),
            _ => {}
        }
        None
    }
}

#[cfg(test)]
mod tests;
