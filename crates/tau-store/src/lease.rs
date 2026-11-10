//! The lease: an exclusive lock on the log, held while a `Store` lives.
//!
//! Unix: `flock` on the whole file (std's `File::try_lock`). An `flock` is advisory, so a
//! reader without the lease (`Store::read`, `Store::first_user`) still reads the log.
//!
//! Windows: a lock there is mandatory (a locked byte cannot be read through another
//! handle), so std's whole-file lock would stop those readers. The lease is instead one
//! byte far past any log's end, [`OFFSET`], locked with `LockFileEx`; Windows allows a
//! lock beyond the end of a file. It is let go when the handle closes, as an `flock` is.

use std::fs::File;
use std::io;

/// Whether `f` was leased by this call, or another holder has it.
pub(crate) enum Taken {
    Yes,
    Held,
}

/// Take the lease on `f`, without waiting.
#[cfg(unix)]
pub(crate) fn try_lease(f: &File) -> io::Result<Taken> {
    match f.try_lock() {
        Ok(()) => Ok(Taken::Yes),
        Err(std::fs::TryLockError::WouldBlock) => Ok(Taken::Held),
        Err(std::fs::TryLockError::Error(e)) => Err(e),
    }
}

/// The byte whose lock is the lease on Windows: past the end of any log.
#[cfg(windows)]
const OFFSET: u64 = u64::MAX - 1;

/// Take the lease on `f`, without waiting.
#[cfg(windows)]
#[allow(unsafe_code)]
pub(crate) fn try_lease(f: &File) -> io::Result<Taken> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::ERROR_LOCK_VIOLATION;
    use windows_sys::Win32::Storage::FileSystem::{
        LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY, LockFileEx,
    };
    use windows_sys::Win32::System::IO::OVERLAPPED;

    // SAFETY: OVERLAPPED is plain data; all zeros is its documented starting value.
    let mut at: OVERLAPPED = unsafe { std::mem::zeroed() };
    #[allow(clippy::cast_possible_truncation)] // the low and high halves, by design
    {
        at.Anonymous.Anonymous.Offset = OFFSET as u32;
        at.Anonymous.Anonymous.OffsetHigh = (OFFSET >> 32) as u32;
    }
    // SAFETY: the handle is open for the life of `f`, and `at` outlives the call: the
    // handle is synchronous, so the call completes before returning.
    let ok = unsafe {
        LockFileEx(
            f.as_raw_handle(),
            LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
            0,
            1,
            0,
            &raw mut at,
        )
    };
    if ok != 0 {
        return Ok(Taken::Yes);
    }
    let e = io::Error::last_os_error();
    if e.raw_os_error() == i32::try_from(ERROR_LOCK_VIOLATION).ok() {
        Ok(Taken::Held)
    } else {
        Err(e)
    }
}

/// Let go of the lease on `f` now. Closing the handle lets it go too, but Windows may
/// take its time over a lock left at close, and the next open must find it free.
#[cfg(windows)]
#[allow(unsafe_code)]
pub(crate) fn release(f: &File) {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::UnlockFileEx;
    use windows_sys::Win32::System::IO::OVERLAPPED;

    // SAFETY: as in `try_lease`.
    let mut at: OVERLAPPED = unsafe { std::mem::zeroed() };
    #[allow(clippy::cast_possible_truncation)] // the low and high halves, by design
    {
        at.Anonymous.Anonymous.Offset = OFFSET as u32;
        at.Anonymous.Anonymous.OffsetHigh = (OFFSET >> 32) as u32;
    }
    // Cleanup: a lease that was not held cannot be let go, and closing the handle
    // lets go of one that was; there is no one to tell.
    // SAFETY: as in `try_lease`.
    let _ = unsafe { UnlockFileEx(f.as_raw_handle(), 0, 1, 0, &raw mut at) };
}

/// Let go of the lease on `f` now: on Unix closing the file does it at once.
#[cfg(unix)]
pub(crate) fn release(_f: &File) {}
