//! A tokenizer's identity: the string a model's `tokenizer` setting holds and a calibration
//! record keeps. Parsing only; nothing here opens a file or a connection.

use std::fmt;
use std::path::Path;

use crate::error::Error;

/// Which tokenizer, and so where its tables come from.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum TokenizerId {
    /// `tiktoken:NAME`: an encoding embedded in this crate.
    Tiktoken(String),
    /// `hf:OWNER/REPO` or `hf:OWNER/REPO@REVISION`: a Hugging Face repository's
    /// `tokenizer.json`, at a commit, tag or branch when one is named.
    Hf {
        repo: String,
        revision: Option<String>,
    },
    /// `file:/abs/tokenizer.json`: a file on this machine.
    File(String),
}

/// One part of a repository's name, or a revision: letters, digits, `.`, `_` and `-`, not
/// starting with a `.` or `-`. A name is used as a directory and in a URL, so nothing that
/// could walk out of either is let through.
fn plain(part: &str) -> bool {
    let allowed = |c: char| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-');
    part.chars().all(allowed) && part.starts_with(|c: char| c.is_ascii_alphanumeric())
}

fn hf(rest: &str, whole: &str) -> Result<TokenizerId, Error> {
    let bad = |why: &str| Error::Identity(format!("{whole:?}: {why}"));
    let (repo, revision) = match rest.split_once('@') {
        Some((repo, revision)) => (repo, Some(revision)),
        None => (rest, None),
    };
    match repo.split_once('/') {
        Some((owner, name)) if plain(owner) && plain(name) => {}
        _ => {
            return Err(bad(
                "a Hugging Face tokenizer is hf:OWNER/REPO or hf:OWNER/REPO@REVISION",
            ));
        }
    }
    if revision.is_some_and(|revision| !plain(revision)) {
        return Err(bad(
            "the revision after @ must be a commit, a tag or a branch name",
        ));
    }
    Ok(TokenizerId::Hf {
        repo: repo.to_owned(),
        revision: revision.map(str::to_owned),
    })
}

impl TokenizerId {
    /// Read an identity string.
    ///
    /// # Errors
    /// [`Error::Identity`], saying which form was expected, when the string is none of the
    /// three; `none` (a setting that turns tokenizers off) is not an identity.
    pub fn parse(text: &str) -> Result<Self, Error> {
        let bad = |why: &str| Error::Identity(format!("{text:?}: {why}"));
        match text.split_once(':') {
            Some(("tiktoken", name)) if plain(name) => Ok(Self::Tiktoken(name.to_owned())),
            Some(("tiktoken", _)) => Err(bad("a tiktoken encoding is tiktoken:NAME")),
            Some(("hf", rest)) => hf(rest, text),
            Some(("file", path)) if Path::new(path).is_absolute() => {
                Ok(Self::File(path.to_owned()))
            }
            Some(("file", _)) => Err(bad(
                "a tokenizer file is file:/abs/tokenizer.json, an absolute path",
            )),
            _ => Err(bad(
                "a tokenizer is tiktoken:NAME, hf:OWNER/REPO[@REVISION] or file:/abs/tokenizer.json",
            )),
        }
    }
}

impl fmt::Display for TokenizerId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Tiktoken(name) => write!(f, "tiktoken:{name}"),
            Self::Hf {
                repo,
                revision: Some(revision),
            } => write!(f, "hf:{repo}@{revision}"),
            Self::Hf {
                repo,
                revision: None,
            } => write!(f, "hf:{repo}"),
            Self::File(path) => write!(f, "file:{path}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_three_forms_read_and_print_back() {
        let absolute = if cfg!(windows) {
            "file:C:\\models\\tokenizer.json"
        } else {
            "file:/models/tokenizer.json"
        };
        for text in [
            "tiktoken:o200k_base",
            "hf:Qwen/Qwen3-8B",
            "hf:Qwen/Qwen3-8B@b968826d9c46",
            absolute,
        ] {
            assert_eq!(TokenizerId::parse(text).unwrap().to_string(), text);
        }
        assert_eq!(
            TokenizerId::parse("hf:Qwen/Qwen3-8B@main").unwrap(),
            TokenizerId::Hf {
                repo: "Qwen/Qwen3-8B".into(),
                revision: Some("main".into())
            }
        );
    }

    #[test]
    fn anything_else_is_refused_with_the_form_expected() {
        for text in [
            "none",
            "o200k_base",
            "tiktoken:",
            "hf:Qwen3-8B",
            "hf:Qwen/Qwen3/8B",
            "hf:../x",
            "hf:Qwen/..",
            "hf:Qwen/Qwen3-8B@",
            "hf:Qwen/Qwen3-8B@refs/pr/1",
            "file:tokenizer.json",
            "https://huggingface.co/Qwen/Qwen3-8B",
        ] {
            let Err(Error::Identity(message)) = TokenizerId::parse(text) else {
                panic!("{text} was read as an identity");
            };
            assert!(message.contains(text), "{message}");
        }
    }
}
