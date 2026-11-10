//! The template engine set up as `transformers` sets up Jinja2, with Python's behavior put in
//! wherever `MiniJinja`'s own differs: what `{{ value }}` prints, `tojson`, the string methods
//! and tests chat templates use. Each function here exists because a rendering differed
//! without it; `tests/fixtures/tokens/templates` holds the cases.

use minijinja::syntax::SyntaxConfig;
use minijinja::value::{Kwargs, Rest, Tuple, Value, ValueKind};
use minijinja::{AutoEscape, Environment, ErrorKind, State};

use crate::pytext::{self, JsonStyle};

pub(crate) type Failure = minijinja::Error;

pub(crate) fn refuse(text: String) -> Failure {
    Failure::new(ErrorKind::InvalidOperation, text)
}

/// `json.dumps`'s `indent`: a string, or that many spaces; `None` prints on one line.
fn json_indent(given: Option<Value>) -> Result<Option<String>, Failure> {
    let Some(given) = given.filter(|v| !v.is_none()) else {
        return Ok(None);
    };
    match given.kind() {
        ValueKind::String => Ok(given.as_str().map(str::to_owned)),
        ValueKind::Bool => Ok(Some(" ".repeat(usize::from(given.is_true())))),
        _ if given.is_integer() => Ok(Some(" ".repeat(given.as_usize().unwrap_or(0)))),
        _ => Err(refuse("tojson: indent must be an int or a str".into())),
    }
}

/// `json.dumps`'s `separators`: between items and after a key. Python's defaults depend on
/// whether there is an indent.
fn json_separators(given: Option<Value>, indented: bool) -> Result<(String, String), Failure> {
    let Some(given) = given.filter(|v| !v.is_none()) else {
        let item = if indented { "," } else { ", " };
        return Ok((item.to_owned(), ": ".to_owned()));
    };
    let both: Vec<Value> = given.try_iter()?.collect();
    match both.as_slice() {
        [item, key] => item
            .as_str()
            .zip(key.as_str())
            .map(|(a, b)| (a.to_owned(), b.to_owned())),
        _ => None,
    }
    .ok_or_else(|| refuse("tojson: separators must be two strings".into()))
}

/// `tojson`, as `transformers` defines it: `json.dumps(x, ensure_ascii=False, indent=None,
/// separators=None, sort_keys=False)`, the four options by position or by name.
#[expect(
    clippy::needless_pass_by_value,
    reason = "a filter's arguments are taken by value"
)]
fn tojson(value: &Value, positional: Rest<Value>, kwargs: Kwargs) -> Result<Value, Failure> {
    let option = |index: usize, name: &str| -> Result<Option<Value>, Failure> {
        let named: Option<Value> = kwargs.get(name)?;
        Ok(named.or_else(|| positional.get(index).cloned()))
    };
    let ascii = option(0, "ensure_ascii")?.is_some_and(|v| v.is_true());
    let indent = json_indent(option(1, "indent")?)?;
    let (item, key) = json_separators(option(2, "separators")?, indent.is_some())?;
    let sort = option(3, "sort_keys")?.is_some_and(|v| v.is_true());
    kwargs.assert_all_used()?;
    let style = JsonStyle {
        ascii,
        indent,
        item,
        key,
        sort,
    };
    let mut out = String::new();
    pytext::json_into(value, &style, 0, &mut out)?;
    Ok(Value::from(out))
}

/// `|string`: Python's `str`.
fn string(value: &Value) -> Result<Value, Failure> {
    if value.kind() == ValueKind::String {
        return Ok(value.clone());
    }
    let mut out = String::new();
    pytext::str_into(value, &mut out)?;
    Ok(Value::from(out))
}

/// `str.strip`, `lstrip` and `rstrip`: Python's white space, or the given characters.
fn strip(text: &str, chars: Option<&str>, left: bool, right: bool) -> String {
    let drop = |c: char| chars.map_or_else(|| pytext::is_space(c), |set| set.contains(c));
    let text = if left {
        text.trim_start_matches(drop)
    } else {
        text
    };
    let text = if right {
        text.trim_end_matches(drop)
    } else {
        text
    };
    text.to_owned()
}

/// `|trim`: `str(value).strip(chars)`.
fn trim(value: &Value, chars: Option<&str>) -> Result<Value, Failure> {
    let text = string(value)?;
    Ok(Value::from(strip(
        text.as_str().unwrap_or_default(),
        chars,
        true,
        true,
    )))
}

/// `str.title`: a letter after a letter is lowered, any other letter is raised, so `a.txt` is
/// `A.Txt`.
fn title(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut after_letter = false;
    for c in text.chars() {
        if after_letter {
            out.extend(c.to_lowercase());
        } else {
            out.extend(c.to_uppercase());
        }
        after_letter = c.is_lowercase() || c.is_uppercase();
    }
    out
}

/// Where Python's `str.splitlines` ends a line: at more than `\n` (a form feed, a line
/// separator, ...).
fn ends_line(c: char) -> bool {
    matches!(
        c,
        '\n' | '\r' | '\u{b}' | '\u{c}' | '\u{85}' | '\u{2028}' | '\u{2029}'
    ) || ('\u{1c}'..='\u{1e}').contains(&c)
}

/// `str.splitlines`.
fn splitlines(text: &str, keep_ends: bool) -> Vec<Value> {
    let mut lines = Vec::new();
    let mut rest = text;
    while let Some(at) = rest.find(ends_line) {
        let mut end = at + rest[at..].chars().next().map_or(1, char::len_utf8);
        if rest[at..].starts_with("\r\n") {
            end += 1;
        }
        lines.push(Value::from(&rest[..if keep_ends { end } else { at }]));
        rest = &rest[end..];
    }
    if !rest.is_empty() {
        lines.push(Value::from(rest));
    }
    lines
}

