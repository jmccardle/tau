//! The tree browser against a model fed the events a server sends.

use ratatui::crossterm::event::{KeyCode, KeyModifiers};
use serde_json::{Value, json};

use super::*;
use crate::{model::Need, wire::Node};

const ME: &str = "tester@host:ratatui";

fn node(id: u64, parent: Option<u64>, body: &Value) -> Value {
    json!({"id": id, "parent": parent, "time": 0, "by": {"Frontend": "t"},
           "body": body, "delivers": []})
}

fn user(id: u64, parent: u64, text: &str) -> Value {
    node(id, Some(parent), &json!({"User": text}))
}

fn reply(id: u64, parent: u64, text: &str) -> Value {
    let body = json!({"Assistant": {"parts": [{"Text": text}], "stop": "End",
                                    "finish_reason": null, "meta": null}});
    node(id, Some(parent), &body)
}

fn view(cursor: u64, at: u64) -> Value {
    json!({"kind": "CursorView", "cursor": cursor, "at": at, "busy": false, "auto": false,
           "questions": [], "items": [], "ending": "Done", "hook_error": null, "model": "m",
           "shown": []})
}

fn hear(model: &mut Model, event: Value) -> Vec<Need> {
    model.hear(serde_json::from_value(event).unwrap())
}

/// A session of two turns (nodes 0..=5), a second branch off node 3 (6, 7), cursor 0 at node 5
/// and cursor 1 labelled `scout` at node 7.
fn session() -> Model {
    let mut model = Model::new(ME);
    hear(&mut model, view(0, 5));
    hear(&mut model, view(1, 7));
    hear(
        &mut model,
        json!({"kind": "Labels", "labels": [["scout", 1]]}),
    );
    let nodes = vec![
        node(0, None, &json!({"System": "sys"})),
        user(1, 0, "first question"),
        reply(2, 1, "first answer"),
        reply(3, 2, "more"),
        user(4, 3, "second question"),
        reply(5, 4, "second answer"),
        user(6, 3, "other question"),
        reply(7, 6, "other answer"),
    ];
    hear(&mut model, json!({"kind": "Appended", "nodes": nodes}));
    model
}

fn opened(model: &Model) -> Tree {
    let mut tree = Tree::new(0);
    tree.redraw(model).unwrap();
    tree
}

fn press(tree: &mut Tree, model: &Model, code: KeyCode) -> Act {
    tree.key(KeyEvent::new(code, KeyModifiers::NONE), model)
        .unwrap()
}

fn screen(tree: &Tree, model: &Model) -> Vec<String> {
    let marks = tree.marks_of(model);
    tree.flat()
        .iter()
        .map(|f| {
            let node = model.node(f.row.node as u64).unwrap();
            let label = treetext::row_label(&f.row, node, &marks, !f.open);
            let (text, _) = treetext::fit(&label, 200);
            format!("{}{text}", "  ".repeat(f.depth))
        })
        .collect()
}

#[test]
fn the_viewed_walk_is_the_spine_and_the_other_branch_a_stub_with_its_cursor() {
    let model = session();
    let tree = opened(&model);
    let rows = screen(&tree, &model);
    assert_eq!(rows.len(), 5, "{rows:#?}");
    assert!(rows[0].starts_with("system: system prompt"), "{rows:#?}");
    assert!(rows[1].starts_with("user: first question  +2 · 2 calls") || rows[1].contains("+2"));
    assert!(
        rows[1].contains("1 branch") || rows[2].contains('├'),
        "{rows:#?}"
    );
    let stub = rows.iter().find(|r| r.contains('├')).unwrap();
    assert!(stub.contains("2 nodes, 1 turn"), "{stub}");
    assert!(stub.contains("◀ 1 scout"), "{stub}");
    assert!(
        rows.iter().any(|r| r.contains("◀ [0]")),
        "the viewed cursor's own mark is bracketed: {rows:#?}"
    );
}

#[test]
fn the_highlight_follows_the_viewed_cursor_until_it_is_moved_by_hand() {
    let mut model = session();
    let mut tree = opened(&model);
    let at = |tree: &Tree| tree.highlighted().unwrap().row.node;
    assert_eq!(at(&tree), 5, "the cursor's own node, in the turn it opened");
    assert!(tree.title().ends_with("following"));
    press(&mut tree, &model, KeyCode::Up);
    assert!(tree.title().ends_with("paused (f follows)"));
    hear(&mut model, view(0, 5)); // the session changes: a paused highlight stays
    tree.redraw(&model).unwrap();
    assert_ne!(at(&tree), 5);
    press(&mut tree, &model, KeyCode::Char('f'));
    assert_eq!(at(&tree), 5);
    assert!(tree.title().ends_with("following"));
}

