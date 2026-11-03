//! The model against a recorded stream: what a real `tau serve` sent to a client that said
//! `hello`, `watch`ed cursor 0, read the walk to node 6 and sent "hello" (testdata/session.jsonl;
//! the session has a tool call, a thinking part and a message from another client).

use serde_json::Value;

use super::*;
use crate::wire::{Event, WalkResult};

const ME: &str = "tester@host:ratatui";

/// The server's messages in the order recorded, each as the event or the response it was.
enum Message {
    Event(Event),
    Response { id: u64, result: Value },
}

fn recorded() -> Vec<Message> {
    include_str!("../../testdata/session.jsonl")
        .lines()
        .map(|line| {
            let message: Value = serde_json::from_str(line).unwrap();
            match message.get("id") {
                Some(id) => Message::Response {
                    id: id.as_u64().unwrap(),
                    result: message["result"].clone(),
                },
                None => Message::Event(serde_json::from_value(message["params"].clone()).unwrap()),
            }
        })
        .collect()
}

/// Feed the recording up to and including response `id`, doing what the model asks as it goes:
/// the walk it asks for is the recorded answer to `walk`.
fn replay_through(model: &mut Model, id: u64) -> Vec<Need> {
    let mut asked = Vec::new();
    for message in recorded() {
        match message {
            Message::Event(event) => asked.extend(model.hear(event)),
            Message::Response { id: got, result } => {
                if got == 3 {
                    let WalkResult(nodes) = serde_json::from_value(result).unwrap();
                    asked.extend(model.walked(nodes));
                }
                if got == id {
                    break;
                }
            }
        }
    }
    asked
}

fn shown(rows: &[Row]) -> Vec<String> {
    rows.iter()
        .map(|r| format!("{}: {}", r.label, r.body))
        .collect()
}

#[test]
fn the_snapshot_picks_the_first_cursor_and_asks_to_watch_and_read_its_walk() {
    let mut model = Model::new(ME);
    let first = recorded().into_iter().find_map(|m| match m {
        Message::Event(e @ Event::CursorView(_)) => Some(e),
        _ => None,
    });
    assert!(
        model.hear(first.unwrap()).is_empty(),
        "nothing is chosen before the snapshot is whole"
    );
    let labels = recorded().into_iter().find_map(|m| match m {
        Message::Event(e @ Event::Labels(_)) => Some(e),
        _ => None,
    });
    assert_eq!(
        model.hear(labels.unwrap()),
        vec![Need::Watch(0), Need::Walk(6)]
    );
    assert_eq!(model.cursor(), Some(0));
}

#[test]
fn the_walk_reads_as_a_conversation() {
    let mut model = Model::new(ME);
    let asked = replay_through(&mut model, 3);
    assert_eq!(asked, vec![Need::Watch(0), Need::Walk(6)]);
    assert_eq!(
        shown(&model.rows()),
        vec![
            "you: list the files",
            "thinking: I should look",
            "assistant: Looking.",
            "call: bash {\"cmd\":\"ls\"}",
            "bash: a.txt\nb.txt",
            "assistant: Two files.",
            "ann@elsewhere:web: thanks",
        ]
    );
    assert_eq!(model.rows()[0].kind, Kind::Mine);
    assert_eq!(
        model.rows()[6].kind,
        Kind::Other,
        "another client's message is under its name"
    );
}

#[test]
fn a_reply_streams_over_the_walk_and_the_node_replaces_it() {
    let mut model = Model::new(ME);
    let all = recorded().len();
    // Up to the second `Live` delta: the user node arrived, the reply is a layer.
    let mut heard = 0;
    let mut stopped_at_text = None;
    for message in recorded() {
        heard += 1;
        match message {
            Message::Event(event) => {
                let live = matches!(&event, Event::Live(_));
                model.hear(event);
                if live {
                    stopped_at_text = Some(shown(&model.rows()));
                }
            }
            Message::Response { id: 3, result } => {
                let WalkResult(nodes) = serde_json::from_value(result).unwrap();
                model.walked(nodes);
            }
            Message::Response { .. } => {}
        }
        if heard == all - 2 {
            break; // before the reply's node
        }
    }
    let streaming = stopped_at_text.unwrap();
    assert_eq!(streaming.last().unwrap(), "assistant: mock: hello");
    assert_eq!(
        streaming[streaming.len() - 2],
        "you: hello",
        "over the user node, which arrived"
    );
    assert!(model.view().unwrap().busy);

    // The reply's node and the cursor resting: the layer is gone and the node says the same.
    let mut model = Model::new(ME);
    replay_through(&mut model, u64::MAX);
    let rows = shown(&model.rows());
    assert_eq!(
        rows[rows.len() - 2..],
        ["you: hello", "assistant: mock: hello"]
    );
    assert_eq!(
        rows.iter().filter(|r| r.contains("mock: hello")).count(),
        1,
        "said once"
    );
    assert!(!model.view().unwrap().busy);
    assert!(model.trouble().is_none());
}

#[test]
fn a_pending_line_is_shown_until_it_is_delivered() {
    let mut model = Model::new(ME);
    let mut seen_pending = false;
    for message in recorded() {
        match message {
            Message::Event(event) => {
                model.hear(event);
                seen_pending |= shown(&model.pending()) == ["tester@host:ratatui (waiting): hello"]
                    || shown(&model.pending()) == ["you (waiting): hello"];
            }
            Message::Response { id: 3, result } => {
                let WalkResult(nodes) = serde_json::from_value(result).unwrap();
                model.walked(nodes);
            }
            Message::Response { .. } => {}
        }
    }
    assert!(seen_pending);
    assert!(model.pending().is_empty());
}

