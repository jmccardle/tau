//! Shared by the test binaries: where the fixtures are, and the hashes the goldens are kept as.
//! The goldens are what the reference implementations print (`scripts/tokens_goldens.py`).

#![allow(dead_code, reason = "each test binary uses its own part")]

use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use serde_json::Value;
use sha2::{Digest, Sha256};
use tau_tokens::Now;

pub fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

pub fn fixtures() -> PathBuf {
    root().join("tests/fixtures/tokens")
}

pub fn read(path: &Path) -> Vec<u8> {
    std::fs::read(path).unwrap_or_else(|err| panic!("{}: {err}", path.display()))
}

pub fn json(path: &Path) -> Value {
    serde_json::from_slice(&read(path)).unwrap_or_else(|err| panic!("{}: {err}", path.display()))
}

pub fn fixture(name: &str) -> Value {
    json(&fixtures().join(name))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().fold(String::new(), |mut out, byte| {
        let _ = write!(out, "{byte:02x}");
        out
    })
}

/// sha256 of the ids in decimal, joined by commas: how goldens.json keeps a list of ids.
pub fn ids_hash(ids: &[u32]) -> String {
    let joined = ids.iter().map(u32::to_string).collect::<Vec<_>>().join(",");
    hex(&Sha256::digest(joined.as_bytes()))
}

pub fn text_hash(text: &str) -> String {
    hex(&Sha256::digest(text.as_bytes()))
}

/// The time the references were made at (`now` in goldens.json and templates.json).
pub fn now(golden: &Value) -> Now {
    let part = |n: usize| u32::try_from(golden["now"][n].as_u64().unwrap()).unwrap();
    Now::new(
        part(0).cast_signed(),
        part(1),
        part(2),
        (part(3), part(4), part(5)),
    )
    .unwrap()
}

/// A request of requests.json (conversations.json, each part as JSON text) as
/// `ChatTemplate::render` takes it.
pub struct Request {
    pub name: String,
    pub messages: Vec<u8>,
    pub tools: Option<Vec<u8>>,
    pub generate: bool,
    pub extra: Option<Vec<u8>>,
}

pub fn requests() -> Vec<Request> {
    let bytes = |value: &Value| value.as_str().map(|text| text.as_bytes().to_vec());
    fixture("requests.json")
        .as_array()
        .unwrap()
        .iter()
        .map(|request| Request {
            name: request["name"].as_str().unwrap().to_owned(),
            messages: bytes(&request["messages"]).unwrap(),
            tools: bytes(&request["tools"]),
            generate: request["generate"].as_bool().unwrap(),
            extra: bytes(&request["extra"]),
        })
        .collect()
}
