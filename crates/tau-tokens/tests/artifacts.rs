//! Real tokenizers and chat templates against the reference implementations. Every test here
//! reads files that are not checked in (a `tokenizer.json` is 5 to 35 MB), so each is ignored by
//! default, with the command that fetches them as its reason, and `scripts/check.sh` runs them
//! with `--include-ignored` when the files are there. A file that is missing then fails the
//! test: nothing here passes by not running.
//!
//! The reference values (goldens.json) are what `tokenizers`, `transformers` and Jinja2 print
//! for the same files and inputs: ids for every text, and for every request the rendered text
//! and the ids of the rendered text. `transformers` is what vLLM runs.

mod common;

use std::path::PathBuf;

use serde_json::Value;
use tau_tokens::{ChatTemplate, Error, Tokenizer, catalogue};

const FETCH: &str = "needs the tokenizer files: scripts/tokens_goldens.py fetch";

struct Artifact {
    name: String,
    dir: PathBuf,
    count: bool,
    golden: Value,
}

fn artifacts() -> Vec<Artifact> {
    let goldens = common::fixture("goldens.json");
    let manifest = common::fixture("artifacts.json");
    let files = common::root().join("target/tokens-files");
    let all: Vec<Artifact> = manifest["artifacts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|artifact| {
            let name = artifact["name"].as_str().unwrap().to_owned();
            let repo = artifact["repo"].as_str().unwrap();
            let revision = artifact["revision"].as_str().unwrap();
            let golden = goldens["artifacts"][&name].clone();
            assert!(
                golden.is_object(),
                "{name} has no goldens: scripts/tokens_goldens.py generate"
            );
            Artifact {
                name,
                dir: files.join(repo).join(revision),
                count: artifact["count"].as_bool().unwrap(),
                golden,
            }
        })
        .collect();
    assert!(
        all.len() >= 15,
        "artifacts.json lists {} artifacts",
        all.len()
    );
    all
}

fn tokenizer(artifact: &Artifact) -> Tokenizer {
    Tokenizer::from_json(&common::read(&artifact.dir.join("tokenizer.json"))).unwrap()
}

/// The repository's template as a caller gets it: `chat_template.jinja` when the repository has
/// one, else `tokenizer_config.json`'s; the config gives the special tokens either way.
fn template(artifact: &Artifact) -> ChatTemplate {
    let config = common::read(&artifact.dir.join("tokenizer_config.json"));
    let jinja = artifact.dir.join("chat_template.jinja");
    let source = jinja
        .exists()
        .then(|| String::from_utf8(common::read(&jinja)).unwrap());
    ChatTemplate::from_config(&config, source.as_deref()).unwrap()
}

fn report(problems: &[String], compared: usize, least: usize) {
    assert!(compared >= least, "only {compared} comparisons were made");
    assert!(
        problems.is_empty(),
        "{} of {compared} differ:\n{}",
        problems.len(),
        problems.join("\n")
    );
}

#[test]
#[ignore = "needs the tokenizer files: scripts/tokens_goldens.py fetch"]
fn every_text_has_the_ids_the_reference_gives() {
    let texts = common::fixture("texts.json");
    let mut problems = Vec::new();
    let mut compared = 0;
    for artifact in artifacts().iter().filter(|a| a.count) {
        let tokenizer = tokenizer(artifact);
        for (key, text) in texts.as_object().unwrap() {
            let text = text.as_str().unwrap();
            let golden = &artifact.golden["texts"][key];
            assert!(
                golden.get("transformers").is_none(),
                "{key}: the two references disagree"
            );
            let ids = tokenizer.encode(text).unwrap();
            let count = tokenizer.count(text).unwrap();
            compared += 1;
            if common::ids_hash(&ids) != golden["ids"].as_str().unwrap() {
                problems.push(format!(
                    "{} {key}: ids differ ({} ids, reference {})",
                    artifact.name,
                    ids.len(),
                    golden["count"]
                ));
            }
            if count != ids.len() || Some(count as u64) != golden["count"].as_u64() {
                problems.push(format!(
                    "{} {key}: count {count}, {} ids, reference {}",
                    artifact.name,
                    ids.len(),
                    golden["count"]
                ));
            }
        }
    }
    report(&problems, compared, 200);
}

