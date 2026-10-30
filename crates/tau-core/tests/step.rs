//! The step machine driven by a scripted driver: no Python, no clock.

use tau_core::step::{Answer, Draft, Ending, Hook, Job, Step};
use tau_core::tree::{Author, Body, Error, ItemData, Outcome, Part, Record, Session, Stop};

fn reply(parts: Vec<Part>, stop: Stop) -> Draft {
    Draft {
        body: Body::Assistant {
            parts,
            stop,
            finish_reason: None,
            meta: None,
        },
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

/// Runs a step to rest, answering each job with `script`. Returns the jobs seen.
fn drive(
    s: &mut Session,
    step: &mut Step,
    mut script: impl FnMut(&Job, &mut Session) -> Vec<Answer>,
) -> Vec<Job> {
    let mut seen = vec![];
    for _ in 0..100 {
        let job = step.next_job(s).unwrap();
        seen.push(job.clone());
        if job == Job::Rest {
            return seen;
        }
        for answer in script(&job, s) {
            step.answer(s, answer, 0).unwrap();
        }
    }
    panic!("no rest: {seen:?}");
}

fn bodies(s: &Session, cursor: u64) -> Vec<Body> {
    s.tree
        .walk(s.at(cursor).unwrap())
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
    let mut step = Step::wake(&s, c, false);
    drive(&mut s, &mut step, |job, _| match job {
        Job::Sample => vec![Answer::Sampled {
            reply: Some(reply(vec![text("hello")], Stop::End)),
            error: None,
        }],
        _ => vec![echo(job)],
    });
    assert_eq!(
        bodies(&s, c)[2..],
        [
            Body::User("hi".into()),
            Body::Assistant {
                parts: vec![text("hello")],
                stop: Stop::End,
                finish_reason: None,
                meta: None,
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
    let mut step = Step::wake(&s, c, false);
    drive(&mut s, &mut step, |job, _| match job {
        Job::Sample => {
            samples += 1;
            let r = if samples == 1 {
                reply(vec![call("a"), call("b")], Stop::Tools)
            } else {
                reply(vec![text("done")], Stop::End)
            };
            vec![Answer::Sampled {
                reply: Some(r),
                error: None,
            }]
        }
        // b finishes first
        Job::RunCalls { .. } => ["b", "a"]
            .map(|id| Answer::CallDone {
                call: id.into(),
                outcome: Outcome::Ok(id.into()),
                by: Author::Tools,
            })
            .to_vec(),
        _ => vec![echo(job)],
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
            stop: Stop::End,
            finish_reason: None,
            meta: None,
        }
    );
    assert_eq!(s.items(c).count(), 0, "nothing left behind");
    let results = s.tree.walk(s.at(c).unwrap())[4];
    assert_eq!(results.by, Author::Tools);
}

#[test]
fn each_outcome_records_who_produced_it() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "look");
    let mut samples = 0;
    let mut step = Step::wake(&s, c, false);
    drive(&mut s, &mut step, |job, _| match job {
        Job::Sample => {
            samples += 1;
            let r = if samples == 1 {
                reply(vec![call("a"), call("b")], Stop::Tools)
            } else {
                reply(vec![text("done")], Stop::End)
            };
            vec![Answer::Sampled {
                reply: Some(r),
                error: None,
            }]
        }
        // a ran; b was denied by a permissions extension's pre_tool
        Job::RunCalls { .. } => vec![
            Answer::CallDone {
                call: "a".into(),
                outcome: Outcome::Ok("listing".into()),
                by: Author::Tools,
            },
            Answer::CallDone {
                call: "b".into(),
                outcome: Outcome::Err("denied".into()),
                by: Author::Extension("permissions".into()),
            },
        ],
        _ => vec![echo(job)],
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
    let mut step = Step::wake(&s, c, false);
    let jobs = drive(&mut s, &mut step, |job, s| {
        if let Job::Hooks { point, .. } = job
            && matches!(point, Hook::PostReply | Hook::PostResult | Hook::PostStep)
        {
            assert!(s.synced(), "{point:?} handed out before a sync");
        }
        match job {
            Job::Sample => {
                samples += 1;
                let r = if samples == 1 {
                    reply(vec![call("a")], Stop::Tools)
                } else {
                    reply(vec![text("done")], Stop::End)
                };
                vec![Answer::Sampled {
                    reply: Some(r),
                    error: None,
                }]
            }
            Job::RunCalls { .. } => vec![Answer::CallDone {
                call: "a".into(),
                outcome: Outcome::Ok("x".into()),
                by: Author::Tools,
            }],
            _ => vec![echo(job)],
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
            Record::Label { .. } => "label",
            Record::Sync { pad: Some(_) } => "SYNC+pad",
            Record::Sync { pad: None } => "SYNC",
        })
        .collect();
    // input + reply (and the delivered item gone), synced before post_reply;
    // Results, before post_result; the second reply, before post_step.
    assert_eq!(
        kinds,
        [
            "write", "write", "SYNC+pad", "write", "SYNC", "write", "SYNC"
        ]
    );
}

/// Runs until the batch is handed out, answers one call's outcome, then drops
/// everything a crash drops and reloads.
fn crash_mid_batch() -> (Session, u64) {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "look");
    let mut step = Step::wake(&s, c, false);
    loop {
        match step.next_job(&mut s).unwrap() {
            Job::Sample => step
                .answer(
                    &mut s,
                    Answer::Sampled {
                        reply: Some(reply(vec![call("a"), call("b")], Stop::Tools)),
                        error: None,
                    },
                    0,
                )
                .unwrap(),
            Job::RunCalls { .. } => {
                step.answer(
                    &mut s,
                    Answer::CallDone {
                        call: "a".into(),
                        outcome: Outcome::Ok("done".into()),
                        by: Author::Tools,
                    },
                    0,
                )
                .unwrap();
                break; // the power goes out: b never finishes
            }
            job => step.answer(&mut s, echo(&job), 0).unwrap(),
        }
    }
    drop(step);
    tau_core::tree::reconcile(&mut s);
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
    let mut step = Step::wake(&s, c, false);
    let jobs = drive(&mut s, &mut step, |job, _| match job {
        Job::Sample => vec![Answer::Sampled {
            reply: Some(reply(vec![text("checking")], Stop::End)),
            error: None,
        }],
        _ => vec![echo(job)],
    });
    assert!(
        !jobs.iter().any(|j| matches!(j, Job::RunCalls { .. })),
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
    let mut step = Step::wake(&s, c, true);
    let jobs = drive(&mut s, &mut step, |job, _| match job {
        Job::RunCalls { calls: run, .. } => run
            .iter()
            .map(|r| Answer::CallDone {
                call: r.id.clone(),
                outcome: Outcome::Ok("again".into()),
                by: Author::Tools,
            })
            .collect(),
        _ => vec![echo(job)],
    });
    let ran: Vec<String> = jobs
        .iter()
        .filter_map(|j| {
            if let Job::RunCalls { calls: r, .. } = j {
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
    let mut step = Step::wake(&s, c, false);
    let jobs = drive(&mut s, &mut step, |job, _| match job {
        Job::Hooks {
            point: Hook::PreInput,
            ..
        } => vec![Answer::Hooks {
            stop: Some("model: fast".into()),
            draft: None,
        }],
        _ => vec![echo(job)],
    });
    assert!(has_hook(
        &jobs,
        Hook::PostStep,
        Some(&Ending::Halted("model: fast".into()))
    ));
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
    let mut step = Step::wake(&s, c, false);
    drive(&mut s, &mut step, |job, s| match job {
        Job::Sample => vec![Answer::Sampled {
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
            vec![echo(job)]
        }
        _ => vec![echo(job)],
    });
    assert_eq!(
        bodies(&s, c)[3..],
        [
            Body::User("(reminder)".into()),
            Body::Assistant {
                parts: vec![text("hello")],
                stop: Stop::End,
                finish_reason: None,
                meta: None,
            }
        ]
    );
}

#[test]
fn abort_during_sample_commits_the_partial_reply() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false);
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
                    step.answer(
                        &mut s,
                        Answer::Sampled {
                            reply: Some(reply(vec![text("hel")], Stop::Interrupted)),
                            error: None,
                        },
                        0,
                    )
                    .unwrap();
                }
                job => step.answer(&mut s, echo(&job), 0).unwrap(),
            }
        }
    };
    assert!(cancelled);
    assert!(has_hook(&jobs, Hook::PostReply, None));
    assert!(has_hook(&jobs, Hook::PostStep, Some(&Ending::Aborted)));
    assert_eq!(
        bodies(&s, c).last().unwrap(),
        &Body::Assistant {
            parts: vec![text("hel")],
            stop: Stop::Interrupted,
            finish_reason: None,
            meta: None,
        }
    );
    assert!(!s.auto[&c]);
}

fn pre_steps(jobs: &[Job]) -> usize {
    jobs.iter()
        .filter(|j| {
            matches!(
                j,
                Job::Hooks {
                    point: Hook::PreStep,
                    ..
                }
            )
        })
        .count()
}

#[test]
fn an_item_added_while_busy_causes_one_more_step() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let mut step = Step::wake(&s, c, false);
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
        _ => vec![echo(job)],
    });
    assert_eq!(pre_steps(&jobs), 2, "one more step, none after it");
}

#[test]
fn a_hook_that_declines_every_input_runs_one_step_then_auto_step_goes_off() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let before = bodies(&s, c).len();
    let mut step = Step::wake(&s, c, false);
    // `ok()` answers `pre_input` with no draft: the input is declined.
    let jobs = drive(&mut s, &mut step, |_, _| vec![ok()]);
    assert_eq!(pre_steps(&jobs), 1, "the cursor does not try again");
    assert!(!jobs.contains(&Job::Sample));
    assert!(!s.auto[&c], "a cursor that cannot move stops trying");
    assert!(
        s.items(c).any(|i| matches!(i.data, ItemData::Input { .. })),
        "the input stays pending"
    );
    assert_eq!(bodies(&s, c).len(), before, "nothing was written");
}

