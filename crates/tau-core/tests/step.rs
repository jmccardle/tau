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
            Record::Label { .. } => "label",
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

/// Samples `replies` in order; answers every call `Ok(call id)` by `Tools`;
/// every hook passes.
fn scripted(mut replies: Vec<Draft>) -> impl FnMut(&Job, &mut Session) -> Vec<Fed> {
    replies.reverse();
    move |job, _| match job {
        Job::Sample => vec![Fed::Sampled {
            reply: Some(replies.pop().expect("a scripted reply")),
            error: None,
        }],
        Job::RunCalls(run) => run
            .iter()
            .map(|r| Fed::CallDone {
                call: r.id.clone(),
                outcome: Outcome::Ok(r.id.clone()),
                by: Author::Tools,
            })
            .collect(),
        Job::Wait { .. } => vec![Fed::Waited],
        _ => vec![ok()],
    }
}

fn ending(jobs: &[Job]) -> Ending {
    jobs.iter()
        .find_map(|j| match j {
            Job::Hooks {
                point: Hook::PostStep,
                ending: Some(e),
                ..
            } => Some(e.clone()),
            _ => None,
        })
        .expect("a post_step")
}

#[test]
fn a_tool_error_is_an_outcome_and_the_model_sees_it() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "look");
    let mut samples = 0;
    let mut step = Step::wake(&s, c, false, 0);
    drive(&mut s, &mut step, |job, _| match job {
        Job::Sample => {
            samples += 1;
            let r = if samples == 1 {
                reply(vec![call("a")], Stop::Tools)
            } else {
                reply(vec![text("it failed")], Stop::End)
            };
            vec![Fed::Sampled {
                reply: Some(r),
                error: None,
            }]
        }
        Job::RunCalls(_) => vec![Fed::CallDone {
            call: "a".into(),
            outcome: Outcome::Err("no such file".into()),
            by: Author::Tools,
        }],
        _ => vec![ok()],
    });
    let b = bodies(&s, c);
    assert_eq!(
        b[4],
        Body::Results(vec![(
            "a".into(),
            Outcome::Err("no such file".into()),
            Author::Tools
        )])
    );
    assert!(matches!(b[5], Body::Assistant { .. }));
}

#[test]
fn an_http_error_leaves_a_side_node_and_the_cursor_where_it_was() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false, 0);
    let jobs = drive(&mut s, &mut step, |job, _| match job {
        Job::Sample => vec![Fed::Sampled {
            reply: None,
            error: Some("HTTP 500".into()),
        }],
        _ => vec![ok()],
    });
    assert_eq!(ending(&jobs), Ending::Failed("HTTP 500".into()));
    assert_eq!(bodies(&s, c).last().unwrap(), &Body::User("hi".into()));
    let side = s.node(s.at(c).unwrap() + 1).unwrap();
    assert_eq!(side.parent, Some(s.at(c).unwrap()));
    assert!(matches!(&side.body, Body::Custom { kind, .. } if kind == "sample_error"));
    assert!(!s.auto[&c]);
}

#[test]
fn a_mid_stream_error_commits_the_partial_reply_and_fails() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false, 0);
    let jobs = drive(&mut s, &mut step, |job, _| match job {
        Job::Sample => vec![Fed::Sampled {
            reply: Some(reply(vec![text("hel")], Stop::Error)),
            error: Some("connection reset".into()),
        }],
        _ => vec![ok()],
    });
    assert!(jobs.contains(&Job::Hooks {
        point: Hook::PostReply,
        draft: None,
        ending: None
    }));
    assert_eq!(ending(&jobs), Ending::Failed("connection reset".into()));
    assert_eq!(
        bodies(&s, c).last().unwrap(),
        &Body::Assistant {
            parts: vec![text("hel")],
            stop: Stop::Error
        }
    );
}

#[test]
fn abort_during_sample_with_no_parts_commits_nothing() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false, 0);
    let mut jobs = vec![];
    loop {
        let job = step.next_job(&mut s).unwrap();
        jobs.push(job.clone());
        match job {
            Job::Rest => break,
            Job::Sample => {
                assert!(step.abort(&mut s));
                step.feed(
                    &mut s,
                    Fed::Sampled {
                        reply: None,
                        error: None,
                    },
                    0,
                )
                .unwrap();
            }
            _ => step.feed(&mut s, ok(), 0).unwrap(),
        }
    }
    assert_eq!(ending(&jobs), Ending::Aborted);
    assert!(!jobs.iter().any(|j| matches!(
        j,
        Job::Hooks {
            point: Hook::PostReply,
            ..
        }
    )));
    assert_eq!(bodies(&s, c).last().unwrap(), &Body::User("hi".into()));
}

