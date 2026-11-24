//! Images in the store (D30): kept once beside the log, named by their hash, written before the
//! record that names them; and a v1 log, written by the v1 store, opening on this build.

use std::fs;
use std::path::{Path, PathBuf};

use tau_core::tree::{Author, Body, Image, Outcome, Record, Session, Stop};
use tau_store::{Error, HEADER, Store, VERSION, fill_images, has_image, read_image};

fn png(w: u32, h: u32, salt: u8) -> Vec<u8> {
    let mut d = b"\x89PNG\r\n\x1a\n\0\0\0\x0dIHDR".to_vec();
    d.extend_from_slice(&w.to_be_bytes());
    d.extend_from_slice(&h.to_be_bytes());
    d.extend_from_slice(&[8, 2, 0, 0, 0, salt]);
    d
}

fn results(images: Vec<Image>) -> Body {
    Body::Results(vec![(
        "c1".into(),
        Outcome::Ok {
            value: "[image]".into(),
            images,
        },
        Author::Tools,
    )])
}

/// A session with a batch at its cursor, in `dir`: the store and the cursor.
fn at_a_batch(dir: &Path) -> (Session, Store, u64) {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let store = Store::create(dir, &mut s).unwrap();
    let call = tau_core::tree::Part::Call {
        id: "c1".into(),
        tool: "read".into(),
        args_raw: "{}".into(),
        replay: None,
    };
    s.write(
        c,
        Body::Assistant {
            parts: vec![call],
            stop: Stop::Tools,
            finish_reason: None,
            meta: None,
        },
        vec![],
        Author::Provider,
        1,
    )
    .unwrap();
    (s, store, c)
}

fn fixture() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/sessions/v1")
}

fn copy_dir(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for found in fs::read_dir(from).unwrap() {
        let found = found.unwrap();
        fs::copy(found.path(), to.join(found.file_name())).unwrap();
    }
}

fn header_version(dir: &Path) -> u32 {
    let log = fs::read(dir.join("log")).unwrap();
    u32::from_le_bytes(log[8..HEADER].try_into().unwrap())
}

#[test]
fn an_image_is_kept_once_beside_the_log_and_reloads_as_a_reference() {
    let d = tempfile::tempdir().unwrap();
    let (mut s, store, c) = at_a_batch(d.path());
    let img = Image::from_bytes(png(4, 2, 1)).unwrap();
    s.write(c, results(vec![img.clone()]), vec![], Author::Core, 2)
        .unwrap();
    s.sync();
    let store = store.save(&mut s).unwrap();
    assert!(has_image(d.path(), &img.sha256));
    let log = fs::read_to_string(d.path().join("log")).unwrap_or_else(|_| {
        String::from_utf8_lossy(&fs::read(d.path().join("log")).unwrap()).into_owned()
    });
    assert!(log.contains(&img.sha256), "the log names the image");
    assert!(!log.contains("IHDR"), "the log never holds its bytes");
    drop(store);

    let (_store, loaded) = Store::open(d.path()).unwrap();
    let node = loaded.tree.node(loaded.at(c).unwrap()).unwrap();
    let image = node.body.images().next().unwrap();
    assert_eq!(*image, img);
    assert!(image.data.is_none(), "a loaded image carries no bytes");
    assert_eq!(&*read_image(d.path(), image).unwrap(), &png(4, 2, 1)[..]);
}

#[test]
fn a_reference_without_bytes_must_name_a_kept_image() {
    let d = tempfile::tempdir().unwrap();
    let (mut s, store, c) = at_a_batch(d.path());
    let unknown = Image::from_bytes(png(1, 1, 9)).unwrap().reference();
    s.write(c, results(vec![unknown.clone()]), vec![], Author::Core, 2)
        .unwrap();
    s.sync();
    let err = store.save(&mut s).unwrap_err();
    assert!(matches!(&err, Error::MissingImage(sha) if *sha == unknown.sha256));
    assert!(err.to_string().contains("has no image"));
}

