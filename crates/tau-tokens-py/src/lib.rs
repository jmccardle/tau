//! Python bindings for `tau-tokens`, built as `tau_tokens._core` (the wheel `ffwf-tau-tokens`).
//! Thin by rule: conversion and error mapping only. Behavior lives in `tau-tokens`, where it is
//! tested without an interpreter.
//!
//! The module speaks plain Python values: `str`, `bytes`, `int`, lists and dicts as the chat API
//! has them. Messages and tools cross as JSON text, written by Python's own `json.dumps`, so the
//! template sees what a server sees. Counting, encoding and rendering release the interpreter.

use std::collections::HashMap;

use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use tau_tokens as core;

create_exception!(
    tau_tokens._core,
    TokenizerError,
    PyValueError,
    "Bytes that are not a `tokenizer.json`, a name that is not an embedded encoding, or text a tokenizer could not encode."
);
create_exception!(
    tau_tokens._core,
    TemplateError,
    PyValueError,
    "A chat template that does not parse, cannot render the request, reads a clock it was not given, or refused the request (`raise_exception`); or a request that is not what a template is given."
);
create_exception!(
    tau_tokens._core,
    IdentityError,
    PyValueError,
    "A tokenizer identity that is none of `tiktoken:NAME`, `hf:OWNER/REPO[@REVISION]` and `file:/abs/tokenizer.json`."
);

fn raised(err: core::Error) -> PyErr {
    match err {
        core::Error::Tokenizer(text) => TokenizerError::new_err(text),
        core::Error::Template(text) => TemplateError::new_err(text),
        core::Error::Identity(text) => IdentityError::new_err(text),
    }
}

/// A tokenizer: an embedded `OpenAI` encoding, or a Hugging Face `tokenizer.json`.
///
/// `count(text)` is always `len(encode(text))`. Nothing is added around the text (no BOS).
/// A Hugging Face tokenizer finds its special tokens in the text, as a server does when it
/// encodes a rendered request; a tiktoken encoding reads them as ordinary text. One tokenizer
/// may be used from several threads at once.
#[pyclass(frozen, module = "tau_tokens._core")]
#[derive(Debug)]
struct Tokenizer {
    inner: core::Tokenizer,
}

#[pymethods]
impl Tokenizer {
    /// One of the embedded encodings: `o200k_base` or `cl100k_base`. Any other name is a
    /// `TokenizerError`.
    #[staticmethod]
    fn tiktoken(py: Python<'_>, name: &str) -> PyResult<Self> {
        // The first use of an encoding builds its table, which takes a moment.
        let inner = py
            .detach(|| core::Tokenizer::tiktoken(name))
            .map_err(raised)?;
        Ok(Self { inner })
    }

    /// A Hugging Face tokenizer from the bytes of its `tokenizer.json`. Truncation and padding
    /// the file asks for are turned off. Bytes that are not such a file are a `TokenizerError`.
    #[staticmethod]
    fn from_json(py: Python<'_>, data: &[u8]) -> PyResult<Self> {
        let inner = py
            .detach(|| core::Tokenizer::from_json(data))
            .map_err(raised)?;
        Ok(Self { inner })
    }

    /// The token ids of `text`.
    fn encode(&self, py: Python<'_>, text: &str) -> PyResult<Vec<u32>> {
        py.detach(|| self.inner.encode(text)).map_err(raised)
    }

    /// How many tokens `text` is. A Hugging Face tokenizer remembers the count of every piece
    /// of text between two special tokens, so counting a conversation again after a message
    /// was added tokenizes only that message.
    fn count(&self, py: Python<'_>, text: &str) -> PyResult<usize> {
        py.detach(|| self.inner.count(text)).map_err(raised)
    }

    /// How many token ids the tokenizer has, added tokens included.
    #[getter]
    fn vocab_size(&self) -> usize {
        self.inner.vocab_size()
    }

    /// The work `count` has done so far, as `(tokenized, reused)`: pieces of text it tokenized,
    /// and pieces whose count it remembered.
    fn work(&self) -> (u64, u64) {
        self.inner.work()
    }
}

/// `json.dumps(value)`: the JSON text of a request's messages, tools or extra variables.
fn json_text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    let kwargs = PyDict::new(value.py());
    kwargs.set_item("ensure_ascii", false)?;
    kwargs.set_item("allow_nan", false)?;
    value
        .py()
        .import("json")?
        .call_method("dumps", (value,), Some(&kwargs))?
        .extract()
}

/// A `datetime.datetime` as the time a template's `strftime_now` prints.
fn clock(now: &Bound<'_, PyAny>) -> PyResult<core::Now> {
    let field = |name: &str| -> PyResult<u32> { now.getattr(name)?.extract() };
    let year: i32 = now.getattr("year")?.extract()?;
    let time = (field("hour")?, field("minute")?, field("second")?);
    core::Now::new(year, field("month")?, field("day")?, time).map_err(raised)
}

/// A model's chat template: what turns a request's messages and tools into the text the model
/// is given, rendered as `transformers`' `apply_chat_template` renders it.
///
/// `ChatTemplate(source, tokens)` takes the template's text and the special tokens it may name
/// (`{"bos_token": "<s>", ...}`). A template that does not parse is a `TemplateError`.
#[pyclass(frozen, module = "tau_tokens._core")]
#[derive(Debug)]
struct ChatTemplate {
    inner: core::ChatTemplate,
}

#[pymethods]
impl ChatTemplate {
    #[new]
    #[pyo3(signature = (source, tokens = None))]
    fn new(source: &str, tokens: Option<HashMap<String, String>>) -> PyResult<Self> {
        let tokens: Vec<(String, String)> = tokens.unwrap_or_default().into_iter().collect();
        let inner = core::ChatTemplate::new(source, &tokens).map_err(raised)?;
        Ok(Self { inner })
    }

