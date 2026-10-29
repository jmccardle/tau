//! The session machine: frontend calls, wakes, locks, and `max_steps` across
//! cursors. Driven by a scripted driver, as in `step.rs`.

use tau_core::machine::Machine;
use tau_core::step::{Draft, Ending, Fed, Hook, Job};
use tau_core::tree::{Author, Body, Config, Error, ItemData, Part, Session, Stop};

fn ok() -> Fed {
    Fed::Hooks {
        stop: None,
        draft: None,
    }
}

fn said(t: &str) -> Fed {
    Fed::Sampled {
        reply: Some(Draft {
            body: Body::Assistant {
                parts: vec![Part::Text(t.into())],
                stop: Stop::End,
                finish_reason: None,
            },
            delivers: vec![],
            by: Author::Provider,
        }),
        error: None,
    }
}

fn term() -> String {
    "term".into()
}

/// Runs `cursor` until it rests, answering each job with `answer`.
fn run(
    m: &mut Machine,
    cursor: u64,
    mut answer: impl FnMut(&Job, &mut Machine) -> Vec<Fed>,
) -> Vec<Job> {
    let mut seen = vec![];
    for _ in 0..200 {
        let job = m.next_job(cursor).unwrap();
        seen.push(job.clone());
        if job == Job::Rest {
            return seen;
        }
        for fed in answer(&job, m) {
            m.feed(cursor, fed, 0).unwrap();
        }
    }
    panic!("no rest: {seen:?}");
}

fn plain(job: &Job, _: &mut Machine) -> Vec<Fed> {
    match job {
        Job::Sample => vec![said("ok")],
        Job::Wait { .. } => vec![Fed::Waited],
        _ => vec![ok()],
    }
}

fn points(jobs: &[Job]) -> Vec<Hook> {
    jobs.iter()
        .filter_map(|j| match j {
            Job::Hooks { point, .. } => Some(*point),
            _ => None,
        })
        .collect()
}

fn ending(jobs: &[Job]) -> Ending {
    jobs.iter()
        .find_map(|j| match j {
            Job::Hooks {
                ending: Some(e), ..
            } => Some(e.clone()),
            _ => None,
        })
        .unwrap()
}

fn machine() -> (Machine, u64) {
    let (s, c) = Session::create("{}", "sys", 0);
    (Machine::new(s), c)
}

#[test]
fn a_send_wakes_the_cursor_and_runs_a_turn() {
    let (mut m, c) = machine();
    m.send(c, term(), "hi", 0).unwrap();
    assert!(m.busy(c));
    run(&mut m, c, plain);
    assert!(!m.busy(c));
    let s = &m.session;
    assert!(matches!(
        s.node(s.at(c).unwrap()).unwrap().body,
        Body::Assistant { .. }
    ));
}

#[test]
fn a_go_to_at_rest_moves_turns_auto_off_and_wakes_with_no_phase() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    m.send(c, term(), "hi", 0).unwrap();
    run(&mut m, c, plain);
    let user = m
        .session
        .node(m.session.at(c).unwrap())
        .unwrap()
        .parent
        .unwrap();
    m.session.auto.insert(c, true);
    m.go_to(c, user, term(), 0).unwrap();
    assert_eq!(m.session.at(c), Ok(user));
    assert!(!m.session.auto[&c]);
    let jobs = run(&mut m, c, plain);
    assert_eq!(points(&jobs), [Hook::PreStep, Hook::PostStep]);
    assert_eq!(m.go_to(c, 99, term(), 0), Err(Error::NoSuchNode(99)));
    assert_ne!(sys, user);
}

#[test]
fn a_go_to_is_refused_while_a_hook_runs() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    m.send(c, term(), "hi", 0).unwrap();
    let job = m.next_job(c).unwrap();
    assert!(matches!(
        job,
        Job::Hooks {
            point: Hook::PreStep,
            ..
        }
    ));
    assert_eq!(m.go_to(c, sys, term(), 0), Err(Error::HookRunning(c)));
    m.feed(c, ok(), 0).unwrap();
    run(&mut m, c, plain);
}