#[test]
fn a_discarded_reply_runs_one_step_then_auto_step_goes_off() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let root = s.at(c).unwrap();
    s.write(c, Body::User("hi".into()), vec![], Author::Core, 0)
        .unwrap();
    s.auto.insert(c, true);
    assert_ne!(s.at(c).unwrap(), root);
    let before = bodies(&s, c).len();
    let mut step = Step::wake(&s, c, false);
    let jobs = drive(&mut s, &mut step, |job, _| match job {
        Job::Sample => vec![Answer::Sampled {
            reply: Some(reply(vec![text("lost")], Stop::End)),
            error: None,
        }],
        _ => vec![ok()], // `pre_reply` leaves no draft
    });
    assert_eq!(pre_steps(&jobs), 1);
    assert_eq!(jobs.iter().filter(|j| **j == Job::Sample).count(), 1);
    assert!(!s.auto[&c]);
    assert_eq!(bodies(&s, c).len(), before, "the reply was never written");
}

/// Samples `replies` in order; answers every call `Ok(call id)` by `Tools`;
/// every hook passes.
fn scripted(mut replies: Vec<Draft>) -> impl FnMut(&Job, &mut Session) -> Vec<Answer> {
    replies.reverse();
    move |job, _| match job {
        Job::Sample => vec![Answer::Sampled {
            reply: Some(replies.pop().expect("a scripted reply")),
            error: None,
        }],
        Job::RunCalls { calls: run, .. } => run
            .iter()
            .map(|r| Answer::CallDone {
                call: r.id.clone(),
                outcome: Outcome::Ok(r.id.clone()),
                by: Author::Tools,
            })
            .collect(),
        _ => vec![echo(job)],
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
    let mut step = Step::wake(&s, c, false);
    drive(&mut s, &mut step, |job, _| match job {
        Job::Sample => {
            samples += 1;
            let r = if samples == 1 {
                reply(vec![call("a")], Stop::Tools)
            } else {
                reply(vec![text("it failed")], Stop::End)
            };
            vec![Answer::Sampled {
                reply: Some(r),
                error: None,
            }]
        }
        Job::RunCalls { .. } => vec![Answer::CallDone {
            call: "a".into(),
            outcome: Outcome::Err("no such file".into()),
            by: Author::Tools,
        }],
        _ => vec![echo(job)],
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
    let mut step = Step::wake(&s, c, false);
    let jobs = drive(&mut s, &mut step, |job, _| match job {
        Job::Sample => vec![Answer::Sampled {
            reply: None,
            error: Some("HTTP 500".into()),
        }],
        _ => vec![echo(job)],
    });
    assert_eq!(ending(&jobs), Ending::Failed("HTTP 500".into()));
    assert_eq!(bodies(&s, c).last().unwrap(), &Body::User("hi".into()));
    let side = s.tree.node(s.at(c).unwrap() + 1).unwrap();
    assert_eq!(side.parent, Some(s.at(c).unwrap()));
    assert!(matches!(&side.body, Body::Custom { kind, .. } if kind == "sample_error"));
    assert!(!s.auto[&c]);
}

#[test]
fn a_mid_stream_error_commits_the_partial_reply_and_fails() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false);
    let jobs = drive(&mut s, &mut step, |job, _| match job {
        Job::Sample => vec![Answer::Sampled {
            reply: Some(reply(vec![text("hel")], Stop::Error)),
            error: Some("connection reset".into()),
        }],
        _ => vec![echo(job)],
    });
    assert!(has_hook(&jobs, Hook::PostReply, None));
    assert_eq!(ending(&jobs), Ending::Failed("connection reset".into()));
    assert_eq!(
        bodies(&s, c).last().unwrap(),
        &Body::Assistant {
            parts: vec![text("hel")],
            stop: Stop::Error,
            finish_reason: None,
            meta: None,
        }
    );
}

#[test]
fn abort_during_sample_with_no_parts_commits_nothing() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false);
    let mut jobs = vec![];
    loop {
        let job = step.next_job(&mut s).unwrap();
        jobs.push(job.clone());
        match job {
            Job::Rest => break,
            Job::Sample => {
                assert!(step.abort(&mut s));
                step.answer(
                    &mut s,
                    Answer::Sampled {
                        reply: None,
                        error: None,
                    },
                    0,
                )
                .unwrap();
            }
            job => step.answer(&mut s, echo(&job), 0).unwrap(),
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
    let mut step = Step::wake(&s, c, false);
    let mut jobs = vec![];
    loop {
        let job = step.next_job(&mut s).unwrap();
        jobs.push(job.clone());
        match job {
            Job::Rest => break,
            Job::Sample => step
                .answer(
                    &mut s,
                    Answer::Sampled {
                        reply: Some(reply(vec![call("a"), call("b")], Stop::Tools)),
                        error: None,
                    },
                    0,
                )
                .unwrap(),
            Job::RunCalls { .. } => {
                let done = |call: &str, outcome| Answer::CallDone {
                    call: call.into(),
                    outcome,
                    by: Author::Tools,
                };
                step.answer(&mut s, done("a", Outcome::Ok("a".into())), 0)
                    .unwrap();
                assert!(step.abort(&mut s), "calls are in flight");
                step.answer(&mut s, done("b", Outcome::Interrupted), 0)
                    .unwrap();
            }
            job => step.answer(&mut s, echo(&job), 0).unwrap(),
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
    let mut step = Step::wake(&s, c, false);
    let mut script = scripted(vec![
        reply(vec![call("a")], Stop::Tools),
        reply(vec![text("seen both")], Stop::End),
    ]);
    drive(&mut s, &mut step, |job, s| {
        if matches!(job, Job::RunCalls { .. }) {
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
    let mut step = Step::wake(&s, c, false);
    loop {
        match step.next_job(&mut s).unwrap() {
            Job::Rest => break,
            Job::Sample => {
                send(&mut s, c, "wait, also");
                step.abort(&mut s);
                step.answer(
                    &mut s,
                    Answer::Sampled {
                        reply: None,
                        error: None,
                    },
                    0,
                )
                .unwrap();
            }
            job => step.answer(&mut s, echo(&job), 0).unwrap(),
        }
    }
    assert_eq!(s.items(c).count(), 1, "still pending");
    assert!(!s.auto[&c]);
    send(&mut s, c, "go on");
    let mut step = Step::wake(&s, c, false);
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
    let mut step = Step::wake(&s, c, false);
    let jobs = drive(&mut s, &mut step, |job, _| match job {
        Job::Hooks {
            point: Hook::PreSample,
            ..
        } => vec![Answer::Hooks {
            stop: Some("over budget".into()),
            draft: None,
        }],
        _ => vec![echo(job)],
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
    let mut step = Step::wake(&s, c, false);
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
                stop: Stop::End,
                finish_reason: None,
                meta: None,
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
            sys,
            Author::Provider,
            Body::Assistant {
                parts: vec![call("old")],
                stop: Stop::Tools,
                finish_reason: None,
                meta: None,
            },
            vec![],
            0,
        )
        .unwrap();
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false);
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
            && !moved
        {
            moved = true;
            s.move_to(c, old, Author::Extension("nav".into()), 0)
                .unwrap();
        }
        script(job, s)
    });
    let ran: Vec<&str> = jobs
        .iter()
        .filter_map(|j| match j {
            Job::RunCalls { calls: r, .. } => Some(r[0].id.as_str()),
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
    let mut step = Step::wake(&s, c, false);
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
                vec![Answer::Sampled {
                    reply: Some(reply(vec![call("a")], Stop::Tools)),
                    error: None,
                }]
            }
            Job::RunCalls { .. } => vec![Answer::CallDone {
                call: "a".into(),
                outcome: Outcome::Ok("first".into()),
                by: Author::Tools,
            }],
            _ => vec![echo(job)],
        }
    });
    let batch = s.tree.node(s.at(c).unwrap()).unwrap().parent.unwrap();
    (s, c, batch)
}

fn results_under(s: &Session, batch: u64, cursors: &[u64]) -> Vec<Body> {
    let mut seen: Vec<u64> = cursors
        .iter()
        .map(|&c| s.at(c).unwrap())
        .filter(|&n| s.tree.node(n).unwrap().parent == Some(batch))
        .collect();
    seen.sort_unstable();
    seen.dedup();
    seen.into_iter()
        .map(|n| s.tree.node(n).unwrap().body.clone())
        .collect()
}

#[test]
fn going_to_a_batch_and_stepping_writes_a_sibling_results() {
    let (mut s, c, batch) = answered_batch();
    let first = s.at(c).unwrap();
    s.move_to(c, batch, Author::Frontend("term".into()), 0)
        .unwrap();
    let mut step = Step::wake(&s, c, true);
    drive(&mut s, &mut step, scripted(vec![]));
    let second = s.at(c).unwrap();
    assert_ne!(first, second);
    assert_eq!(s.tree.node(second).unwrap().parent, Some(batch));
    assert_eq!(
        s.tree.node(first).unwrap().body,
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
        let mut step = Step::wake(&s, cur, true);
        drive(&mut s, &mut step, scripted(vec![]));
    }
    assert_ne!(s.at(c).unwrap(), s.at(d).unwrap());
    assert_eq!(results_under(&s, batch, &[c, d]).len(), 2);
}

// A answer is accepted only while the job it answers is out; anything else is
// `WrongAnswer` and changes nothing.

fn refused(s: &mut Session, step: &mut Step, c: u64, answer: Answer) {
    let before = s.clone();
    assert_eq!(step.answer(s, answer, 0), Err(Error::WrongAnswer(c)));
    assert_eq!(*s, before, "a refused answer changes nothing");
}

fn stop() -> Answer {
    Answer::Hooks {
        stop: Some("no".into()),
        draft: None,
    }
}

#[test]
fn a_second_hooks_feed_before_the_next_job_is_refused() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false);
    // To the pre_sample hook, answer it, then answer again with no job out.
    for _ in 0..2 {
        let job = step.next_job(&mut s).unwrap();
        step.answer(&mut s, echo(&job), 0).unwrap();
    }
    assert!(matches!(
        step.next_job(&mut s).unwrap(),
        Job::Hooks {
            point: Hook::PreSample,
            ..
        }
    ));
    step.answer(&mut s, ok(), 0).unwrap();
    refused(&mut s, &mut step, c, ok());
    refused(&mut s, &mut step, c, stop());
    refused(&mut s, &mut step, c, Answer::HookFailed("x".into()));
}

