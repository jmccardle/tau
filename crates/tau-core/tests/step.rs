//! The step machine driven by a scripted driver: no Python, no clock.

use tau_core::step::{Draft, Ending, Fed, Hook, Job, Step};
use tau_core::tree::{Author, Body, ItemData, Outcome, Part, Session, Stop};

fn reply(parts: Vec<Part>, stop: Stop) -> Draft {
    Draft {
        body: Body::Assistant { parts, stop },
        delivers: vec![],
        by: Author::Provider,
    }
}

fn text(t: &str) -> Part {
    Part::Text(t.into())
}

fn call(id: &str) -> Part {
    Part::Call {
        id: id.into(),
        tool: "ls".into(),
        args_raw: "{}".into(),
    }
}

fn ok() -> Fed {
    Fed::Hooks {
        stop: None,
        draft: None,
    }
}

/// Runs a step to rest, answering each job with `answer`. Returns the jobs seen.
fn drive(
    s: &mut Session,
    step: &mut Step,
    mut answer: impl FnMut(&Job, &mut Session) -> Vec<Fed>,
) -> Vec<Job> {
    let mut seen = vec![];
    for _ in 0..100 {
        let job = step.next_job(s, 0).unwrap();
        seen.push(job.clone());
        if job == Job::Rest {
            return seen;
        }
        for fed in answer(&job, s) {
            step.feed(s, fed, 0).unwrap();
        }
    }
    panic!("no rest: {seen:?}");
}

fn bodies(s: &Session, cursor: u64) -> Vec<Body> {
    s.walk(s.at(cursor).unwrap())
        .into_iter()
        .map(|n| n.body.clone())
        .collect()
}

fn send(s: &mut Session, cursor: u64, t: &str) {
    s.add_item(
        cursor,
        ItemData::Input {
            client: "term".into(),
            text: t.into(),
        },
    );
    s.auto.insert(cursor, true);
}

#[test]
fn plain_turn() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false, 0);
    drive(&mut s, &mut step, |job, _| match job {
        Job::Sample => vec![Fed::Sampled {
            reply: Some(reply(vec![text("hello")], Stop::End)),
            error: None,
        }],
        _ => vec![ok()],
    });
    assert_eq!(
        bodies(&s, c)[2..],
        [
            Body::User("hi".into()),
            Body::Assistant {
                parts: vec![text("hello")],
                stop: Stop::End
            }
        ]
    );
    assert_eq!(s.items(c).count(), 0);
}

#[test]
fn batch_answers_in_call_order_then_samples_again() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "look");
    let mut samples = 0;
    let mut step = Step::wake(&s, c, false, 0);
    drive(&mut s, &mut step, |job, _| match job {
        Job::Sample => {
            samples += 1;
            let r = if samples == 1 {
                reply(vec![call("a"), call("b")], Stop::Tools)
            } else {
                reply(vec![text("done")], Stop::End)
            };
            vec![Fed::Sampled {
                reply: Some(r),
                error: None,
            }]
        }
        // b finishes first
        Job::RunCalls(_) => ["b", "a"]
            .map(|id| Fed::CallDone {
                call: id.into(),
                outcome: Outcome::Ok(id.into()),
            })
            .to_vec(),
        _ => vec![ok()],
    });
    let b = bodies(&s, c);
    assert_eq!(
        b[4],
        Body::Results(vec![
            ("a".into(), Outcome::Ok("a".into())),
            ("b".into(), Outcome::Ok("b".into()))
        ])
    );
    assert_eq!(
        b[5],
        Body::Assistant {
            parts: vec![text("done")],
            stop: Stop::End
        }
    );
    assert_eq!(
        s.items(c).count(),
        0,
        "the Results node delivered the journal"
    );
}

#[test]
fn crash_mid_batch_keeps_finished_calls() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "look");
    let mut step = Step::wake(&s, c, false, 0);
    loop {
        match step.next_job(&mut s, 0).unwrap() {
            Job::Sample => step
                .feed(
                    &mut s,
                    Fed::Sampled {
                        reply: Some(reply(vec![call("a"), call("b")], Stop::Tools)),
                        error: None,
                    },
                    0,
                )
                .unwrap(),
            Job::RunCalls(_) => {
                step.feed(
                    &mut s,
                    Fed::CallDone {
                        call: "a".into(),
                        outcome: Outcome::Ok("ten minutes".into()),
                    },
                    0,
                )
                .unwrap();
                break; // crash: b never finishes
            }
            _ => step.feed(&mut s, ok(), 0).unwrap(),
        }
    }
    drop(step);
    s.reconcile();
    let mut step = Step::wake(&s, c, true, 0);
    let jobs = drive(&mut s, &mut step, |job, _| match job {
        Job::RunCalls(run) => run
            .iter()
            .map(|r| Fed::CallDone {
                call: r.id.clone(),
                outcome: Outcome::Interrupted,
            })
            .collect(),
        _ => vec![ok()],
    });
    let run = jobs
        .iter()
        .find_map(|j| {
            if let Job::RunCalls(r) = j {
                Some(r.clone())
            } else {
                None
            }
        })
        .unwrap();
    assert_eq!(
        (run.len(), run[0].id.as_str(), run[0].resumed),
        (1, "b", true)
    );
    assert_eq!(
        bodies(&s, c).last().unwrap(),
        &Body::Results(vec![
            ("a".into(), Outcome::Ok("ten minutes".into())),
            ("b".into(), Outcome::Interrupted)
        ])
    );
}

