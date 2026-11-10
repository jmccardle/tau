//! macOS: there is no `/proc`. Every pid comes from libproc (`proc_listallpids`),
//! each one's session from `getsid`, and whether it is a zombie from its BSD info
//! (`proc_pidinfo`, which answers for zombies too). The shell's exit is seen
//! with a kqueue (`EVFILT_PROC`, `NOTE_EXIT`), which leaves it unreaped: macOS
//! has no `waitid(WNOWAIT)` that nix offers.

use std::ffi::c_int;
use std::mem::size_of;

use nix::errno::Errno;
use nix::sys::event::{EvFlags, EventFilter, FilterFlag, KEvent, Kqueue};
use nix::unistd::{Pid, getsid};

use crate::{Result, ToolError};

/// Room for pids that appear between counting them and listing them.
const SLACK: usize = 64;

/// Block until the child `pid` has exited, leaving it unreaped (its pid cannot be
/// reused until it is reaped).
pub(crate) fn wait_exited(pid: Pid) -> nix::Result<()> {
    let ident = usize::try_from(pid.as_raw()).map_err(|_| Errno::EINVAL)?;
    let event = || {
        KEvent::new(
            ident,
            EventFilter::EVFILT_PROC,
            EvFlags::EV_ADD | EvFlags::EV_ONESHOT,
            FilterFlag::NOTE_EXIT,
            0,
            0,
        )
    };
    let queue = Kqueue::new()?;
    let now = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    match queue.kevent(&[event()], &mut [], Some(now)) {
        // Already exited: it is unreaped, so the pid is still our child's.
        Err(Errno::ESRCH) => return Ok(()),
        Err(e) => return Err(e),
        Ok(_) => {}
    }
    let mut seen = [event()];
    loop {
        match queue.kevent(&[], &mut seen, None) {
            Ok(0) | Err(Errno::EINTR) => {}
            Ok(_) => return Ok(()),
            Err(e) => return Err(e),
        }
    }
}

/// Every pid on the system, as libproc lists them.
#[allow(unsafe_code)]
fn all_pids() -> Result<Vec<libc::pid_t>> {
    let failed = |e: std::io::Error| {
        ToolError(format!(
            "could not list the processes to find the command's: {e}"
        ))
    };
    loop {
        // SAFETY: a null buffer of size 0 asks only for the number of pids.
        let count = unsafe { libc::proc_listallpids(std::ptr::null_mut(), 0) };
        let count = usize::try_from(count).map_err(|_| failed(std::io::Error::last_os_error()))?;
        let room = count + SLACK;
        let mut pids: Vec<libc::pid_t> = vec![0; room];
        let bytes = c_int::try_from(room * size_of::<libc::pid_t>())
            .map_err(|_| failed(std::io::Error::other("too many processes")))?;
        // SAFETY: `pids` holds `room` pids, and `bytes` is its size in bytes; the call
        // writes at most that many and answers how many it wrote.
        let got = unsafe { libc::proc_listallpids(pids.as_mut_ptr().cast(), bytes) };
        let got = usize::try_from(got).map_err(|_| failed(std::io::Error::last_os_error()))?;
        if got < room {
            pids.truncate(got);
            return Ok(pids);
        }
        // Full: more processes appeared than the slack allowed for; count again.
    }
}

/// Whether `pid` is alive and not a zombie. A pid whose information cannot be read for
/// a reason other than being gone counts as alive: it is there, so it is signalled.
#[allow(unsafe_code)]
fn live(pid: libc::pid_t) -> bool {
    // SAFETY: `proc_bsdinfo` is plain data (integers and byte arrays), so all zeros is a
    // valid value of it.
    let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    let size = c_int::try_from(size_of::<libc::proc_bsdinfo>()).unwrap_or(c_int::MAX);
    // SAFETY: `info` is a `proc_bsdinfo` of `size` bytes, which is what
    // PROC_PIDTBSDINFO writes into.
    let got = unsafe {
        libc::proc_pidinfo(
            pid,
            libc::PROC_PIDTBSDINFO,
            0,
            (&raw mut info).cast(),
            size,
        )
    };
    if got == size {
        info.pbi_status != libc::SZOMB
    } else {
        Errno::last() != Errno::ESRCH
    }
}

/// The live (not zombie) processes of session `sid`.
pub(super) fn members(sid: Pid) -> Result<Vec<Pid>> {
    Ok(all_pids()?
        .into_iter()
        .filter(|&pid| pid > 0)
        .map(Pid::from_raw)
        // A process gone since the listing (ESRCH) is a process fewer.
        .filter(|&pid| getsid(Some(pid)) == Ok(sid) && live(pid.as_raw()))
        .collect())
}
