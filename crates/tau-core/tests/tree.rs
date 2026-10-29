//! The tree's views and label records, without the step machine.

use tau_core::step::{Fed, Job, Step};
use tau_core::tree::{Author, Body, Config, Error, Node, Record, Scratchpad, Session};

fn user(s: &mut Session, c: u64, t: &str) -> u64 {
    s.write(c, Body::User(t.into()), vec![], Author::Core, 0)
        .unwrap()
}

#[test]
fn a_label_names_one_cursor_moves_when_set_again_and_is_recorded() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let d = s.open(s.at(c).unwrap(), Author::Core, 0).unwrap();
    s.outbox.clear();
    s.set_label("main", c).unwrap();
    s.set_label("main", c).unwrap(); // already there: no record
    s.set_label("scout", c).unwrap();
    s.set_label("main", d).unwrap();
    assert_eq!(s.label("main"), Ok(d));
    assert_eq!(s.labels_of(c).collect::<Vec<_>>(), ["scout"]);
    s.remove_label("scout").unwrap();
    assert_eq!(
        s.remove_label("scout"),
        Err(Error::NoSuchLabel("scout".into()))
    );
    assert_eq!(s.set_label("x", 99), Err(Error::NoSuchCursor(99)));
    let label = |l: &str, c: Option<u64>| Record::Label {
        label: l.into(),
        cursor: c,
    };
    assert_eq!(
        s.outbox,
        [
            label("main", Some(c)),
            label("scout", Some(c)),
            label("main", Some(d)),
            label("scout", None)
        ]
    );
}

#[test]
fn config_at_is_the_nearest_config_on_the_walk() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let a = user(&mut s, c, "a");
    let fast = Config {
        data: "fast".into(),
    };
    s.write(c, Body::Config(fast.clone()), vec![], Author::Core, 0)
        .unwrap();
    let b = user(&mut s, c, "b");
    assert_eq!(s.config_at(a).unwrap(), &Config::from("{}"));
    assert_eq!(s.config_at(b).unwrap(), &fast);
    assert_eq!(s.config_at(77), Err(Error::NoSuchNode(77)));
}

#[test]
fn lca_is_the_deepest_shared_node_and_none_across_trees() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let a = user(&mut s, c, "a");
    let b1 = user(&mut s, c, "b1");
    s.move_to(c, a, Author::Core, 0).unwrap();
    let b2 = user(&mut s, c, "b2");
    let b2x = user(&mut s, c, "b2x");
    assert_eq!(s.lca(b1, b2x), Ok(Some(a)));
    assert_eq!(s.lca(b2, b2x), Ok(Some(b2)));
    let other = s.append_root(Author::Core, "{}".into(), 0);
    assert_eq!(s.lca(b1, other), Ok(None));
}

#[test]
fn a_step_sees_its_cursor_moves_since_it_woke() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let d = s.open(s.at(c).unwrap(), Author::Core, 0).unwrap();
    let before = user(&mut s, c, "before");
    let mut step = Step::wake(&s, c, true);
    assert_eq!(
        step.next_job(&mut s).unwrap(),
        Job::Hooks {
            point: tau_core::step::Hook::PreStep,
            draft: None,
            ending: None,
        }
    );
    let by = Author::Extension("nav".into());
    let note = s
        .append(before, by.clone(), Body::User("n".into()), vec![], 0)
        .unwrap();
    s.move_to(c, note, by.clone(), 0).unwrap();
    s.move_to(d, note, by, 0).unwrap(); // another cursor's move: not shown
    step.feed(
        &mut s,
        Fed::Hooks {
            stop: None,
            draft: None,
        },
        0,
    )
    .unwrap();
    let seen: Vec<(Option<u64>, u64)> = step.moves(&s).map(|m| (m.from, m.to)).collect();
    assert_eq!(seen, [(Some(before), note)]);
}

#[test]
fn a_logged_root_that_is_not_a_config_does_not_replay() {
    let root = Node {
        id: 0,
        parent: None,
        time: 0,
        by: Author::Core,
        body: Body::System("s".into()),
        delivers: vec![],
    };
    let log = [Record::Write {
        node: root,
        mv: None,
    }];
    assert_eq!(
        Session::load(log, Scratchpad::default()).map(|_| ()),
        Err(Error::BadRecord(0))
    );
}
