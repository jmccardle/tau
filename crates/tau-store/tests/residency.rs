//! Residency (D14): bodies on no cursor's walk leave memory once synced, and
//! read back from the log equal to what was written. Residency is not state.

use proptest::prelude::*;
use tau_core::machine::Machine;
use tau_core::step::{Fed, Job};
use tau_core::tree::{Author, Body, Error, NodeId, Session};
use tau_store::Store;

#[derive(Clone, Debug)]
enum Op {
    Open(usize),
    User(usize),
    Go(usize, usize),
    Sync,
    Reopen,
}

fn op() -> impl Strategy<Value = Op> {
    let n = 0..64usize;
    prop_oneof![
        1 => n.clone().prop_map(Op::Open),
        3 => n.clone().prop_map(Op::User),
        2 => (n.clone(), n.clone()).prop_map(|(c, t)| Op::Go(c, t)),
        2 => Just(Op::Sync),
        1 => Just(Op::Reopen),
    ]
}

fn count(f: impl Fn(u64) -> bool) -> u64 {
    (0..u64::MAX).take_while(|i| f(*i)).count() as u64
}

/// Every node, resident or stored.
fn nodes(s: &Session) -> u64 {
    count(|n| s.parent(n).is_ok())
}

fn stored(s: &Session) -> Vec<NodeId> {
    (0..nodes(s))
        .filter(|&n| matches!(s.node(n), Err(Error::Stored(_))))
        .collect()
}

/// `f`, after loading whatever `Stored` names, as a driver does.
fn retry<T>(
    store: &mut Store,
    s: &mut Session,
    mut f: impl FnMut(&mut Session) -> Result<T, Error>,
) -> T {
    match f(s) {
        Err(Error::Stored(ids)) => {
            store.load(s, &ids).unwrap();
            f(s).unwrap()
        }
        r => r.unwrap(),
    }
}

/// `s` with its outbox and in-memory counts dropped, as compared across a reload.
fn state(s: &Session) -> Session {
    let mut s = s.clone();
    s.outbox.clear();
    s.changes.clear();
    s.taken.clear();
    s
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(64))]

    /// One session through a store that evicts after every save, another in
    /// memory only: the same ops give the same state once every stored body
    /// is loaded back, across reopens.
    #[test]
    fn evicted_bodies_load_back_as_written(ops in prop::collection::vec(op(), 0..80)) {
        let d = tempfile::tempdir().unwrap();
        let (mut s, _) = Session::create("{}", "sys", 0);
        let mut plain = s.clone();
        let mut store = Store::create(d.path(), &mut s).unwrap();
        plain.sync();
        for op in &ops {
            let (nc, nn) = (count(|c| s.at(c).is_ok()), nodes(&s));
            let cursor = |i: usize| i as u64 % nc;
            let node = |i: usize| i as u64 % nn;
            match *op {
                Op::Open(t) => {
                    retry(&mut store, &mut s, |s| s.open(node(t), Author::Core, 0));
                    plain.open(node(t), Author::Core, 0).unwrap();
                }
                Op::User(i) => {
                    for x in [&mut s, &mut plain] {
                        x.write(cursor(i), Body::User(format!("u{i}")), vec![], Author::Core, 0)
                            .unwrap();
                    }
                }
                Op::Go(i, t) => {
                    retry(&mut store, &mut s, |s| s.move_to(cursor(i), node(t), Author::Core, 0));
                    plain.move_to(cursor(i), node(t), Author::Core, 0).unwrap();
                }
                Op::Sync => {
                    s.sync();
                    plain.sync();
                }
                Op::Reopen => {
                    s.sync();
                    plain.sync();
                    store.save(&mut s).unwrap();
                    drop(store);
                    (store, s) = Store::open(d.path()).unwrap();
                }
            }
            store.save(&mut s).unwrap();
            store.evict(&mut s);
            for c in 0..count(|c| s.at(c).is_ok()) {
                prop_assert!(s.walk(s.at(c).unwrap()).is_ok(), "a cursor's walk is in memory");
            }
        }
        let cold = stored(&s);
        store.load(&mut s, &cold).unwrap();
        prop_assert!(stored(&s).is_empty());
        prop_assert_eq!(state(&s), state(&plain));
    }
}

