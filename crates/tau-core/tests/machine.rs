//! The session machine: frontend calls and wakes across
//! cursors. Driven by a scripted driver, as in `step.rs`.

use tau_core::machine::Machine;
use tau_core::step::{Answer, Draft, Ending, Hook, Job, SampleEnd};
use tau_core::tree::{Author, Body, Config, Error, ItemData, Part, Session, Stop};

/// Input, then auto-step on: what a frontend does to send.
fn send(m: &mut Machine, cursor: u64, client: String, text: &str) -> u64 {
    let item = m.input(cursor, client, text).unwrap();
    m.set_auto(cursor, true).unwrap();
    item
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

fn said(t: &str) -> Answer {
    sampled(Draft {
        body: Body::Assistant {
            parts: vec![Part::text(t)],
            stop: Stop::End,
            finish_reason: None,
            meta: None,
        },
        delivers: vec![],
        by: Author::Provider,
    })
}

fn term() -> String {
    "term".into()
}

/// Runs `cursor` until it rests, answering each job with `script`.
fn run(
    m: &mut Machine,
    cursor: u64,
    mut script: impl FnMut(&Job, &mut Machine) -> Vec<Answer>,
) -> Vec<Job> {
    let mut seen = vec![];
    for _ in 0..200 {
        let job = m.next_job(cursor).unwrap();
        seen.push(job.clone());
        if job == Job::Rest {
            return seen;
        }
        for answer in script(&job, m) {
            m.answer(cursor, answer, 0).unwrap();
        }
    }
    panic!("no rest: {seen:?}");
}

fn plain(job: &Job, _: &mut Machine) -> Vec<Answer> {
    match job {
        Job::Sample => vec![said("ok")],
        _ => vec![echo(job)],
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
    send(&mut m, c, term(), "hi");
    assert!(m.busy(c));
    run(&mut m, c, plain);
    assert!(!m.busy(c));
    let s = &m.session;
    assert!(matches!(
        s.tree.node(s.at(c).unwrap()).unwrap().body,
        Body::Assistant { .. }
    ));
}

#[test]
fn a_go_to_at_rest_moves_turns_auto_off_and_wakes_with_no_phase() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    send(&mut m, c, term(), "hi");
    run(&mut m, c, plain);
    let user = m
        .session
        .tree
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
fn a_go_to_while_a_hook_runs_aborts_lets_the_hook_finish_then_moves() {
    let (mut m, c) = machine();
    send(&mut m, c, term(), "hi");
    let job = m.next_job(c).unwrap();
    assert!(matches!(
        job,
        Job::Hooks {
            point: Hook::PreStep,
            ..
        }
    ));
    assert_eq!(
        m.go_to(c, 0, term(), 0),
        Ok(false),
        "nothing to cancel: the hook finishes"
    );
    m.answer(c, ok(), 0).unwrap();
    let jobs = run(&mut m, c, plain);
    assert_eq!(ending(&jobs), Ending::Aborted);
    assert!(!jobs.contains(&Job::Sample), "aborted: no phase ran");
    assert_eq!(m.session.at(c), Ok(0), "moved after post_step");
    assert_eq!(m.session.items(c).count(), 1, "the input stays pending");
}

#[test]
fn a_go_to_on_a_busy_cursor_aborts_runs_post_hooks_then_moves() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    send(&mut m, c, term(), "hi");
    let mut cancelled = None;
    let jobs = run(&mut m, c, |job, m| match job {
        Job::Sample if cancelled.is_none() => {
            cancelled = Some(m.go_to(c, sys, term(), 0).unwrap());
            vec![Answer::Sampled {
                parts: vec![],
                end: SampleEnd::Cancelled,
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
    m.input(c, term(), "hi").unwrap(); // input, auto-step off
    assert_eq!(m.step(c), Err(Error::Busy(c)));
    let jobs = run(&mut m, c, plain);
    assert_eq!(points(&jobs), [Hook::PreStep, Hook::PostStep]);
    m.step(c).unwrap();
    let jobs = run(&mut m, c, plain);
    assert!(jobs.contains(&Job::Sample), "asked: the phases ran");
    assert_eq!(
        jobs.iter().filter(|j| **j == Job::Sample).count(),
        1,
        "one step only"
    );
}

#[test]
fn an_item_added_while_busy_causes_one_more_step() {
    let (mut m, first_cursor) = machine();
    let sys = m.session.at(first_cursor).unwrap();
    let c = m.open(sys, Author::Core, 0, None).unwrap(); // opening wakes
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
            m.add_item(c, data, None).unwrap(); // joins the running step
        }
        plain(job, m)
    });
    let pre_steps = points(&jobs)
        .iter()
        .filter(|p| **p == Hook::PreStep)
        .count();
    assert_eq!(pre_steps, 2, "the item woke exactly one more step");
}

#[test]
fn opening_a_cursor_wakes_it_and_an_item_for_it_wakes_it_again() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    let d = m
        .open(sys, Author::Extension("agents".into()), 0, None)
        .unwrap();
    assert!(m.busy(d), "opening wakes (Q32)");
    run(&mut m, d, plain);
    let task = ItemData::Extension {
        name: "agents".into(),
        data: "look around".into(),
    };
    m.add_item(d, task, None).unwrap();
    assert!(m.busy(d), "an item wakes its cursor");
    assert!(!m.busy(c));
}

#[test]
fn frontend_calls_sync_on_return() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    m.input(c, term(), "hi").unwrap();
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
    send(&mut m, c, term(), "hi");
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
        .append(sys, by.clone(), Body::User("n".into()), vec![], 0)
        .unwrap();
    m.session.move_to(c, note, by, 0).unwrap();
    assert_eq!(m.moves_since_step(c).count(), 1);
    m.answer(c, ok(), 0).unwrap();
    run(&mut m, c, plain);
}

#[test]
fn a_feed_that_answers_nothing_is_refused_and_changes_nothing() {
    let (mut m, c) = machine();
    // No step yet.
    assert_eq!(m.answer(c, ok(), 0), Err(Error::WrongAnswer(c)));
    send(&mut m, c, term(), "hi");
    let job = m.next_job(c).unwrap();
    assert!(matches!(
        job,
        Job::Hooks {
            point: Hook::PreStep,
            ..
        }
    ));
    // A pre_step hook job is out: a sample result does not answer it.
    assert_eq!(m.answer(c, said("x"), 0), Err(Error::WrongAnswer(c)));
    assert_eq!(
        m.session.tree.walk(m.session.at(c).unwrap()).len(),
        2,
        "nothing committed"
    );
    // The hook job is still out: its own answer is still accepted.
    m.answer(c, ok(), 0).unwrap();
    run(&mut m, c, plain);
}

#[test]
fn loading_wakes_every_cursor_for_pre_step_and_post_step_only() {
    let (mut s, c) = Session::create("{}", "sys", 0);
    let sys = s.at(c).unwrap();
    let d = s.open(sys, Author::Core, 0).unwrap();
    s.auto.insert(c, false);
    let mut m = Machine::loaded(s);
    for cursor in [c, d] {
        assert!(m.busy(cursor));
        let jobs = run(&mut m, cursor, plain);
        assert_eq!(points(&jobs), [Hook::PreStep, Hook::PostStep]);
        assert!(!m.busy(cursor));
    }
}

/// A `pre_input` hook that rewrites the input and delivers nothing.
fn undelivering(job: &Job, m: &mut Machine) -> Vec<Answer> {
    match job {
        Job::Hooks {
            point: Hook::PreInput,
            draft: Some(d),
            ..
        } => vec![Answer::Hooks {
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
    s.tree
        .walk(s.at(c).unwrap())
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
        for answer in undelivering(&job, m) {
            m.answer(c, answer, 0).unwrap();
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
    send(&mut m, c, term(), "one");
    send(&mut m, c, term(), "two");
    let jobs = one_step(&mut m, c);
    let p = points(&jobs);
    assert_eq!(p.iter().filter(|h| **h == Hook::PreInput).count(), 2);
    assert!(jobs.contains(&Job::Sample), "the step went on to sample");
    assert_eq!(users(&m, c), 2);
    assert_eq!(m.session.items(c).count(), 2, "both are still pending");
}

#[test]
fn a_left_input_steps_and_samples_each_time_without_spinning() {
    // The hook's choice, predictable: every step makes progress (one User,
    // one sample); nothing but the hook ends it.
    let (mut m, c) = machine();
    send(&mut m, c, term(), "hi");
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
    assert_eq!(m.next_job(999), Err(Error::NoSuchCursor(999)));
    assert_eq!(m.answer(999, ok(), 0), Err(Error::NoSuchCursor(999)));
    assert_eq!(
        m.session, before,
        "no record for a cursor that is not there"
    );
    assert_eq!(m.session.auto.keys().copied().collect::<Vec<_>>(), [c]);
}

#[test]
fn add_item_and_open_inside_a_pre_hook_ride_the_phases_sync() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    m.input(c, term(), "hi").unwrap();
    assert!(m.session.synced());
    let Job::Hooks {
        point: Hook::PreStep,
        ..
    } = m.next_job(c).unwrap()
    else {
        panic!("a pre_step job");
    };
    let tick = ItemData::Extension {
        name: "timer".into(),
        data: "tick".into(),
    };
    m.add_item(c, tick.clone(), Some(c)).unwrap();
    assert!(!m.session.synced(), "no early sync inside a hook");
    m.open(sys, Author::Extension("ext".into()), 0, Some(c))
        .unwrap();
    assert!(!m.session.synced());
    m.answer(c, ok(), 0).unwrap();
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
    m.add_item(c, tick, None).unwrap();
    assert!(m.session.synced());
    m.open(sys, Author::Extension("ext".into()), 0, None)
        .unwrap();
    assert!(m.session.synced());
}

#[test]
fn a_hook_acts_on_its_cursor_only_while_a_hook_job_is_out() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    let by = Author::Extension("x".into());
    // Resting: no hook job is out.
    assert_eq!(m.hook_move(c, sys, by.clone(), 0), Err(Error::NotInHook(c)));
    assert_eq!(
        m.hook_write(c, note(), vec![], by.clone(), 0),
        Err(Error::NotInHook(c))
    );
    assert_eq!(m.hook_set_auto(c, true), Err(Error::NotInHook(c)));
    m.step(c).unwrap();
    let Job::Hooks { .. } = m.next_job(c).unwrap() else {
        panic!("a hook job");
    };
    // The job is out: all three apply at once, with no sync of their own.
    let written = m.hook_write(c, note(), vec![], by.clone(), 0).unwrap();
    assert_eq!(m.session.at(c), Ok(written));
    m.hook_move(c, sys, by.clone(), 0).unwrap();
    assert_eq!(m.session.at(c), Ok(sys));
    assert_eq!(
        m.hook_move(c, 99, by.clone(), 0),
        Err(Error::NoSuchNode(99))
    );
    m.hook_set_auto(c, true).unwrap();
    assert!(m.session.auto[&c]);
    assert!(
        !matches!(
            m.session.outbox.last(),
            Some(tau_core::tree::Record::Sync { .. })
        ),
        "the phase's own sync covers a hook's writes"
    );
    m.answer(c, ok(), 0).unwrap();
    // Answer, the job is no longer out; the step has handed out no other yet.
    assert_eq!(m.hook_move(c, sys, by, 0), Err(Error::NotInHook(c)));
    run(&mut m, c, plain);
}

#[test]
fn only_the_hooks_own_calls_skip_the_sync() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    let other = m.open(sys, Author::Core, 0, None).unwrap();
    run(&mut m, other, plain);
    m.step(c).unwrap();
    let Job::Hooks { .. } = m.next_job(c).unwrap() else {
        panic!("a hook job");
    };
    // Another caller's label rides no hook: it syncs now.
    m.set_label("main", other, None).unwrap();
    assert!(
        m.session.synced(),
        "a frontend call syncs while a hook is out"
    );
    // The hook's own call does not.
    m.set_label("mine", c, Some(c)).unwrap();
    assert!(!m.session.synced());
    // A cursor with no hook job out is no hook: that call syncs.
    m.set_label("other", other, Some(other)).unwrap();
    assert!(m.session.synced());
    // Another cursor's hook does not cover this one's call.
    assert!(matches!(m.next_job(other).unwrap(), Job::Rest));
    m.answer(c, ok(), 0).unwrap();
    run(&mut m, c, plain);
}

#[test]
fn a_write_delivers_only_what_is_pending_for_its_own_cursor() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    let other = m.open(sys, Author::Core, 0, None).unwrap();
    let theirs = m
        .add_item(
            other,
            ItemData::Extension {
                name: "x".into(),
                data: "d".into(),
            },
            None,
        )
        .unwrap();
    let by = Author::Extension("x".into());
    assert_eq!(
        m.session.write(c, note(), vec![theirs], by.clone(), 0),
        Err(Error::NotPending(theirs))
    );
    assert_eq!(
        m.session.write(c, note(), vec![99], by, 0),
        Err(Error::NotPending(99))
    );
    assert_eq!(m.session.items(other).count(), 1, "nothing was delivered");
    assert_eq!(m.session.at(c), Ok(sys));
}

#[test]
fn a_hook_that_delivered_the_input_itself_leaves_no_draft_to_commit() {
    let (mut m, c) = machine();
    let item = send(&mut m, c, term(), "hi");
    let by = Author::Extension("x".into());
    let mut writes = 0;
    run(&mut m, c, |job, m| match job {
        Job::Hooks {
            point: Hook::PreInput,
            ..
        } => {
            m.hook_write(c, note(), vec![item], by.clone(), 0).unwrap();
            writes += 1;
            vec![ok()] // the draft is cleared too: nothing to commit
        }
        _ => plain(job, m),
    });
    assert_eq!(writes, 1);
    assert_eq!(users(&m, c), 0, "the input was delivered once, by the hook");
    assert_eq!(m.session.items(c).count(), 0);
}

#[test]
fn a_hook_that_delivered_the_input_but_kept_the_draft_fails_the_step() {
    let (mut m, c) = machine();
    let item = send(&mut m, c, term(), "hi");
    let by = Author::Extension("x".into());
    let jobs = run(&mut m, c, |job, m| match job {
        Job::Hooks {
            point: Hook::PreInput,
            draft,
            ..
        } => {
            m.hook_write(c, note(), vec![item], by.clone(), 0).unwrap();
            vec![Answer::Hooks {
                stop: None,
                draft: draft.clone(),
            }]
        }
        _ => plain(job, m),
    });
    assert_eq!(
        ending(&jobs),
        Ending::Failed(Error::NotPending(item).to_string()),
        "the commit delivers an item that is gone"
    );
    assert_eq!(users(&m, c), 0);
    assert!(!m.session.auto[&c]);
}

#[test]
fn removing_an_item_is_no_change_and_adding_one_is() {
    let d = || ItemData::Extension {
        name: "x".into(),
        data: "d".into(),
    };
    let pre_steps = |jobs: &[Job]| points(jobs).iter().filter(|p| **p == Hook::PreStep).count();
    // Remove an item while the step it woke is running: no second step.
    let (mut m, c) = machine();
    let item = m.add_item(c, d(), None).unwrap();
    let first = m.next_job(c).unwrap();
    m.remove_item(item, None).unwrap();
    m.answer(c, ok(), 0).unwrap();
    let mut jobs = vec![first];
    jobs.extend(run(&mut m, c, plain));
    assert_eq!(pre_steps(&jobs), 1);
    // Add one while it runs: the scratchpad changed, so another step follows.
    let (mut m, c) = machine();
    m.add_item(c, d(), None).unwrap();
    let first = m.next_job(c).unwrap();
    m.add_item(c, d(), None).unwrap();
    m.answer(c, ok(), 0).unwrap();
    let mut jobs = vec![first];
    jobs.extend(run(&mut m, c, plain));
    assert_eq!(pre_steps(&jobs), 2);
}

#[test]
fn a_hook_reads_children_labels_and_the_node_count() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    let a = m.append(sys, note(), Author::Core, 0, None).unwrap();
    let b = m.append(sys, note(), Author::Core, 0, None).unwrap();
    assert_eq!(m.session.tree.children(sys).unwrap(), &[a, b]);
    assert_eq!(m.session.tree.children(0).unwrap(), &[sys]);
    assert_eq!(m.session.tree.children(99), Err(Error::NoSuchNode(99)));
    assert_eq!(m.session.tree.node_count(), 4);
    m.set_label("main", c, None).unwrap();
    assert_eq!(m.session.labels().collect::<Vec<_>>(), vec![("main", c)]);
}

