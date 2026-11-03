//! The planner on trees written down as (parent, is a user node) pairs.

use std::time::Instant;

use super::*;

/// `spec[i]` is node `i`: its parent and whether it is a user node. Reply nodes have one call.
fn shape(spec: &[(Option<usize>, bool)]) -> Shape {
    let facts: Vec<Facts> = spec
        .iter()
        .map(|&(parent, user)| Facts {
            parent,
            user,
            calls: usize::from(!user),
            chars: 40,
        })
        .collect();
    let mut shape = Shape::default();
    shape.extend(&facts).unwrap();
    shape
}

fn at(pairs: &[(u64, usize)]) -> BTreeMap<u64, usize> {
    pairs.iter().copied().collect()
}

/// root 0; turn A: user 1, replies 2, 3; turn B: user 4, replies 5.
fn two_turns() -> Vec<(Option<usize>, bool)> {
    vec![
        (None, false),
        (Some(0), true),
        (Some(1), false),
        (Some(2), false),
        (Some(3), true),
        (Some(4), false),
    ]
}

fn keys(rows: &[Row]) -> Vec<Key> {
    rows.iter().map(Row::key).collect()
}

/// How many rows show each node once everything is unfolded.
fn shown_everywhere(plan: &mut Plan, count: usize) -> Vec<usize> {
    fn walk(plan: &Plan, rows: &[Row], seen: &mut [usize]) {
        for row in rows {
            // A stub stands for its branch: its first node is shown by the line it unfolds to.
            if row.kind != Kind::Stub {
                seen[row.node] += 1;
            }
            if row.expandable {
                walk(plan, &plan.children(row), seen);
            }
        }
    }
    let mut seen = vec![0; count];
    let top = plan.top().to_vec();
    walk(plan, &top, &mut seen);
    seen
}

#[test]
fn the_spine_is_the_walk_with_each_turn_one_folded_row() {
    let shape = shape(&two_turns());
    let (open, gone) = (at(&[(0, 5)]), at(&[]));
    let mut plan = Plan::new(&shape, &open, &gone, 0).unwrap();
    let top = plan.top().to_vec();
    assert_eq!(
        keys(&top),
        [(Kind::Node, 0), (Kind::Turn, 1), (Kind::Turn, 4)]
    );
    assert_eq!((top[1].size, top[1].calls), (2, 2), "+2 nodes, 2 calls");
    assert!(!top[1].opens, "no cursor in it: starts folded");
    assert!(top[2].opens, "the cursor's turn starts open");
    assert_eq!(top[2].cursors, [0]);
}

#[test]
fn a_branch_in_the_middle_of_a_turn_hangs_inside_it_and_at_its_end_beside_it() {
    // Turn A has a second reply to node 2 (a middle node: node 6) and to node 3 (its last: 7).
    let mut spec = two_turns();
    spec.push((Some(2), false)); // 6
    spec.push((Some(3), false)); // 7
    let shape = shape(&spec);
    let (open, gone) = (at(&[(0, 5)]), at(&[]));
    let mut plan = Plan::new(&shape, &open, &gone, 0).unwrap();
    let top = plan.top().to_vec();
    assert_eq!(top[1].branches, 1, "node 6 hangs inside turn A");
    assert_eq!(
        keys(&top)[1..],
        [(Kind::Turn, 1), (Kind::Stub, 7), (Kind::Turn, 4)],
        "node 7 hangs level with turn A"
    );
    let inside = plan.children(&top[1]);
    assert_eq!(
        keys(&inside),
        [(Kind::Node, 2), (Kind::Stub, 6), (Kind::Node, 3)]
    );
}

#[test]
fn every_node_is_shown_once_when_everything_is_unfolded() {
    // A fork in the middle of a turn, one at its end, a branch under a branch, a cursor on
    // another branch, and a closed cursor.
    let mut spec = two_turns();
    spec.extend([
        (Some(2), false), // 6
        (Some(3), false), // 7
        (Some(7), true),  // 8
        (Some(8), false), // 9
        (Some(8), false), // 10
        (Some(5), true),  // 11
    ]);
    let shape = shape(&spec);
    let (open, gone) = (at(&[(0, 11), (1, 10)]), at(&[(2, 6)]));
    for viewed in [0, 1, 2] {
        let mut plan = Plan::new(&shape, &open, &gone, viewed).unwrap();
        assert_eq!(
            shown_everywhere(&mut plan, spec.len()),
            vec![1; spec.len()],
            "viewing cursor {viewed}"
        );
    }
}

#[test]
fn a_lone_child_below_the_viewed_cursor_is_a_stub_not_lost() {
    let shape = shape(&two_turns());
    let (open, gone) = (at(&[(0, 2)]), at(&[]));
    let mut plan = Plan::new(&shape, &open, &gone, 0).unwrap();
    assert_eq!(
        shown_everywhere(&mut plan, 6),
        vec![1; 6],
        "node 3 and what follows it are still in the tree"
    );
    let stub = plan.top().iter().find(|r| r.kind == Kind::Stub).unwrap();
    assert_eq!((stub.node, stub.size, stub.turns), (3, 3, 1));
}