#[test]
fn a_go_to_on_a_busy_cursor_aborts_runs_post_hooks_then_moves() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    m.send(c, term(), "hi", 0).unwrap();
    let mut cancelled = None;
    let jobs = run(&mut m, c, |job, m| match job {
        Job::Sample if cancelled.is_none() => {
            cancelled = Some(m.go_to(c, sys, term(), 0).unwrap());
            vec![Fed::Sampled {
                reply: None,
                error: None,
            }]
        }
        _ => plain(job, m),
    });
    assert_eq!(cancelled, Some(true), "the sample in flight is cancelled");
    assert_eq!(ending(&jobs), Ending::Aborted);
    assert_eq!(m.session.at(c), Ok(sys), "moved after post_step");
    assert_eq!(
        points(&jobs),
        [
            Hook::PreStep,
            Hook::PreInput,
            Hook::PreSample,
            Hook::PostStep, // the aborted step
            Hook::PreStep,  // the go-to's wake, auto-step off
            Hook::PostStep
        ]
    );
}

#[test]
fn a_frontend_step_is_refused_on_a_busy_cursor_and_runs_once_at_rest() {
    let (mut m, c) = machine();
    m.input(c, term(), "hi", 0).unwrap(); // input, auto-step off
    assert_eq!(m.step(c, term(), 0), Err(Error::Busy(c)));
    let jobs = run(&mut m, c, plain);
    assert_eq!(points(&jobs), [Hook::PreStep, Hook::PostStep]);
    m.step(c, term(), 0).unwrap();
    let jobs = run(&mut m, c, plain);
    assert!(jobs.contains(&Job::Sample), "asked: the phases ran");
    assert_eq!(
        jobs.iter().filter(|j| **j == Job::Sample).count(),
        1,
        "one step only"
    );
}

#[test]
fn a_question_locks_the_cursor_and_abort_leaves_it_open() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    m.ask(c).unwrap();
    assert_eq!(m.input(c, term(), "hi", 0), Err(Error::Locked(c)));
    assert_eq!(m.send(c, term(), "hi", 0), Err(Error::Locked(c)));
    assert_eq!(m.go_to(c, sys, term(), 0), Err(Error::Locked(c)));
    assert_eq!(m.step(c, term(), 0), Err(Error::Locked(c)));
    assert!(!m.abort(c).unwrap());
    assert!(m.locked(c), "abort does not close it");
    m.answered(c).unwrap();
    assert!(!m.locked(c));
    m.send(c, term(), "hi", 0).unwrap();
    run(&mut m, c, plain);
}

#[test]
fn a_wake_during_the_throttle_joins_the_scheduled_step() {
    let (mut m, first_cursor) = machine();
    let sys = m.session.at(first_cursor).unwrap();
    let c = m.open(sys, Author::Core, 0).unwrap(); // opening wakes
    let mut first = true;
    let jobs = run(&mut m, c, |job, m| {
        if matches!(
            job,
            Job::Hooks {
                point: Hook::PreStep,
                ..
            }
        ) && first
        {
            first = false;
            let data = ItemData::Extension {
                name: "timer".into(),
                data: "tick".into(),
            };
            m.add_item(c, data, 0).unwrap();
        }
        if let Job::Wait { .. } = job {
            let data = ItemData::Extension {
                name: "timer".into(),
                data: "tock".into(),
            };
            m.add_item(c, data, 0).unwrap(); // wakes c: already scheduled
        }
        plain(job, m)
    });
    let pre_steps = points(&jobs)
        .iter()
        .filter(|p| **p == Hook::PreStep)
        .count();
    assert_eq!(pre_steps, 2, "the throttled restart only");
}

