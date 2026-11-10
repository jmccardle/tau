//! The processes of a command, found by session.
//!
//! A command is started as a session leader (`setsid` in the child, before
//! `exec`), so every process it starts, in any process group, shares its
//! session id; only a process that itself calls `setsid` leaves it. A process
//! group alone is not enough: `timeout(1)` and job-control shells move their
//! children to groups of their own.
//!
//! How a session's live processes are listed, and how the shell's exit is seen
//! without reaping it, is the platform's: Linux reads `/proc/<pid>/stat` and
//! waits with `waitid(WNOWAIT)` (`linux.rs`); macOS lists processes through
//! libproc and waits with a kqueue (`macos.rs`). Signalling them is the same on both.

use std::time::Duration;

use nix::errno::Errno;
use nix::sys::signal::{Signal, kill, killpg};
use nix::unistd::Pid;

use crate::{Result, ToolError};

#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "linux")]
use linux::members;
#[cfg(target_os = "linux")]
pub(crate) use linux::wait_exited;

#[cfg(target_os = "macos")]
mod macos;
#[cfg(target_os = "macos")]
use macos::members;
#[cfg(target_os = "macos")]
pub(crate) use macos::wait_exited;

/// Passes over the process list before giving up on processes that keep appearing.
const SWEEPS: usize = 200;
const SWEEP_PAUSE: Duration = Duration::from_millis(5);

fn failed(e: Errno) -> ToolError {
    ToolError(format!("could not kill the command's processes: {e}"))
}

/// Send `signal` to the process group and to every other process of the session.
///
/// # Errors
/// A signal that failed for a reason other than the process being gone (the
/// first such failure; every process is still signalled), or the process list unreadable.
pub(crate) fn signal_all(leader: Pid, signal: Signal) -> Result<()> {
    let group = killpg(leader, signal);
    let others = members(leader)?;
    signal_each(group, others.into_iter().map(|pid| kill(pid, signal)))
}

/// The first error among `results` other than ESRCH (already gone, which is what
/// we wanted); all of them have been produced by then.
fn signal_each(
    first: std::result::Result<(), Errno>,
    rest: impl Iterator<Item = std::result::Result<(), Errno>>,
) -> Result<()> {
    std::iter::once(first)
        .chain(rest)
        .fold(None, |kept, result| match result {
            Err(e) if e != Errno::ESRCH => kept.or(Some(e)),
            _ => kept,
        })
        .map_or(Ok(()), |e| Err(failed(e)))
}

/// SIGKILL the process group and the session, again and again until no live
/// process of the session is left (a process may start another while dying).
///
/// # Errors
/// The first failure of [`signal_all`] or of the process scan, reported once the sweeps
/// are done (a failure does not stop them); or processes still there after [`SWEEPS`] passes.
pub(crate) fn kill_all(leader: Pid) -> Result<()> {
    let mut first = None;
    for _ in 0..SWEEPS {
        first = keep_first(first, signal_all(leader, Signal::SIGKILL));
        match members(leader) {
            Ok(left) if left.is_empty() => return first.map_or(Ok(()), Err),
            Ok(_) => {}
            Err(e) => first = keep_first(first, Err(e)),
        }
        std::thread::sleep(SWEEP_PAUSE);
    }
    Err(first.unwrap_or_else(|| {
        ToolError(format!(
            "processes of the command are still running after {SWEEPS} attempts to kill them"
        ))
    }))
}

/// `kept` if there is one, else the error in `result`: a failed sweep must not end the
/// sweeps, and the first failure is the one reported.
fn keep_first(kept: Option<ToolError>, result: Result<()>) -> Option<ToolError> {
    kept.or_else(|| result.err())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_member_is_signalled_and_the_first_failure_is_kept() {
        let mut sent = 0;
        let results = [
            Err(Errno::ESRCH),
            Err(Errno::EPERM),
            Ok(()),
            Err(Errno::EINVAL),
        ];
        let outcome = signal_each(Ok(()), results.into_iter().inspect(|_| sent += 1));
        assert_eq!(sent, 4, "no member was skipped after the first failure");
        let err = outcome.unwrap_err();
        assert!(err.0.contains(&Errno::EPERM.to_string()), "{err}");
        assert!(signal_each(Err(Errno::ESRCH), [Ok(())].into_iter()).is_ok());
    }

    #[test]
    fn a_failed_sweep_is_kept_and_a_later_failure_does_not_replace_it() {
        let first = keep_first(None, Err(ToolError("one".into())));
        assert_eq!(first, Some(ToolError("one".into())));
        let still = keep_first(first, Err(ToolError("two".into())));
        assert_eq!(still, Some(ToolError("one".into())));
        assert_eq!(keep_first(None, Ok(())), None);
    }

    #[test]
    fn the_session_of_a_live_process_is_found() {
        // This test process, and its own session.
        let me = Pid::this();
        let sid = nix::unistd::getsid(None).unwrap();
        assert!(members(sid).unwrap().contains(&me));
    }
}
