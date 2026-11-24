//! Writes `v1/`, a session from the v1 store (tau 0.16, ea0a3dcd), which D30's reader must open.
//! Run from a checkout of ea0a3dcd as `crates/tau-store/examples/v1_fixture.rs`:
//! `cargo run -p tau-store --example v1_fixture -- <dir>`. Not built here.
use tau_core::tree::{Author, Body, Outcome, Part, Session, Stop};
use tau_store::Store;

fn main() {
    let dir = std::path::PathBuf::from(std::env::args().nth(1).expect("a directory"));
    let config = r#"{"model": "fake"}"#;
    let (mut s, c) = Session::create(config, "You are a test.", 1_000);
    let mut store = Store::create(&dir, &mut s).unwrap();
    s.write(c, Body::User("read a.txt".into()), vec![], Author::Frontend("term".into()), 2_000).unwrap();
    let call = Part::Call { id: "call_1".into(), tool: "read".into(), args_raw: r#"{"path": "a.txt"}"#.into(), replay: None };
    s.write(c, Body::Assistant { parts: vec![call], stop: Stop::Tools, finish_reason: Some("tool_calls".into()), meta: None }, vec![], Author::Provider, 3_000).unwrap();
    s.write(c, Body::Results(vec![("call_1".into(), Outcome::Ok("hello\n".into()), Author::Tools)]), vec![], Author::Core, 4_000).unwrap();
    s.write(c, Body::Assistant { parts: vec![Part::Text { text: "It says hello.".into(), replay: None }], stop: Stop::End, finish_reason: Some("stop".into()), meta: None }, vec![], Author::Provider, 5_000).unwrap();
    s.set_label("main", c).unwrap();
    s.sync();
    store = store.save(&mut s).unwrap();
    drop(store);
}
