//! Text as Python prints it. A chat template is written for Jinja2, where `{{ value }}` is
//! `str(value)` and `tojson` is `json.dumps`; a server renders it that way, so a count of the
//! rendering is right only if every value prints the same here: `True` and `None`, `1e-05`,
//! `{'a': 1}`, `{"a": 1}` with a space after the colon.

use std::fmt::Write as _;

use minijinja::value::{Value, ValueKind};
use minijinja::{Error, ErrorKind};

/// What `json.dumps` was asked for (the keyword arguments of `tojson`).
#[derive(Debug, Clone)]
pub(crate) struct JsonStyle {
    /// `ensure_ascii`: every non-ASCII character as `\uXXXX`.
    pub ascii: bool,
    /// `indent`: the text one level of nesting is indented by; `None` prints on one line.
    pub indent: Option<String>,
    /// `separators[0]`, between items.
    pub item: String,
    /// `separators[1]`, between a key and its value.
    pub key: String,
    /// `sort_keys`.
    pub sort: bool,
}

fn refuse(what: &str) -> Error {
    Error::new(ErrorKind::InvalidOperation, what.to_owned())
}

/// `repr(float)`: the shortest digits that read back as the same number, in positional notation
/// from 1e-4 up to 1e16 and with an exponent of at least two digits outside it.
pub(crate) fn float(x: f64) -> String {
    if x.is_nan() {
        return "nan".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    let shortest = format!("{x:e}");
    let (mantissa, exponent) = shortest.split_once('e').unwrap_or((&shortest, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let (sign, mantissa) = match mantissa.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", mantissa),
    };
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let body = if (-4..16).contains(&exponent) {
        positional(&digits, exponent)
    } else {
        let (first, rest) = digits.split_at(1);
        let point = if rest.is_empty() { "" } else { "." };
        let direction = if exponent < 0 { '-' } else { '+' };
        format!(
            "{first}{point}{rest}e{direction}{:02}",
            exponent.unsigned_abs()
        )
    };
    format!("{sign}{body}")
}

/// `digits` with the decimal point `exponent` places after the first digit.
fn positional(digits: &str, exponent: i32) -> String {
    let Ok(whole) = usize::try_from(exponent + 1) else {
        let zeros = "0".repeat(exponent.unsigned_abs() as usize - 1);
        return format!("0.{zeros}{digits}");
    };
    if whole == 0 {
        return format!("0.{digits}");
    }
    if digits.len() <= whole {
        let zeros = "0".repeat(whole - digits.len());
        return format!("{digits}{zeros}.0");
    }
    let (before, after) = digits.split_at(whole);
    format!("{before}.{after}")
}

/// Whether Python's `repr` prints this character as itself. Rust's `Debug` for a string keeps
/// the same table (control, format, private-use, unassigned and separator characters are
/// escaped); the `a` in front keeps a combining mark from counting as the start of the string,
/// where Rust alone would escape it.
fn printable(c: char) -> bool {
    if c.is_ascii() {
        return (' '..='~').contains(&c);
    }
    let mut pair = String::with_capacity(8);
    pair.push('a');
    pair.push(c);
    pair.escape_debug().count() == 2
}

/// `repr(str)`: single quotes unless the text has one and no double quote.
pub(crate) fn repr_str(text: &str, out: &mut String) {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if printable(c) => out.push(c),
            c => {
                let code = u32::from(c);
                let escaped = match code {
                    0..=0xff => format!("\\x{code:02x}"),
                    0x100..=0xffff => format!("\\u{code:04x}"),
                    _ => format!("\\U{code:08x}"),
                };
                out.push_str(&escaped);
            }
        }
    }
    out.push(quote);
}

/// A number as Python prints it: an integer's digits, or [`float`].
fn number(value: &Value) -> String {
    if value.is_integer() {
        return value.to_string();
    }
    f64::try_from(value.clone()).map_or_else(|_| value.to_string(), float)
}

fn truth(value: &Value) -> bool {
    value.is_true()
}

/// The items of a list or the keys of a mapping.
fn items(value: &Value) -> Result<Vec<Value>, Error> {
    Ok(value.try_iter()?.collect())
}

/// `repr(value)`, appended to `out`.
fn repr(value: &Value, out: &mut String) -> Result<(), Error> {
    match value.kind() {
        ValueKind::String => {
            repr_str(value.as_str().unwrap_or_default(), out);
            Ok(())
        }
        _ => str_into(value, out),
    }
}

/// `str(value)`, appended to `out`: what `{{ value }}` prints.
pub(crate) fn str_into(value: &Value, out: &mut String) -> Result<(), Error> {
    match value.kind() {
        ValueKind::Undefined => {}
        ValueKind::None => out.push_str("None"),
        ValueKind::Bool => out.push_str(if truth(value) { "True" } else { "False" }),
        ValueKind::Number => out.push_str(&number(value)),
        ValueKind::String => out.push_str(value.as_str().unwrap_or_default()),
        ValueKind::Seq => {
            // A tuple prints in parentheses, and one item keeps its comma: `(1,)`.
            let all = items(value)?;
            let (open, close) = if value.is_tuple() {
                ('(', ")")
            } else {
                ('[', "]")
            };
            out.push(open);
            for (n, item) in all.iter().enumerate() {
                if n > 0 {
                    out.push_str(", ");
                }
                repr(item, out)?;
            }
            if value.is_tuple() && all.len() == 1 {
                out.push(',');
            }
            out.push_str(close);
        }
        ValueKind::Map => {
            out.push('{');
            for (n, key) in items(value)?.iter().enumerate() {
                if n > 0 {
                    out.push_str(", ");
                }
                repr(key, out)?;
                out.push_str(": ");
                repr(&value.get_item(key)?, out)?;
            }
            out.push('}');
        }
        _ => out.push_str(&value.to_string()),
    }
    Ok(())
}

