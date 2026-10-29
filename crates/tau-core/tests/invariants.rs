//! I1–I5 (model.md §4) over random frontend calls and random driver answers.
//! The driver answers each job as a provider, a tool, or a hook would, hooks
//! moving the cursor included; the invariants are checked after every call.

use std::collections::BTreeMap;

use proptest::prelude::*;
use tau_core::machine::Machine;
use tau_core::step::{Draft, Fed, Hook, Job};
use tau_core::tree::{
    Author, Body, Config, CursorId, Error, Next, Node, NodeId, Outcome, Part, Record, Session,
    Stop, calls,
};

#[derive(Clone, Debug)]
enum Op {
    Send(usize),
    Input(usize),
    Step(usize),
    Go(usize, usize),
    Auto(usize, bool),
    Abort(usize),
    Open(usize),
    /// Answer the cursor's next job; the number picks the answer.
    Drive(usize, usize),
}

fn op() -> impl Strategy<Value = Op> {
    let n = 0..64usize;
    prop_oneof![
        1 => n.clone().prop_map(Op::Send),
        1 => n.clone().prop_map(Op::Input),
        1 => n.clone().prop_map(Op::Step),
        1 => (n.clone(), n.clone()).prop_map(|(c, t)| Op::Go(c, t)),
        1 => (n.clone(), any::<bool>()).prop_map(|(c, on)| Op::Auto(c, on)),
        1 => n.clone().prop_map(Op::Abort),
        1 => n.clone().prop_map(Op::Open),
        16 => (n.clone(), 0..256usize).prop_map(|(c, k)| Op::Drive(c, k)),
    ]
}

fn nodes(s: &Session) -> Vec<Node> {
    (0..).map_while(|n| s.node(n).ok().cloned()).collect()
}

fn cursors(s: &Session) -> u64 {
    (0..CursorId::MAX).take_while(|c| s.at(*c).is_ok()).count() as u64
}

fn reply(parts: Vec<Part>, stop: Stop) -> Draft {
    Draft {
        body: Body::Assistant {
            parts,
            stop,
            finish_reason: None,
        },
        delivers: vec![],
        by: Author::Provider,
    }
}

fn ok() -> Fed {
    Fed::Hooks {
        stop: None,
        draft: None,
    }
}

/// A batch handed out by `RunCalls` and not yet joined.
struct Batch {
    node: NodeId,
    calls: Vec<String>,
    fed: BTreeMap<String, Outcome>,
}

#[derive(Default)]
struct Driver {
    batches: BTreeMap<CursorId, Batch>,
    run: usize,
    joined: usize,
}

impl Driver {
    /// One job of `c`'s step, answered by `k`; checks I1 and I4 at the commits.
    fn drive(&mut self, m: &mut Machine, c: CursorId, k: usize) -> Result<(), TestCaseError> {
        let job = m.next_job(c).unwrap();
        let before = nodes(&m.session).len();
        let fed = match &job {
            Job::Rest => return Ok(()),
            Job::Hooks { point, .. } => hook(m, c, *point, k, before)?,
            Job::Sample => sample(k),
            Job::RunCalls { calls: run, .. } => {
                let ids = run.iter().map(|r| r.id.clone()).collect();
                return self.run_calls(m, c, ids);
            }
            Job::Pending => self.call_done(c, k),
            Job::Wait { .. } => Fed::Waited,
        };
        let at = m.session.at(c).unwrap();
        m.feed(c, fed, 0).unwrap();
        let after = nodes(&m.session);
        if matches!(
            job,
            Job::Hooks {
                point: Hook::PreReply,
                ..
            }
        ) && after.len() > before
        {
            // I1: the reply lands under `at` as it stands at the commit.
            let n = &after[before];
            prop_assert_eq!(n.parent, Some(at));
            prop_assert!(matches!(n.body, Body::Assistant { .. }), "a reply");
        }
        if job == Job::Pending {
            self.check_join(m, c, &after[before..])?;
        }
        Ok(())
    }

    fn run_calls(
        &mut self,
        m: &Machine,
        c: CursorId,
        ids: Vec<String>,
    ) -> Result<(), TestCaseError> {
        let node = m.session.at(c).unwrap();
        let batch: Vec<String> = calls(&m.session.node(node).unwrap().body)
            .into_iter()
            .map(|(id, ..)| id.to_string())
            .collect();
        prop_assert_eq!(&ids, &batch, "RunCalls hands out the batch at `at`");
        self.batches.insert(
            c,
            Batch {
                node,
                calls: ids,
                fed: BTreeMap::new(),
            },
        );
        self.run += 1;
        Ok(())
    }