#[test]
fn abort_during_the_throttle_cancels_the_restart() {
    let (mut m, first_cursor) = machine();
    let sys = m.session.at(first_cursor).unwrap();
    let c = m.open(sys, Author::Core, 0).unwrap(); // opening wakes
    let mut first = true;
    let jobs = run(&mut m, c, |job, m| {
        if matches!(
            job,
            Job::Hooks {
                point: Hook::PreStep,
                ..
            }
        ) && first
        {
            first = false;
            let data = ItemData::Extension {
                name: "timer".into(),
                data: "tick".into(),
            };
            m.add_item(c, data, 0).unwrap();
        }
        if let Job::Wait { .. } = job {
            assert!(m.abort(c).unwrap(), "the driver cancels its sleeper");
            return vec![];
        }
        plain(job, m)
    });
    assert_eq!(jobs.last(), Some(&Job::Rest));
    let pre_steps = points(&jobs)
        .iter()
        .filter(|p| **p == Hook::PreStep)
        .count();
    assert_eq!(pre_steps, 1);
}

#[test]
fn max_steps_stops_a_continue_loop_and_a_frontend_action_resets_it() {
    let config = Config {
        max_steps: Some(2),
        data: "{}".into(),
    };
    let (s, c) = Session::create(config, "sys", 0);
    let mut m = Machine::new(s);
    // A "keep going" extension: post_reply writes a User node.
    let keep_going = |job: &Job, m: &mut Machine| {
        if matches!(
            job,
            Job::Hooks {
                point: Hook::PostReply,
                ..
            }
        ) {
            let by = Author::Extension("continue".into());
            m.session
                .write(c, Body::User("continue".into()), vec![], by, 0)
                .unwrap();
        }
        plain(job, m)
    };
    m.send(c, term(), "go", 0).unwrap();
    let jobs = run(&mut m, c, keep_going);
    assert_eq!(jobs.iter().filter(|j| **j == Job::Sample).count(), 2);
    assert!(!m.session.auto[&c]);
    m.send(c, term(), "again", 0).unwrap();
    let jobs = run(&mut m, c, keep_going);
    assert_eq!(
        jobs.iter().filter(|j| **j == Job::Sample).count(),
        2,
        "the send reset the count"
    );
}

#[test]
fn opening_a_cursor_wakes_it_and_an_item_for_it_wakes_it_again() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    let d = m.open(sys, Author::Extension("agents".into()), 0).unwrap();
    assert!(m.busy(d), "opening wakes (Q32)");
    run(&mut m, d, plain);
    let task = ItemData::Extension {
        name: "agents".into(),
        data: "look around".into(),
    };
    m.add_item(d, task, 0).unwrap();
    assert!(m.busy(d), "an item wakes its cursor");
    assert!(!m.busy(c));
}

#[test]
fn frontend_calls_sync_on_return() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    m.input(c, term(), "hi", 0).unwrap();
    assert!(m.session.synced());
    run(&mut m, c, plain);
    m.session.set_label("main", c).unwrap();
    m.go_to(c, sys, term(), 0).unwrap();
    assert!(m.session.synced());
}

#[test]
fn moves_since_step_are_the_running_steps() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    m.send(c, term(), "hi", 0).unwrap();
    let job = m.next_job(c).unwrap();
    assert!(matches!(
        job,
        Job::Hooks {
            point: Hook::PreStep,
            ..
        }
    ));
    let by = Author::Extension("nav".into());
    let note = m
        .session
        .append(Some(sys), by.clone(), Body::User("n".into()), vec![], 0)
        .unwrap();
    m.session.move_to(c, note, by, 0).unwrap();
    assert_eq!(m.moves_since_step(c).count(), 1);
    m.feed(c, ok(), 0).unwrap();
    run(&mut m, c, plain);
}

