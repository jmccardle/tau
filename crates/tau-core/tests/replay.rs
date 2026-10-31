//! Reload is replay (I7): `Session::load` over the log's records and the
//! scratchpad file gives back the state at the last sync.

use proptest::prelude::*;
use tau_core::machine::Machine;
use tau_core::step::{Answer, Draft, Job, SampleEnd};
use tau_core::tree::{
    Author, Body, CursorId, Error, ItemData, NodeId, Outcome, Part, Record, Scratchpad, Session,
    Stop,
};

/// Input, then auto-step on: what a frontend does to send.
fn send(m: &mut Machine, cursor: u64, by: Author, text: &str) -> u64 {
    let item = m.input(cursor, by, text).unwrap();
    m.set_auto(cursor, true).unwrap();
    item
}

/// What the store holds after a crash: the records up to the last `Sync` and
/// the scratchpad from the last `Sync` that carried one.
fn on_disk(s: &Session) -> (Vec<Record>, Scratchpad) {
    let end = s
        .outbox
        .iter()
        .rposition(|r| matches!(r, Record::Sync { .. }))
        .map_or(0, |i| i + 1);
    let log = s.outbox[..end].to_vec();
    let pad = log
        .iter()
        .rev()
        .find_map(|r| match r {
            Record::Sync { pad: Some(p) } => Some(p.clone()),
            _ => None,
        })
        .unwrap_or_default();
    (log, pad)
}

fn crash(s: &Session) -> Session {
    let (log, pad) = on_disk(s);
    tau_core::tree::load(log, pad).unwrap()
}

/// `s` as a load would give it back: nothing queued, every cursor at rest
/// with auto-step off, no in-memory counts or notices.
fn as_loaded(s: &Session) -> Session {
    let mut s = s.clone();
    tau_core::tree::reconcile(&mut s);
    s.outbox.clear();
    s.changes.clear();
    s.notices.clear();
    s
}

#[derive(Clone, Debug)]
enum Op {
    Open(usize),
    User(usize),
    Batch(usize, usize),
    Answer(usize),
    Side(usize),
    Go(usize, usize),
    Label(usize, usize),
    Unlabel(usize),
    Input(usize, usize),
    Deliver(usize),
    Drop(usize),
    Sync,
}

fn op() -> impl Strategy<Value = Op> {
    let n = 0..64usize;
    prop_oneof![
        n.clone().prop_map(Op::Open),
        n.clone().prop_map(Op::User),
        (n.clone(), 1..4usize).prop_map(|(c, k)| Op::Batch(c, k)),
        n.clone().prop_map(Op::Answer),
        n.clone().prop_map(Op::Side),
        (n.clone(), n.clone()).prop_map(|(c, t)| Op::Go(c, t)),
        (0..3usize, n.clone()).prop_map(|(l, c)| Op::Label(l, c)),
        (0..3usize).prop_map(Op::Unlabel),
        (n.clone(), 0..4usize).prop_map(|(c, a)| Op::Input(c, a)),
        n.clone().prop_map(Op::Deliver),
        n.clone().prop_map(Op::Drop),
        Just(Op::Sync),
    ]
}

/// Who wrote an input: a frontend, an extension, or any other author (core records them all).
fn author(k: usize) -> Author {
    match k % 4 {
        0 => Author::Frontend("t".into()),
        1 => Author::Extension("ext".into()),
        2 => Author::Tools,
        _ => Author::Core,
    }
}

fn cursors(s: &Session) -> u64 {
    (0..CursorId::MAX).take_while(|c| s.at(*c).is_ok()).count() as u64
}

fn nodes(s: &Session) -> u64 {
    (0..NodeId::MAX)
        .take_while(|n| s.tree.node(*n).is_ok())
        .count() as u64
}

