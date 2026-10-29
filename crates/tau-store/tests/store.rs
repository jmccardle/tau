//! The store (D12): a session on disk reloads as the state at its last
//! `Sync` (I7), through torn tails, garbage, and a held lease.

use std::fs;
use std::path::Path;

use proptest::prelude::*;
use tau_core::machine::Machine;
use tau_core::step::{Draft, Fed, Hook, Job};
use tau_core::tree::{Author, Body, ItemData, Part, Session, Stop};
use tau_store::{Error, HEADER, Store, VERSION};

/// `s` as a load gives it back: nothing queued, every cursor at rest with
/// auto-step off, no in-memory counts or notices.
fn as_loaded(s: &Session) -> Session {
    let mut s = s.clone();
    s.reconcile();
    s.outbox.clear();
    s.changes.clear();
    s.taken.clear();
    s.notices.clear();
    s
}

fn dir() -> tempfile::TempDir {
    tempfile::tempdir().unwrap()
}

#[derive(Clone, Debug)]
enum Op {
    Open(usize),
    User(usize),
    Go(usize, usize),
    Label(usize, usize),
    Input(usize),
    Deliver(usize),
    Sync,
}

fn op() -> impl Strategy<Value = Op> {
    let n = 0..64usize;
    prop_oneof![
        n.clone().prop_map(Op::Open),
        n.clone().prop_map(Op::User),
        (n.clone(), n.clone()).prop_map(|(c, t)| Op::Go(c, t)),
        (0..3usize, n.clone()).prop_map(|(l, c)| Op::Label(l, c)),
        n.clone().prop_map(Op::Input),
        n.clone().prop_map(Op::Deliver),
        Just(Op::Sync),
    ]
}

fn count(f: impl Fn(u64) -> bool) -> u64 {
    (0..u64::MAX).take_while(|i| f(*i)).count() as u64
}

fn apply(s: &mut Session, op: &Op) {
    let nc = count(|c| s.at(c).is_ok());
    let nn = count(|n| s.node(n).is_ok());
    let cursor = move |i: usize| i as u64 % nc;
    let node = move |i: usize| i as u64 % nn;
    let by = Author::Core;
    match *op {
        Op::Open(t) => drop(s.open(node(t), by, 0).unwrap()),
        Op::User(i) => drop(
            s.write(cursor(i), Body::User("u".into()), vec![], by, 0)
                .unwrap(),
        ),
        Op::Go(i, t) => s.move_to(cursor(i), node(t), by, 0).unwrap(),
        Op::Label(l, i) => s.set_label(&format!("l{l}"), cursor(i)).unwrap(),
        Op::Input(i) => {
            let data = ItemData::Input {
                client: "t".into(),
                text: "in".into(),
            };
            s.add_item(cursor(i), data);
        }
        Op::Deliver(i) => {
            let first = s.items(cursor(i)).next().map(|it| it.id);
            if let Some(item) = first {
                s.write(cursor(i), Body::User("in".into()), vec![item], by, 0)
                    .unwrap();
            }
        }
        Op::Sync => s.sync(),
    }
}

/// What is on disk after each save that wrote: the log's length, the
/// scratchpad file's bytes, and the state then.
type Synced = (u64, Option<Vec<u8>>, Session);

fn disk(path: &Path, s: &Session) -> Synced {
    let log = fs::metadata(path.join("log")).unwrap().len();
    (log, fs::read(path.join("scratchpad")).ok(), as_loaded(s))
}

