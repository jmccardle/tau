//! The tokenizer catalogue and fingerprinting (D26 §7.4): which tokenizers a model might use,
//! and whether the counts a server reported agree with one of them. All of it is arithmetic on
//! values the caller has; nothing here fetches a tokenizer or counts with one.

use std::collections::HashMap;
use std::sync::OnceLock;

use serde::Deserialize;

/// The catalogue's data, embedded.
const DATA: &str = include_str!("catalogue.json");
/// A server may pad a vocabulary up to a multiple of one of these.
const PADDING: [usize; 2] = [64, 128];
/// Pairs with one constant residual that confirm a candidate, and the size one of them must
/// reach (three one-word replies that agree prove little).
const CONFIRMING_PAIRS: usize = 3;
const LARGE_PAIR: u64 = 200;

/// One base tokenizer of the catalogue.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CatalogueTokenizer {
    /// Its identity (`tiktoken:o200k_base`, `hf:OWNER/REPO@COMMIT`), which says where to get it.
    pub id: String,
    /// How many token ids it has.
    pub vocab: usize,
    /// Other vocabulary sizes a server reports for it (a model's embedding size).
    pub served: Vec<usize>,
    /// Patterns over a lowercased model id, `*` for any run of characters.
    pub models: Vec<String>,
}

#[derive(Deserialize)]
struct File {
    #[serde(rename = "about")]
    _about: String,
    tokenizers: Vec<CatalogueTokenizer>,
}

/// The tokenizer catalogue, in its file's order.
///
/// # Panics
/// Only if the embedded file is not the catalogue's JSON, which the crate's tests rule out.
#[must_use]
pub fn catalogue() -> &'static [CatalogueTokenizer] {
    static PARSED: OnceLock<Vec<CatalogueTokenizer>> = OnceLock::new();
    PARSED.get_or_init(|| {
        let file: File = serde_json::from_str(DATA).expect("catalogue.json is the catalogue");
        file.tokenizers
    })
}

/// Whether `text` matches `pattern`, where `*` stands for any run of characters. Returns how
/// many characters the pattern spelled out, which is how specific the match is.
fn matched(pattern: &str, text: &str) -> Option<usize> {
    let parts: Vec<&str> = pattern.split('*').collect();
    let spelled = parts.iter().map(|part| part.chars().count()).sum();
    let [first, middle @ .., last] = parts.as_slice() else {
        return (pattern == text).then_some(spelled);
    };
    let mut rest = text.strip_prefix(first)?.strip_suffix(last)?;
    for part in middle {
        let at = rest.find(part)?;
        rest = &rest[at + part.len()..];
    }
    Some(spelled)
}

impl CatalogueTokenizer {
    /// Whether a server that reports `n_vocab` token ids could be using this tokenizer: its own
    /// size, one of the sizes known to be served, or its size padded to a multiple of 64 or 128.
    #[must_use]
    pub fn fits(&self, n_vocab: usize) -> bool {
        n_vocab == self.vocab
            || self.served.contains(&n_vocab)
            || PADDING
                .iter()
                .any(|unit| self.vocab.div_ceil(*unit) * unit == n_vocab)
    }

    /// The prior a model id gives this tokenizer: the length of the most specific of its
    /// patterns that matches the id, or 0 when none does.
    #[must_use]
    pub fn prior(&self, model: &str) -> usize {
        let model = model.to_lowercase();
        self.models
            .iter()
            .filter_map(|pattern| matched(pattern, &model))
            .max()
            .unwrap_or(0)
    }
}

/// The catalogue's candidates for a model, best first: every tokenizer that fits the server's
/// vocabulary size (all of them when the server reports none), each with its prior, ranked by
/// prior and then by the catalogue's order. A prior of 0 means the model id says nothing for
/// that tokenizer.
#[must_use]
pub fn candidates(
    model: &str,
    n_vocab: Option<usize>,
) -> Vec<(&'static CatalogueTokenizer, usize)> {
    let mut out: Vec<_> = catalogue()
        .iter()
        .filter(|tokenizer| n_vocab.is_none_or(|n| tokenizer.fits(n)))
        .map(|tokenizer| (tokenizer, tokenizer.prior(model)))
        .collect();
    out.sort_by_key(|(_, prior)| std::cmp::Reverse(*prior));
    out
}

/// One comparison of a server's count with a candidate tokenizer's count of the same text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pair {
    /// What must stay the same for the residual to be a constant: a reply's kind, or the mix
    /// of roles in a span of input.
    pub group: String,
    /// The tokens the server reported.
    pub reported: u64,
    /// The tokens the candidate counts in the text alone.
    pub counted: u64,
}

/// What the pairs say about a candidate tokenizer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    /// Nothing against it, and not enough for it yet.
    Candidate,
    /// Its residual was the same over at least three pairs of one group, one of them of at
    /// least 200 tokens.
    Confirmed,
    /// Two pairs of one group left different residuals: this is not the server's tokenizer.
    Dropped,
}

