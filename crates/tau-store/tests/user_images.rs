//! User images in the store (D31): an input's images are on disk before the scratchpad that names
//! them, and a file says version 3 only once it holds a record only 3 reads.

use std::fs;
use std::path::Path;

use tau_core::tree::{Author, Body, Image, ItemData, Record, Session};
use tau_store::{BASE, Error, HEADER, Store, VERSION, has_image};

fn png(w: u32, h: u32, salt: u8) -> Vec<u8> {
    let mut d = b"\x89PNG\r\n\x1a\n\0\0\0\x0dIHDR".to_vec();
    d.extend_from_slice(&w.to_be_bytes());
    d.extend_from_slice(&h.to_be_bytes());
    d.extend_from_slice(&[8, 2, 0, 0, 0, salt]);
    d
}

fn version_of(file: &Path) -> u32 {
    let bytes = fs::read(file).unwrap();
    u32::from_le_bytes(bytes[8..HEADER].try_into().unwrap())
}

fn input(text: &str, images: Vec<Image>) -> ItemData {
    ItemData::Input {
        by: Author::Frontend("t".into()),
        text: text.into(),
        images,
    }
}

#[test]
fn a_session_stays_at_the_base_version_until_a_user_image_is_written() {
    let d = tempfile::tempdir().unwrap();
    let (mut s, c) = Session::create("{}", "sys", 0);
    let mut store = Store::create(d.path(), &mut s).unwrap();
    assert_eq!(version_of(&d.path().join("log")), BASE);

    s.write(c, Body::user("plain"), vec![], Author::Core, 1)
        .unwrap();
    s.sync();
    store = store.save(&mut s).unwrap();
    assert_eq!(version_of(&d.path().join("log")), BASE, "appends alone");

    let image = Image::from_bytes(png(4, 3, 1)).unwrap();
    s.write(
        c,
        Body::User {
            text: "look".into(),
            images: vec![image.clone()],
        },
        vec![],
        Author::Core,
        2,
    )
    .unwrap();
    s.sync();
    drop(store.save(&mut s).unwrap());
    assert_eq!(version_of(&d.path().join("log")), VERSION);
    assert!(has_image(d.path(), &image.sha256));

    let (_store, again) = Store::open(d.path()).unwrap();
    let last = again.tree.node(again.tree.node_count() as u64 - 1).unwrap();
    assert_eq!(
        last.body,
        Body::User {
            text: "look".into(),
            images: vec![image.reference()],
        }
    );
    assert!(last.body.images().all(|i| i.data.is_none()), "a reference");
}

#[test]
fn a_pads_images_are_written_before_the_pad_and_only_it_says_3() {
    let d = tempfile::tempdir().unwrap();
    let (mut s, c) = Session::create("{}", "sys", 0);
    let mut store = Store::create(d.path(), &mut s).unwrap();
    let image = Image::from_bytes(png(5, 5, 2)).unwrap();
    let item = s.add_item(c, input("see", vec![image.clone()]));
    s.sync();
    store = store.save(&mut s).unwrap();
    assert!(has_image(d.path(), &image.sha256), "before the pad");
    assert_eq!(version_of(&d.path().join("scratchpad")), VERSION);
    assert_eq!(
        version_of(&d.path().join("log")),
        BASE,
        "no record needs 3 yet"
    );

    // Reopened, the item's image is a reference the directory keeps: it saves again.
    drop(store);
    let (store, mut s) = Store::open(d.path()).unwrap();
    let loaded = s.items(c).next().unwrap().clone();
    assert_eq!(loaded.data, input("see", vec![image.reference()]));
    assert!(loaded.data.images()[0].data.is_none());

    // Delivered: the node takes the image, the pad no longer holds one.
    s.write(
        c,
        Body::User {
            text: "see".into(),
            images: loaded.data.images().to_vec(),
        },
        vec![item],
        Author::Frontend("t".into()),
        3,
    )
    .unwrap();
    s.sync();
    drop(store.save(&mut s).unwrap());
    assert_eq!(version_of(&d.path().join("scratchpad")), BASE);
    assert_eq!(version_of(&d.path().join("log")), VERSION);
}

#[test]
fn a_pad_naming_an_image_it_has_no_bytes_for_is_refused() {
    let d = tempfile::tempdir().unwrap();
    let (mut s, c) = Session::create("{}", "sys", 0);
    let store = Store::create(d.path(), &mut s).unwrap();
    let image = Image::from_bytes(png(5, 5, 3)).unwrap().reference();
    s.add_item(c, input("see", vec![image.clone()]));
    s.sync();
    let err = store.save(&mut s).unwrap_err();
    assert!(
        matches!(&err, Error::MissingImage(sha) if *sha == image.sha256),
        "{err}"
    );
    assert!(!d.path().join("scratchpad").exists());
}

#[test]
fn a_session_created_holding_a_user_image_starts_at_3() {
    // A fork's copy of a walk is created whole.
    let d = tempfile::tempdir().unwrap();
    let (mut s, c) = Session::create("{}", "sys", 0);
    let image = Image::from_bytes(png(2, 2, 4)).unwrap();
    s.write(
        c,
        Body::User {
            text: String::new(),
            images: vec![image.clone()],
        },
        vec![],
        Author::Core,
        1,
    )
    .unwrap();
    assert!(s.outbox.iter().any(|r| matches!(r, Record::Write { .. })));
    let count = s.tree.node_count();
    drop(Store::create(d.path(), &mut s).unwrap());
    assert_eq!(version_of(&d.path().join("log")), VERSION);
    assert!(has_image(d.path(), &image.sha256));
    let (_store, again) = Store::open(d.path()).unwrap();
    assert_eq!(again.tree.node_count(), count);
}