#[test]
fn a_second_feed_at_a_pre_input_hook_is_refused_not_a_panic() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false);
    let job = step.next_job(&mut s).unwrap();
    step.answer(&mut s, echo(&job), 0).unwrap();
    let job = step.next_job(&mut s).unwrap();
    assert!(matches!(
        job,
        Job::Hooks {
            point: Hook::PreInput,
            ..
        }
    ));
    step.answer(&mut s, echo(&job), 0).unwrap();
    refused(&mut s, &mut step, c, ok());
}

#[test]
fn a_stop_at_rest_does_not_restart_the_step() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let mut step = Step::wake(&s, c, false);
    drive(&mut s, &mut step, |_, _| vec![ok()]);
    assert!(step.resting());
    refused(&mut s, &mut step, c, stop());
    refused(&mut s, &mut step, c, Answer::HookFailed("x".into()));
    assert!(step.resting());
    assert_eq!(step.next_job(&mut s), Ok(Job::Rest));
}

#[test]
fn a_hook_feed_while_a_sample_is_out_is_refused() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false);
    loop {
        match step.next_job(&mut s).unwrap() {
            Job::Sample => break,
            job => step.answer(&mut s, echo(&job), 0).unwrap(),
        }
    }
    refused(&mut s, &mut step, c, Answer::HookFailed("x".into()));
    refused(&mut s, &mut step, c, stop());
    refused(&mut s, &mut step, c, ok());
    // The sample is still the job.
    assert_eq!(step.next_job(&mut s), Ok(Job::Sample));
}