#[test]
fn a_folded_row_lists_the_cursors_inside_it_and_a_stub_those_on_its_branch() {
    let mut spec = two_turns();
    spec.extend([(Some(2), false), (Some(6), false)]); // 6, 7: a branch off node 2
    let shape = shape(&spec);
    let (open, gone) = (at(&[(0, 5), (1, 7)]), at(&[(2, 3)]));
    let mut plan = Plan::new(&shape, &open, &gone, 0).unwrap();
    let top = plan.top().to_vec();
    let turn_a = &top[1];
    assert_eq!(
        turn_a.cursors,
        [1],
        "cursor 1 is inside turn A, on its hung branch"
    );
    assert_eq!(turn_a.closed, [2], "and closed cursor 2 is at node 3");
    assert!(turn_a.opens, "a cursor below a hung branch opens the turn");
    let stub = plan
        .children(turn_a)
        .into_iter()
        .find(|r| r.kind == Kind::Stub);
    let stub = stub.unwrap();
    assert_eq!(
        (stub.node, stub.cursors.clone(), stub.size),
        (6, vec![1], 2)
    );
}

#[test]
fn a_stub_goes_on_toward_the_lowest_open_cursor_else_the_longest_line() {
    // Node 1 (user) forks into 2 (a short branch) and 3 -> 4 -> 5 (a long one); the spine is
    // elsewhere, so the fork is below a stub: root 0 forks into 1 (stub) and 6 (the spine).
    let spec = [
        (None, false),
        (Some(0), true),
        (Some(1), false),
        (Some(1), false),
        (Some(3), false),
        (Some(4), false),
        (Some(0), true),
    ];
    let shape = shape(&spec);
    let gone = at(&[]);
    let longest = at(&[(0, 6)]);
    let mut plan = Plan::new(&shape, &longest, &gone, 0).unwrap();
    let stub = plan
        .top()
        .iter()
        .find(|r| r.kind == Kind::Stub)
        .unwrap()
        .clone();
    let line = plan.children(&stub);
    let turn = line.iter().find(|r| r.kind == Kind::Turn).unwrap();
    assert_eq!(turn.node, 1);
    let inside = plan.children(turn);
    assert_eq!(
        keys(&inside)[0],
        (Kind::Stub, 2),
        "the short branch is the stub; the longer line is the turn's own"
    );

    let toward = at(&[(0, 6), (1, 2)]);
    let mut plan = Plan::new(&shape, &toward, &gone, 0).unwrap();
    let stub = plan
        .top()
        .iter()
        .find(|r| r.kind == Kind::Stub)
        .unwrap()
        .clone();
    let turn = plan
        .children(&stub)
        .into_iter()
        .find(|r| r.kind == Kind::Turn)
        .unwrap();
    let inside = plan.children(&turn);
    assert!(
        inside.iter().any(|r| r.key() == (Kind::Stub, 3)),
        "an open cursor at node 2 makes node 2 the line and node 3 the stub: {:?}",
        keys(&inside)
    );
}

#[test]
fn locate_gives_the_rows_to_unfold_to_reach_a_node() {
    let mut spec = two_turns();
    spec.push((Some(2), false)); // 6, hung inside turn A
    let shape = shape(&spec);
    let (open, gone) = (at(&[(0, 5)]), at(&[]));
    let mut plan = Plan::new(&shape, &open, &gone, 0).unwrap();
    assert_eq!(
        keys(&plan.locate(3, None).unwrap()),
        [(Kind::Turn, 1), (Kind::Node, 3)]
    );
    assert_eq!(
        keys(&plan.locate(6, Some((Kind::Stub, 6))).unwrap()),
        [(Kind::Turn, 1), (Kind::Stub, 6)]
    );
    assert!(plan.locate(99, None).is_err());
}

#[test]
fn a_viewed_cursor_that_is_not_there_and_a_tree_of_two_roots_are_errors() {
    let shape = shape(&two_turns());
    assert!(Plan::new(&shape, &at(&[(0, 5)]), &at(&[]), 9).is_err());
    assert!(Plan::new(&shape, &at(&[(0, 50)]), &at(&[]), 0).is_err());
    let mut two = Shape::default();
    let root = Facts {
        parent: None,
        user: false,
        calls: 0,
        chars: 0,
    };
    assert!(two.extend(&[root, root]).is_err());
}

#[test]
fn a_walk_is_measured_in_nodes_and_tokens() {
    let shape = shape(&two_turns());
    assert_eq!(shape.walk_of(5), (5 + 1, 6 * 40 / 4));
}

/// A tree of 30,000 nodes: 15,000 turns, with one fork every hundred turns. It reports what
/// the plan costs and asserts nothing about time (a loaded machine would make that flaky).
#[test]
fn thirty_thousand_nodes_are_planned_and_reported() {
    let mut spec = vec![(None, false)];
    let mut tip = 0;
    for turn in 0..15_000usize {
        spec.push((Some(tip), true));
        spec.push((Some(spec.len() - 1), false));
        tip = spec.len() - 1;
        if turn % 100 == 0 {
            spec.push((Some(tip), false));
        }
    }
    let started = Instant::now();
    let shape = shape(&spec);
    let counted = started.elapsed();
    let (open, gone) = (at(&[(0, tip)]), at(&[]));
    let started = Instant::now();
    let mut plan = Plan::new(&shape, &open, &gone, 0).unwrap();
    let top = plan.top().len();
    let planned = started.elapsed();
    let started = Instant::now();
    let seen = shown_everywhere(&mut plan, spec.len());
    let unfolded = started.elapsed();
    eprintln!(
        "{} nodes: counted in {counted:?}, planned {top} top rows in {planned:?}, \
         every row made in {unfolded:?}",
        spec.len()
    );
    assert!(seen.iter().all(|&n| n == 1), "every node once");
}