/// Applies `op` with every index taken modulo what exists; ops that do not
/// apply (an answer with no batch, a delivery with no item) do nothing.
fn apply(s: &mut Session, op: &Op) {
    let (nc, nn) = (cursors(s), nodes(s));
    let cursor = move |i: usize| i as u64 % nc;
    let node = move |i: usize| i as u64 % nn;
    let by = Author::Core;
    match *op {
        Op::Open(t) => drop(s.open(node(t), by, 0).unwrap()),
        Op::User(i) => drop(
            s.write(cursor(i), Body::User("u".into()), vec![], by, 0)
                .unwrap(),
        ),
        Op::Batch(i, k) => {
            let parts = (0..k)
                .map(|j| Part::Call {
                    id: format!("c{j}"),
                    tool: "t".into(),
                    args_raw: "{}".into(),
                    replay: None,
                })
                .collect();
            let body = Body::Assistant {
                parts,
                stop: Stop::Tools,
                finish_reason: None,
                // Half the batches carry meta, so replay sees both shapes.
                meta: (k % 2 == 0).then(|| r#"{"model":"m","usage":{"total_tokens":3}}"#.into()),
            };
            s.write(cursor(i), body, vec![], Author::Provider, 0)
                .unwrap();
        }
        Op::Answer(i) => {
            let cur = cursor(i);
            let at = s.at(cur).unwrap();
            let out: Vec<_> = tau_core::tree::calls(&s.tree.node(at).unwrap().body)
                .into_iter()
                .map(|(id, ..)| (id.to_string(), Outcome::Ok("x".into()), Author::Tools))
                .collect();
            if !out.is_empty() {
                s.write(cur, Body::Results(out), vec![], Author::Tools, 0)
                    .unwrap();
            }
        }
        Op::Side(t) => {
            let body = Body::Custom {
                kind: "note".into(),
                data: "n".into(),
            };
            s.append(node(t), by, body, vec![], 0).unwrap();
        }
        Op::Go(i, t) => s.move_to(cursor(i), node(t), by, 0).unwrap(),
        Op::Label(l, i) => s.set_label(&format!("l{l}"), cursor(i)).unwrap(),
        Op::Unlabel(l) => drop(s.remove_label(&format!("l{l}"))),
        Op::Input(i, a) => {
            let data = ItemData::Input {
                by: author(a),
                text: "in".into(),
            };
            s.add_item(cursor(i), data);
        }
        Op::Deliver(i) => {
            let cur = cursor(i);
            let first = s.items(cur).next().map(|it| it.id);
            if let Some(item) = first {
                s.write(cur, Body::User("in".into()), vec![item], by, 0)
                    .unwrap();
            }
        }
        Op::Drop(k) => {
            let ids: Vec<u64> = s.scratchpad().items.iter().map(|i| i.id).collect();
            if !ids.is_empty() {
                s.remove_item(ids[k % ids.len()]);
            }
        }
        Op::Sync => s.sync(),
    }
}

proptest! {
    /// I7: at every sync, a load of what is on disk equals the state then.
    #[test]
    fn reload_at_any_sync_equals_the_state_at_that_sync(ops in prop::collection::vec(op(), 0..80)) {
        let (mut s, _) = Session::create("{}", "sys", 0);
        s.sync();
        let mut synced = vec![as_loaded(&s)];
        for op in &ops {
            apply(&mut s, op);
            if matches!(op, Op::Sync) && synced.last() != Some(&as_loaded(&s)) {
                synced.push(as_loaded(&s));
            }
        }
        // A crash now loses everything after the last sync.
        prop_assert_eq!(&crash(&s), synced.last().unwrap());
        s.sync();
        prop_assert_eq!(crash(&s), as_loaded(&s));
    }

    /// The log is synced before the scratchpad is rewritten, so a crash in
    /// between gives a newer log with an older scratchpad: nothing is
    /// delivered twice.
    #[test]
    fn an_older_scratchpad_never_redelivers(ops in prop::collection::vec(op(), 0..80)) {
        let (mut s, _) = Session::create("{}", "sys", 0);
        s.sync();
        let mut pads = vec![Scratchpad::default()];
        for op in &ops {
            apply(&mut s, op);
            if let Some(Record::Sync { pad: Some(p) }) = s.outbox.last()
                && matches!(op, Op::Sync)
            {
                pads.push(p.clone());
            }
        }
        s.sync();
        let (log, _) = on_disk(&s);
        for pad in pads {
            let loaded = tau_core::tree::load(log.clone(), pad).unwrap();
            for item in &loaded.scratchpad().items {
                let delivered = (0..nodes(&loaded))
                    .any(|n| loaded.tree.node(n).unwrap().delivers.contains(&item.id));
                prop_assert!(!delivered, "item {} pending and delivered", item.id);
            }
        }
    }
}

#[test]
fn a_record_out_of_sequence_is_refused_with_its_position() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    s.write(c, Body::User("a".into()), vec![], Author::Core, 0)
        .unwrap();
    let mut log = s.outbox.clone();
    log.swap(0, 1); // the System node before its Config root
    assert_eq!(
        tau_core::tree::load(log, Scratchpad::default()),
        Err(Error::BadRecord(0))
    );
    let mut log = s.outbox.clone();
    log.remove(2); // the cursor's opening move
    assert_eq!(
        tau_core::tree::load(log, Scratchpad::default()),
        Err(Error::BadRecord(2))
    );
}