#[test]
fn a_stop_while_calls_are_out_does_not_discard_the_batch() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false);
    loop {
        match step.next_job(&mut s).unwrap() {
            Job::Sample => step
                .answer(
                    &mut s,
                    Answer::Sampled {
                        reply: Some(reply(vec![call("a")], Stop::Tools)),
                        error: None,
                    },
                    0,
                )
                .unwrap(),
            Job::RunCalls { .. } => break,
            job => step.answer(&mut s, echo(&job), 0).unwrap(),
        }
    }
    refused(&mut s, &mut step, c, stop());
    refused(&mut s, &mut step, c, Answer::HookFailed("x".into()));
    let unknown = Answer::CallDone {
        call: "zzz".into(),
        outcome: Outcome::Ok("x".into()),
        by: Author::Tools,
    };
    refused(&mut s, &mut step, c, unknown);
    let done = Answer::CallDone {
        call: "a".into(),
        outcome: Outcome::Ok("x".into()),
        by: Author::Tools,
    };
    step.answer(&mut s, done.clone(), 0).unwrap();
    refused(&mut s, &mut step, c, done);
}

#[test]
fn a_call_done_with_no_batch_out_is_refused() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let mut step = Step::wake(&s, c, false);
    let done = Answer::CallDone {
        call: "a".into(),
        outcome: Outcome::Ok("x".into()),
        by: Author::Tools,
    };
    refused(&mut s, &mut step, c, done.clone());
    drive(&mut s, &mut step, |_, _| vec![ok()]);
    refused(&mut s, &mut step, c, done);
    refused(
        &mut s,
        &mut step,
        c,
        Answer::Sampled {
            reply: None,
            error: None,
        },
    );
}