/// A crash with the log cut at `len` (plus `garbage`) and the scratchpad file
/// as the last save that finished before `len` left it.
fn crash_at(log: &[u8], len: usize, garbage: &[u8], pad: Option<&Vec<u8>>) -> tempfile::TempDir {
    let d = dir();
    let mut bytes = log[..len].to_vec();
    bytes.extend_from_slice(garbage);
    fs::write(d.path().join("log"), bytes).unwrap();
    if let Some(pad) = pad {
        fs::write(d.path().join("scratchpad"), pad).unwrap();
    }
    d
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(64))]

    /// I7 through the store: a log cut anywhere, with anything after the cut,
    /// opens as the state at the last `Sync` wholly before the cut.
    #[test]
    fn a_torn_tail_opens_as_the_last_sync_before_it(
        ops in prop::collection::vec(op(), 0..60),
        cut in any::<prop::sample::Index>(),
        garbage in prop::collection::vec(any::<u8>(), 0..40),
    ) {
        let d = dir();
        let (mut s, _) = Session::create("{}", "sys", 0);
        let mut store = Store::create(d.path(), &mut s).unwrap();
        let mut synced = vec![disk(d.path(), &s)];
        for op in &ops {
            apply(&mut s, op);
            store.save(&mut s).unwrap();
            let now = disk(d.path(), &s);
            if now.0 != synced.last().unwrap().0 {
                synced.push(now);
            }
        }
        drop(store);
        let log = fs::read(d.path().join("log")).unwrap();
        let first = usize::try_from(synced[0].0).unwrap();
        let len = first + cut.index(log.len() - first + 1);
        let k = synced.iter().rposition(|(l, ..)| *l <= len as u64).unwrap();
        let (_, pad, want) = &synced[k];
        let crashed = crash_at(&log, len, &garbage, pad.as_ref());
        let (store, loaded) = Store::open(crashed.path()).unwrap();
        prop_assert_eq!(&loaded, want);
        // Reopening cuts nothing more: the cut was made durable.
        drop(store);
        let (mut store, mut again) = Store::open(crashed.path()).unwrap();
        prop_assert_eq!(store.dropped, 0);
        prop_assert_eq!(&again, want);
        // The session goes on from the cut, and that reloads too.
        again.write(0, Body::User("after".into()), vec![], Author::Core, 0).unwrap();
        again.sync();
        store.save(&mut again).unwrap();
        drop(store);
        let (_, after) = Store::open(crashed.path()).unwrap();
        prop_assert_eq!(after, as_loaded(&again));
    }
}

#[test]
fn a_session_reopens_as_it_was_synced() {
    let d = dir();
    let (mut s, c) = Session::create("{}", "sys", 0);
    let mut store = Store::create(d.path(), &mut s).unwrap();
    s.set_label("main", c).unwrap();
    let data = ItemData::Input {
        client: "term".into(),
        text: "pending".into(),
    };
    s.add_item(c, data);
    s.sync();
    store.save(&mut s).unwrap();
    s.write(
        c,
        Body::User("never synced".into()),
        vec![],
        Author::Core,
        0,
    )
    .unwrap();
    store.save(&mut s).unwrap();
    drop(store);
    let (store, loaded) = Store::open(d.path()).unwrap();
    assert_eq!(store.dropped, 0, "unsynced records were never written");
    assert_eq!(loaded.label("main"), Ok(c));
    assert_eq!(loaded.items(c).count(), 1);
    assert!(matches!(
        loaded.node(loaded.at(c).unwrap()).unwrap().body,
        Body::System(_)
    ));
}

#[test]
fn one_save_over_many_syncs_reopens_as_the_last() {
    let d = dir();
    let (mut s, c) = Session::create("{}", "sys", 0);
    let mut store = Store::create(d.path(), &mut s).unwrap();
    let input = |text: &str| ItemData::Input {
        client: "term".into(),
        text: text.into(),
    };
    let first = s.add_item(c, input("first"));
    s.sync();
    s.write(c, Body::User("first".into()), vec![first], Author::Core, 0)
        .unwrap();
    s.sync();
    s.add_item(c, input("second"));
    s.sync();
    // The last `Sync` carries no pad: the newest is the one before it.
    s.write(c, Body::User("plain".into()), vec![], Author::Core, 0)
        .unwrap();
    s.sync();
    store.save(&mut s).unwrap();
    drop(store);
    let (store, loaded) = Store::open(d.path()).unwrap();
    assert_eq!(store.dropped, 0);
    assert_eq!(loaded, as_loaded(&s));
    let texts: Vec<_> = loaded.items(c).map(|i| i.data.clone()).collect();
    assert_eq!(texts, vec![input("second")]);
}

#[test]
fn the_log_never_holds_items() {
    let d = dir();
    let (mut s, c) = Session::create("{}", "sys", 0);
    let mut store = Store::create(d.path(), &mut s).unwrap();
    let data = ItemData::Input {
        client: "term".into(),
        text: "in the scratchpad only".into(),
    };
    s.add_item(c, data);
    s.sync();
    store.save(&mut s).unwrap();
    let log = fs::read(d.path().join("log")).unwrap();
    let pad = fs::read(d.path().join("scratchpad")).unwrap();
    let has = |b: &[u8]| b.windows(6).any(|w| w == b"scratc");
    assert!(!has(&log));
    assert!(has(&pad));
}

