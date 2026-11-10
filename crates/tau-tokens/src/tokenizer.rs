//! A tokenizer: text in, token ids or a count out. Two kinds, one interface: `OpenAI`'s encodings
//! (tables embedded in this crate, read through `tiktoken-rs`) and anything Hugging Face's
//! `tokenizers` loads from the bytes of a `tokenizer.json`.
//!
//! The count of a text is always the length of its encoding. For a Hugging Face tokenizer it is
//! reached a faster way (the memo, below), which runs the library's own steps on the library's
//! own pieces; the tests hold the two equal.

use std::sync::{Mutex, PoisonError};

use tiktoken_rs::CoreBPE;
use tokenizers::tokenizer::{
    Model, NormalizedString, OffsetReferential, OffsetType, PreTokenizedString, PreTokenizer,
};

use crate::error::Error;
use crate::memo::{Memo, Piece};

/// The embedded encodings, each with the number of token ids it has (tiktoken's `n_vocab`).
const EMBEDDED: [(&str, usize); 2] = [("o200k_base", 200_019), ("cl100k_base", 100_277)];

enum Kind {
    Tiktoken { bpe: &'static CoreBPE, vocab: usize },
    Hf(Box<tokenizers::Tokenizer>),
}

/// A tokenizer and the memo of what it has counted.
pub struct Tokenizer {
    kind: Kind,
    memo: Mutex<Memo>,
}

impl std::fmt::Debug for Tokenizer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let kind = match self.kind {
            Kind::Tiktoken { .. } => "tiktoken",
            Kind::Hf(_) => "hf",
        };
        f.debug_struct("Tokenizer")
            .field("kind", &kind)
            .field("vocab", &self.vocab_size())
            .field("memo", &self.memo().work())
            .finish()
    }
}

fn failed(err: impl std::fmt::Display) -> Error {
    Error::Tokenizer(format!("the tokenizer could not encode the text: {err}"))
}

/// The tokens one piece of the text becomes: the tokenizer's pre-tokenizer, then its model, as
/// `encode` runs them on the same piece.
fn piece_tokens(inner: &tokenizers::Tokenizer, piece: NormalizedString) -> Result<usize, Error> {
    let mut pre = PreTokenizedString::from(piece);
    if let Some(pre_tokenizer) = inner.get_pre_tokenizer() {
        pre_tokenizer.pre_tokenize(&mut pre).map_err(failed)?;
    }
    inner
        .get_model()
        .tokenize_in_pretokenized(&mut pre, None)
        .map_err(failed)?;
    Ok(tokens_in(&pre))
}

/// How many tokens the tokenized pieces of a text hold.
fn tokens_in(pre: &PreTokenizedString) -> usize {
    pre.get_splits(OffsetReferential::Original, OffsetType::Byte)
        .iter()
        .map(|(_, _, tokens)| tokens.as_ref().map_or(0, Vec::len))
        .sum()
}

impl Tokenizer {
    /// One of the embedded `OpenAI` encodings: `o200k_base` or `cl100k_base`.
    ///
    /// # Errors
    /// [`Error::Tokenizer`] for any other name.
    pub fn tiktoken(name: &str) -> Result<Self, Error> {
        let (bpe, vocab) = match name {
            "o200k_base" => (tiktoken_rs::o200k_base_singleton(), EMBEDDED[0].1),
            "cl100k_base" => (tiktoken_rs::cl100k_base_singleton(), EMBEDDED[1].1),
            _ => {
                let known: Vec<&str> = EMBEDDED.iter().map(|(name, _)| *name).collect();
                return Err(Error::Tokenizer(format!(
                    "no embedded encoding named {name:?} (there are: {})",
                    known.join(", ")
                )));
            }
        };
        Ok(Self {
            kind: Kind::Tiktoken { bpe, vocab },
            memo: Mutex::default(),
        })
    }