#[test]
fn abort_during_tools_interrupts_unfinished_calls_and_runs_post_result_once() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "look");
    let mut step = Step::wake(&s, c, false, 0);
    let mut jobs = vec![];
    loop {
        let job = step.next_job(&mut s).unwrap();
        jobs.push(job.clone());
        match job {
            Job::Rest => break,
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
                let done = |call: &str, outcome| Fed::CallDone {
                    call: call.into(),
                    outcome,
                    by: Author::Tools,
                };
                step.feed(&mut s, done("a", Outcome::Ok("a".into())), 0)
                    .unwrap();
                assert!(step.abort(&mut s), "calls are in flight");
                step.feed(&mut s, done("b", Outcome::Interrupted), 0)
                    .unwrap();
            }
            _ => step.feed(&mut s, ok(), 0).unwrap(),
        }
    }
    let post_results = jobs
        .iter()
        .filter(|j| {
            matches!(
                j,
                Job::Hooks {
                    point: Hook::PostResult,
                    ..
                }
            )
        })
        .count();
    assert_eq!(post_results, 1);
    assert_eq!(ending(&jobs), Ending::Aborted);
    assert_eq!(
        bodies(&s, c).last().unwrap(),
        &Body::Results(vec![
            ("a".into(), Outcome::Ok("a".into()), Author::Tools),
            ("b".into(), Outcome::Interrupted, Author::Tools)
        ]),
        "no sample after the abort"
    );
}

#[test]
fn input_while_busy_waits_and_the_next_step_delivers_it_after_the_batch() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "look");
    let mut step = Step::wake(&s, c, false, 0);
    let mut script = scripted(vec![
        reply(vec![call("a")], Stop::Tools),
        reply(vec![text("seen both")], Stop::End),
    ]);
    drive(&mut s, &mut step, |job, s| {
        if matches!(job, Job::RunCalls(_)) {
            send(s, c, "also this");
        }
        script(job, s)
    });
    let b = bodies(&s, c);
    assert!(matches!(b[4], Body::Results(_)));
    assert_eq!(b[5], Body::User("also this".into()));
    assert!(matches!(b[6], Body::Assistant { .. }));
    assert_eq!(b.len(), 7);
}

#[test]
fn abort_leaves_pending_input_pending_and_a_send_then_delivers_it() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false, 0);
    loop {
        match step.next_job(&mut s).unwrap() {
            Job::Rest => break,
            Job::Sample => {
                send(&mut s, c, "wait, also");
                step.abort(&mut s);
                step.feed(
                    &mut s,
                    Fed::Sampled {
                        reply: None,
                        error: None,
                    },
                    0,
                )
                .unwrap();
            }
            _ => step.feed(&mut s, ok(), 0).unwrap(),
        }
    }
    assert_eq!(s.items(c).count(), 1, "still pending");
    assert!(!s.auto[&c]);
    send(&mut s, c, "go on");
    let mut step = Step::wake(&s, c, false, 0);
    drive(
        &mut s,
        &mut step,
        scripted(vec![
            reply(vec![text("one")], Stop::End),
            reply(vec![text("two")], Stop::End),
        ]),
    );
    let users: Vec<Body> = bodies(&s, c)
        .into_iter()
        .filter(|b| matches!(b, Body::User(_)))
        .collect();
    assert_eq!(
        users,
        [
            Body::User("hi".into()),
            Body::User("wait, also".into()),
            Body::User("go on".into())
        ]
    );
    assert_eq!(s.items(c).count(), 0);
}

#[test]
fn a_cursor_hook_string_halts_the_step() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false, 0);
    let jobs = drive(&mut s, &mut step, |job, _| match job {
        Job::Hooks {
            point: Hook::PreSample,
            ..
        } => vec![Fed::Hooks {
            stop: Some("over budget".into()),
            draft: None,
        }],
        _ => vec![ok()],
    });
    assert!(!jobs.contains(&Job::Sample));
    assert_eq!(ending(&jobs), Ending::Halted("over budget".into()));
    assert_eq!(bodies(&s, c).last().unwrap(), &Body::User("hi".into()));
    assert!(!s.auto[&c]);
}

