//! The tree's views and label records, without the step machine.

use tau_core::step::{Answer, Job, Step};
use tau_core::tree::{Author, Body, Config, Error, Node, Provenance, Record, Scratchpad, Session};

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
    assert_eq!(s.tree.config_at(a).unwrap(), &Config::from("{}"));
    assert_eq!(s.tree.config_at(b).unwrap(), &fast);
    assert_eq!(s.tree.config_at(77), Err(Error::NoSuchNode(77)));
}

#[test]
fn lca_is_the_deepest_shared_node_and_none_across_trees() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let a = user(&mut s, c, "a");
    let b1 = user(&mut s, c, "b1");
    s.move_to(c, a, Author::Core, 0).unwrap();
    let b2 = user(&mut s, c, "b2");
    let b2x = user(&mut s, c, "b2x");
    assert_eq!(s.tree.lca(b1, b2x), Ok(Some(a)));
    assert_eq!(s.tree.lca(b2, b2x), Ok(Some(b2)));
    let other = s.append_root(Author::Core, "{}".into(), 0);
    assert_eq!(s.tree.lca(b1, other), Ok(None));
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
            node: None,
            ending: None,
        }
    );
    let by = Author::Extension("nav".into());
    let note = s
        .append(before, by.clone(), Body::User("n".into()), vec![], 0)
        .unwrap();
    s.move_to(c, note, by.clone(), 0).unwrap();
    s.move_to(d, note, by, 0).unwrap(); // another cursor's move: not shown
    step.answer(
        &mut s,
        Answer::Hooks {
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
        copied_from: None,
        derived_from: None,
    };
    let log = [Record::Write {
        node: root,
        mv: None,
    }];
    assert_eq!(
        tau_core::tree::load(log, Scratchpad::default()).map(|_| ()),
        Err(Error::BadRecord(0))
    );
}

#[test]
fn a_fork_is_one_walk_copied_node_for_node() {
    let (mut s, c) = Session::create("{}", "sys", 1);
    let a = s
        .write(
            c,
            Body::User("a".into()),
            vec![],
            Author::Frontend("t".into()),
            5,
        )
        .unwrap();
    let b = user(&mut s, c, "b");
    // Another branch, a label, a second cursor and a pending item: none of them are copied.
    let other = s.open(a, Author::Core, 6).unwrap();
    s.write(
        other,
        Body::User("elsewhere".into()),
        vec![],
        Author::Core,
        7,
    )
    .unwrap();
    s.set_label("main", c).unwrap();
    s.add_item(
        c,
        tau_core::tree::ItemData::Input {
            by: Author::Core,
            text: "pending".into(),
        },
    );
    let (copy, cursor) = s.fork(b, 99).unwrap();
    let walk = s.tree.walk(b);
    assert_eq!(copy.tree.node_count(), walk.len());
    for (i, original) in walk.iter().enumerate() {
        let node = copy.tree.node(i as u64).unwrap();
        assert_eq!(node.parent, i.checked_sub(1).map(|p| p as u64));
        assert_eq!(
            (&node.body, &node.by, node.time),
            (&original.body, &original.by, original.time)
        );
    }
    assert_eq!(cursor, 0);
    assert_eq!(
        copy.at(0),
        Ok(walk.len() as u64 - 1),
        "cursor 0 on the copied tip"
    );
    assert_eq!(copy.at(1), Err(Error::NoSuchCursor(1)));
    assert_eq!(copy.labels().count(), 0);
    assert_eq!(copy.items(0).count(), 0);
    assert_eq!(copy.auto.get(&0), Some(&false));
}

#[test]
fn a_forks_delivers_are_emptied_and_a_missing_node_is_refused() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let item = s.add_item(
        c,
        tau_core::tree::ItemData::Input {
            by: Author::Core,
            text: "hi".into(),
        },
    );
    let tip = s
        .write(c, Body::User("hi".into()), vec![item], Author::Core, 0)
        .unwrap();
    assert_eq!(s.tree.node(tip).unwrap().delivers, [item]);
    let (copy, _) = s.fork(tip, 0).unwrap();
    assert_eq!(copy.tree.node(2).unwrap().delivers, Vec::<u64>::new());
    assert_eq!(s.fork(77, 0).map(|_| ()), Err(Error::NoSuchNode(77)));
}