    /// A Hugging Face tokenizer from the bytes of its `tokenizer.json`. Truncation and padding
    /// that the file asks for are turned off: they would change a count.
    ///
    /// # Errors
    /// [`Error::Tokenizer`] when the bytes are not a `tokenizer.json` this library reads.
    pub fn from_json(bytes: &[u8]) -> Result<Self, Error> {
        let not_one = |err: &dyn std::fmt::Display| {
            Error::Tokenizer(format!("not a tokenizer.json this reads: {err}"))
        };
        let mut inner = tokenizers::Tokenizer::from_bytes(bytes).map_err(|e| not_one(&e))?;
        inner.with_truncation(None).map_err(|e| not_one(&e))?;
        inner.with_padding(None);
        Ok(Self {
            kind: Kind::Hf(Box::new(inner)),
            memo: Mutex::default(),
        })
    }

    /// The token ids of `text`. Nothing is added around it (no BOS, no EOS). A Hugging Face
    /// tokenizer finds its added and special tokens in the text, as it does for a server
    /// encoding a rendered request; a tiktoken encoding reads the text as ordinary text, so
    /// `<|endoftext|>` typed by a person is several tokens.
    ///
    /// # Errors
    /// [`Error::Tokenizer`] when the tokenizer fails on the text.
    pub fn encode(&self, text: &str) -> Result<Vec<u32>, Error> {
        match &self.kind {
            Kind::Tiktoken { bpe, .. } => Ok(bpe.encode_ordinary(text)),
            Kind::Hf(inner) => {
                let encoding = inner.encode(text, false).map_err(failed)?;
                Ok(encoding.get_ids().to_vec())
            }
        }
    }

    /// How many tokens `text` is: always `encode(text).len()`. A Hugging Face tokenizer splits
    /// a text at its added tokens before anything else, and counts each piece between them on
    /// its own; the pieces' counts are remembered, so counting a conversation again after one
    /// message was added tokenizes only that message.
    ///
    /// # Errors
    /// [`Error::Tokenizer`] when the tokenizer fails on the text.
    pub fn count(&self, text: &str) -> Result<usize, Error> {
        let inner = match &self.kind {
            Kind::Tiktoken { bpe, .. } => return Ok(bpe.encode_ordinary(text).len()),
            Kind::Hf(inner) => inner,
        };
        let mut split = inner
            .get_added_vocabulary()
            .extract_and_normalize(inner.get_normalizer(), text);
        // The added tokens are tokenized already; what is left is the text between them.
        let mut total = tokens_in(&split);
        let mut pieces = Vec::new();
        split
            .split(|_, piece| {
                pieces.push(piece);
                Ok(Vec::<NormalizedString>::new())
            })
            .map_err(failed)?;
        self.memo().begin();
        for normalized in pieces {
            let piece = Piece {
                text: normalized.get().to_owned(),
                first: normalized.offsets_original().0 == 0,
            };
            let known = self.memo().get(&piece);
            total += if let Some(tokens) = known {
                tokens
            } else {
                let tokens = piece_tokens(inner, normalized)?;
                self.memo().put(piece, tokens);
                tokens
            };
        }
        self.memo().end();
        Ok(total)
    }

    /// How many token ids the tokenizer has, added tokens included.
    #[must_use]
    pub fn vocab_size(&self) -> usize {
        match &self.kind {
            Kind::Tiktoken { vocab, .. } => *vocab,
            Kind::Hf(inner) => inner.get_vocab_size(true),
        }
    }

    /// The work `count` has done so far: pieces of text tokenized, and pieces whose count was
    /// read from the memo instead.
    #[must_use]
    pub fn work(&self) -> (u64, u64) {
        self.memo().work()
    }

    fn memo(&self) -> std::sync::MutexGuard<'_, Memo> {
        // A count that panicked left the memo with whole pieces only: nothing in it is wrong.
        self.memo.lock().unwrap_or_else(PoisonError::into_inner)
    }
}