#[test]
fn the_lease_admits_one_store() {
    let d = dir();
    let (mut s, _) = Session::create("{}", "sys", 0);
    let store = Store::create(d.path(), &mut s).unwrap();
    assert!(matches!(Store::open(d.path()), Err(Error::Leased)));
    drop(store);
    let (_held, _) = Store::open(d.path()).unwrap();
    assert!(matches!(Store::open(d.path()), Err(Error::Leased)));
}

#[test]
fn create_refuses_an_existing_session() {
    let d = dir();
    let (mut s, _) = Session::create("{}", "sys", 0);
    drop(Store::create(d.path(), &mut s).unwrap());
    let (mut other, _) = Session::create("{}", "other", 0);
    assert!(matches!(
        Store::create(d.path(), &mut other),
        Err(Error::Exists)
    ));
    let (_, loaded) = Store::open(d.path()).unwrap();
    assert_eq!(loaded, as_loaded(&s), "the first session is untouched");
    let names: Vec<_> = fs::read_dir(d.path())
        .unwrap()
        .map(|f| f.unwrap().file_name())
        .collect();
    assert_eq!(names, ["log"], "no stray files");
}

#[test]
fn a_cut_tail_is_kept_aside() {
    let d = dir();
    let (mut s, _) = Session::create("{}", "sys", 0);
    drop(Store::create(d.path(), &mut s).unwrap());
    let log = d.path().join("log");
    let len = fs::metadata(&log).unwrap().len();
    let mut bytes = fs::read(&log).unwrap();
    bytes.extend_from_slice(b"torn");
    fs::write(&log, bytes).unwrap();
    let (store, loaded) = Store::open(d.path()).unwrap();
    assert_eq!(store.dropped, 4);
    assert_eq!(loaded, as_loaded(&s));
    assert_eq!(
        fs::read(d.path().join(format!("dropped-{len}"))).unwrap(),
        b"torn"
    );
}

#[test]
fn files_that_are_not_sessions_are_refused() {
    let d = dir();
    let (mut s, _) = Session::create("{}", "sys", 0);
    drop(Store::create(d.path(), &mut s).unwrap());
    let log = d.path().join("log");
    let good = fs::read(&log).unwrap();

    let mut v2 = good.clone();
    v2[8..HEADER].copy_from_slice(&(VERSION + 1).to_le_bytes());
    fs::write(&log, v2).unwrap();
    assert!(matches!(Store::open(d.path()), Err(Error::Version(v)) if v == VERSION + 1));

    fs::write(&log, b"not a log at all").unwrap();
    assert!(matches!(Store::open(d.path()), Err(Error::NotASession(_))));

    fs::write(&log, &good[..HEADER]).unwrap();
    assert!(
        matches!(Store::open(d.path()), Err(Error::NotASession(_))),
        "a log with no Sync"
    );

    fs::write(&log, &good).unwrap();
    fs::write(d.path().join("scratchpad"), b"tau-pad\0\x01\0\0\0junk").unwrap();
    assert!(matches!(Store::open(d.path()), Err(Error::Corrupt(_))));
}

#[test]
fn a_turn_saved_after_every_call_reopens_at_its_reply() {
    let d = dir();
    let (mut s, c) = Session::create("{}", "sys", 0);
    let mut store = Store::create(d.path(), &mut s).unwrap();
    let mut m = Machine::new(s);
    m.send(c, "term".into(), "hi", 0).unwrap();
    store.save(&mut m.session).unwrap();
    loop {
        let job = m.next_job(c).unwrap();
        store.save(&mut m.session).unwrap();
        let fed = match job {
            Job::Rest => break,
            Job::Sample => Fed::Sampled {
                reply: Some(Draft {
                    body: Body::Assistant {
                        parts: vec![Part::Text("hello".into())],
                        stop: Stop::End,
                    },
                    delivers: vec![],
                    by: Author::Provider,
                }),
                error: None,
            },
            _ => Fed::Hooks {
                stop: None,
                draft: None,
            },
        };
        m.feed(c, fed, 0).unwrap();
        store.save(&mut m.session).unwrap();
    }
    assert!(m.session.synced());
    drop(store);
    let (_, loaded) = Store::open(d.path()).unwrap();
    assert_eq!(loaded, as_loaded(&m.session));
    assert_eq!(loaded.items(c).count(), 0, "the input was delivered");
}