#[test]
fn a_go_to_into_a_cold_line_loads_it_first() {
    let d = tempfile::tempdir().unwrap();
    let (mut s, c) = Session::create("{}", "sys", 0);
    let mut store = Store::create(d.path(), &mut s).unwrap();
    let sys = s.at(c).unwrap();
    let old = s
        .write(c, Body::User("old".into()), vec![], Author::Core, 0)
        .unwrap();
    s.move_to(c, sys, Author::Core, 0).unwrap();
    s.write(c, Body::User("new".into()), vec![], Author::Core, 0)
        .unwrap();
    assert_eq!(store.evict(&mut s), 0, "nothing synced yet");
    s.sync();
    store.save(&mut s).unwrap();
    assert_eq!(store.evict(&mut s), 1);
    assert_eq!(s.node(old), Err(Error::Stored(vec![old])));
    assert!(s.config_at(old).is_ok(), "a Config stays in memory");
    assert_eq!(s.parent(old), Ok(Some(sys)), "its place in the tree stays");
    assert_eq!(
        s.move_to(c, old, Author::Core, 0),
        Err(Error::Stored(vec![old]))
    );
    store.load(&mut s, &[old]).unwrap();
    assert_eq!(s.node(old).unwrap().body, Body::User("old".into()));
    drop(store);
    // Offsets come back from the log on open.
    let (mut store, mut s) = Store::open(d.path()).unwrap();
    assert_eq!(store.evict(&mut s), 1, "a loaded session evicts at once");
    store.load(&mut s, &[old]).unwrap();
    s.move_to(c, old, Author::Core, 0).unwrap();
    assert_eq!(s.node(old).unwrap().body, Body::User("old".into()));
}

fn ok() -> Fed {
    Fed::Hooks {
        stop: None,
        draft: None,
    }
}

#[test]
fn a_waiting_go_to_whose_line_was_evicted_waits_for_the_load() {
    let dir = tempfile::tempdir().unwrap();
    let (mut s, c) = Session::create("{}", "sys", 0);
    let sys = s.at(c).unwrap();
    let old = s
        .write(c, Body::User("old".into()), vec![], Author::Core, 0)
        .unwrap();
    s.move_to(c, sys, Author::Core, 0).unwrap();
    let mut store = Store::create(dir.path(), &mut s).unwrap();
    let mut m = Machine::new(s);
    m.send(c, "term".into(), "hi", 0).unwrap();
    while m.next_job(c).unwrap() != Job::Sample {
        m.feed(c, ok(), 0).unwrap();
    }
    // The go-to waits on the busy cursor; meanwhile its line is evicted.
    assert!(
        m.go_to(c, old, "term".into(), 0).unwrap(),
        "the sample is cancelled"
    );
    store.save(&mut m.session).unwrap();
    store.evict(&mut m.session);
    assert_eq!(m.session.node(old), Err(Error::Stored(vec![old])));
    let cancelled = Fed::Sampled {
        reply: None,
        error: None,
    };
    m.feed(c, cancelled, 0).unwrap();
    let mut loaded = false;
    loop {
        let job = match m.next_job(c) {
            Err(Error::Stored(ids)) => {
                assert_eq!(ids, [old]);
                store.load(&mut m.session, &ids).unwrap();
                loaded = true;
                continue;
            }
            job => job.unwrap(),
        };
        if job == Job::Rest {
            break;
        }
        m.feed(c, ok(), 0).unwrap();
        store.save(&mut m.session).unwrap();
        store.evict(&mut m.session);
    }
    assert!(loaded);
    assert_eq!(m.session.at(c), Ok(old), "the go-to applied after the load");
}
