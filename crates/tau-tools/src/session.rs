//! The processes of a command, found by session.
//!
//! A command is started as a session leader (`setsid` in the child, before
//! `exec`), so every process it starts, in any process group, shares its
//! session id; only a process that itself calls `setsid` leaves it. A process
//! group alone is not enough: `timeout(1)` and job-control shells move their
//! children to groups of their own. Sessions are found by reading
//! `/proc/<pid>/stat`, so this is Linux only.

use std::fs;
use std::time::Duration;

use nix::errno::Errno;
use nix::sys::signal::{Signal, kill, killpg};
use nix::unistd::Pid;

use crate::{Result, ToolError};

/// Passes over `/proc` before giving up on processes that keep appearing.
const SWEEPS: usize = 200;
const SWEEP_PAUSE: Duration = Duration::from_millis(5);

/// The live (not zombie) processes of session `sid`.
fn members(sid: Pid) -> Result<Vec<Pid>> {
    let scan_failed = |e: std::io::Error| {
        ToolError(format!(
            "could not find the command's processes in /proc: {e}"
        ))
    };
    let mut found = Vec::new();
    for item in fs::read_dir("/proc").map_err(scan_failed)? {
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
        // "pid (comm) state ppid pgrp session ...": comm may hold spaces and
        // parentheses, so count from its last ')'.
        let Some((_, after)) = stat.rsplit_once(')') else {
            continue;
        };
        let mut fields = after.split_whitespace();
        let (state, session) = (fields.next(), fields.nth(2));
        if state != Some("Z") && session.and_then(|s| s.parse::<i32>().ok()) == Some(sid.as_raw()) {
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
/// A signal that failed for a reason other than the process being gone, or
/// `/proc` unreadable.
pub(crate) fn signal_all(leader: Pid, signal: Signal) -> Result<()> {
    match killpg(leader, signal) {
        // ESRCH: already gone, which is what we wanted.
        Ok(()) | Err(Errno::ESRCH) => {}
        Err(e) => return Err(failed(e)),
    }
    for pid in members(leader)? {
        match kill(pid, signal) {
            Ok(()) | Err(Errno::ESRCH) => {}
            Err(e) => return Err(failed(e)),
        }
    }
    Ok(())
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