#[test]
fn a_pre_sample_move_chooses_what_is_sampled_and_where_the_reply_lands() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false, 0);
    let mut script = scripted(vec![reply(vec![text("hello")], Stop::End)]);
    drive(&mut s, &mut step, |job, s| {
        if let Job::Hooks {
            point: Hook::PreSample,
            ..
        } = job
        {
            let by = Author::Extension("remind".into());
            s.write(c, Body::User("(be brief)".into()), vec![], by, 0)
                .unwrap();
        }
        script(job, s)
    });
    assert_eq!(
        bodies(&s, c)[3..],
        [
            Body::User("(be brief)".into()),
            Body::Assistant {
                parts: vec![text("hello")],
                stop: Stop::End
            }
        ]
    );
}

#[test]
fn a_post_reply_move_changes_where_the_tools_phase_looks() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    // An older batch elsewhere in the tree.
    let sys = s.at(c).unwrap();
    let old = s
        .append(
            Some(sys),
            Author::Provider,
            Body::Assistant {
                parts: vec![call("old")],
                stop: Stop::Tools,
            },
            vec![],
            0,
        )
        .unwrap();
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false, 0);
    let mut script = scripted(vec![
        reply(vec![text("hello")], Stop::End),
        reply(vec![text("after old")], Stop::End),
    ]);
    let mut moved = false;
    let jobs = drive(&mut s, &mut step, |job, s| {
        if let Job::Hooks {
            point: Hook::PostReply,
            ..
        } = job
        {
            if !moved {
                moved = true;
                s.move_to(c, old, Author::Extension("nav".into()), 0)
                    .unwrap();
            }
        }
        script(job, s)
    });
    let ran: Vec<&str> = jobs
        .iter()
        .filter_map(|j| match j {
            Job::RunCalls(r) => Some(r[0].id.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(ran, ["old"]);
    let b = bodies(&s, c);
    assert_eq!(b.len(), 5, "sys, old batch, its Results, the reply");
    assert!(matches!(b[2], Body::Assistant { .. }));
    assert!(matches!(b[3], Body::Results(_)));
}

/// A session resting at a one-call batch, answered once.
fn answered_batch() -> (Session, u64, u64) {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "look");
    let mut step = Step::wake(&s, c, false, 0);
    let mut first = true;
    drive(&mut s, &mut step, |job, s| {
        if let Job::Hooks {
            point: Hook::PostResult,
            ..
        } = job
        {
            s.auto.insert(c, false); // stop after the batch
        }
        match job {
            Job::Sample if first => {
                first = false;
                vec![Fed::Sampled {
                    reply: Some(reply(vec![call("a")], Stop::Tools)),
                    error: None,
                }]
            }
            Job::RunCalls(_) => vec![Fed::CallDone {
                call: "a".into(),
                outcome: Outcome::Ok("first".into()),
                by: Author::Tools,
            }],
            _ => vec![ok()],
        }
    });
    let batch = s.node(s.at(c).unwrap()).unwrap().parent.unwrap();
    (s, c, batch)
}

fn results_under(s: &Session, batch: u64, cursors: &[u64]) -> Vec<Body> {
    let mut seen: Vec<u64> = cursors
        .iter()
        .map(|&c| s.at(c).unwrap())
        .filter(|&n| s.node(n).unwrap().parent == Some(batch))
        .collect();
    seen.sort_unstable();
    seen.dedup();
    seen.into_iter()
        .map(|n| s.node(n).unwrap().body.clone())
        .collect()
}

#[test]
fn going_to_a_batch_and_stepping_writes_a_sibling_results() {
    let (mut s, c, batch) = answered_batch();
    let first = s.at(c).unwrap();
    s.move_to(c, batch, Author::Frontend("term".into()), 0)
        .unwrap();
    let mut step = Step::wake(&s, c, true, 0);
    drive(&mut s, &mut step, scripted(vec![]));
    let second = s.at(c).unwrap();
    assert_ne!(first, second);
    assert_eq!(s.node(second).unwrap().parent, Some(batch));
    assert_eq!(
        s.node(first).unwrap().body,
        Body::Results(vec![(
            "a".into(),
            Outcome::Ok("first".into()),
            Author::Tools
        )]),
        "the first answer is final"
    );
}

#[test]
fn two_cursors_at_one_batch_both_run_it() {
    let (mut s, c, batch) = answered_batch();
    s.move_to(c, batch, Author::Frontend("term".into()), 0)
        .unwrap();
    let d = s.open(batch, Author::Frontend("term".into()), 0).unwrap();
    for cur in [c, d] {
        let mut step = Step::wake(&s, cur, true, 0);
        drive(&mut s, &mut step, scripted(vec![]));
    }
    assert_ne!(s.at(c).unwrap(), s.at(d).unwrap());
    assert_eq!(results_under(&s, batch, &[c, d]).len(), 2);
}