    /// A template from the bytes of a `tokenizer_config.json`: its `chat_template` and its
    /// special tokens. `source`, when given, is the template instead (a repository's
    /// `chat_template.jinja`, or the server's own), and the file gives only the tokens.
    #[staticmethod]
    #[pyo3(signature = (config, source = None))]
    fn from_config(config: &[u8], source: Option<&str>) -> PyResult<Self> {
        let inner = core::ChatTemplate::from_config(config, source).map_err(raised)?;
        Ok(Self { inner })
    }

    /// The text the model is given for a request.
    ///
    /// `messages` and `tools` are lists of dicts as the chat API takes them (a tool call's
    /// `arguments` as the server passes them to the template: most parse them into a dict).
    /// `add_generation_prompt` ends the text where the reply begins. `extra` is more variables
    /// for the template (`enable_thinking`, `reasoning_effort`). `now` is the
    /// `datetime.datetime` that `strftime_now` prints; a template that reads the clock without
    /// one is a `TemplateError`, as is a template that fails on the request or refuses it.
    #[pyo3(signature = (messages, tools = None, *, add_generation_prompt = true, extra = None, now = None))]
    fn render(
        &self,
        py: Python<'_>,
        messages: &Bound<'_, PyAny>,
        tools: Option<&Bound<'_, PyAny>>,
        add_generation_prompt: bool,
        extra: Option<&Bound<'_, PyAny>>,
        now: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<String> {
        let messages = json_text(messages)?;
        let tools = tools.map(json_text).transpose()?;
        let extra = extra.map(json_text).transpose()?;
        let now = now.map(clock).transpose()?;
        py.detach(|| {
            self.inner.render(
                messages.as_bytes(),
                tools.as_ref().map(String::as_bytes),
                add_generation_prompt,
                extra.as_ref().map(String::as_bytes),
                now,
            )
        })
        .map_err(raised)
    }
}

/// Read a tokenizer identity: `("tiktoken", name, None)`, `("hf", "OWNER/REPO", revision)` with
/// `revision` `None` when the identity names none, or `("file", path, None)`. Anything else is
/// an `IdentityError` that says which forms there are.
#[pyfunction]
fn parse_identity(text: &str) -> PyResult<(&'static str, String, Option<String>)> {
    Ok(match core::TokenizerId::parse(text).map_err(raised)? {
        core::TokenizerId::Tiktoken(name) => ("tiktoken", name, None),
        core::TokenizerId::Hf { repo, revision } => ("hf", repo, revision),
        core::TokenizerId::File(path) => ("file", path, None),
    })
}

/// The tokenizer catalogue: one dict per base tokenizer, with its `id` (an identity, pinned to
/// a commit when it is a Hugging Face repository), `vocab` (how many token ids it has),
/// `served` (other vocabulary sizes a server reports for it) and `models` (patterns over a
/// lowercased model id, `*` for any run of characters).
#[pyfunction]
fn catalogue(py: Python<'_>) -> PyResult<Bound<'_, PyList>> {
    let out = PyList::empty(py);
    for tokenizer in core::catalogue() {
        let item = PyDict::new(py);
        item.set_item("id", &tokenizer.id)?;
        item.set_item("vocab", tokenizer.vocab)?;
        item.set_item("served", &tokenizer.served)?;
        item.set_item("models", &tokenizer.models)?;
        out.append(item)?;
    }
    Ok(out)
}

/// The catalogue's candidates for a model, best first, as `(id, prior)`. With `n_vocab` (the
/// vocabulary size the server reports) only tokenizers of that size are kept, allowing for
/// padding. `prior` is how specific the catalogue's match of the model id is; 0 means the id
/// says nothing for that tokenizer.
#[pyfunction]
#[pyo3(signature = (model, n_vocab = None))]
fn candidates(model: &str, n_vocab: Option<usize>) -> Vec<(String, usize)> {
    let ranked = core::candidates(model, n_vocab);
    ranked
        .into_iter()
        .map(|(tokenizer, prior)| (tokenizer.id.clone(), prior))
        .collect()
}

/// What a server's counts say about a candidate tokenizer: `"candidate"`, `"confirmed"` or
/// `"dropped"`. Each pair is `(group, reported, counted)`: the tokens the server reported for
/// some text and the tokens the candidate counts in it; `group` names what must stay the same
/// for their difference to be a constant (a reply, or a mix of roles). Confirmed: at least
/// three pairs of one group with one difference, one of them of 200 tokens or more. Dropped:
/// two pairs of one group with different differences.
#[pyfunction]
fn verdict(pairs: Vec<(String, u64, u64)>) -> &'static str {
    let pairs: Vec<core::Pair> = pairs
        .into_iter()
        .map(|(group, reported, counted)| core::Pair {
            group,
            reported,
            counted,
        })
        .collect();
    match core::verdict(&pairs) {
        core::Verdict::Candidate => "candidate",
        core::Verdict::Confirmed => "confirmed",
        core::Verdict::Dropped => "dropped",
    }
}

#[pymodule(gil_used = false)]
mod _core {
    #[pymodule_export]
    use super::{ChatTemplate, Tokenizer, candidates, catalogue, parse_identity, verdict};

    #[pymodule_init]
    fn init(m: &pyo3::Bound<'_, pyo3::types::PyModule>) -> pyo3::PyResult<()> {
        use pyo3::types::PyModuleMethods;
        let py = m.py();
        m.add("TokenizerError", py.get_type::<super::TokenizerError>())?;
        m.add("TemplateError", py.get_type::<super::TemplateError>())?;
        m.add("IdentityError", py.get_type::<super::IdentityError>())
    }
}