#[test]
#[ignore = "needs the tokenizer files: scripts/tokens_goldens.py fetch"]
fn every_request_renders_and_counts_as_the_reference() {
    let goldens = common::fixture("goldens.json");
    let now = common::now(&goldens);
    let dump = common::root().join("target/tokens-dump-rust");
    let mut problems = Vec::new();
    let mut compared = 0;
    for artifact in &artifacts() {
        let template = template(artifact);
        let tokenizer = artifact.count.then(|| tokenizer(artifact));
        for request in common::requests() {
            let golden = &artifact.golden["requests"][&request.name];
            let ours = template.render(
                &request.messages,
                request.tools.as_deref(),
                request.generate,
                request.extra.as_deref(),
                Some(now),
            );
            compared += 1;
            let label = format!("{} {}", artifact.name, request.name);
            let rendered = match (ours, golden["rendered"].as_str()) {
                (Ok(ours), Some(theirs)) if common::text_hash(&ours) == theirs => ours,
                (Ok(ours), Some(_)) => {
                    let path = dump
                        .join(&artifact.name)
                        .join(format!("{}.txt", request.name));
                    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                    std::fs::write(&path, &ours).unwrap();
                    problems.push(format!(
                        "{label}: the rendering differs ({} bytes, reference {}); ours is in {}",
                        ours.len(),
                        golden["bytes"],
                        path.display()
                    ));
                    continue;
                }
                (Err(Error::Template(_)), None) => continue,
                (Err(err), _) => {
                    problems.push(format!(
                        "{label}: we fail where the reference renders: {err}"
                    ));
                    continue;
                }
                (Ok(_), None) => {
                    problems.push(format!(
                        "{label}: we render what the reference refuses ({})",
                        golden["error"]
                    ));
                    continue;
                }
            };
            let Some(tokenizer) = &tokenizer else {
                continue;
            };
            let ids = tokenizer.encode(&rendered).unwrap();
            let count = tokenizer.count(&rendered).unwrap();
            if common::ids_hash(&ids) != golden["ids"].as_str().unwrap() || count != ids.len() {
                problems.push(format!(
                    "{label}: count {count}, {} ids, reference {}",
                    ids.len(),
                    golden["count"]
                ));
            }
        }
    }
    report(&problems, compared, 300);
}

#[test]
#[ignore = "needs the tokenizer files: scripts/tokens_goldens.py fetch"]
fn the_catalogue_states_the_sizes_the_files_have() {
    let mut checked = 0;
    for artifact in artifacts().iter().filter(|a| a.count) {
        let repo = artifact.golden["repo"].as_str().unwrap();
        let revision = artifact.golden["revision"].as_str().unwrap();
        let id = format!("hf:{repo}@{revision}");
        let listed = catalogue()
            .iter()
            .find(|t| t.id == id)
            .unwrap_or_else(|| panic!("{id} is not in the catalogue"));
        let tokenizer = tokenizer(artifact);
        assert_eq!(tokenizer.vocab_size(), listed.vocab, "{id}");
        assert_eq!(
            Some(listed.vocab as u64),
            artifact.golden["vocab"].as_u64(),
            "{id}"
        );
        let config = common::json(&artifact.dir.join("config.json"));
        let served = config["vocab_size"]
            .as_u64()
            .or_else(|| config["text_config"]["vocab_size"].as_u64());
        let served = usize::try_from(
            served.unwrap_or_else(|| panic!("{id}: config.json has no vocab_size")),
        )
        .unwrap();
        assert!(
            listed.fits(served),
            "{id}: the model's own size {served} does not fit the catalogue's"
        );
        checked += 1;
    }
    let hf = catalogue()
        .iter()
        .filter(|t| t.id.starts_with("hf:"))
        .count();
    assert_eq!(
        checked, hf,
        "every Hugging Face tokenizer of the catalogue is an artifact"
    );
}

#[test]
fn the_reason_names_the_command() {
    assert!(FETCH.contains("scripts/tokens_goldens.py fetch"));
}