#[test]
fn a_damaged_record_is_cut_not_believed() {
    let d = dir();
    let (mut s, c) = Session::create("{}", "sys", 0);
    let mut store = Store::create(d.path(), &mut s).unwrap();
    let before = as_loaded(&s);
    s.write(c, Body::User("hello".into()), vec![], Author::Core, 0)
        .unwrap();
    s.sync();
    store.save(&mut s).unwrap();
    drop(store);
    let log = d.path().join("log");
    let mut bytes = fs::read(&log).unwrap();
    let at = bytes.windows(5).position(|w| w == b"hello").unwrap();
    bytes[at] = b'j'; // still valid JSON, now wrong
    fs::write(&log, bytes).unwrap();
    let (_, loaded) = Store::open(d.path()).unwrap();
    assert_eq!(loaded, before);
}

#[test]
fn open_and_add_item_outside_a_step_are_durable_on_return() {
    let d = dir();
    let (mut s, c) = Session::create("{}", "sys", 0);
    let mut store = Store::create(d.path(), &mut s).unwrap();
    let mut m = Machine::new(s);
    let sys = m.session.at(c).unwrap();
    let tick = ItemData::Extension {
        name: "timer".into(),
        data: "tick".into(),
    };
    m.add_item(c, tick, 0).unwrap();
    let other = m.open(sys, Author::Extension("ext".into()), 0).unwrap();
    store.save(&mut m.session).unwrap();
    drop(store); // as a crash: nothing is saved after the calls returned
    let (_, loaded) = Store::open(d.path()).unwrap();
    assert!(loaded.at(other).is_ok(), "the opened cursor is there");
    assert_eq!(loaded.items(c).count(), 1, "the item is there");
}

#[test]
fn load_wakes_a_cursor_with_input_pending_for_its_hooks_only() {
    let d = dir();
    let (mut s, c) = Session::create("{}", "sys", 0);
    let mut store = Store::create(d.path(), &mut s).unwrap();
    let mut m = Machine::new(s);
    m.send(c, "term".into(), "hi", 0).unwrap();
    store.save(&mut m.session).unwrap();
    drop(m);
    drop(store); // closed before the step ran
    let (_store, loaded) = Store::open(d.path()).unwrap();
    let mut m = Machine::loaded(loaded, 1);
    assert!(m.busy(c));
    let mut jobs = vec![];
    loop {
        let job = m.next_job(c).unwrap();
        if job == Job::Rest {
            break;
        }
        jobs.push(job);
        m.feed(
            c,
            Fed::Hooks {
                stop: None,
                draft: None,
            },
            1,
        )
        .unwrap();
    }
    let points: Vec<_> = jobs
        .iter()
        .map(|j| match j {
            Job::Hooks { point, .. } => *point,
            other => panic!("only hooks run after a load, got {other:?}"),
        })
        .collect();
    assert_eq!(points, [Hook::PreStep, Hook::PostStep]);
    assert_eq!(m.session.items(c).count(), 1, "the input is still pending");
    assert_eq!(m.session.walk(m.session.at(c).unwrap()).len(), 2);
}

#[test]
fn after_a_failed_save_the_store_says_so_and_refuses() {
    let d = dir();
    let (mut s, c) = Session::create("{}", "sys", 0);
    let mut store = Store::create(d.path(), &mut s).unwrap();
    assert!(!store.failed());
    // The store cannot create its temporary scratchpad file here.
    fs::create_dir(d.path().join("scratchpad.new")).unwrap();
    let mut m = Machine::new(s);
    m.input(c, "term".into(), "hi", 0).unwrap();
    assert!(matches!(store.save(&mut m.session), Err(Error::Io(_))));
    assert!(store.failed());
    m.session.set_label("x", c).unwrap();
    m.session.sync();
    assert!(matches!(store.save(&mut m.session), Err(Error::Failed)));
}
