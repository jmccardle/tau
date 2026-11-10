//! What this crate refuses, by what was wrong with the input.

use std::fmt;

/// Why a tokenizer, a template, an identity or a count could not be had. Each variant carries
/// the sentence a person reads; nothing here is a fallback for another.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// The bytes are not a `tokenizer.json`, the name is not an embedded encoding, or the
    /// tokenizer could not encode the text.
    Tokenizer(String),
    /// The chat template does not parse, cannot be rendered with these messages, reads a clock
    /// it was not given, or raised (`raise_exception`); or the messages are not the JSON a
    /// template is given.
    Template(String),
    /// A tokenizer identity that is none of `tiktoken:NAME`, `hf:OWNER/REPO[@REVISION]` and
    /// `file:/abs/tokenizer.json`.
    Identity(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Tokenizer(text) | Self::Template(text) | Self::Identity(text) => {
                f.write_str(text)
            }
        }
    }
}

impl std::error::Error for Error {}