#[test]
fn a_node_records_where_it_came_from_and_the_source_must_exist() {
    let (mut s, _) = Session::create("{}", "sys", 0);
    let a = s
        .append(1, Author::Core, Body::User("a".into()), vec![], 1)
        .unwrap();
    let copy = s
        .append_from(
            1,
            Author::Core,
            Body::User("a".into()),
            vec![],
            2,
            Provenance {
                copied_from: Some(a),
                derived_from: None,
            },
        )
        .unwrap();
    let summary = s
        .append_from(
            1,
            Author::Core,
            Body::User("s".into()),
            vec![],
            3,
            Provenance {
                copied_from: None,
                derived_from: Some(a),
            },
        )
        .unwrap();
    let n = |id| s.tree.node(id).unwrap().clone();
    assert_eq!((n(a).copied_from, n(a).derived_from), (None, None));
    assert_eq!((n(copy).copied_from, n(copy).derived_from), (Some(a), None));
    assert_eq!(
        (n(summary).copied_from, n(summary).derived_from),
        (None, Some(a))
    );
    let before = s.tree.node_count();
    let missing = Provenance {
        copied_from: Some(99),
        derived_from: None,
    };
    assert_eq!(
        s.append_from(1, Author::Core, Body::User("x".into()), vec![], 4, missing),
        Err(Error::NoSuchNode(99))
    );
    assert_eq!(s.tree.node_count(), before, "a refused node is not added");
}

#[test]
fn a_fork_renumbers_provenance_and_drops_a_source_outside_the_walk() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let side = s
        .append(1, Author::Core, Body::User("side".into()), vec![], 1)
        .unwrap();
    let a = s
        .write(c, Body::User("a".into()), vec![], Author::Core, 2)
        .unwrap();
    let copy = s
        .append_from(
            a,
            Author::Core,
            Body::User("b".into()),
            vec![],
            3,
            Provenance {
                copied_from: Some(a),
                derived_from: Some(side),
            },
        )
        .unwrap();
    let (forked, _) = s.fork(copy, 9).unwrap();
    let last = forked.tree.node(3).unwrap();
    assert_eq!(last.copied_from, Some(2), "node a is node 2 of the copy");
    assert_eq!(
        last.derived_from, None,
        "the side branch is not in the walk"
    );
}

#[test]
fn a_fork_strips_usage_from_a_copy_whose_source_it_drops() {
    use tau_core::tree::{Part, Stop};
    let reply = |meta: &str| Body::Assistant {
        parts: vec![Part::text("r")],
        stop: Stop::End,
        finish_reason: None,
        meta: Some(meta.into()),
    };
    let (mut s, c) = Session::create("{}", "sys", 0);
    let side = s
        .append(
            1,
            Author::Core,
            reply(r#"{"usage":{"input":9,"output":1}}"#),
            vec![],
            1,
        )
        .unwrap();
    let a = s
        .write(c, Body::User("a".into()), vec![], Author::Core, 2)
        .unwrap();
    let kept = s
        .append_from(
            a,
            Author::Core,
            reply(r#"{"model":"m","usage":{"input":9,"output":1}}"#),
            vec![],
            3,
            Provenance {
                copied_from: Some(side),
                derived_from: None,
            },
        )
        .unwrap();
    let (forked, _) = s.fork(kept, 9).unwrap();
    let copy = forked.tree.node(3).unwrap();
    assert_eq!(copy.copied_from, None);
    let Body::Assistant { meta, .. } = &copy.body else {
        panic!("a reply")
    };
    assert_eq!(meta.as_deref(), Some(r#"{"model":"m"}"#));
}
