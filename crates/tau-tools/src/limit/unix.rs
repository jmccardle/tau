//! The overflow directory on Unix: an open directory fd, checked with `fstat`, and files
//! made relative to it with `openat`, so a swapped path cannot redirect us.

use std::fs::{DirBuilder, File};
use std::os::fd::OwnedFd;
use std::os::unix::fs::DirBuilderExt;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use nix::dir::Dir;
use nix::errno::Errno;
use nix::fcntl::{AtFlags, OFlag, open, openat};
use nix::sys::stat::{Mode, SFlag, fstat, fstatat};
use nix::unistd::{Uid, UnlinkatFlags, unlinkat};

use super::{KEEP_FILES, is_overflow_name};

/// The overflow directory, open and checked.
#[derive(Debug)]
pub(super) struct PrivateDir(OwnedFd);

impl PrivateDir {
    /// Open `dir` (created `0700` if absent) as a directory fd, refusing a symlink,
    /// and check with `fstat` on that fd that it is ours and private. Then drop what
    /// is old in it. The error is the reason, for the note.
    pub(super) fn open(dir: &Path) -> Result<Self, String> {
        let shown = dir.display();
        DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(dir)
            .map_err(|e| format!("the overflow directory {shown} could not be created: {e}"))?;
        let flags = OFlag::O_RDONLY | OFlag::O_DIRECTORY | OFlag::O_NOFOLLOW | OFlag::O_CLOEXEC;
        let fd = open(dir, flags, Mode::empty()).map_err(|e| match e {
            // O_NOFOLLOW with O_DIRECTORY reports a symlink as ENOTDIR or ELOOP.
            Errno::ELOOP | Errno::ENOTDIR => match std::fs::symlink_metadata(dir) {
                Ok(meta) if meta.file_type().is_symlink() => {
                    format!("the overflow directory {shown} is a symlink")
                }
                _ => format!("the overflow directory {shown} is not a directory"),
            },
            e => format!("the overflow directory {shown} cannot be opened: {e}"),
        })?;
        let stat =
            fstat(&fd).map_err(|e| format!("cannot inspect the overflow directory {shown}: {e}"))?;
        if let Some(problem) = dir_problem(stat.st_uid, u32::from(stat.st_mode)) {
            return Err(format!("the overflow directory {shown} {problem}"));
        }
        remove_old_files(&fd);
        Ok(Self(fd))
    }

    /// A new file `name` (mode `0600`) in the directory; `None` if the name is taken.
    /// The error is the reason.
    pub(super) fn create(&self, name: &str) -> Result<Option<File>, String> {
        let flags =
            OFlag::O_WRONLY | OFlag::O_CREAT | OFlag::O_EXCL | OFlag::O_NOFOLLOW | OFlag::O_CLOEXEC;
        match openat(&self.0, name, flags, Mode::from_bits_truncate(0o600)) {
            Ok(fd) => Ok(Some(File::from(fd))),
            Err(Errno::EEXIST) => Ok(None),
            Err(e) => Err(e.to_string()),
        }
    }
}

/// Remove the overflow files (see [`is_overflow_name`]) in the directory `fd` that are older than [`KEEP_FILES`].
/// Cleanup: a file that cannot be listed or removed stays, which costs disk and
/// nothing else, so there is no one to tell.
fn remove_old_files(fd: &OwnedFd) {
    let Ok(listing) = fd
        .try_clone()
        .map_err(drop)
        .and_then(|dup| Dir::from_fd(dup).map_err(drop))
    else {
        return;
    };
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    let mut listing = listing;
    let names: Vec<_> = listing
        .iter()
        .flatten()
        .map(|item| item.file_name().to_owned())
        .filter(|name| is_overflow_name(name.to_bytes()))
        .collect();
    for name in names {
        let Ok(stat) = fstatat(fd, name.as_c_str(), AtFlags::AT_SYMLINK_NOFOLLOW) else {
            continue;
        };
        let regular =
            SFlag::from_bits_truncate(stat.st_mode) & SFlag::S_IFMT == SFlag::S_IFREG;
        let age = now.saturating_sub(u64::try_from(stat.st_mtime).unwrap_or(0));
        if regular && age > KEEP_FILES.as_secs() {
            let _ = unlinkat(fd, name.as_c_str(), UnlinkatFlags::NoRemoveDir);
        }
    }
}

/// What is wrong with a directory of this owner and mode, if anything.
pub(super) fn dir_problem(uid: u32, mode: u32) -> Option<String> {
    if uid != Uid::effective().as_raw() {
        Some(format!("is owned by uid {uid}, not by this process"))
    } else if mode & 0o077 != 0 {
        Some(format!(
            "has mode {:04o}; it must be accessible only by its owner (0700)",
            mode & 0o7777
        ))
    } else {
        None
    }
}
