//! A chat template rendered the way a server renders it. The reference is `transformers`'
//! `apply_chat_template`: Jinja2 with `trim_blocks` and `lstrip_blocks`, loop controls, its own
//! `tojson`, `raise_exception` and `strftime_now`, the tokenizer's named special tokens as
//! variables, and `tools` and `documents` always defined (`None` when there are none). vLLM calls
//! that function; llama.cpp's own renderer follows it. `MiniJinja` is the engine here, with
//! Python's printing (`pytext`) and Python's string methods put in where the two differ.

use minijinja::syntax::SyntaxConfig;
use minijinja::value::{Kwargs, Rest, Tuple, Value, ValueKind};
use minijinja::{AutoEscape, Environment, ErrorKind, State};

use crate::error::Error;
use crate::now::Now;
use crate::pytext::{self, JsonStyle};
use crate::values;

/// The special tokens `apply_chat_template` hands a template by name.
const TOKEN_NAMES: [&str; 7] = [
    "bos_token",
    "eos_token",
    "unk_token",
    "sep_token",
    "pad_token",
    "cls_token",
    "mask_token",
];
/// A message's reasoning, under the names templates read it by; `transformers` fills in the
/// first and second from whichever is present.
const REASONING: [&str; 3] = ["reasoning_content", "thinking", "reasoning"];
/// What the caller's other variables may not replace: they are arguments of their own.
const RESERVED: [&str; 3] = ["messages", "tools", "add_generation_prompt"];
const DEFAULT: &str = "default";
const TOOL_USE: &str = "tool_use";

type Failure = minijinja::Error;

fn refuse(text: String) -> Failure {
    Failure::new(ErrorKind::InvalidOperation, text)
}

/// A model's chat template and the special tokens it names: what turns a request's messages and
/// tools into the text the model is given.
#[derive(Debug)]
pub struct ChatTemplate {
    env: Environment<'static>,
    /// Whether the model has a second template for requests with tools.
    tool_use: bool,
    tokens: Vec<(String, String)>,
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
    let indent = match option(1, "indent")? {
        Some(v) if v.kind() == ValueKind::String => v.as_str().map(str::to_owned),
        Some(v) if v.kind() == ValueKind::Bool => Some(" ".repeat(usize::from(v.is_true()))),
        Some(v) if v.is_integer() => Some(" ".repeat(v.as_usize().unwrap_or(0))),
        Some(v) if !v.is_none() => {
            return Err(refuse("tojson: indent must be an int or a str".into()));
        }
        _ => None,
    };
    let (item, key) = match option(2, "separators")? {
        Some(v) if !v.is_none() => {
            let both: Vec<Value> = v.try_iter()?.collect();
            match (
                both.first().and_then(Value::as_str),
                both.get(1).and_then(Value::as_str),
            ) {
                (Some(item), Some(key)) if both.len() == 2 => (item.to_owned(), key.to_owned()),
                _ => return Err(refuse("tojson: separators must be two strings".into())),
            }
        }
        _ if indent.is_some() => (",".to_owned(), ": ".to_owned()),
        _ => (", ".to_owned(), ": ".to_owned()),
    };
    let sort = option(3, "sort_keys")?.is_some_and(|v| v.is_true());
    kwargs.assert_all_used()?;
    let mut out = String::new();
    pytext::json_into(
        value,
        &JsonStyle {
            ascii,
            indent,
            item,
            key,
            sort,
        },
        0,
        &mut out,
    )?;
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
        let cased = c.is_lowercase() || c.is_uppercase();
        if after_letter {
            out.extend(c.to_lowercase());
        } else {
            out.extend(c.to_uppercase());
        }
        after_letter = cased;
    }
    out
}