#[test]
fn a_pre_input_draft_that_delivers_nothing_is_written_and_the_item_stays_pending() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false);
    step.next_job(&mut s).unwrap();
    step.answer(&mut s, ok(), 0).unwrap();
    let Job::Hooks {
        point: Hook::PreInput,
        draft: Some(handed),
        ..
    } = step.next_job(&mut s).unwrap()
    else {
        panic!("pre_input with a draft");
    };
    let edited = Answer::Hooks {
        stop: None,
        draft: Some(Draft {
            body: Body::User("edited".into()),
            delivers: vec![],
            by: handed.by.clone(),
        }),
    };
    step.answer(&mut s, edited, 0).unwrap();
    assert_eq!(bodies(&s, c).last(), Some(&Body::User("edited".into())));
    assert_eq!(
        s.items(c).count(),
        1,
        "what the draft did not deliver stays"
    );
}

#[test]
fn a_pre_input_hook_that_leaves_no_draft_commits_nothing_and_the_input_stays_pending() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    s.auto.insert(c, false);
    let mut step = Step::wake(&s, c, true);
    let jobs = drive(&mut s, &mut step, |job, _| match job {
        Job::Hooks {
            point: Hook::PreInput,
            ..
        } => vec![ok()],
        Job::Sample => vec![Answer::Sampled {
            reply: Some(reply(vec![text("hello")], Stop::End)),
            error: None,
        }],
        _ => vec![echo(job)],
    });
    assert!(
        !bodies(&s, c).iter().any(|b| matches!(b, Body::User(_))),
        "no User node: the hook took the draft away"
    );
    assert_eq!(s.items(c).count(), 1, "the input is still pending");
    assert_eq!(
        jobs.iter()
            .filter(|j| matches!(
                j,
                Job::Hooks {
                    point: Hook::PreInput,
                    ..
                }
            ))
            .count(),
        1,
        "offered once"
    );
}