#[test]
fn an_extension_appends_a_note_and_removes_items() {
    let (mut m, c) = machine();
    let sys = m.session.at(c).unwrap();
    let by = Author::Extension("x".into());
    let body = Body::Custom {
        kind: "note".into(),
        data: "n".into(),
    };
    let note = m.append(sys, body.clone(), by.clone(), 0, None).unwrap();
    assert_eq!(m.session.at(c), Ok(sys), "no cursor moved");
    assert_eq!(m.session.tree.node(note).unwrap().parent, Some(sys));
    let root = m.append_root(Config::from("{}"), by, 0, None);
    assert_eq!(m.session.tree.node(root).unwrap().parent, None);
    assert_eq!(m.session.at(c), Ok(sys), "no cursor moved");
    let item = m
        .add_item(
            c,
            ItemData::Extension {
                name: "x".into(),
                data: "d".into(),
            },
            None,
        )
        .unwrap();
    m.remove_item(item, None).unwrap();
    assert_eq!(m.session.items(c).count(), 0);
    assert_eq!(m.remove_item(item, None), Err(Error::NoSuchItem(item)));
}

fn note() -> Body {
    Body::Custom {
        kind: "note".into(),
        data: "n".into(),
    }
}

/// A sample that finished with `d`'s body.
fn sampled(d: Draft) -> Answer {
    let Body::Assistant {
        parts,
        stop,
        finish_reason,
        meta,
    } = d.body
    else {
        panic!("a reply is an Assistant body")
    };
    Answer::Sampled {
        parts,
        end: SampleEnd::Finished {
            stop,
            reason: finish_reason,
            meta,
        },
    }
}