#[test]
fn a_cursor_that_is_closed_under_us_is_lost() {
    let mut model = Model::new(ME);
    replay_through(&mut model, 3);
    let gone = serde_json::json!({"kind": "CursorGone", "cursor": 0, "at": 6});
    model.hear(serde_json::from_value(gone).unwrap());
    assert_eq!(model.lost(), Some("cursor 0 was closed"));
}

#[test]
fn with_only_closed_cursors_the_first_is_reopened() {
    let mut model = Model::new(ME);
    let gone = serde_json::json!({"kind": "CursorGone", "cursor": 2, "at": 6});
    let labels = serde_json::json!({"kind": "Labels", "labels": []});
    assert!(model.hear(serde_json::from_value(gone).unwrap()).is_empty());
    assert_eq!(
        model.hear(serde_json::from_value(labels).unwrap()),
        vec![Need::Reopen(2)]
    );
}

#[test]
fn a_session_with_no_cursor_is_lost_not_shown_empty() {
    let mut model = Model::new(ME);
    let labels = serde_json::json!({"kind": "Labels", "labels": []});
    model.hear(serde_json::from_value(labels).unwrap());
    assert_eq!(model.lost(), Some("the session has no cursor"));
}

#[test]
fn only_the_questions_for_this_client_are_its_to_answer() {
    let mut model = Model::new(ME);
    replay_through(&mut model, 3);
    let question = |id: u64, to: Value| {
        serde_json::json!({"id": id, "to": to, "extension": "gate", "text": "ok?",
                           "schema": {"type": "string", "enum": ["yes", "no"]}})
    };
    let view = serde_json::json!({"kind": "CursorView", "cursor": 0, "at": 6, "busy": true,
        "auto": true, "items": [], "ending": null, "hook_error": null, "model": "m", "shown": [],
        "questions": [question(1, serde_json::json!("someone@else:web")), question(2, Value::Null),
                      question(3, serde_json::json!(ME))]});
    model.hear(serde_json::from_value(view).unwrap());
    let ids: Vec<u64> = model.questions().iter().map(|q| q.id).collect();
    assert_eq!(ids, vec![2, 3]);
}

#[test]
fn a_failed_step_is_reported() {
    let mut model = Model::new(ME);
    replay_through(&mut model, 3);
    let view = serde_json::json!({"kind": "CursorView", "cursor": 0, "at": 6, "busy": false,
        "auto": true, "items": [], "ending": {"Failed": "boom"}, "hook_error": null,
        "model": "m", "shown": [], "questions": []});
    model.hear(serde_json::from_value(view).unwrap());
    assert_eq!(model.trouble().as_deref(), Some("the step failed: boom"));
}

fn event(json: &serde_json::Value) -> Event {
    serde_json::from_value(json.clone()).unwrap()
}

fn view_of(cursor: u64, at: u64) -> Event {
    event(&serde_json::json!({
        "kind": "CursorView", "cursor": cursor, "at": at, "busy": false, "auto": false,
        "questions": [], "items": [], "ending": null, "hook_error": null, "model": "m",
        "shown": []
    }))
}

#[test]
fn the_viewed_cursor_closing_moves_the_view_to_the_lowest_open_one() {
    let mut model = Model::new(ME);
    model.hear(view_of(0, 6));
    model.hear(view_of(1, 3));
    model.hear(event(
        &serde_json::json!({"kind": "Labels", "labels": [["scout", 1]]}),
    ));
    assert_eq!(model.cursor(), Some(0));
    let gone = event(&serde_json::json!({"kind": "CursorGone", "cursor": 0, "at": 6}));
    let needs = model.hear(gone);
    assert_eq!(model.cursor(), Some(1));
    assert!(needs.contains(&Need::Watch(1)));
    assert_eq!(model.lost(), None);
    assert!(model.notices().any(|n| n.contains("showing cursor 1")));
    assert_eq!(
        model.gone().get(&0),
        Some(&6),
        "its last node is kept for restarting"
    );
    assert_eq!(model.labels(), [("scout".to_owned(), 1)]);
}

#[test]
fn switching_the_viewed_cursor_drops_the_live_layer() {
    let mut model = Model::new(ME);
    replay_through(&mut model, 3);
    model.hear(view_of(1, 3));
    model.hear(event(&serde_json::json!({
        "kind": "Live", "cursor": 0, "delta": {"kind": "TextDelta", "text": "streaming"}
    })));
    assert!(shown(&model.rows()).iter().any(|r| r.contains("streaming")));
    model.attach(1);
    assert!(!shown(&model.rows()).iter().any(|r| r.contains("streaming")));
}

#[test]
fn a_provider_waiting_to_retry_is_said_until_the_sample_goes_on() {
    let mut model = Model::new(ME);
    replay_through(&mut model, u64::MAX);
    let live = |delta: Value| -> Event {
        serde_json::from_value(serde_json::json!({"kind": "Live", "cursor": 0, "delta": delta}))
            .unwrap()
    };
    assert_eq!(model.retrying(), None);
    model.hear(live(serde_json::json!({
        "kind": "Retrying", "attempt": 1, "attempts": 3, "seconds": 2.0,
        "reason": "HTTP 529: overloaded_error: Overloaded"
    })));
    assert_eq!(
        model.retrying(),
        Some("HTTP 529: overloaded_error: Overloaded: retry 1 of 3 in 2 s")
    );
    model.hear(live(serde_json::json!({
        "kind": "Retrying", "attempt": 2, "attempts": 3, "seconds": 2.5, "reason": "HTTP 503"
    })));
    assert_eq!(model.retrying(), Some("HTTP 503: retry 2 of 3 in 2.5 s"));
    model.hear(live(
        serde_json::json!({"kind": "TextDelta", "text": "back"}),
    ));
    assert_eq!(model.retrying(), None, "the next delta takes the line away");
}