#[test]
fn a_feed_that_answers_nothing_is_refused_and_changes_nothing() {
    let (mut m, c) = machine();
    // No step yet.
    assert_eq!(m.feed(c, ok(), 0), Err(Error::WrongFeed(c)));
    m.send(c, term(), "hi", 0).unwrap();
    let job = m.next_job(c).unwrap();
    assert!(matches!(
        job,
        Job::Hooks {
            point: Hook::PreStep,
            ..
        }
    ));
    // A pre_step hook job is out: a sample result does not answer it.
    assert_eq!(m.feed(c, said("x"), 0), Err(Error::WrongFeed(c)));
    assert_eq!(
        m.session.walk(m.session.at(c).unwrap()).len(),
        2,
        "nothing committed"
    );
    // The hook job is still out, so a go-to is still refused.
    assert_eq!(m.go_to(c, 0, term(), 0), Err(Error::HookRunning(c)));
    m.feed(c, ok(), 0).unwrap();
    run(&mut m, c, plain);
}

#[test]
fn a_stray_waited_after_an_abort_in_the_throttle_is_refused() {
    let (mut m, first_cursor) = machine();
    let sys = m.session.at(first_cursor).unwrap();
    let c = m.open(sys, Author::Core, 0).unwrap();
    let mut first = true;
    let mut refused = false;
    run(&mut m, c, |job, m| {
        if matches!(
            job,
            Job::Hooks {
                point: Hook::PreStep,
                ..
            }
        ) && first
        {
            first = false;
            let data = ItemData::Extension {
                name: "t".into(),
                data: "tick".into(),
            };
            m.add_item(c, data, 0).unwrap();
        }
        if let Job::Wait { .. } = job {
            assert!(m.abort(c).unwrap());
            refused = m.feed(c, Fed::Waited, 0) == Err(Error::WrongFeed(c));
            return vec![];
        }
        plain(job, m)
    });
    assert!(refused);
}

#[test]
fn loading_wakes_every_cursor_for_pre_step_and_post_step_only() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let sys = s.at(c).unwrap();
    let d = s.open(sys, Author::Core, 0).unwrap();
    s.auto.insert(c, false);
    let mut m = Machine::loaded(s, 0);
    for cursor in [c, d] {
        assert!(m.busy(cursor));
        let jobs = run(&mut m, cursor, plain);
        assert_eq!(points(&jobs), [Hook::PreStep, Hook::PostStep]);
        assert!(!m.busy(cursor));
    }
}

#[test]
fn notices_are_drained_once() {
    let (mut s, _) = Session::create("{}", "sys", 0);
    s.notices.push(("term".into(), "no".into()));
    assert_eq!(s.take_notices(), [("term".to_string(), "no".to_string())]);
    assert_eq!(s.take_notices().len(), 0);
}

/// A `pre_input` hook that rewrites the input and delivers nothing.
fn undelivering(job: &Job, m: &mut Machine) -> Vec<Fed> {
    match job {
        Job::Hooks {
            point: Hook::PreInput,
            draft: Some(d),
            ..
        } => vec![Fed::Hooks {
            stop: None,
            draft: Some(Draft {
                body: Body::User("edited".into()),
                delivers: vec![],
                by: d.by.clone(),
            }),
        }],
        _ => plain(job, m),
    }
}

fn users(m: &Machine, c: u64) -> usize {
    let s = &m.session;
    s.walk(s.at(c).unwrap())
        .iter()
        .filter(|n| matches!(n.body, Body::User(_)))
        .count()
}

/// Jobs from now through the end of the current step's `post_step`.
fn one_step(m: &mut Machine, c: u64) -> Vec<Job> {
    let mut seen = vec![];
    for _ in 0..50 {
        let job = m.next_job(c).unwrap();
        seen.push(job.clone());
        let last = matches!(
            job,
            Job::Hooks {
                point: Hook::PostStep,
                ..
            } | Job::Rest
        );
        for fed in undelivering(&job, m) {
            m.feed(c, fed, 0).unwrap();
        }
        if last {
            return seen;
        }
    }
    panic!("a step that never ends: {seen:?}");
}

