//! The step machine driven by a scripted driver: no Python, no clock.

use tau_core::step::{Draft, Ending, Fed, Hook, Job, Step};
use tau_core::tree::{Author, Body, ItemData, Outcome, Part, Record, Session, Stop};

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
        let job = step.next_job(s).unwrap();
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
                by: Author::Tools,
            })
            .to_vec(),
        _ => vec![ok()],
    });
    let b = bodies(&s, c);
    assert_eq!(
        b[4],
        Body::Results(vec![
            ("a".into(), Outcome::Ok("a".into()), Author::Tools),
            ("b".into(), Outcome::Ok("b".into()), Author::Tools)
        ])
    );
    assert_eq!(
        b[5],
        Body::Assistant {
            parts: vec![text("done")],
            stop: Stop::End
        }
    );
    assert_eq!(s.items(c).count(), 0, "nothing left behind");
    let results = s.walk(s.at(c).unwrap())[4];
    assert_eq!(results.by, Author::Tools);
}

#[test]
fn each_outcome_records_who_produced_it() {
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
        // a ran; b was denied by a permissions extension's pre_tool
        Job::RunCalls(_) => vec![
            Fed::CallDone {
                call: "a".into(),
                outcome: Outcome::Ok("listing".into()),
                by: Author::Tools,
            },
            Fed::CallDone {
                call: "b".into(),
                outcome: Outcome::Err("denied".into()),
                by: Author::Extension("permissions".into()),
            },
        ],
        _ => vec![ok()],
    });
    assert_eq!(
        bodies(&s, c)[4],
        Body::Results(vec![
            ("a".into(), Outcome::Ok("listing".into()), Author::Tools),
            (
                "b".into(),
                Outcome::Err("denied".into()),
                Author::Extension("permissions".into())
            ),
        ])
    );
}

#[test]
fn post_hooks_run_on_synced_data_and_pre_phase_writes_share_one_sync() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    s.outbox.clear();
    send(&mut s, c, "look");
    let mut samples = 0;
    let mut step = Step::wake(&s, c, false, 0);
    let jobs = drive(&mut s, &mut step, |job, s| {
        if let Job::Hooks { point, .. } = job {
            if matches!(point, Hook::PostReply | Hook::PostResult | Hook::PostStep) {
                assert!(s.synced(), "{point:?} handed out before a sync");
            }
        }
        match job {
            Job::Sample => {
                samples += 1;
                let r = if samples == 1 {
                    reply(vec![call("a")], Stop::Tools)
                } else {
                    reply(vec![text("done")], Stop::End)
                };
                vec![Fed::Sampled {
                    reply: Some(r),
                    error: None,
                }]
            }
            Job::RunCalls(_) => vec![Fed::CallDone {
                call: "a".into(),
                outcome: Outcome::Ok("x".into()),
                by: Author::Tools,
            }],
            _ => vec![ok()],
        }
    });
    assert_eq!(jobs.last(), Some(&Job::Rest));
    assert!(s.synced(), "at rest everything is synced");
    let kinds: Vec<&str> = s
        .outbox
        .iter()
        .map(|r| match r {
            Record::Write { .. } => "write",
            Record::Move(_) => "move",
            Record::Scratchpad => "pad",
            Record::Sync => "SYNC",
        })
        .collect();
    // send's item; input + reply, synced before post_reply; Results, synced
    // before post_result; the second reply, synced before post_step.
    assert_eq!(
        kinds,
        [
            "pad", "write", "pad", "write", "SYNC", "write", "SYNC", "write", "SYNC"
        ]
    );
}

/// Runs until the batch is handed out, feeds one call's outcome, then drops
/// everything a crash drops and reloads.
fn crash_mid_batch() -> (Session, u64) {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "look");
    let mut step = Step::wake(&s, c, false, 0);
    loop {
        match step.next_job(&mut s).unwrap() {
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
                        outcome: Outcome::Ok("done".into()),
                        by: Author::Tools,
                    },
                    0,
                )
                .unwrap();
                break; // the power goes out: b never finishes
            }
            _ => step.feed(&mut s, ok(), 0).unwrap(),
        }
    }
    drop(step);
    s.reconcile();
    (s, c)
}

#[test]
fn after_a_crash_mid_batch_input_goes_under_the_unanswered_batch() {
    let (mut s, c) = crash_mid_batch();
    assert!(
        matches!(bodies(&s, c).last(), Some(Body::Assistant { .. })),
        "rests at the batch"
    );
    send(&mut s, c, "the power went out, check the files");
    let mut step = Step::wake(&s, c, false, 0);
    let jobs = drive(&mut s, &mut step, |job, _| match job {
        Job::Sample => vec![Fed::Sampled {
            reply: Some(reply(vec![text("checking")], Stop::End)),
            error: None,
        }],
        _ => vec![ok()],
    });
    assert!(
        !jobs.iter().any(|j| matches!(j, Job::RunCalls(_))),
        "input first: nothing reruns"
    );
    let b = bodies(&s, c);
    assert!(matches!(b[3], Body::Assistant { .. }));
    assert_eq!(
        b[4],
        Body::User("the power went out, check the files".into())
    );
}

#[test]
fn after_a_crash_mid_batch_a_step_reruns_the_whole_batch() {
    let (mut s, c) = crash_mid_batch();
    let mut step = Step::wake(&s, c, true, 0);
    let jobs = drive(&mut s, &mut step, |job, _| match job {
        Job::RunCalls(run) => run
            .iter()
            .map(|r| Fed::CallDone {
                call: r.id.clone(),
                outcome: Outcome::Ok("again".into()),
                by: Author::Tools,
            })
            .collect(),
        _ => vec![ok()],
    });
    let ran: Vec<String> = jobs
        .iter()
        .filter_map(|j| {
            if let Job::RunCalls(r) = j {
                Some(r.iter().map(|c| c.id.clone()))
            } else {
                None
            }
        })
        .flatten()
        .collect();
    assert_eq!(ran, ["a", "b"]);
    assert_eq!(
        bodies(&s, c).last().unwrap(),
        &Body::Results(vec![
            ("a".into(), Outcome::Ok("again".into()), Author::Tools),
            ("b".into(), Outcome::Ok("again".into()), Author::Tools)
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
            let job = step.next_job(&mut s).unwrap();
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