/// The pairs of a mapping as Python has them: tuples, which print as `('a', 1)`.
fn as_tuples(pairs: &Value) -> Result<Value, Failure> {
    let mut out = Vec::new();
    for pair in pairs.try_iter()? {
        out.push(Value::from_object(Tuple::new(pair.try_iter()?.collect())));
    }
    Ok(Value::from(out))
}

/// `|escape` and `|e`: the five characters as `markupsafe` writes them (`&#34;`, not `&quot;`).
fn escape(value: &Value) -> Result<Value, Failure> {
    if value.is_safe() {
        return Ok(value.clone());
    }
    let text = string(value)?;
    let mut out = String::new();
    for c in text.as_str().unwrap_or_default().chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '\'' => out.push_str("&#39;"),
            '"' => out.push_str("&#34;"),
            c => out.push(c),
        }
    }
    Ok(Value::from_safe_string(out))
}

/// `|items`: a mapping's pairs.
fn items(value: &Value) -> Result<Value, Failure> {
    as_tuples(&minijinja::filters::items(value)?)
}

/// `|dictsort`: a mapping's pairs, sorted.
fn dictsort(value: &Value, kwargs: Kwargs) -> Result<Value, Failure> {
    as_tuples(&minijinja::filters::dictsort(value, kwargs)?)
}

/// Jinja2's `sequence` test: anything with a length and items, which a string and a mapping
/// have too.
fn is_sequence(value: &Value) -> bool {
    matches!(
        value.kind(),
        ValueKind::String | ValueKind::Seq | ValueKind::Map
    )
}

/// Jinja2's `iterable` test: a string, a list, a mapping.
fn is_iterable(value: &Value) -> bool {
    is_sequence(value) || value.kind() == ValueKind::Iterable
}

/// Jinja2's `number` test: Python's `bool` is a number.
fn is_number(value: &Value) -> bool {
    matches!(value.kind(), ValueKind::Number | ValueKind::Bool)
}

/// The three strips of a string, with Python's white space or the characters given.
fn strip_method(text: &str, name: &str, args: &[Value]) -> Option<Result<Value, Failure>> {
    let (left, right) = match name {
        "strip" => (true, true),
        "lstrip" => (true, false),
        "rstrip" => (false, true),
        _ => return None,
    };
    let chars = match args {
        [] => None,
        [one] if one.is_none() => None,
        [one] if one.as_str().is_some() => one.as_str(),
        [_] => {
            let why = format!("{name}: the characters to strip must be a string");
            return Some(Err(refuse(why)));
        }
        _ => return None,
    };
    Some(Ok(Value::from(strip(text, chars, left, right))))
}

/// The string methods written here; `None` for one that is `minijinja-contrib`'s.
fn string_method(text: &str, name: &str, args: &[Value]) -> Option<Result<Value, Failure>> {
    match (name, args) {
        ("title", []) => Some(Ok(Value::from(title(text)))),
        ("splitlines", []) => Some(Ok(Value::from(splitlines(text, false)))),
        ("splitlines", [keep]) => Some(Ok(Value::from(splitlines(text, keep.is_true())))),
        _ => strip_method(text, name, args),
    }
}

/// A method call on a value: Python's string, list and dict methods, from `minijinja-contrib`
/// except where its answer is not Python's.
fn method(state: &mut State, value: &Value, name: &str, args: &[Value]) -> Result<Value, Failure> {
    if let Some(result) = value
        .as_str()
        .and_then(|text| string_method(text, name, args))
    {
        return result;
    }
    let result = minijinja_contrib::pycompat::unknown_method_callback(state, value, name, args)?;
    if name == "items" && value.kind() == ValueKind::Map {
        return as_tuples(&result);
    }
    Ok(result)
}

/// `raise_exception(message)`: the template refuses the request, and so would the server.
fn raise_exception(message: &Value) -> Result<Value, Failure> {
    let mut text = String::new();
    pytext::str_into(message, &mut text)?;
    Err(refuse(format!("the template refused the request: {text}")))
}

/// The engine as `transformers` configures Jinja2 (`trim_blocks`, `lstrip_blocks`, loop
/// controls, no escaping), printing and behaving as Python does.
pub(crate) fn environment() -> Environment<'static> {
    let mut env = Environment::new();
    let mut syntax = SyntaxConfig::builder();
    syntax.trim_blocks(true).lstrip_blocks(true);
    env.set_syntax(syntax.build().expect("Jinja's own delimiters are valid"));
    env.set_auto_escape_callback(|_| AutoEscape::None);
    env.set_unknown_method_callback(method);
    env.set_formatter(|out, _state, value| {
        let mut text = String::new();
        pytext::str_into(value, &mut text)?;
        out.write_str(&text).map_err(Failure::from)
    });
    // MiniJinja's own `pprint` and `attr` do not print or look up what Jinja2's do, and no
    // chat template needs them: refusing them is better than rendering something else.
    env.remove_filter("pprint");
    env.remove_filter("attr");
    env.add_filter("tojson", tojson);
    env.add_filter("string", string);
    env.add_filter("trim", trim);
    env.add_filter("escape", escape);
    env.add_filter("e", escape);
    env.add_filter("items", items);
    env.add_filter("dictsort", dictsort);
    env.add_test("sequence", is_sequence);
    env.add_test("iterable", is_iterable);
    env.add_test("number", is_number);
    env.add_function("raise_exception", raise_exception);
    env
}