#[test]
fn pre_input_string_refuses_with_a_notice() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "/model fast");
    let mut step = Step::wake(&s, c, false, 0);
    let jobs = drive(&mut s, &mut step, |job, _| match job {
        Job::Hooks {
            point: Hook::PreInput,
            ..
        } => vec![Fed::Hooks {
            stop: Some("model: fast".into()),
            draft: None,
        }],
        _ => vec![ok()],
    });
    assert!(jobs.contains(&Job::Hooks {
        point: Hook::PostStep,
        draft: None,
        ending: Some(Ending::Halted("model: fast".into()))
    }));
    assert_eq!(s.notices, [("term".into(), "model: fast".into())]);
    assert_eq!(
        (s.items(c).count(), s.auto[&c], bodies(&s, c).len()),
        (0, false, 2)
    );
}

#[test]
fn pre_reply_move_chooses_where_the_reply_lands() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false, 0);
    drive(&mut s, &mut step, |job, s| match job {
        Job::Sample => vec![Fed::Sampled {
            reply: Some(reply(vec![text("hello")], Stop::End)),
            error: None,
        }],
        Job::Hooks {
            point: Hook::PreReply,
            ..
        } => {
            let note = s
                .write(
                    c,
                    Body::User("(reminder)".into()),
                    vec![],
                    Author::Extension("ctx".into()),
                    0,
                )
                .unwrap();
            s.move_to(c, note, Author::Extension("ctx".into()), 0)
                .unwrap();
            vec![ok()]
        }
        _ => vec![ok()],
    });
    assert_eq!(
        bodies(&s, c)[3..],
        [
            Body::User("(reminder)".into()),
            Body::Assistant {
                parts: vec![text("hello")],
                stop: Stop::End
            }
        ]
    );
}

#[test]
fn abort_during_sample_commits_the_partial_reply() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false, 0);
    let mut cancelled = false;
    let jobs = {
        let mut seen = vec![];
        loop {
            let job = step.next_job(&mut s, 0).unwrap();
            seen.push(job.clone());
            match job {
                Job::Rest => break seen,
                Job::Sample => {
                    cancelled = step.abort(&mut s);
                    step.feed(
                        &mut s,
                        Fed::Sampled {
                            reply: Some(reply(vec![text("hel")], Stop::Interrupted)),
                            error: None,
                        },
                        0,
                    )
                    .unwrap();
                }
                _ => step.feed(&mut s, ok(), 0).unwrap(),
            }
        }
    };
    assert!(cancelled);
    assert!(jobs.contains(&Job::Hooks {
        point: Hook::PostReply,
        draft: None,
        ending: None
    }));
    assert!(jobs.contains(&Job::Hooks {
        point: Hook::PostStep,
        draft: None,
        ending: Some(Ending::Aborted)
    }));
    assert_eq!(
        bodies(&s, c).last().unwrap(),
        &Body::Assistant {
            parts: vec![text("hel")],
            stop: Stop::Interrupted
        }
    );
    assert!(!s.auto[&c]);
}

#[test]
fn a_step_that_committed_nothing_is_throttled() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let mut step = Step::wake(&s, c, false, 0);
    let mut woke = false;
    let jobs = drive(&mut s, &mut step, |job, s| match job {
        Job::Hooks {
            point: Hook::PreStep,
            ..
        } if !woke => {
            woke = true;
            s.add_item(
                c,
                ItemData::Extension {
                    name: "timer".into(),
                    data: "tick".into(),
                },
            );
            vec![ok()]
        }
        Job::Wait { .. } => vec![Fed::Waited],
        _ => vec![ok()],
    });
    assert!(jobs.contains(&Job::Wait { until: 250 }));
    assert_eq!(
        jobs.iter()
            .filter(|j| matches!(
                j,
                Job::Hooks {
                    point: Hook::PreStep,
                    ..
                }
            ))
            .count(),
        2,
        "the item left in place does not spin"
    );
}