/// `str.splitlines`: Python ends a line at more than `\n` (a form feed, a line separator, ...).
fn splitlines(text: &str, keep_ends: bool) -> Vec<Value> {
    let ends = |c: char| {
        matches!(
            c,
            '\n' | '\r' | '\u{b}' | '\u{c}' | '\u{1c}'
                ..='\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}'
        )
    };
    let mut lines = Vec::new();
    let mut rest = text;
    while let Some(at) = rest.find(ends) {
        let mut end = at + rest[at..].chars().next().map_or(1, char::len_utf8);
        if rest[at..].starts_with("\r\n") {
            end += 1;
        }
        lines.push(Value::from(if keep_ends {
            &rest[..end]
        } else {
            &rest[..at]
        }));
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

/// A method call on a value: the three strips here, the rest of Python's string, list and dict
/// methods from `minijinja-contrib`.
fn method(state: &mut State, value: &Value, name: &str, args: &[Value]) -> Result<Value, Failure> {
    let sides = match name {
        "strip" => Some((true, true)),
        "lstrip" => Some((true, false)),
        "rstrip" => Some((false, true)),
        _ => None,
    };
    if let (Some(text), Some((left, right)), true) = (value.as_str(), sides, args.len() <= 1) {
        let chars = args.first().filter(|v| !v.is_none());
        return match chars.map(Value::as_str) {
            Some(None) => Err(refuse(format!(
                "{name}: the characters to strip must be a string"
            ))),
            Some(set) => Ok(Value::from(strip(text, set, left, right))),
            None => Ok(Value::from(strip(text, None, left, right))),
        };
    }
    match (value.as_str(), name, args) {
        (Some(text), "title", []) => return Ok(Value::from(title(text))),
        (Some(text), "splitlines", []) => return Ok(Value::from(splitlines(text, false))),
        (Some(text), "splitlines", [keep]) => {
            return Ok(Value::from(splitlines(text, keep.is_true())));
        }
        _ => {}
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

fn environment() -> Environment<'static> {
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

fn template_error(err: &Failure) -> Error {
    let mut text = err.to_string();
    let mut source = std::error::Error::source(err);
    while let Some(inner) = source {
        text.push_str(": ");
        text.push_str(&inner.to_string());
        source = inner.source();
    }
    Error::Template(text)
}

/// A special token of `tokenizer_config.json`: its text, or the `content` of the object that
/// describes it.
fn token_text(value: &Value) -> Option<String> {
    match value.kind() {
        ValueKind::String => value.as_str().map(str::to_owned),
        ValueKind::Map => value.get_attr("content").ok()?.as_str().map(str::to_owned),
        _ => None,
    }
}

/// The templates of a `tokenizer_config.json`, by name: one string is the default; a list
/// names each (`default`, `tool_use`).
fn config_templates(config: &Value) -> Result<Vec<(String, String)>, Error> {
    let missing = || Error::Template("tokenizer_config.json has no chat_template".to_owned());
    let bad = || Error::Template("tokenizer_config.json: chat_template is not a template".into());
    let found = config.get_attr("chat_template").map_err(|_| bad())?;
    match found.kind() {
        ValueKind::String => Ok(vec![(DEFAULT.to_owned(), found.to_string())]),
        ValueKind::Seq => {
            let mut out = Vec::new();
            for item in found.try_iter().map_err(|_| bad())? {
                let name = item
                    .get_attr("name")
                    .ok()
                    .and_then(|v| v.as_str().map(str::to_owned));
                let text = item
                    .get_attr("template")
                    .ok()
                    .and_then(|v| v.as_str().map(str::to_owned));
                out.push(name.zip(text).ok_or_else(bad)?);
            }
            Ok(out)
        }
        ValueKind::Undefined | ValueKind::None => Err(missing()),
        _ => Err(bad()),
    }
}

/// One message with its reasoning under every name a template may read it by, as
/// `transformers` passes it: `thinking` first, the message's own keys, `reasoning_content` last.
fn with_reasoning(message: &Value) -> Result<Value, Failure> {
    if message.kind() != ValueKind::Map {
        return Err(refuse("each message must be an object".to_owned()));
    }
    let own = values::pairs(message)?;
    let reasoning = REASONING.iter().find_map(|name| {
        own.iter()
            .find(|(k, v)| k == name && !v.is_none())
            .map(|(_, v)| v.clone())
    });
    let Some(reasoning) = reasoning else {
        return Ok(message.clone());
    };
    let mut out = vec![("thinking".to_owned(), reasoning.clone())];
    for (key, value) in own {
        match out.iter_mut().find(|(k, _)| *k == key) {
            Some(pair) => pair.1 = value,
            None => out.push((key, value)),
        }
    }
    match out.iter_mut().find(|(k, _)| k == "reasoning_content") {
        Some(pair) => pair.1 = reasoning,
        None => out.push(("reasoning_content".to_owned(), reasoning)),
    }
    Ok(Value::from_pairs(out))
}

fn messages_value(bytes: &[u8]) -> Result<Value, Error> {
    let parsed = values::parse(bytes, "messages")?;
    if parsed.kind() != ValueKind::Seq {
        return Err(Error::Template("messages: must be a JSON array".to_owned()));
    }
    let mut out = Vec::new();
    for message in parsed.try_iter().map_err(|e| template_error(&e))? {
        out.push(with_reasoning(&message).map_err(|e| template_error(&e))?);
    }
    Ok(Value::from(out))
}

impl ChatTemplate {
    fn build(
        templates: Vec<(String, String)>,
        tokens: Vec<(String, String)>,
    ) -> Result<Self, Error> {
        let mut env = environment();
        let tool_use = templates.iter().any(|(name, _)| name == TOOL_USE);
        for (name, source) in templates {
            env.add_template_owned(name, source)
                .map_err(|e| template_error(&e))?;
        }
        Ok(Self {
            env,
            tool_use,
            tokens,
        })
    }

    /// A template from its source text, with the special tokens it may name (`bos_token`,
    /// `eos_token`, ...) given as pairs.
    ///
    /// # Errors
    /// [`Error::Template`] when the source does not parse.
    pub fn new(source: &str, tokens: &[(String, String)]) -> Result<Self, Error> {
        Self::build(
            vec![(DEFAULT.to_owned(), source.to_owned())],
            tokens.to_vec(),
        )
    }

    /// A template from the bytes of a `tokenizer_config.json`: its `chat_template` (one, or
    /// several by name) and its special tokens. `source`, when given, is the template instead
    /// (a repository's `chat_template.jinja`, or a server's own), and the file gives the tokens.
    ///
    /// # Errors
    /// [`Error::Template`] when the bytes are not that file, when there is no template, or when
    /// one does not parse.
    pub fn from_config(config: &[u8], source: Option<&str>) -> Result<Self, Error> {
        let config = values::parse_file(config, "tokenizer_config.json")?;
        if config.kind() != ValueKind::Map {
            return Err(Error::Template(
                "tokenizer_config.json: must be a JSON object".to_owned(),
            ));
        }
        let tokens = TOKEN_NAMES
            .iter()
            .filter_map(|name| {
                let text = token_text(&config.get_attr(name).ok()?)?;
                Some(((*name).to_owned(), text))
            })
            .collect();
        let templates = match source {
            Some(source) => vec![(DEFAULT.to_owned(), source.to_owned())],
            None => config_templates(&config)?,
        };
        Self::build(templates, tokens)
    }

    /// The text the model is given for a request. `messages` is a JSON array of message
    /// objects and `tools` a JSON array of tool definitions, both as the chat API takes them;
    /// `add_generation_prompt` ends the text where the reply begins; `extra` is a JSON object of
    /// more variables for the template (`enable_thinking`, `reasoning_effort`); `now` is what
    /// `strftime_now` prints.
    ///
    /// # Errors
    /// [`Error::Template`] when an input is not that JSON, when the template fails on it or
    /// refuses it (`raise_exception`), and when it reads the clock and `now` is `None`.
    pub fn render(
        &self,
        messages: &[u8],
        tools: Option<&[u8]>,
        add_generation_prompt: bool,
        extra: Option<&[u8]>,
        now: Option<Now>,
    ) -> Result<String, Error> {
        let tools = tools
            .map(|bytes| values::parse(bytes, "tools"))
            .transpose()?;
        let name = if tools.is_some() && self.tool_use {
            TOOL_USE
        } else {
            DEFAULT
        };
        let template = self.env.get_template(name).map_err(|_| {
            Error::Template("the model has several chat templates and none is `default`".to_owned())
        })?;
        let clock = Value::from_function(move |format: &str| -> Result<String, Failure> {
            let now =
                now.ok_or_else(|| refuse("the template reads the clock; pass `now`".into()))?;
            now.strftime(format).map_err(|err| refuse(err.to_string()))
        });
        let mut context: Vec<(String, Value)> = vec![
            ("messages".to_owned(), messages_value(messages)?),
            ("tools".to_owned(), tools.unwrap_or_else(|| Value::from(()))),
            ("documents".to_owned(), Value::from(())),
            (
                "add_generation_prompt".to_owned(),
                Value::from(add_generation_prompt),
            ),
            ("strftime_now".to_owned(), clock),
        ];
        context.extend(
            self.tokens
                .iter()
                .map(|(k, v)| (k.clone(), Value::from(v.as_str()))),
        );
        context.extend(extra_variables(extra)?);
        let context = Value::from_pairs(context);
        template.render(context).map_err(|e| template_error(&e))
    }
}

/// The caller's own variables for the template, from a JSON object.
fn extra_variables(extra: Option<&[u8]>) -> Result<Vec<(String, Value)>, Error> {
    let Some(bytes) = extra else {
        return Ok(Vec::new());
    };
    let parsed = values::parse(bytes, "extra")?;
    if parsed.kind() != ValueKind::Map {
        return Err(Error::Template("extra: must be a JSON object".to_owned()));
    }
    let all = values::pairs(&parsed).map_err(|e| template_error(&e))?;
    if let Some((name, _)) = all
        .iter()
        .find(|(name, _)| RESERVED.contains(&name.as_str()))
    {
        return Err(Error::Template(format!(
            "extra: `{name}` is an argument of its own"
        )));
    }
    Ok(all)
}