#[test]
fn enter_goes_to_a_node_and_asks_to_unfold_a_stub_first() {
    let model = session();
    let mut tree = opened(&model);
    assert_eq!(
        press(&mut tree, &model, KeyCode::Enter),
        Act::Go { cursor: 0, node: 5 }
    );
    assert_eq!(press(&mut tree, &model, KeyCode::Char('o')), Act::Open(5));
    let stub = tree
        .flat()
        .iter()
        .position(|f| f.row.kind == Kind::Stub)
        .unwrap();
    press(&mut tree, &model, KeyCode::Home);
    while tree.highlight() != stub {
        press(&mut tree, &model, KeyCode::Down);
    }
    let Act::Say(said) = press(&mut tree, &model, KeyCode::Enter) else {
        panic!("a stub is not a node")
    };
    assert!(said.contains("Unfold the branch"));
    press(&mut tree, &model, KeyCode::Right);
    assert!(tree.flat().len() > 5, "the stub's own line is shown");
    press(&mut tree, &model, KeyCode::Down); // into the branch
    assert!(matches!(
        press(&mut tree, &model, KeyCode::Enter),
        Act::Go { .. }
    ));
}

#[test]
fn v_views_the_cursor_on_the_branch_and_the_walk_changes() {
    let mut model = session();
    let mut tree = opened(&model);
    let stub = tree
        .flat()
        .iter()
        .position(|f| f.row.kind == Kind::Stub)
        .unwrap();
    press(&mut tree, &model, KeyCode::Home);
    while tree.highlight() != stub {
        press(&mut tree, &model, KeyCode::Down);
    }
    assert_eq!(press(&mut tree, &model, KeyCode::Char('v')), Act::View(1));
    let needs = model.attach(1);
    assert!(needs.contains(&Need::Watch(1)));
    tree.view_changed();
    tree.redraw(&model).unwrap();
    assert_eq!(model.cursor(), Some(1));
    assert!(
        tree.walk_line().starts_with("cursor 1: a walk of 6 nodes"),
        "{}",
        tree.walk_line()
    );
    assert_eq!(tree.highlighted().unwrap().row.node, 7, "cursor 1's node");
    let rows = screen(&tree, &model);
    assert!(rows.iter().any(|r| r.contains("◀ [1] scout")), "{rows:#?}");
    assert!(
        rows.iter().any(|r| r.contains("◀ 0")),
        "the other cursor is a stub now: {rows:#?}"
    );
}

#[test]
fn v_where_no_cursor_is_says_so() {
    let model = session();
    let mut tree = opened(&model);
    press(&mut tree, &model, KeyCode::Home);
    assert_eq!(
        press(&mut tree, &model, KeyCode::Char('v')),
        Act::Say("No open cursor here".to_owned())
    );
}

#[test]
fn folds_are_kept_across_a_redraw_and_left_steps_out() {
    let mut model = session();
    let mut tree = opened(&model);
    let turn = tree.flat().iter().position(|f| f.row.node == 1).unwrap();
    press(&mut tree, &model, KeyCode::Home);
    while tree.highlight() != turn {
        press(&mut tree, &model, KeyCode::Down);
    }
    press(&mut tree, &model, KeyCode::Right);
    let open = tree.flat().len();
    assert!(open > 5);
    press(&mut tree, &model, KeyCode::Down);
    press(&mut tree, &model, KeyCode::Left); // a leaf: steps out to its turn
    assert_eq!(tree.highlighted().unwrap().row.node, 1);
    hear(&mut model, view(0, 5));
    tree.redraw(&model).unwrap();
    assert_eq!(
        tree.flat().len(),
        open,
        "the unfold the person made survived"
    );
    press(&mut tree, &model, KeyCode::Left); // folds it
    assert_eq!(tree.flat().len(), 5);
}

#[test]
fn brackets_jump_between_cursor_marks_around_the_ends() {
    let model = session();
    let mut tree = opened(&model);
    press(&mut tree, &model, KeyCode::Char('['));
    let first = tree.highlighted().unwrap().row.node;
    assert_eq!(
        first, 7,
        "back from node 5 wraps to cursor 1's node 7, in the unfolded branch"
    );
    press(&mut tree, &model, KeyCode::Char(']'));
    assert_eq!(
        tree.highlighted().unwrap().row.node,
        5,
        "forward from the last wraps to node 5"
    );
}

#[test]
fn a_node_that_arrives_extends_the_tree_without_losing_the_picture() {
    let mut model = session();
    let mut tree = opened(&model);
    hear(
        &mut model,
        json!({"kind": "Appended", "nodes": [user(8, 5, "third")]}),
    );
    hear(&mut model, view(0, 8));
    tree.redraw(&model).unwrap();
    let rows = screen(&tree, &model);
    assert!(
        rows.iter().any(|r| r.starts_with("user: third")),
        "{rows:#?}"
    );
    assert_eq!(
        tree.highlighted().unwrap().row.node,
        8,
        "following the cursor"
    );
}

