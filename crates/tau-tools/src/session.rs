//! The processes of a command, found by session.
//!
//! A command is started as a session leader (`setsid` in the child, before
//! `exec`), so every process it starts, in any process group, shares its
//! session id; only a process that itself calls `setsid` leaves it. A process
//! group alone is not enough: `timeout(1)` and job-control shells move their
//! children to groups of their own. Sessions are found by reading
//! `/proc/<pid>/stat`, so this is Linux only.

use std::fs;
use std::path::Path;
use std::time::Duration;

use nix::errno::Errno;
use nix::sys::signal::{Signal, kill, killpg};
use nix::unistd::Pid;

use crate::{Result, ToolError};

/// Passes over `/proc` before giving up on processes that keep appearing.
const SWEEPS: usize = 200;
const SWEEP_PAUSE: Duration = Duration::from_millis(5);

/// The session id of the process whose `/proc/<pid>/stat` text is `stat`; none
/// for a zombie or a line that is not a stat line.
fn live_session(stat: &str) -> Option<i32> {
    // "pid (comm) state ppid pgrp session ...": comm may hold spaces and
    // parentheses, so count from its last ')'.
    let (_, after) = stat.rsplit_once(')')?;
    let mut fields = after.split_whitespace();
    let state = fields.next()?;
    let session = fields.nth(2)?.parse().ok()?;
    (state != "Z").then_some(session)
}

/// The live (not zombie) processes of session `sid`.
fn members(sid: Pid) -> Result<Vec<Pid>> {
    members_in(Path::new("/proc"), sid)
}

/// [`members`], reading the process directories under `proc`.
fn members_in(proc: &Path, sid: Pid) -> Result<Vec<Pid>> {
    let scan_failed = |e: std::io::Error| {
        ToolError(format!(
            "could not find the command's processes in /proc: {e}"
        ))
    };
    let mut found = Vec::new();
    for item in fs::read_dir(proc).map_err(scan_failed)? {
        let item = item.map_err(scan_failed)?;
        let Some(pid) = item
            .file_name()
            .to_str()
            .and_then(|n| n.parse::<i32>().ok())
        else {
            continue;
        };
        // The process may be gone by now; that is a process fewer.
        let Ok(stat) = fs::read_to_string(item.path().join("stat")) else {
            continue;
        };
        if live_session(&stat) == Some(sid.as_raw()) {
            found.push(Pid::from_raw(pid));
        }
    }
    Ok(found)
}

fn failed(e: Errno) -> ToolError {
    ToolError(format!("could not kill the command's processes: {e}"))
}

/// Send `signal` to the process group and to every other process of the session.
///
/// # Errors
/// A signal that failed for a reason other than the process being gone (the
/// first such failure; every process is still signalled), or `/proc` unreadable.
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
/// As [`signal_all`], or processes still there after [`SWEEPS`] passes.
pub(crate) fn kill_all(leader: Pid) -> Result<()> {
    for _ in 0..SWEEPS {
        signal_all(leader, Signal::SIGKILL)?;
        if members(leader)?.is_empty() {
            return Ok(());
        }
        std::thread::sleep(SWEEP_PAUSE);
    }
    Err(
        format!("processes of the command are still running after {SWEEPS} attempts to kill them")
            .into(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    // Lines as the kernel writes them: pid (comm) state ppid pgrp session ...
    const SLEEPING: &str = "4242 (sleep) S 4200 4242 4242 0 -1 4194560 100 0 0 0";
    const ZOMBIE: &str = "4243 (sleep) Z 4242 4242 4242 0 -1 4194500 0 0 0 0";
    const SPACED: &str = "77 (tmux: server) S 1 77 77 0 -1 4194560 0 0 0 0";
    const PARENS: &str = "78 (a) S (b) R 1 78 78 0 -1 4194560 0 0 0 0";

    #[test]
    fn a_live_process_has_its_session() {
        assert_eq!(live_session(SLEEPING), Some(4242));
    }

    #[test]
    fn a_zombie_has_none() {
        assert_eq!(live_session(ZOMBIE), None);
    }

    #[test]
    fn a_command_name_with_spaces_or_parentheses_is_read_from_its_last_paren() {
        assert_eq!(live_session(SPACED), Some(77));
        assert_eq!(live_session(PARENS), Some(78));
    }

    #[test]
    fn a_malformed_line_has_none() {
        for line in [
            "",
            "no parentheses at all",
            "5 (short",
            "5 (short) S 1",
            "5 (short) S 1 5",
            "5 (x) S 1 5 notanumber 0",
        ] {
            assert_eq!(live_session(line), None, "{line:?}");
        }
    }

    fn fake_proc(procs: &[(&str, Option<&str>)]) -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        for (name, stat) in procs {
            let path = dir.path().join(name);
            fs::create_dir(&path).unwrap();
            if let Some(stat) = stat {
                fs::write(path.join("stat"), stat).unwrap();
            }
        }
        dir
    }

    #[test]
    fn the_scan_finds_live_members_and_skips_the_rest() {
        let proc = fake_proc(&[
            ("4242", Some(SLEEPING)),
            ("4243", Some(ZOMBIE)),
            ("77", Some(SPACED)),
            // A process that vanished between listing and reading: no stat file.
            ("9999", None),
            // A malformed stat, and names that are not pids.
            ("5", Some("garbage")),
            ("self", Some(SLEEPING)),
            ("sys", None),
        ]);
        let pids = members_in(proc.path(), Pid::from_raw(4242)).unwrap();
        assert_eq!(pids, vec![Pid::from_raw(4242)]);
    }

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
    fn an_unreadable_proc_is_an_error() {
        let err = members_in(Path::new("/nonexistent/proc"), Pid::from_raw(1)).unwrap_err();
        assert!(
            err.0.contains("could not find the command's processes"),
            "{err}"
        );
    }
}
