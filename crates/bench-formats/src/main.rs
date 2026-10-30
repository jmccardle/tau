//! Log encodings, compression, and repeated text, measured on a session log
//! built from real text (D14). Tool results and replies are slices of source
//! files and docs read from the corpus directories (default: the cargo
//! registry), taken in order so no text repeats except by copying. Each
//! cycle runs `TURNS` turns, then copies the newest quarter of the walk and
//! every user node verbatim as a new line with a summary on top, as D3
//! describes compaction, and moves the cursor there.
//!
//! Every record is encoded and decoded in each format and checked equal.
//! Output is Markdown.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use tau_core::tree::{Author, Body, CursorId, Outcome, Part, Record, Session, Stop};

const CYCLES: usize = 5;
const TURNS: usize = 150;
/// Characters per piece of a turn.
const USER: usize = 300;
const THINKING: usize = 400;
const TEXT: usize = 300;
const RESULT: usize = 3_000;
const FINAL: usize = 500;
const SUMMARY: usize = 8_000;
const ROUNDS: usize = 4;

/// Every `.rs` and `.md` file under `dirs`, in path order, concatenated.
fn corpus(dirs: &[PathBuf]) -> Vec<char> {
    let mut files = vec![];
    let mut stack: Vec<PathBuf> = dirs.to_vec();
    while let Some(d) = stack.pop() {
        let Ok(read) = std::fs::read_dir(&d) else {
            continue;
        };
        for path in read.flatten().map(|f| f.path()) {
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().is_some_and(|x| x == "rs" || x == "md") {
                files.push(path);
            }
        }
    }
    files.sort();
    files
        .iter()
        .filter_map(|p| std::fs::read_to_string(p).ok())
        .flat_map(|t| t.chars().collect::<Vec<_>>())
        .collect()
}

/// Consecutive slices of the corpus; never the same text twice.
struct Text {
    chars: Vec<char>,
    at: usize,
}

impl Text {
    fn take(&mut self, n: usize) -> String {
        assert!(
            self.at + n <= self.chars.len(),
            "corpus too small: pass more directories"
        );
        let t = self.chars[self.at..self.at + n].iter().collect();
        self.at += n;
        t
    }
}

fn turn(s: &mut Session, c: CursorId, text: &mut Text, calls: &mut u64) {
    s.write(
        c,
        Body::User(text.take(USER)),
        vec![],
        Author::Frontend("t".into()),
        0,
    )
    .unwrap();
    for _ in 0..ROUNDS {
        *calls += 1;
        let id = format!("c{calls}");
        let parts = vec![
            Part::Thinking(text.take(THINKING)),
            Part::Text(text.take(TEXT)),
            Part::Call {
                id: id.clone(),
                tool: "read".into(),
                args_raw: r#"{"path":"src/lib.rs"}"#.into(),
            },
        ];
        let reply = Body::Assistant {
            parts,
            stop: Stop::Tools, finish_reason: None,
 meta: None, };
        s.write(c, reply, vec![], Author::Provider, 0).unwrap();
        let results = Body::Results(vec![(id, Outcome::Ok(text.take(RESULT)), Author::Tools)]);
        s.write(c, results, vec![], Author::Tools, 0).unwrap();
    }
    let done = Body::Assistant {
        parts: vec![Part::Text(text.take(FINAL))],
        stop: Stop::End, finish_reason: None,
 meta: None, };
    s.write(c, done, vec![], Author::Provider, 0).unwrap();
    s.sync();
}

/// The newest quarter of the walk and every user node, copied under the
/// system prompt, a summary on top; the cursor moves there.
fn compact(s: &mut Session, c: CursorId, text: &mut Text) {
    let walk: Vec<Body> = s
        .walk(s.at(c).unwrap())
        .iter()
        .skip(2)
        .map(|n| n.body.clone())
        .collect();
    let recent = walk.len() * 3 / 4;
    let by = Author::Extension("compact".into());
    let mut parent = 1;
    for (i, body) in walk.into_iter().enumerate() {
        if i >= recent || matches!(body, Body::User(_)) {
            parent = s.append(parent, by.clone(), body, vec![], 0).unwrap();
        }
    }
    let summary = Body::User(text.take(SUMMARY));
    let top = s
        .append(parent, by.clone(), summary, vec![], 0)
        .unwrap();
    s.move_to(c, top, by, 0).unwrap();
    s.sync();
}

fn ms(d: Duration) -> String {
    format!("{:.0} ms", d.as_secs_f64() * 1e3)
}

fn mb(n: usize) -> String {
    format!("{:.1} MB", n as f64 / 1e6)
}

type Encode = fn(&Record) -> Vec<u8>;
type Decode = fn(&[u8]) -> Record;