/// A string as `json.dumps` writes it.
fn json_str(text: &str, ascii: bool, out: &mut String) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if u32::from(c) < 0x20 || (ascii && !c.is_ascii()) => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// A float as `json.dumps` writes it: `repr`, with JavaScript's names for the three that have
/// no JSON form.
fn json_float(x: f64) -> String {
    if x.is_nan() {
        "NaN".to_owned()
    } else if x.is_infinite() {
        if x > 0.0 { "Infinity" } else { "-Infinity" }.to_owned()
    } else {
        float(x)
    }
}

/// A mapping's key as `json.dumps` writes it. Python turns a number, a boolean or `None` into
/// its JSON text and refuses anything else.
fn json_key(key: &Value) -> Result<String, Error> {
    match key.kind() {
        ValueKind::String => Ok(key.as_str().unwrap_or_default().to_owned()),
        ValueKind::None => Ok("null".to_owned()),
        ValueKind::Bool => Ok(if truth(key) { "true" } else { "false" }.to_owned()),
        ValueKind::Number if key.is_integer() => Ok(key.to_string()),
        ValueKind::Number => f64::try_from(key.clone())
            .map(json_float)
            .map_err(|_| refuse("tojson: a key that is not a number json.dumps can write")),
        _ => Err(refuse("tojson: keys must be str, int, float, bool or None")),
    }
}

fn newline(style: &JsonStyle, depth: usize, out: &mut String) {
    if let Some(indent) = &style.indent {
        out.push('\n');
        for _ in 0..depth {
            out.push_str(indent);
        }
    }
}

fn json_list(
    value: &Value,
    style: &JsonStyle,
    depth: usize,
    out: &mut String,
) -> Result<(), Error> {
    let all = items(value)?;
    if all.is_empty() {
        out.push_str("[]");
        return Ok(());
    }
    out.push('[');
    for (n, item) in all.iter().enumerate() {
        if n > 0 {
            out.push_str(&style.item);
        }
        newline(style, depth + 1, out);
        json_into(item, style, depth + 1, out)?;
    }
    newline(style, depth, out);
    out.push(']');
    Ok(())
}

fn json_dict(
    value: &Value,
    style: &JsonStyle,
    depth: usize,
    out: &mut String,
) -> Result<(), Error> {
    let mut pairs = Vec::new();
    for key in items(value)? {
        pairs.push((json_key(&key)?, value.get_item(&key)?));
    }
    if pairs.is_empty() {
        out.push_str("{}");
        return Ok(());
    }
    if style.sort {
        // Python compares the keys as strings, by code point; so does `str`'s `Ord`.
        pairs.sort_by(|a, b| a.0.cmp(&b.0));
    }
    out.push('{');
    for (n, (key, item)) in pairs.iter().enumerate() {
        if n > 0 {
            out.push_str(&style.item);
        }
        newline(style, depth + 1, out);
        json_str(key, style.ascii, out);
        out.push_str(&style.key);
        json_into(item, style, depth + 1, out)?;
    }
    newline(style, depth, out);
    out.push('}');
    Ok(())
}

/// `json.dumps(value, ...)`, appended to `out`. A value JSON has no form for (an undefined
/// name, a macro) is an error, as it is a `TypeError` in Python.
pub(crate) fn json_into(
    value: &Value,
    style: &JsonStyle,
    depth: usize,
    out: &mut String,
) -> Result<(), Error> {
    match value.kind() {
        ValueKind::None => out.push_str("null"),
        ValueKind::Bool => out.push_str(if truth(value) { "true" } else { "false" }),
        ValueKind::Number if value.is_integer() => out.push_str(&value.to_string()),
        ValueKind::Number => {
            let x = f64::try_from(value.clone())
                .map_err(|_| refuse("tojson: a number json.dumps cannot write"))?;
            out.push_str(&json_float(x));
        }
        ValueKind::String => json_str(value.as_str().unwrap_or_default(), style.ascii, out),
        ValueKind::Seq => json_list(value, style, depth, out)?,
        ValueKind::Map => json_dict(value, style, depth, out)?,
        kind => {
            return Err(refuse(&format!("tojson: {kind} is not JSON serializable")));
        }
    }
    Ok(())
}

/// Python's `str.isspace`: Unicode white space, and the four information separators
/// (U+001C to U+001F) that Python counts and Unicode's property does not.
pub(crate) fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats_print_as_python_repr() {
        let cases = [
            (1.0, "1.0"),
            (0.1, "0.1"),
            (-0.0, "-0.0"),
            (1e-5, "1e-05"),
            (0.0001, "0.0001"),
            (1.5e-7, "1.5e-07"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (1.234_567_89e22, "1.23456789e+22"),
            (123.456, "123.456"),
            (5e-324, "5e-324"),
            (1.797_693_134_862_315_7e308, "1.7976931348623157e+308"),
        ];
        for (x, text) in cases {
            assert_eq!(float(x), text);
        }
    }

    #[test]
    fn strings_print_as_python_repr() {
        let cases = [
            ("plain", "'plain'"),
            ("it's", "\"it's\""),
            ("both ' and \"", "'both \\' and \"'"),
            ("line\nbreak\ttab\\", "'line\\nbreak\\ttab\\\\'"),
            ("nbsp\u{a0}zwsp\u{200b}", "'nbsp\\xa0zwsp\\u200b'"),
            ("e\u{301} 日本 😀", "'e\u{301} 日本 😀'"),
            ("\u{7f}\u{1}", "'\\x7f\\x01'"),
        ];
        for (text, expected) in cases {
            let mut out = String::new();
            repr_str(text, &mut out);
            assert_eq!(out, expected);
        }
    }
}