#[test]
fn a_cursor_is_busy_while_a_go_to_waits_on_its_aborted_step() {
    let (mut m, c) = machine();
    send(&mut m, c, term(), "hi");
    let _ = m.next_job(c).unwrap(); // pre_step is out
    assert_eq!(m.go_to(c, 0, term(), 0), Ok(false));
    m.answer(c, ok(), 0).unwrap();
    // Answer the aborted step's post_step: it rests, but the go-to has not applied.
    let post_step = m.next_job(c).unwrap();
    assert!(matches!(
        post_step,
        Job::Hooks {
            point: Hook::PostStep,
            ..
        }
    ));
    m.answer(c, ok(), 0).unwrap();
    assert!(m.busy(c), "the go-to still waits for next_job");
    assert_ne!(m.session.at(c), Ok(0));
    assert_eq!(m.step(c), Err(Error::Busy(c)));
    run(&mut m, c, plain);
    assert!(!m.busy(c));
    assert_eq!(m.session.at(c), Ok(0));
}

#[test]
fn a_reply_naming_a_call_id_twice_is_refused_with_nothing_changed() {
    let (mut m, c) = machine();
    send(&mut m, c, term(), "hi");
    let twice = Answer::Sampled {
        parts: vec![call("a"), call("a")],
        end: SampleEnd::Finished {
            stop: Stop::Tools,
            reason: None,
            meta: None,
        },
    };
    let jobs = run(&mut m, c, |job, m| match job {
        Job::Sample => vec![twice.clone()],
        _ => plain(job, m),
    });
    assert_eq!(
        ending(&jobs),
        Ending::Failed(Error::DuplicateCall.to_string())
    );
    assert!(!jobs.iter().any(|j| matches!(j, Job::RunCalls { .. })));
    let at = m.session.at(c).unwrap();
    assert_eq!(
        m.session.tree.node(at).unwrap().body,
        Body::User("hi".into())
    );
}

fn call(id: &str) -> Part {
    Part::Call {
        id: id.into(),
        tool: "ls".into(),
        args_raw: "{}".into(),
        replay: None,
    }
}