fn row(name: &str, records: &[Record], encode: Encode, decode: Decode) {
    let t = Instant::now();
    let blobs: Vec<Vec<u8>> = records.iter().map(encode).collect();
    let enc = t.elapsed();
    let t = Instant::now();
    let back: Vec<Record> = blobs.iter().map(|b| decode(b)).collect();
    let dec = t.elapsed();
    assert!(back == records, "{name} does not round-trip");
    let all = blobs.concat();
    let t = Instant::now();
    let z = zstd::encode_all(&all[..], 3).unwrap();
    let zenc = t.elapsed();
    let t = Instant::now();
    zstd::decode_all(&z[..]).unwrap();
    let zdec = t.elapsed();
    println!(
        "| {name} | {} | {} | {} | {} ({:.1}×) | {} | {} |",
        mb(all.len()),
        ms(enc),
        ms(dec),
        mb(z.len()),
        all.len() as f64 / z.len() as f64,
        ms(zenc),
        ms(zdec),
    );
}

fn json(r: &Record) -> Vec<u8> {
    serde_json::to_vec(r).unwrap()
}

fn cbor(r: &Record) -> Vec<u8> {
    let mut v = vec![];
    ciborium::into_writer(r, &mut v).unwrap();
    v
}

/// BSON needs a document at the top; a record that is not one is wrapped.
fn bson_enc(r: &Record) -> Vec<u8> {
    let doc = bson::doc! { "r": bson::to_bson(r).unwrap() };
    bson::to_vec(&doc).unwrap()
}

fn bson_dec(b: &[u8]) -> Record {
    let doc: bson::Document = bson::from_slice(b).unwrap();
    bson::from_bson(doc.get("r").unwrap().clone()).unwrap()
}

/// The text of a body: each part, result, or message as stored.
fn strings(body: &Body) -> Vec<&str> {
    match body {
        Body::User(t) | Body::System(t) => vec![t],
        Body::Assistant { parts, .. } => parts
            .iter()
            .map(|p| match p {
                Part::Text(t) | Part::Thinking(t) => t.as_str(),
                Part::Call { args_raw, .. } => args_raw.as_str(),
            })
            .collect(),
        Body::Results(r) => r
            .iter()
            .map(|(_, o, _)| match o {
                Outcome::Ok(t) | Outcome::Err(t) => t.as_str(),
                Outcome::Interrupted => "",
            })
            .collect(),
        Body::Config(c) => vec![&c.data],
        Body::Custom { data, .. } => vec![data],
    }
}

fn repeated(records: &[Record]) {
    let mut seen = std::collections::HashSet::new();
    let (mut total, mut distinct) = (0, 0);
    for r in records {
        if let Record::Write { node, .. } = r {
            for t in strings(&node.body) {
                total += t.len();
                if seen.insert(t) {
                    distinct += t.len();
                }
            }
        }
    }
    println!(
        "\nText {}; distinct by content {} ({:.0}% is repeats).",
        mb(total),
        mb(distinct),
        100.0 - 100.0 * distinct as f64 / total as f64
    );
}

fn main() {
    let dirs: Vec<PathBuf> = match std::env::args()
        .skip(1)
        .map(PathBuf::from)
        .collect::<Vec<_>>()
    {
        d if d.is_empty() => {
            let home = std::env::var("HOME").expect("HOME, or pass corpus directories");
            vec![Path::new(&home).join(".cargo/registry/src")]
        }
        d => d,
    };
    let mut text = Text {
        chars: corpus(&dirs),
        at: 0,
    };
    let (mut s, c) = Session::create("{}", "You are a coding agent.", 0);
    let mut calls = 0;
    for _ in 0..CYCLES {
        for _ in 0..TURNS {
            turn(&mut s, c, &mut text, &mut calls);
        }
        compact(&mut s, c, &mut text);
    }
    let records = std::mem::take(&mut s.outbox);
    println!(
        "# Log encodings\n\n{} records; {} corpus characters used of {}.\n",
        records.len(),
        text.at,
        text.chars.len()
    );
    println!(
        "| format | size | encode | decode | zstd -3, whole log | zstd encode | zstd decode |"
    );
    println!("|---|---|---|---|---|---|---|");
    row("JSON (serde_json)", &records, json, |b| {
        serde_json::from_slice(b).unwrap()
    });
    row("CBOR (ciborium)", &records, cbor, |b| {
        ciborium::from_reader(b).unwrap()
    });
    row(
        "MessagePack, named fields (rmp-serde)",
        &records,
        |r| rmp_serde::to_vec_named(r).unwrap(),
        |b| rmp_serde::from_slice(b).unwrap(),
    );
    row("BSON (bson)", &records, bson_enc, bson_dec);
    row(
        "postcard, no field names",
        &records,
        |r| postcard::to_stdvec(r).unwrap(),
        |b| postcard::from_bytes(b).unwrap(),
    );
    let t = Instant::now();
    let per: usize = records
        .iter()
        .map(|r| zstd::encode_all(&json(r)[..], 3).unwrap().len())
        .sum();
    println!(
        "\nJSON, zstd -3 per record: {} ({}).",
        mb(per),
        ms(t.elapsed())
    );
    repeated(&records);
}