#[test]
fn a_damaged_image_file_is_missing_not_believed() {
    let d = tempfile::tempdir().unwrap();
    let (mut s, store, c) = at_a_batch(d.path());
    let img = Image::from_bytes(png(3, 3, 2)).unwrap();
    s.write(c, results(vec![img.clone()]), vec![], Author::Core, 2)
        .unwrap();
    s.sync();
    drop(store.save(&mut s).unwrap());
    fs::write(d.path().join("images").join(&img.sha256), b"other").unwrap();
    assert!(matches!(
        read_image(d.path(), &img.reference()),
        Err(Error::MissingImage(_))
    ));
}

#[test]
fn fill_images_gives_a_copy_the_bytes_its_source_keeps() {
    let d = tempfile::tempdir().unwrap();
    let (mut s, store, c) = at_a_batch(d.path());
    let img = Image::from_bytes(png(2, 2, 3)).unwrap();
    s.write(c, results(vec![img]), vec![], Author::Core, 2)
        .unwrap();
    s.sync();
    drop(store.save(&mut s).unwrap());
    let (_held, loaded) = Store::open(d.path()).unwrap();
    let (mut copy, _) = loaded.fork(loaded.at(c).unwrap(), 9).unwrap();
    fill_images(&mut copy.outbox, d.path()).unwrap();
    let filled = copy.outbox.iter().find_map(|r| match r {
        Record::Write { node, .. } => node.body.images().next().cloned(),
        _ => None,
    });
    assert!(filled.unwrap().data.is_some());
    let e = tempfile::tempdir().unwrap();
    let fork_dir = e.path().join("fork");
    drop(Store::create(&fork_dir, &mut copy).unwrap());
    let kept = fs::read_dir(fork_dir.join("images")).unwrap().count();
    assert_eq!(kept, 1, "the fork keeps its own copy");
}

#[test]
fn a_v1_log_written_by_the_v1_store_opens_and_is_marked_v2_at_its_first_write() {
    let d = tempfile::tempdir().unwrap();
    copy_dir(&fixture(), d.path());
    assert_eq!(header_version(d.path()), 1);

    let read = Store::read(d.path()).unwrap();
    assert_eq!(header_version(d.path()), 1, "a read copy writes nothing");

    drop(Store::open(d.path()).unwrap());
    assert_eq!(
        header_version(d.path()),
        1,
        "opened and closed, nothing written"
    );

    let (mut store, mut s) = Store::open(d.path()).unwrap();
    assert_eq!(header_version(d.path()), 1);
    assert_eq!(s.tree.node_count(), read.tree.node_count());
    let c = s.label("main").unwrap();
    let results = s
        .tree
        .walk(s.at(c).unwrap())
        .into_iter()
        .find(|n| matches!(n.body, Body::Results(_)))
        .unwrap()
        .clone();
    assert_eq!(
        results.body,
        Body::Results(vec![(
            "call_1".into(),
            Outcome::ok("hello\n"),
            Author::Tools
        )])
    );

    // It goes on as a v2 session: an image result lands and reloads.
    s.write(
        c,
        Body::User("and the picture".into()),
        vec![],
        Author::Core,
        9,
    )
    .unwrap();
    s.sync();
    store = store.save(&mut s).unwrap();
    assert_eq!(
        header_version(d.path()),
        VERSION,
        "marked before its first append"
    );
    drop(store);
    let (_store, again) = Store::open(d.path()).unwrap();
    assert_eq!(again.tree.node_count(), read.tree.node_count() + 1);
}

/// `payload` as one record on disk: its length, the CRC-32 of length and payload, the payload.
fn record(payload: &str) -> Vec<u8> {
    let len = u32::try_from(payload.len()).unwrap().to_le_bytes();
    let mut h = crc32fast::Hasher::new();
    h.update(&len);
    h.update(payload.as_bytes());
    [&len[..], &h.finalize().to_le_bytes(), payload.as_bytes()].concat()
}

