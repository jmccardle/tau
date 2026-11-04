//! What rows say, on nodes written as the server sends them.

use serde_json::json;

use super::*;
use crate::plan::{Kind, Row};

fn node(body: &serde_json::Value) -> Node {
    serde_json::from_value(json!({
        "id": 3, "parent": 2, "time": 0, "by": {"Frontend": "ann@web"}, "body": body,
        "delivers": []
    }))
    .unwrap()
}

fn assistant(parts: &serde_json::Value) -> Node {
    node(
        &json!({"Assistant": {"parts": parts, "stop": "End", "finish_reason": null,
                               "meta": null}}),
    )
}

#[test]
fn elide_cuts_with_an_ellipsis_and_leaves_what_fits() {
    assert_eq!(elide("hello", 10), "hello");
    assert_eq!(elide("hello world", 6), "hello…");
    assert_eq!(elide("hello", 1), "…");
    assert_eq!(
        elide("日本語です", 5),
        "日本…",
        "wide characters count two cells"
    );
}

#[test]
fn nodes_have_a_tag_and_a_line() {
    let (tag, word, text) = describe(&node(&json!({"User": "list  the\nfiles"})));
    assert_eq!(
        (tag, word, text.as_str()),
        (Tag::User, "user", "list the files")
    );
    let said = assistant(&json!([{"Thinking": "hm"}, {"Text": "Two files."}]));
    assert_eq!(describe(&said).2, "Two files.");
    let calls = assistant(&json!([{"Call": {"id": "c", "tool": "bash", "args_raw": "{}"}}]));
    assert_eq!(describe(&calls).2, "calls bash");
    let results =
        node(&json!({"Results": [["c", {"Ok": "x"}, "Tools"], ["d", "Interrupted", "Tools"]]}));
    assert_eq!(describe(&results).2, "2 results");
    let config = node(
        &json!({"Config": {"data": "{\"model\": \"m\", \"extensions\": {\"gate\": {\"n\": 3}}}"}}),
    );
    assert_eq!(describe(&config).2, "model: m  gate: n=3");
}

#[test]
fn the_planner_is_told_calls_and_the_characters_the_model_reads() {
    let reply = assistant(&json!([
        {"Thinking": "abcd"},
        {"Text": "efgh"},
        {"Call": {"id": "c", "tool": "bash", "args_raw": "12345678"}}
    ]));
    let facts = facts(&reply);
    assert_eq!((facts.calls, facts.chars, facts.user), (1, 16, false));
    assert_eq!(facts.parent, Some(2));
    assert!(super::facts(&node(&json!({"User": "abc"}))).user);
    assert_eq!(
        super::facts(&node(&json!({"Custom": {"kind": "k", "data": "ddd"}}))).chars,
        0
    );
}

fn marks() -> Marks {
    let open = BTreeMap::from([
        (
            0,
            Flags {
                at: 3,
                busy: false,
                asking: false,
            },
        ),
        (
            1,
            Flags {
                at: 3,
                busy: true,
                asking: true,
            },
        ),
    ]);
    let gone = BTreeMap::from([(2, 3)]);
    Marks::new(open, &gone, vec![("scout".into(), 1), ("old".into(), 2)], 0)
}

fn turn(size: usize, calls: usize, branches: usize) -> Row {
    Row {
        kind: Kind::Turn,
        node: 3,
        size,
        calls,
        branches,
        cursors: vec![0, 1],
        closed: vec![2],
        expandable: true,
        ..Row::default()
    }
}

#[test]
fn a_folded_turn_summarizes_itself_and_lists_every_cursor_inside() {
    let user = node(&json!({"User": "fix it"}));
    let label = row_label(&turn(14, 6, 1), &user, &marks(), true);
    assert_eq!(label.body, "user: fix it");
    assert_eq!(
        label.suffix,
        "  +14 · 6 calls · 1 branch  ◀ [0]  ◀ 1 scout …?  ○ 2 old"
    );
    assert_eq!(
        label.dim_at,
        Some(
            "  +14 · 6 calls · 1 branch  ◀ [0]  ◀ 1 scout …?"
                .chars()
                .count()
        )
    );
}

#[test]
fn an_open_turn_shows_only_the_cursors_at_its_own_node() {
    let user = node(&json!({"User": "fix it"}));
    let mut row = turn(1, 1, 0);
    row.node = 9;
    let label = row_label(&row, &user, &marks(), false);
    assert_eq!(label.suffix, "  +1 · 1 call", "no cursor is at node 9");
    row.node = 3;
    let label = row_label(&row, &user, &marks(), false);
    assert!(label.suffix.contains("◀ [0]") && label.suffix.contains("○ 2 old"));
}

#[test]
fn a_stub_says_its_size_and_the_cursors_on_its_branch() {
    let user = node(&json!({"User": "x"}));
    let stub = Row {
        kind: Kind::Stub,
        node: 3,
        size: 3,
        turns: 1,
        cursors: vec![1],
        expandable: true,
        ..Row::default()
    };
    let label = row_label(&stub, &user, &marks(), true);
    assert_eq!(label.body, "├┄ 3 nodes, 1 turn");
    assert_eq!(label.suffix, "  ◀ 1 scout …?");
    assert_eq!(label.tag, Tag::Stub);
}

#[test]
fn fit_cuts_the_body_first_and_keeps_the_marks() {
    let label = RowLabel {
        body: "user: a very long question indeed".into(),
        suffix: "  +2  ◀ 1  ○ 2".into(),
        dim_at: Some(8),
        tag: Tag::User,
        tag_size: 5,
    };
    let (text, dim) = fit(&label, 30);
    assert_eq!(text.chars().count(), 30);
    assert!(text.ends_with("  +2  ◀ 1  ○ 2"));
    assert_eq!(
        dim,
        Some(text.chars().count() - "  +2  ◀ 1  ○ 2".chars().count() + 8)
    );
    let (text, dim) = fit(&label, 10);
    assert_eq!(
        (text.chars().count(), dim),
        (10, None),
        "no room: cut whole, nothing dimmed"
    );
}

#[test]
fn the_detail_of_a_stub_says_what_it_holds_and_how_it_begins() {
    let user = node(&json!({"User": "begin here"}));
    let stub = Row {
        kind: Kind::Stub,
        node: 3,
        size: 4,
        turns: 2,
        cursors: vec![1],
        closed: vec![2],
        ..Row::default()
    };
    let text = detail_text(&stub, &user);
    assert!(text.starts_with("4 nodes and 2 turns on this branch\ncursor 1 is on it\n"));
    assert!(text.contains("cursor 2 (closed) was on it"));
    assert!(text.contains("It begins:\n\nuser  ·  node 3  ·  by ann@web\n\nbegin here"));
}

#[test]
fn a_copy_and_a_derived_node_say_so() {
    let marks = Marks::new(BTreeMap::new(), &BTreeMap::new(), vec![], 0);
    let row = Row {
        kind: Kind::Node,
        ..Row::default()
    };
    let with = |extra: serde_json::Value| -> Node {
        let mut n = serde_json::to_value(node(&json!({"User": "hi"}))).unwrap();
        n.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        serde_json::from_value(n).unwrap()
    };
    let copy = with(json!({"copied_from": 7}));
    let derived = with(json!({"derived_from": 6}));
    assert_eq!(
        row_label(&row, &copy, &marks, true).suffix,
        "  ≡ (copy of 7)"
    );
    assert_eq!(row_label(&row, &derived, &marks, true).suffix, "  ⧉");
    assert_eq!(
        row_label(&row, &node(&json!({"User": "hi"})), &marks, true).suffix,
        ""
    );
}
