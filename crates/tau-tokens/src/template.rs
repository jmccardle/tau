//! A chat template rendered the way a server renders it. The reference is `transformers`'
//! `apply_chat_template`: Jinja2 with `trim_blocks` and `lstrip_blocks`, loop controls, its own
//! `tojson`, `raise_exception` and `strftime_now`, the tokenizer's named special tokens as
//! variables, and `tools` and `documents` always defined (`None` when there are none). vLLM calls
//! that function; llama.cpp's own renderer follows it. `MiniJinja` is the engine here, set up
//! in `pyjinja` to print and behave as Python does.

use minijinja::Environment;
use minijinja::value::{Value, ValueKind};

use crate::error::Error;
use crate::now::Now;
use crate::pyjinja::{Failure, environment, refuse};
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

/// A model's chat template and the special tokens it names: what turns a request's messages and
/// tools into the text the model is given.
#[derive(Debug)]
pub struct ChatTemplate {
    env: Environment<'static>,
    /// Whether the model has a second template for requests with tools.
    tool_use: bool,
    tokens: Vec<(String, String)>,
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