/// How many node and move records are queued: a refused write queues none.
fn writes(s: &Session) -> usize {
    s.outbox
        .iter()
        .filter(|r| !matches!(r, tau_core::tree::Record::Sync { .. }))
        .count()
}

#[test]
fn a_reply_naming_a_call_id_twice_fails_the_step_and_changes_nothing() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "go");
    let mut step = Step::wake(&s, c, false);
    let mut before = None;
    let jobs = drive(&mut s, &mut step, |job, s| match job {
        Job::Sample => {
            before = Some((bodies(s, c), writes(s)));
            vec![Answer::Sampled {
                reply: Some(reply(vec![call("a"), call("a")], Stop::Tools)),
                error: None,
            }]
        }
        _ => vec![echo(job)],
    });
    assert!(
        !jobs.iter().any(|j| matches!(j, Job::RunCalls { .. })),
        "no batch is handed out"
    );
    let failed = Error::DuplicateCall.to_string();
    assert!(jobs.iter().any(|j| matches!(
        j,
        Job::Hooks { point: Hook::PostStep, ending: Some(Ending::Failed(e)), .. } if *e == failed
    )));
    assert_eq!(Some((bodies(&s, c), writes(&s))), before);
    assert!(!s.auto[&c]);
}

/// Whether `jobs` hold a hook job at `point` with this `ending`, whatever its serial.
fn has_hook(jobs: &[Job], point: Hook, ending: Option<&Ending>) -> bool {
    jobs.iter().any(|j| {
        matches!(j, Job::Hooks { point: p, draft: None, ending: e, .. } if *p == point && e.as_ref() == ending)
    })
}