#[test]
fn labels_survive_a_reload() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let d = s.open(s.at(c).unwrap(), Author::Core, 0).unwrap();
    s.set_label("main", c).unwrap();
    s.set_label("scout", c).unwrap();
    s.set_label("main", d).unwrap();
    s.remove_label("scout").unwrap();
    s.sync();
    let loaded = crash(&s);
    assert_eq!(loaded.label("main"), Ok(d));
    assert_eq!(
        loaded.label("scout"),
        Err(Error::NoSuchLabel("scout".into()))
    );
}

fn reply(parts: Vec<Part>, stop: Stop) -> Answer {
    sampled(Draft {
        body: Body::Assistant {
            parts,
            stop,
            finish_reason: None,
            meta: None,
        },
        delivers: vec![],
        by: Author::Provider,
    })
}

/// The hooks' answer that changes nothing: a job's draft comes back as it was.
fn echo(job: &Job) -> Answer {
    match job {
        Job::Hooks { draft, .. } => Answer::Hooks {
            stop: None,
            draft: draft.clone(),
        },
        _ => ok(),
    }
}

fn ok() -> Answer {
    Answer::Hooks {
        stop: None,
        draft: None,
    }
}

/// Runs `m`'s cursor until `stop` says a job is where the power goes out.
fn until(
    m: &mut Machine,
    c: u64,
    mut stop: impl FnMut(&Job) -> bool,
    mut script: impl FnMut(&Job) -> Vec<Answer>,
) {
    for _ in 0..200 {
        let job = m.next_job(c).unwrap();
        if stop(&job) {
            return;
        }
        assert_ne!(job, Job::Rest, "rested before the crash point");
        for answer in script(&job) {
            m.answer(c, answer, 0).unwrap();
        }
    }
    panic!("no crash point");
}

#[test]
fn crash_during_a_sample_loses_the_unsynced_input_node_and_redelivers_its_item() {
    let (s, c) = Session::create("{}", "sys", 0);
    let mut m = Machine::new(s);
    send(&mut m, c, Author::Frontend("term".into()), "hi");
    until(&mut m, c, |j| *j == Job::Sample, |j| vec![echo(j)]);
    // The input was committed but not yet synced; its item was.
    let mut m = Machine::new(crash(&m.session));
    assert_eq!(m.session.next(c), tau_core::tree::Next::Wait);
    assert_eq!(m.session.items(c).count(), 1, "the input is pending again");
    send(&mut m, c, Author::Frontend("term".into()), "");
    let mut sampled = 0;
    until(
        &mut m,
        c,
        |j| *j == Job::Rest,
        |j| match j {
            Job::Sample => {
                sampled += 1;
                vec![reply(vec![Part::text("hello")], Stop::End)]
            }
            _ => vec![echo(j)],
        },
    );
    assert_eq!(sampled, 1);
    let users = m
        .session
        .tree
        .walk(m.session.at(c).unwrap())
        .iter()
        .filter(|n| matches!(&n.body, Body::User(t) if t == "hi"))
        .count();
    assert_eq!(users, 1, "delivered once");
}

#[test]
fn crash_mid_batch_rests_at_the_batch_after_a_real_reload() {
    let (s, c) = Session::create("{}", "sys", 0);
    let mut m = Machine::new(s);
    send(&mut m, c, Author::Frontend("term".into()), "look");
    let call = |id: &str| Part::Call {
        id: id.into(),
        tool: "ls".into(),
        args_raw: "{}".into(),
        replay: None,
    };
    let fed_a = std::cell::Cell::new(false);
    until(
        &mut m,
        c,
        |j| *j == Job::Pending && fed_a.get(),
        |j| match j {
            Job::Sample => vec![reply(vec![call("a"), call("b")], Stop::Tools)],
            Job::RunCalls { .. } => {
                fed_a.set(true);
                vec![Answer::CallDone {
                    call: "a".into(),
                    outcome: Outcome::Ok("done".into()),
                    by: Author::Tools,
                }]
            }
            _ => vec![echo(j)],
        },
    );
    let loaded = crash(&m.session);
    let at = loaded.tree.node(loaded.at(c).unwrap()).unwrap();
    assert!(
        matches!(
            at.body,
            Body::Assistant {
                stop: Stop::Tools,
                ..
            }
        ),
        "rests at the batch"
    );
    assert!(!loaded.auto[&c]);
}

/// A sample that finished with `d`'s body.
fn sampled(d: Draft) -> Answer {
    let Body::Assistant {
        parts,
        stop,
        finish_reason,
        meta,
    } = d.body
    else {
        panic!("a reply is an Assistant body")
    };
    Answer::Sampled {
        parts,
        end: SampleEnd::Finished {
            stop,
            reason: finish_reason,
            meta,
        },
    }
}
