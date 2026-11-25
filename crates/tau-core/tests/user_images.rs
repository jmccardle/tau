//! `Body::User` and `ItemData::Input` with images (D31): the text alone encodes as before, so a
//! v2 log and pad decode unchanged; with images, the text and the references, never the bytes.

use tau_core::tree::{Author, Body, Image, ItemData};

fn png(w: u32, h: u32) -> Vec<u8> {
    let mut d = b"\x89PNG\r\n\x1a\n\0\0\0\x0dIHDR".to_vec();
    d.extend_from_slice(&w.to_be_bytes());
    d.extend_from_slice(&h.to_be_bytes());
    d.extend_from_slice(&[8, 2, 0, 0, 0]);
    d
}

#[test]
fn a_user_body_without_images_encodes_as_a_v2_log_holds_it() {
    let plain = Body::user("hi");
    assert_eq!(serde_json::to_string(&plain).unwrap(), r#"{"User":"hi"}"#);
    let v2: Body = serde_json::from_str(r#"{"User":"hi"}"#).unwrap();
    assert_eq!(v2, plain);
    assert_eq!(v2.images().count(), 0);
}

#[test]
fn a_user_body_with_images_carries_their_references_after_the_text() {
    let img = Image::from_bytes(png(2, 3)).unwrap();
    let with = Body::User {
        text: "look".into(),
        images: vec![img.clone()],
    };
    let text = serde_json::to_string(&with).unwrap();
    assert_eq!(
        text,
        format!(
            r#"{{"User":{{"text":"look","images":[{{"mime":"image/png","sha256":"{}","width":2,"height":3}}]}}}}"#,
            img.sha256
        ),
        "the bytes are never encoded"
    );
    let back: Body = serde_json::from_str(&text).unwrap();
    assert_eq!(back, with);
    assert_eq!(back.images().count(), 1);
    assert!(back.images().all(|i| i.data.is_none()));
}

#[test]
fn a_user_object_with_anything_else_is_refused() {
    for bad in [
        r#"{"User":{"text":"x","images":[],"extra":1}}"#,
        r#"{"User":{"text":"x"}}"#,
        r#"{"User":3}"#,
    ] {
        assert!(serde_json::from_str::<Body>(bad).is_err(), "{bad}");
    }
}

#[test]
fn an_input_without_images_encodes_as_before() {
    let by = Author::Frontend("t".into());
    let plain = ItemData::Input {
        by: by.clone(),
        text: "hi".into(),
        images: vec![],
    };
    let text = serde_json::to_string(&plain).unwrap();
    assert_eq!(text, r#"{"Input":{"by":{"Frontend":"t"},"text":"hi"}}"#);
    let v2: ItemData = serde_json::from_str(&text).unwrap();
    assert_eq!(v2, plain);

    let img = Image::from_bytes(png(1, 1)).unwrap();
    let with = ItemData::Input {
        by,
        text: "hi".into(),
        images: vec![img],
    };
    let back: ItemData = serde_json::from_str(&serde_json::to_string(&with).unwrap()).unwrap();
    assert_eq!(back, with);
    assert_eq!(back.images().len(), 1);
}