    /// One open call of `c`'s batch done, its outcome picked by `k`.
    fn call_done(&mut self, c: CursorId, k: usize) -> Fed {
        let b = self.batches.get_mut(&c).expect("a batch is running");
        let open: Vec<&String> = b
            .calls
            .iter()
            .filter(|id| !b.fed.contains_key(*id))
            .collect();
        let call = open[k % open.len()].clone();
        let outcome = match k / 8 % 3 {
            0 => Outcome::Ok(format!("{call} ok")),
            1 => Outcome::Err(format!("{call} failed")),
            _ => Outcome::Interrupted,
        };
        b.fed.insert(call.clone(), outcome.clone());
        Fed::CallDone {
            call,
            outcome,
            by: Author::Tools,
        }
    }

    /// After a `CallDone`: once every call of the batch is fed, `added` is
    /// exactly its `Results` (I4).
    fn check_join(
        &mut self,
        m: &Machine,
        c: CursorId,
        added: &[Node],
    ) -> Result<(), TestCaseError> {
        let Some(b) = self
            .batches
            .get(&c)
            .filter(|b| b.fed.len() == b.calls.len())
        else {
            return Ok(());
        };
        // I4: one `Results` under the batch, one outcome per call, in call order.
        prop_assert_eq!(added.len(), 1);
        let want: Vec<_> = b
            .calls
            .iter()
            .map(|id| (id.clone(), b.fed[id].clone(), Author::Tools))
            .collect();
        prop_assert_eq!(added[0].parent, Some(b.node));
        prop_assert_eq!(&added[0].body, &Body::Results(want));
        prop_assert_eq!(m.session.at(c), Ok(added[0].id));
        self.batches.remove(&c);
        self.joined += 1;
        Ok(())
    }
}

/// A hook point's answer, picked by `k`: halt, fail, a hook's move, a
/// `pre_sample` hook answering the sample itself, or nothing.
fn hook(
    m: &mut Machine,
    c: CursorId,
    point: Hook,
    k: usize,
    nn: usize,
) -> Result<Fed, TestCaseError> {
    if point == Hook::PreSample {
        // I1: the sample phase applies only where `next` says so.
        prop_assert_eq!(m.session.next(c), Next::Sample);
    }
    Ok(match k % 16 {
        0 => Fed::Hooks {
            stop: Some("halt".into()),
            draft: None,
        },
        1 => Fed::HookFailed("boom".into()),
        2 => {
            // A hook's `cursor.move`, to a node that may not exist.
            let to = (k / 16) as NodeId % (nn as NodeId + 2);
            let at = m.session.at(c).unwrap();
            let moved = m.session.move_to(c, to, Author::Extension("x".into()), 0);
            if to >= nn as NodeId {
                prop_assert_eq!(moved, Err(Error::NoSuchNode(to))); // I3
                prop_assert_eq!(m.session.at(c), Ok(at));
            }
            ok()
        }
        3 if point == Hook::PreSample => Fed::Hooks {
            stop: None,
            draft: Some(reply(vec![Part::Text("answered".into())], Stop::End)),
        },
        _ => ok(),
    })
}

/// A provider's answer, picked by `k`: text, a batch of one to three calls,
/// an error, or a cancel.
fn sample(k: usize) -> Fed {
    match k % 4 {
        0 => Fed::Sampled {
            reply: Some(reply(vec![Part::Text("hi".into())], Stop::End)),
            error: None,
        },
        1 => {
            let parts = (0..=k / 4 % 3)
                .map(|j| Part::Call {
                    id: format!("c{j}"),
                    tool: "t".into(),
                    args_raw: "{}".into(),
                })
                .collect();
            Fed::Sampled {
                reply: Some(reply(parts, Stop::Tools)),
                error: None,
            }
        }
        2 => Fed::Sampled {
            reply: None,
            error: Some("overloaded".into()),
        },
        _ => Fed::Sampled {
            reply: None,
            error: None,
        },
    }
}

fn apply(m: &mut Machine, driver: &mut Driver, op: &Op) -> Result<(), TestCaseError> {
    let (nc, nn) = (cursors(&m.session), nodes(&m.session).len() as NodeId);
    let cursor = |i: usize| i as CursorId % nc;
    // Up to two ids past the last node: those must be refused (I3).
    let node = |i: usize| i as NodeId % (nn + 2);
    let term = || "term".to_string();
    match *op {
        Op::Send(i) => drop(m.send(cursor(i), term(), "hello", 0).unwrap()),
        Op::Input(i) => drop(m.input(cursor(i), term(), "more", 0).unwrap()),
        Op::Step(i) => {
            let busy = m.busy(cursor(i));
            let stepped = m.step(cursor(i), term(), 0);
            prop_assert_eq!(
                stepped,
                if busy {
                    Err(Error::Busy(cursor(i)))
                } else {
                    Ok(())
                }
            );
        }
        Op::Go(i, t) => {
            let at = m.session.at(cursor(i)).unwrap();
            match m.go_to(cursor(i), node(t), term(), 0) {
                Err(e) => {
                    prop_assert_eq!(e, Error::NoSuchNode(node(t)));
                    prop_assert!(node(t) >= nn);
                    prop_assert_eq!(m.session.at(cursor(i)), Ok(at));
                }
                Ok(_) => prop_assert!(node(t) < nn),
            }
        }
        Op::Auto(i, on) => m.set_auto(cursor(i), on, term(), 0).unwrap(),
        Op::Abort(i) => drop(m.abort(cursor(i))),
        Op::Open(t) => match m.open(node(t), Author::Core, 0, None) {
            Err(e) => prop_assert_eq!(e, Error::NoSuchNode(node(t))),
            Ok(id) => prop_assert_eq!(id, nc),
        },
        Op::Drive(i, k) => driver.drive(m, cursor(i), k)?,
    }
    Ok(())
}

