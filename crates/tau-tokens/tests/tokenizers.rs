//! Tokenizers against the reference implementations, offline: the two embedded `OpenAI`
//! encodings against `tiktoken`, and four small Hugging Face tokenizers (tests/fixtures/tokens/
//! tiny, one per kind of pipeline) against the `tokenizers` library. Then the memo: a count read
//! from it is the count a fresh tokenization gives, on the goldens and on generated text.

mod common;

use proptest::prelude::*;
use tau_tokens::{Error, Tokenizer};

const TINY: [&str; 4] = ["bytelevel", "metaspace", "wordpiece", "split"];

fn tiny(name: &str) -> Tokenizer {
    Tokenizer::from_json(&common::read(
        &common::fixtures().join(format!("tiny/{name}.json")),
    ))
    .unwrap()
}

#[test]
fn the_embedded_encodings_give_tiktokens_ids() {
    let goldens = common::fixture("goldens.json");
    let texts = common::fixture("texts.json");
    let mut compared = 0;
    for (name, golden) in goldens["tiktoken"].as_object().unwrap() {
        let tokenizer = Tokenizer::tiktoken(name).unwrap();
        assert_eq!(
            Some(tokenizer.vocab_size() as u64),
            golden["vocab"].as_u64(),
            "{name}"
        );
        for (key, text) in texts.as_object().unwrap() {
            let ids = tokenizer.encode(text.as_str().unwrap()).unwrap();
            let expected = &golden["texts"][key];
            assert_eq!(
                common::ids_hash(&ids),
                expected["ids"].as_str().unwrap(),
                "{name} {key}"
            );
            assert_eq!(
                Some(ids.len() as u64),
                expected["count"].as_u64(),
                "{name} {key}"
            );
            assert_eq!(
                tokenizer.count(text.as_str().unwrap()).unwrap(),
                ids.len(),
                "{name} {key}"
            );
            compared += 1;
        }
    }
    assert!(compared >= 40, "only {compared} texts compared");
}

#[test]
fn a_tiktoken_encoding_reads_special_token_text_as_text() {
    let tokenizer = Tokenizer::tiktoken("o200k_base").unwrap();
    assert!(tokenizer.count("<|endoftext|>").unwrap() > 1);
    assert_eq!(tokenizer.count("").unwrap(), 0);
}

#[test]
fn what_is_not_a_tokenizer_is_refused() {
    let Err(Error::Tokenizer(message)) = Tokenizer::tiktoken("p50k_base") else {
        panic!("p50k_base is not embedded");
    };
    assert!(message.contains("o200k_base, cl100k_base"), "{message}");
    for bytes in [&b"not json"[..], b"{}", b"{\"model\": 3}", b""] {
        assert!(matches!(
            Tokenizer::from_json(bytes),
            Err(Error::Tokenizer(_))
        ));
    }
}

#[test]
fn the_tiny_tokenizers_give_the_reference_ids_and_counts() {
    let goldens = common::fixture("tiny/cases.json");
    for name in TINY {
        let tokenizer = tiny(name);
        let golden = &goldens[name];
        assert_eq!(
            Some(tokenizer.vocab_size() as u64),
            golden["vocab"].as_u64(),
            "{name}"
        );
        let cases = golden["cases"].as_array().unwrap();
        assert!(cases.len() >= 30, "{name}: {} cases", cases.len());
        for (n, case) in cases.iter().enumerate() {
            let text = case["text"].as_str().unwrap();
            let ids = tokenizer.encode(text).unwrap();
            assert_eq!(
                common::ids_hash(&ids),
                case["ids"].as_str().unwrap(),
                "{name} case {n}: {text:?}"
            );
            assert_eq!(
                Some(tokenizer.count(text).unwrap() as u64),
                case["count"].as_u64(),
                "{name} case {n}: {text:?}"
            );
        }
        // Everything again: nothing is tokenized, and every count is the same.
        let (counted, _) = tokenizer.work();
        for case in cases {
            let text = case["text"].as_str().unwrap();
            assert_eq!(
                Some(tokenizer.count(text).unwrap() as u64),
                case["count"].as_u64()
            );
        }
        assert_eq!(
            tokenizer.work().0,
            counted,
            "{name}: a second count tokenized something"
        );
    }
}

#[test]
fn a_file_that_asks_for_truncation_or_padding_still_counts_everything() {
    let path = common::fixtures().join("tiny/bytelevel.json");
    let mut file: serde_json::Value = serde_json::from_slice(&common::read(&path)).unwrap();
    file["truncation"] = serde_json::json!({"direction": "Right", "max_length": 4, "strategy": "LongestFirst", "stride": 0});
    file["padding"] = serde_json::json!({"strategy": {"Fixed": 64}, "direction": "Right", "pad_to_multiple_of": null, "pad_id": 0, "pad_type_id": 0, "pad_token": "<|im_end|>"});
    let limited = Tokenizer::from_json(&serde_json::to_vec(&file).unwrap()).unwrap();
    let plain = tiny("bytelevel");
    let text = "The quick brown fox jumps over the lazy dog, twice over.";
    assert!(plain.count(text).unwrap() > 4);
    assert_eq!(limited.encode(text).unwrap(), plain.encode(text).unwrap());
    assert_eq!(limited.count(text).unwrap(), plain.count(text).unwrap());
}