#[test]
fn a_hook_that_delivers_nothing_gets_each_pending_input_once_per_step() {
    let (mut m, c) = machine();
    m.send(c, term(), "one", 0).unwrap();
    m.send(c, term(), "two", 0).unwrap();
    let jobs = one_step(&mut m, c);
    let p = points(&jobs);
    assert_eq!(p.iter().filter(|h| **h == Hook::PreInput).count(), 2);
    assert!(jobs.contains(&Job::Sample), "the step went on to sample");
    assert_eq!(users(&m, c), 2);
    assert_eq!(m.session.items(c).count(), 2, "both are still pending");
}

#[test]
fn an_input_a_hook_leaves_pending_keeps_stepping_until_max_steps() {
    let config = Config {
        max_steps: Some(3),
        data: "{}".into(),
    };
    let (s, c) = Session::create(config, "sys", 0);
    let mut m = Machine::new(s);
    m.send(c, term(), "hi", 0).unwrap();
    let jobs = run(&mut m, c, undelivering);
    assert_eq!(jobs.iter().filter(|j| **j == Job::Sample).count(), 3);
    assert_eq!(users(&m, c), 3, "one User per committing step");
    assert!(!m.session.auto[&c]);
    assert_eq!(m.session.items(c).count(), 1, "the input is still pending");
}

#[test]
fn without_max_steps_a_left_input_steps_and_samples_each_time_without_spinning() {
    // The hook's choice, predictable: every step makes progress (one User,
    // one sample); nothing but the hook ends it.
    let (mut m, c) = machine();
    m.send(c, term(), "hi", 0).unwrap();
    for n in 1..=5 {
        let jobs = one_step(&mut m, c);
        let p = points(&jobs);
        assert_eq!(p.iter().filter(|h| **h == Hook::PreInput).count(), 1);
        assert_eq!(jobs.iter().filter(|j| **j == Job::Sample).count(), 1);
        assert_eq!(users(&m, c), n);
        assert!(m.busy(c), "the pending input wakes another step");
    }
}

#[test]
fn a_cursor_that_is_not_open_is_refused_by_every_call_and_nothing_is_recorded() {
    let (mut m, c) = machine();
    let before = m.session.clone();
    assert_eq!(m.abort(999), Err(Error::NoSuchCursor(999)));
    assert_eq!(m.ask(999), Err(Error::NoSuchCursor(999)));
    assert_eq!(m.answered(999), Err(Error::NoSuchCursor(999)));
    assert_eq!(m.next_job(999), Err(Error::NoSuchCursor(999)));
    assert_eq!(m.feed(999, ok(), 0), Err(Error::NoSuchCursor(999)));
    assert_eq!(
        m.session, before,
        "no record for a cursor that is not there"
    );
    assert!(!m.locked(999));
    assert_eq!(m.session.auto.keys().copied().collect::<Vec<_>>(), [c]);
}

#[test]
fn add_item_and_open_inside_a_pre_hook_ride_the_phases_sync() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    m.input(c, term(), "hi", 0).unwrap();
    assert!(m.session.synced());
    let job = m.next_job(c).unwrap();
    assert!(matches!(
        job,
        Job::Hooks {
            point: Hook::PreStep,
            ..
        }
    ));
    let tick = ItemData::Extension {
        name: "timer".into(),
        data: "tick".into(),
    };
    m.add_item(c, tick.clone(), 0).unwrap();
    assert!(!m.session.synced(), "no early sync inside a hook");
    m.open(sys, Author::Extension("ext".into()), 0).unwrap();
    assert!(!m.session.synced());
    m.feed(c, ok(), 0).unwrap();
    run(&mut m, c, plain);
    assert!(m.session.synced(), "the phase's sync covered them");
}

#[test]
fn add_item_and_open_outside_a_step_sync_on_return() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    let tick = ItemData::Extension {
        name: "timer".into(),
        data: "tick".into(),
    };
    m.add_item(c, tick, 0).unwrap();
    assert!(m.session.synced());
    m.open(sys, Author::Extension("ext".into()), 0).unwrap();
    assert!(m.session.synced());
}