fn config() -> impl Strategy<Value = Config> {
    prop::option::of(1..4u32).prop_map(|max_steps| Config {
        max_steps,
        data: "{}".into(),
    })
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(512))]

    /// I1–I4 hold after every frontend call and every job answered, and
    /// every batch handed out is answered once the cursors are brought to rest.
    #[test]
    fn invariants_hold_under_any_driver(cfg in config(), ops in prop::collection::vec(op(), 0..120)) {
        let (s, _) = Session::create(cfg, "sys", 0);
        let mut m = Machine::new(s);
        let mut d = Driver::default();
        for op in &ops {
            let old = nodes(&m.session);
            apply(&mut m, &mut d, op)?;
            let now = nodes(&m.session);
            // I2: a node never changes once appended.
            prop_assert_eq!(&now[..old.len()], &old[..]);
            // I3: every cursor is at a node of its session.
            for c in 0..cursors(&m.session) {
                prop_assert!(m.session.node(m.session.at(c).unwrap()).is_ok());
            }
        }
        // Rest every cursor: abort, then answer whatever the step still asks.
        for c in 0..cursors(&m.session) {
            m.abort(c).unwrap();
            for _ in 0..64 {
                if !m.busy(c) {
                    break;
                }
                d.drive(&mut m, c, 4)?;
            }
            prop_assert!(!m.busy(c), "cursor {} rests", c);
        }
        prop_assert!(d.batches.is_empty());
        prop_assert_eq!(d.run, d.joined, "I4: every batch run is answered");
        // I2: what was queued for the log is each node as it is now.
        let logged: Vec<Node> = m
            .session
            .outbox
            .iter()
            .filter_map(|r| match r {
                Record::Write { node, .. } => Some(node.clone()),
                _ => None,
            })
            .collect();
        prop_assert_eq!(logged, nodes(&m.session));
    }
}

fn body() -> impl Strategy<Value = Body> {
    let ids = prop::collection::vec(0..3u8, 0..4).prop_map(|ids| {
        Body::Results(
            ids.into_iter()
                .map(|i| (format!("c{i}"), Outcome::Ok("x".into()), Author::Tools))
                .collect(),
        )
    });
    prop_oneof![
        Just(Body::Config(Config::from("{}"))),
        Just(Body::System("s".into())),
        Just(Body::User("u".into())),
        Just(Body::Assistant {
            parts: vec![Part::Text("a".into())],
            stop: Stop::End,
            finish_reason: None
        }),
        Just(Body::Custom {
            kind: "note".into(),
            data: "n".into(),
        }),
        ids,
    ]
}

/// What I5 says an append under `parent` gives: V1, V2, and nothing else.
fn expected(s: &Session, parent: Option<NodeId>, body: &Body) -> Result<NodeId, Error> {
    match (parent, body) {
        (None, Body::Config(_)) => {}
        (None, _) => return Err(Error::RootNotConfig),
        (Some(p), _) => drop(s.node(p)?),
    }
    if let Body::Results(out) = body {
        let mut ids: Vec<&String> = out.iter().map(|(c, ..)| c).collect();
        ids.sort();
        ids.dedup();
        if ids.len() < out.len() {
            return Err(Error::DuplicateCall);
        }
    }
    Ok(nodes(s).len() as NodeId)
}

proptest! {
    /// I5: an append is refused only for a root that is not `Config` (V1) or
    /// a `Results` naming a call twice (V2); any other shape, under any node,
    /// is taken. A refused append changes nothing.
    #[test]
    fn appends_check_only_types(
        steps in prop::collection::vec((prop::option::weighted(0.9, 0..12u64), body(), any::<bool>()), 0..60),
    ) {
        let (mut s, c) = Session::create("{}", "sys", 0);
        for (parent, body, write) in steps {
            let (old, queued, at) = (nodes(&s), s.outbox.len(), s.at(c).unwrap());
            let (want, got) = if write {
                let want = expected(&s, Some(at), &body);
                (want, s.write(c, body, vec![], Author::Core, 0))
            } else {
                let want = expected(&s, parent, &body);
                (want, s.append(parent, Author::Core, body, vec![], 0))
            };
            prop_assert_eq!(&got, &want);
            if got.is_err() {
                prop_assert_eq!(nodes(&s), old);
                prop_assert_eq!(s.outbox.len(), queued);
                prop_assert_eq!(s.at(c), Ok(at));
            } else if write {
                prop_assert_eq!(s.at(c), got);
            }
        }
    }
}