/// One turn that calls a tool, with `answer` answer at the first hook job at `point`
/// and every other job answered plainly. Returns the session, the cursor, and the
/// jobs seen.
fn turn_with_hook_answer(point: Hook, answer: Answer) -> (Session, u64, Vec<Job>) {
    let (mut s, c) = Session::create("{}", "sys", 0);
    send(&mut s, c, "hi");
    let mut step = Step::wake(&s, c, false);
    let mut script = scripted(vec![
        reply(vec![call("a")], Stop::Tools),
        reply(vec![text("done")], Stop::End),
    ]);
    let mut answer = Some(answer);
    let jobs = drive(&mut s, &mut step, |job, s| match job {
        Job::Hooks { point: p, .. } if *p == point && answer.is_some() => {
            vec![answer.take().unwrap()]
        }
        _ => script(job, s),
    });
    (s, c, jobs)
}

fn halting(reason: &str) -> Answer {
    Answer::Hooks {
        stop: Some(reason.into()),
        draft: None,
    }
}

fn raised(why: &str) -> Answer {
    Answer::HookFailed(why.into())
}

fn ran(jobs: &[Job], point: Hook) -> bool {
    jobs.iter()
        .any(|j| matches!(j, Job::Hooks { point: p, .. } if *p == point))
}

#[test]
fn a_pre_step_hook_string_halts_before_any_input() {
    let (s, c, jobs) = turn_with_hook_answer(Hook::PreStep, halting("not now"));
    assert_eq!(ending(&jobs), Ending::Halted("not now".into()));
    assert!(!ran(&jobs, Hook::PreInput));
    assert_eq!(s.items(c).count(), 1, "the input is still pending");
    assert!(!s.auto[&c]);
}