#[test]
fn a_conversation_that_grew_by_one_message_tokenizes_only_that_message() {
    let tokenizer = tiny("bytelevel");
    let message = |n: usize| {
        format!("<|im_start|>user\nMessage number {n}: the quick brown fox.<|im_end|>\n")
    };
    let mut conversation: String = (0..200).map(message).collect();
    let before = tokenizer.count(&conversation).unwrap();
    let (counted_before, _) = tokenizer.work();
    conversation.push_str(&message(200));
    let after = tokenizer.count(&conversation).unwrap();
    let (counted_after, reused) = tokenizer.work();
    assert_eq!(after, tokenizer.encode(&conversation).unwrap().len());
    assert!(after > before);
    // One new piece of text between the two added tokens; the "\n" after it was seen before.
    assert_eq!(counted_after - counted_before, 1);
    assert!(reused >= 400, "{reused} pieces reused");
}

#[test]
fn the_same_words_count_differently_at_the_start_of_the_text_where_the_tokenizer_says_so() {
    // Metaspace marks the first word of the text only: "hello" after an added token is not
    // the piece "hello" at the start, and the memo must not mix them up.
    let tokenizer = tiny("metaspace");
    for text in [
        "hello",
        "<s>hello",
        "hello<s>hello",
        "<s>hello<s>hello",
        "hello",
    ] {
        assert_eq!(
            tokenizer.count(text).unwrap(),
            tokenizer.encode(text).unwrap().len(),
            "{text:?}"
        );
    }
    let start = tokenizer.encode("hello").unwrap();
    let later = tokenizer.encode("<s>hello").unwrap();
    assert_ne!(
        start,
        later[1..],
        "this tokenizer no longer tells the start from the rest"
    );
}

/// Text made of the things that decide where a tokenizer cuts: its added tokens, white space,
/// words, digits, punctuation and characters outside ASCII, in any order.
fn text() -> impl Strategy<Value = String> {
    let marks = prop::sample::select(vec![
        "<|im_start|>",
        "<|im_end|>",
        "<|rs|>",
        "<|ls|>",
        "<think>",
        "</think>",
        "WORD",
        "<s>",
        "</s>",
        "[INST]",
        "[/INST]",
        "<unk>",
        "[CLS]",
        "[SEP]",
        "tokenizers",
        "[Tool]",
        "[tool]",
        "TOKENIZERS",
        "<|begin_of_text|>",
        "<|eot_id|>",
        "<|start_header_id|>",
        "<|end_header_id|>",
    ]);
    let filler = prop::sample::select(vec![
        " ", "  ", "\n", "\n\n", "\t", "\r\n", "the", "The", "quick", "it's", "don't", "x", "A",
        "42", "3.14", "1234567", ".", ",", "!", "?", "-", "_", "(", ")", "{", "}", "\"", "'", "`",
        "é", "e\u{301}", "ß", "日本", "語", "😀", "👍🏽", "\u{a0}", "\u{200b}", "\u{3000}", "``",
        "ﬁ", "İ", "<", ">", "|", "<|", "|>", "[", "]", "/", "s>", "<s", "Word", "WORDS", "word",
    ]);
    let part = prop_oneof![1 => marks.prop_map(str::to_owned), 5 => filler.prop_map(str::to_owned), 1 => "\\PC{0,6}"];
    prop::collection::vec(part, 0..40).prop_map(|parts| parts.concat())
}

/// A text, and the same text where its pieces sit elsewhere: after an added token instead of
/// at the start, and twice with an added token between.
fn placed(text: &str) -> Vec<String> {
    let mut out = vec![text.to_owned()];
    for mark in ["<s>", "<|im_start|>", "[CLS]", "<|eot_id|>"] {
        out.push(format!("{mark}{text}"));
        out.push(format!("{text}{mark}{text}"));
    }
    out
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(300))]

    /// The property the memo rests on: however a text is cut at added tokens, and wherever a
    /// piece of it was seen before, the count read through the memo is the number of ids
    /// `encode` gives, for every kind of pipeline.
    #[test]
    fn a_count_is_the_number_of_ids(texts in prop::collection::vec(text(), 1..3)) {
        for name in TINY {
            let tokenizer = tiny(name);
            let every: Vec<String> = texts.iter().flat_map(|text| placed(text)).collect();
            for text in every.iter().chain(every.iter().rev()) {
                let ids = tokenizer.encode(text).unwrap().len();
                prop_assert_eq!(tokenizer.count(text).unwrap(), ids, "{} {:?}", name, text);
            }
        }
    }
}