/// Score a candidate by its residuals (`reported - counted`). The server adds a fixed number of
/// tokens around a text of one kind, so the right tokenizer leaves the same residual every time
/// within a group; a wrong one leaves a residual that moves with the text.
#[must_use]
pub fn verdict(pairs: &[Pair]) -> Verdict {
    // Per group: the residual, how many pairs agreed on it, and whether one of them was large.
    let mut groups: HashMap<&str, (i128, usize, bool)> = HashMap::new();
    for pair in pairs {
        let residual = i128::from(pair.reported) - i128::from(pair.counted);
        let large = pair.counted >= LARGE_PAIR;
        let seen = groups
            .entry(pair.group.as_str()) // rules: allow(entry): HashMap API
            .or_insert((residual, 0, false));
        if seen.0 != residual {
            return Verdict::Dropped;
        }
        seen.1 += 1;
        seen.2 |= large;
    }
    if groups
        .values()
        .any(|(_, agreed, large)| *agreed >= CONFIRMING_PAIRS && *large)
    {
        Verdict::Confirmed
    } else {
        Verdict::Candidate
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity::TokenizerId;

    fn pair(group: &str, reported: u64, counted: u64) -> Pair {
        Pair {
            group: group.to_owned(),
            reported,
            counted,
        }
    }

    #[test]
    fn every_catalogue_id_is_an_identity_pinned_to_a_commit() {
        assert!(catalogue().len() >= 10);
        for tokenizer in catalogue() {
            match TokenizerId::parse(&tokenizer.id).unwrap() {
                TokenizerId::Hf { revision, .. } => assert_eq!(revision.map(|r| r.len()), Some(40)),
                TokenizerId::Tiktoken(name) => {
                    assert_eq!(
                        crate::Tokenizer::tiktoken(&name).unwrap().vocab_size(),
                        tokenizer.vocab
                    );
                }
                TokenizerId::File(_) => panic!("the catalogue names no local file"),
            }
            assert!(
                tokenizer
                    .models
                    .iter()
                    .all(|p| *p == p.to_lowercase() && !p.is_empty())
            );
        }
    }

    #[test]
    fn patterns_match_as_globs_and_the_longest_wins() {
        assert_eq!(matched("gpt-4o*", "gpt-4o-mini"), Some(6));
        assert_eq!(matched("gpt-4*", "gpt-4o-mini"), Some(5));
        assert_eq!(matched("*qwen3*", "qwen/qwen3-8b"), Some(5));
        assert_eq!(matched("*qwen3*", "qwen2.5-coder"), None);
        assert_eq!(matched("gpt-4*", "my-gpt-4"), None);
        assert_eq!(matched("*-it", "gemma-it"), Some(3));
        assert_eq!(matched("*-it", "gemma-it-q4"), None);
        assert_eq!(matched("exact", "exact"), Some(5));
        assert_eq!(matched("exact", "exactly"), None);
        let ranked = candidates("gpt-4o-mini", None);
        assert_eq!(ranked[0].0.id, "tiktoken:o200k_base");
        assert_eq!(ranked[1].0.id, "tiktoken:cl100k_base");
        assert_eq!(
            candidates("GPT-4-turbo", None)[0].0.id,
            "tiktoken:cl100k_base"
        );
        assert_eq!(
            candidates("gpt-6", None)[0].1,
            0,
            "an unknown model has no prior"
        );
    }

    #[test]
    fn the_vocabulary_size_is_a_hard_filter_that_allows_padding() {
        let ids = |n| -> Vec<&str> {
            candidates("anything", Some(n))
                .iter()
                .map(|(t, _)| t.id.split('@').next().unwrap())
                .collect()
        };
        assert_eq!(ids(151_936), ["hf:Qwen/Qwen3-8B"]);
        assert_eq!(ids(151_669), ["hf:Qwen/Qwen3-8B"]);
        assert_eq!(ids(128_256), ["hf:unsloth/Llama-3.2-3B-Instruct"]);
        assert_eq!(ids(262_208), ["hf:unsloth/gemma-3-4b-it"]);
        assert_eq!(ids(151_670), [] as [&str; 0]);
        // 200_019 padded to a multiple of 64 is 200_064: gpt-oss and o200k_base fit by the
        // padding rule, Phi-4-mini by its served size.
        assert_eq!(ids(200_064).len(), 3);
    }

    #[test]
    fn a_constant_residual_confirms_and_a_moving_one_drops() {
        let agreeing = [
            pair("reply", 12, 9),
            pair("reply", 243, 240),
            pair("reply", 53, 50),
        ];
        assert_eq!(verdict(&agreeing[..2]), Verdict::Candidate);
        assert_eq!(verdict(&agreeing), Verdict::Confirmed);
        let small = [
            pair("reply", 12, 9),
            pair("reply", 43, 40),
            pair("reply", 53, 50),
        ];
        assert_eq!(verdict(&small), Verdict::Candidate, "no pair of 200 tokens");
        let mixed = [
            pair("user", 12, 9),
            pair("user+results", 250, 240),
            pair("user", 53, 50),
        ];
        assert_eq!(
            verdict(&mixed),
            Verdict::Candidate,
            "each role mix has its own constant"
        );
        let moving = [pair("reply", 12, 9), pair("reply", 250, 240)];
        assert_eq!(verdict(&moving), Verdict::Dropped);
        assert_eq!(verdict(&[]), Verdict::Candidate);
    }
}