#[test]
fn a_pre_input_hook_that_raises_fails_the_step_and_leaves_the_input_pending() {
    let (s, c, jobs) = turn_with_hook_answer(Hook::PreInput, raised("boom"));
    assert_eq!(ending(&jobs), Ending::Failed("boom".into()));
    assert!(!jobs.contains(&Job::Sample));
    assert_eq!(s.items(c).count(), 1, "the input is still pending");
    assert_eq!(bodies(&s, c).len(), 2, "only the config and system nodes");
    assert!(!s.auto[&c]);
}

#[test]
fn a_pre_sample_hook_that_raises_fails_the_step_without_sampling() {
    let (s, c, jobs) = turn_with_hook_answer(Hook::PreSample, raised("boom"));
    assert_eq!(ending(&jobs), Ending::Failed("boom".into()));
    assert!(!jobs.contains(&Job::Sample));
    assert_eq!(bodies(&s, c).last().unwrap(), &Body::User("hi".into()));
    assert!(!s.auto[&c]);
}

#[test]
fn a_pre_reply_hook_that_raises_drops_the_reply_and_fails_the_step() {
    let (s, c, jobs) = turn_with_hook_answer(Hook::PreReply, raised("boom"));
    assert_eq!(ending(&jobs), Ending::Failed("boom".into()));
    assert!(!ran(&jobs, Hook::PostReply));
    assert_eq!(bodies(&s, c).last().unwrap(), &Body::User("hi".into()));
    assert!(!s.auto[&c]);
}

#[test]
fn a_pre_reply_hook_string_drops_the_reply_and_halts_the_step() {
    let (s, c, jobs) = turn_with_hook_answer(Hook::PreReply, halting("no reply"));
    assert_eq!(ending(&jobs), Ending::Halted("no reply".into()));
    assert!(!ran(&jobs, Hook::PostReply));
    assert_eq!(bodies(&s, c).last().unwrap(), &Body::User("hi".into()));
    assert!(!s.auto[&c]);
}

#[test]
fn a_post_reply_hook_that_raises_fails_the_step_before_any_tool_runs() {
    let (s, c, jobs) = turn_with_hook_answer(Hook::PostReply, raised("boom"));
    assert_eq!(ending(&jobs), Ending::Failed("boom".into()));
    assert!(!jobs.iter().any(|j| matches!(j, Job::RunCalls { .. })));
    assert!(matches!(
        bodies(&s, c).last().unwrap(),
        Body::Assistant { .. }
    ));
    assert!(!s.auto[&c]);
}

#[test]
fn a_post_reply_hook_string_halts_the_step_before_any_tool_runs() {
    let (s, c, jobs) = turn_with_hook_answer(Hook::PostReply, halting("enough"));
    assert_eq!(ending(&jobs), Ending::Halted("enough".into()));
    assert!(!jobs.iter().any(|j| matches!(j, Job::RunCalls { .. })));
    assert!(matches!(
        bodies(&s, c).last().unwrap(),
        Body::Assistant { .. }
    ));
    assert!(!s.auto[&c]);
}

#[test]
fn a_post_result_hook_that_raises_fails_the_step_with_the_results_kept() {
    let (s, c, jobs) = turn_with_hook_answer(Hook::PostResult, raised("boom"));
    assert_eq!(ending(&jobs), Ending::Failed("boom".into()));
    assert_eq!(jobs.iter().filter(|j| **j == Job::Sample).count(), 1);
    assert!(matches!(bodies(&s, c).last().unwrap(), Body::Results(_)));
    assert!(!s.auto[&c]);
}

#[test]
fn a_post_result_hook_string_halts_the_step_with_the_results_kept() {
    let (s, c, jobs) = turn_with_hook_answer(Hook::PostResult, halting("enough"));
    assert_eq!(ending(&jobs), Ending::Halted("enough".into()));
    assert_eq!(jobs.iter().filter(|j| **j == Job::Sample).count(), 1);
    assert!(matches!(bodies(&s, c).last().unwrap(), Body::Results(_)));
    assert!(!s.auto[&c]);
}