fn append_records(dir: &Path, payloads: &[&str]) {
    let mut log = fs::read(dir.join("log")).unwrap();
    for p in payloads {
        log.extend(record(p));
    }
    fs::write(dir.join("log"), log).unwrap();
}

#[test]
fn a_v1_log_that_does_not_load_stays_v1() {
    let d = tempfile::tempdir().unwrap();
    copy_dir(&fixture(), d.path());
    // A node out of sequence: the bytes decode, the replay refuses them.
    let node = r#"{"Write":{"node":{"id":99,"parent":0,"time":1,"by":"Core","body":{"User":"x"},"delivers":[]},"mv":null}}"#;
    append_records(d.path(), &[node, r#"{"Sync":{"pad":null}}"#]);
    assert!(matches!(Store::open(d.path()), Err(Error::Core(_))));
    assert_eq!(
        header_version(d.path()),
        1,
        "0.16 can still open what it wrote"
    );
}

fn results_naming(sha256: &str) -> String {
    format!(
        r#"{{"Write":{{"node":{{"id":6,"parent":5,"time":1,"by":"Core","body":{{"Results":[["call_1",{{"Ok":{{"value":"x","images":[{{"mime":"image/png","sha256":"{sha256}","width":1,"height":1}}]}}}},"Tools"]]}},"delivers":[]}},"mv":null}}}}"#
    )
}

#[test]
fn a_log_naming_an_image_by_anything_but_64_lowercase_hex_does_not_decode() {
    for bad in [
        "../../../etc/passwd".to_owned(),
        "A".repeat(64),
        "a".repeat(63),
    ] {
        let d = tempfile::tempdir().unwrap();
        copy_dir(&fixture(), d.path());
        append_records(
            d.path(),
            &[&results_naming(&bad), r#"{"Sync":{"pad":null}}"#],
        );
        let err = Store::open(d.path()).unwrap_err();
        assert!(matches!(err, Error::Undecodable { .. }), "{bad}: {err}");
        assert!(err.to_string().contains("64 lowercase hex digits"), "{err}");
        assert!(!has_image(d.path(), &bad), "never a path outside images/");
    }
    assert!(!has_image(Path::new("/"), "../etc/passwd"));
}

#[test]
fn a_damaged_image_file_is_replaced_when_its_bytes_come_again() {
    let d = tempfile::tempdir().unwrap();
    let (mut s, store, c) = at_a_batch(d.path());
    let img = Image::from_bytes(png(6, 6, 7)).unwrap();
    s.write(c, results(vec![img.clone()]), vec![], Author::Core, 2)
        .unwrap();
    s.sync();
    let store = store.save(&mut s).unwrap();
    let kept = d.path().join("images").join(&img.sha256);
    fs::write(&kept, b"damaged").unwrap();
    assert!(read_image(d.path(), &img.reference()).is_err());

    // The same image again, from a tool: its bytes repair the file.
    s.write(c, results(vec![img.clone()]), vec![], Author::Core, 3)
        .unwrap();
    s.sync();
    drop(store.save(&mut s).unwrap());
    assert_eq!(
        &*read_image(d.path(), &img.reference()).unwrap(),
        &png(6, 6, 7)[..]
    );
}

#[test]
fn a_version_past_this_build_is_refused_by_name() {
    let d = tempfile::tempdir().unwrap();
    copy_dir(&fixture(), d.path());
    let mut log = fs::read(d.path().join("log")).unwrap();
    log[8..HEADER].copy_from_slice(&(VERSION + 1).to_le_bytes());
    fs::write(d.path().join("log"), log).unwrap();
    let err = Store::open(d.path()).unwrap_err();
    assert!(
        err.to_string().contains(&format!(
            "version {}; this build reads 1 to {VERSION}",
            VERSION + 1
        )),
        "{err}"
    );
}