#[test]
fn a_cursor_at_a_node_not_read_is_reported_and_drawn_once_it_is() {
    let mut model = session();
    let mut tree = opened(&model);
    let before = tree.flat().len();
    hear(&mut model, view(0, 9)); // its node has not arrived
    assert_eq!(tree.redraw(&model).unwrap(), Drawn::Missing(0, 9));
    assert_eq!(tree.flat().len(), before, "the picture is kept meanwhile");
    assert_eq!(
        model.begin_read(),
        vec![Need::Page(8)],
        "the missing nodes are asked for"
    );
    let page: Vec<Node> = vec![
        serde_json::from_value(reply(8, 7, "later")).unwrap(),
        serde_json::from_value(user(9, 8, "last")).unwrap(),
    ];
    model.paged(&page);
    assert_eq!(tree.redraw(&model).unwrap(), Drawn::Done);
    assert_eq!(tree.highlighted().unwrap().row.node, 9);
}

#[test]
fn a_tree_comes_back_after_a_reconnect_where_it_was() {
    let model = session();
    let mut tree = opened(&model);
    press(&mut tree, &model, KeyCode::Home);
    let (following, node) = tree.keep();
    assert!(!following);
    // A fresh model reads the same session; the new tree holds the node.
    let fresh = session();
    let mut again = Tree::new(0);
    again.resume(following, node);
    again.redraw(&fresh).unwrap();
    assert_eq!(again.highlighted().unwrap().row.node, node.unwrap());
    // A node the new session lacks is ignored, not an error.
    again.resume(false, Some(999));
    again.redraw(&fresh).unwrap();
}

#[test]
fn the_whole_tree_is_read_in_pages_until_an_empty_one() {
    let mut model = Model::new(ME);
    hear(&mut model, view(0, 1));
    assert_eq!(model.begin_read(), vec![Need::Page(0)]);
    assert!(model.begin_read().is_empty(), "already reading");
    let nodes: Vec<Node> = vec![
        serde_json::from_value(node(0, None, &json!({"System": "s"}))).unwrap(),
        serde_json::from_value(user(1, 0, "q")).unwrap(),
    ];
    assert_eq!(model.paged(&nodes), vec![Need::Page(2)]);
    assert!(model.reading());
    assert!(model.paged(&[]).is_empty());
    assert!(!model.reading());
    assert_eq!(model.dense(), 2);
}

/// 30,000 nodes: reports what a first draw and a redraw cost, and asserts nothing about time.
#[test]
fn thirty_thousand_nodes_are_drawn_and_redrawn_and_reported() {
    let mut model = Model::new(ME);
    hear(&mut model, view(0, 30_000));
    hear(&mut model, json!({"kind": "Labels", "labels": []}));
    let mut nodes = vec![node(0, None, &json!({"System": "s"}))];
    for turn in 0..15_000u64 {
        let id = 1 + 2 * turn;
        nodes.push(user(id, id - 1, &format!("question {turn}")));
        nodes.push(reply(id + 1, id, &format!("answer {turn}")));
    }
    hear(&mut model, json!({"kind": "Appended", "nodes": nodes}));
    let mut tree = Tree::new(0);
    tree.redraw(&model).unwrap();
    let first = tree.took;
    let rows = tree.flat().len();
    hear(
        &mut model,
        json!({"kind": "Appended", "nodes": [user(30_001, 30_000, "one more")]}),
    );
    hear(&mut model, view(0, 30_001));
    tree.redraw(&model).unwrap();
    let marks = tree.marks_of(&model);
    let started = Instant::now();
    let window = tree.window(40);
    let shown: Vec<_> = window
        .clone()
        .map(|i| {
            let f = &tree.flat()[i];
            let n = model.node(f.row.node as u64).unwrap();
            treetext::row_label(&f.row, n, &marks, !f.open)
        })
        .collect();
    eprintln!(
        "30,002 nodes, {rows} rows: first draw {first:?}, redraw {:?}, words for {} visible rows \
         in {:?}",
        tree.took,
        shown.len(),
        started.elapsed()
    );
    assert_eq!(tree.highlighted().unwrap().row.node, 30_001);
}

#[test]
fn a_provider_waiting_to_retry_is_said_on_the_tree_screen_too() {
    use ratatui::{Terminal, backend::TestBackend}; // rules: allow(backend): ratatui API
    let mut model = session();
    hear(
        &mut model,
        json!({"kind": "Live", "cursor": 0, "delta": {
            "kind": "Retrying", "attempt": 1, "attempts": 3, "seconds": 2.0,
            "reason": "HTTP 529"}}),
    );
    let tree = opened(&model);
    let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap(); // rules: allow(backend): ratatui API
    terminal
        .draw(|f| crate::treeui::draw(f, &tree, &model))
        .unwrap();
    let screen: String = terminal
        .backend() // rules: allow(backend): ratatui API
        .buffer()
        .content()
        .iter()
        .map(|c| c.symbol())
        .collect();
    assert!(screen.contains("HTTP 529: retry 1 of 3 in 2 s"), "{screen}");
}
