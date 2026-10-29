//! The transition log: every position change of a step, for the generated
//! diagrams (`scripts/diagrams.sh`). With the `transitions` feature off every
//! function here is empty and costs nothing.
//!
//! With the feature on, each distinct (scenario, edge) is written once to
//! stderr as a tab-separated `TAU-TRANSITION` line. A scenario is the test
//! thread's name (a `cargo test` test), or the name given to [`scenario`].

use crate::step::{Ending, Fed, Job};

#[cfg(feature = "transitions")]
mod on {
    use std::cell::RefCell;
    use std::collections::HashSet;
    use std::sync::Mutex;

    thread_local! {
        static EVENT: RefCell<String> = const { RefCell::new(String::new()) };
    }
    static SEEN: Mutex<Option<HashSet<String>>> = Mutex::new(None);
    static SCENARIO: Mutex<Option<String>> = Mutex::new(None);

    pub fn set_event(e: String) {
        EVENT.with(|c| *c.borrow_mut() = e);
    }

    pub fn refine(e: &str) {
        EVENT.with(|c| {
            let mut c = c.borrow_mut();
            c.push(' ');
            c.push_str(e);
        });
    }

    pub fn current() -> String {
        EVENT.with(|c| c.borrow().clone())
    }

    pub fn scenario(name: &str) {
        *SCENARIO.lock().unwrap() = Some(name.into());
    }

    pub fn log(kind: &str, from: &str, event: &str, to: &str) {
        let name = SCENARIO.lock().unwrap().clone().unwrap_or_else(|| {
            std::thread::current()
                .name()
                .unwrap_or("unnamed")
                .to_string()
        });
        let line = format!("TAU-TRANSITION\t{name}\t{kind}\t{from}\t{event}\t{to}");
        if SEEN
            .lock()
            .unwrap()
            .get_or_insert_with(HashSet::new)
            .insert(line.clone())
        {
            eprintln!("{line}");
        }
    }
}

/// Name the scenario the following transitions belong to (a pytest test).
pub fn scenario(name: &str) {
    #[cfg(feature = "transitions")]
    on::scenario(name);
    #[cfg(not(feature = "transitions"))]
    let _ = name;
}

/// Set the event the next position changes are caused by (a hop inside `job`).
pub(crate) fn event(label: &str) {
    #[cfg(feature = "transitions")]
    on::set_event(label.into());
    #[cfg(not(feature = "transitions"))]
    let _ = label;
}

/// The event is now this feed, without its payload.
pub(crate) fn fed(fed: &Fed) {
    #[cfg(feature = "transitions")]
    on::set_event(match fed {
        Fed::Hooks { stop: Some(_), .. } => "feed Hooks(stop)".into(),
        Fed::Hooks { .. } => "feed Hooks".into(),
        Fed::HookFailed(_) => "feed HookFailed".into(),
        Fed::Sampled { reply, error } => format!(
            "feed Sampled({})",
            match (reply.is_some(), error.is_some()) {
                (true, true) => "reply, error",
                (true, false) => "reply",
                (false, true) => "error",
                (false, false) => "none",
            }
        ),
        Fed::CallDone { .. } => "feed CallDone".into(),
        Fed::Waited => "feed Waited".into(),
    });
    #[cfg(not(feature = "transitions"))]
    let _ = fed;
}

/// Add the step's ending to the event.
pub(crate) fn ending(e: &Ending) {
    #[cfg(feature = "transitions")]
    on::refine(&format!(
        "[ending {}]",
        match e {
            Ending::Done => "Done",
            Ending::Halted(_) => "Halted",
            Ending::Failed(_) => "Failed",
            Ending::Aborted => "Aborted",
        }
    ));
    #[cfg(not(feature = "transitions"))]
    let _ = e;
}

/// A position change (or the step staying where it is) under the current event.
pub(crate) fn go(from: impl std::fmt::Debug, to: impl std::fmt::Debug) {
    #[cfg(feature = "transitions")]
    on::log(
        "go",
        &format!("{from:?}"),
        &on::current(),
        &format!("{to:?}"),
    );
    #[cfg(not(feature = "transitions"))]
    let _ = (from, to);
}

/// A step wakes: a new step at its first position.
pub(crate) fn wake(asked: bool) {
    #[cfg(feature = "transitions")]
    on::log(
        "go",
        "(wake)",
        if asked { "wake (asked)" } else { "wake" },
        "PreStep",
    );
    #[cfg(not(feature = "transitions"))]
    let _ = asked;
}

/// The step handed out a job while at `pos`.
pub(crate) fn emit(pos: impl std::fmt::Debug, job: &Job) {
    #[cfg(feature = "transitions")]
    {
        let label = match job {
            Job::Hooks {
                point,
                ending: Some(e),
                ..
            } => format!(
                "Hooks({point:?}, {})",
                match e {
                    Ending::Done => "Done",
                    Ending::Halted(_) => "Halted",
                    Ending::Failed(_) => "Failed",
                    Ending::Aborted => "Aborted",
                }
            ),
            Job::Hooks { point, .. } => format!("Hooks({point:?})"),
            Job::RunCalls { .. } => "RunCalls".into(),
            Job::Wait { .. } => "Wait".into(),
            other => format!("{other:?}"),
        };
        let pos = format!("{pos:?}");
        on::log("emit", &pos, &format!("emit {label}"), &pos);
    }
    #[cfg(not(feature = "transitions"))]
    let _ = (pos, job);
}
