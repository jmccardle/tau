//! Output limiting: keep one end of the output, spill the whole of it to a file.
//!
//! A [`Sink`] takes the output in chunks. While the total stays within the
//! limit nothing is written anywhere. At the first byte past it, the output so
//! far and everything after goes to `<dir>/<id>.out` (up to the overflow cap),
//! and memory holds only the window the kept text comes from. [`Sink::finish`]
//! cuts that window on a line boundary and adds the truncation note. A sink made
//! with [`Sink::unspilled`] never writes a file (`read`: the file it reads is
//! already on disk) and adds no note; its caller says where to go on.
//!
//! The overflow directory is private to us, and is checked each time a file is
//! first needed, on the open directory itself: created `0700`, and refused unless
//! it is a real directory (not a symlink) owned by the effective user and closed
//! to everyone else. A directory that fails, or a file that cannot be written (disk
//! full), costs the overflow file, not the call: the answer still has the kept text, and
//! its note says why the full output was not saved. Files are created `0600`, never overwritten, and files
//! in the directory older than a day are removed whenever it is opened.

use std::fmt::Write as _;
use std::fs::{DirBuilder, File};
use std::io::{BufWriter, Write};
use std::mem;
use std::os::fd::OwnedFd;
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use nix::dir::Dir;
use nix::errno::Errno;
use nix::fcntl::{AtFlags, OFlag, open, openat};
use nix::sys::stat::{Mode, SFlag, fstat, fstatat};
use nix::unistd::{Uid, UnlinkatFlags, unlinkat};

use crate::Limits;

/// Which end of the output the model keeps.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Keep {
    /// The first lines (`read`, `grep`).
    Head,
    /// The last lines (`bash`: errors land at the end).
    Tail,
}

const ID_LEN: usize = 8;
const ID_CHARS: &[u8] = b"abcdefghijklmnopqrstuvwxyz0123456789";
const ID_TRIES: usize = 16;
const MIB: usize = 1024 * 1024;
/// Overflow files older than this are removed when the directory is opened.
const KEEP_FILES: Duration = Duration::from_secs(24 * 60 * 60);

/// Added to an answer whose kept text had bytes that are not UTF-8.
pub const LOSSY_NOTE: &str = "[invalid UTF-8 in the output was replaced with U+FFFD]";

#[derive(Debug)]
struct Overflow {
    path: PathBuf,
    out: BufWriter<File>,
    written: usize,
    cap: usize,
    capped: bool,
}

/// Where the full output goes.
#[derive(Debug)]
enum Spill {
    /// Nowhere, ever.
    Never,
    /// A file in `dir`, made when the output first passes the limit.
    Pending {
        dir: PathBuf,
        cap: usize,
    },
    /// The file could not be made or written; the note says why, and how many bytes it
    /// held (0: it was never made).
    Failed {
        reason: String,
        saved: usize,
    },
    Open(Overflow),
}

/// The answer a [`Sink`] makes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Finished {
    /// The text for the model, with its notes.
    pub text: String,
    /// Lines of the output shown (a cut-short line counts as one).
    pub kept_lines: usize,
    /// Bytes of the output shown, without the notes.
    pub kept_bytes: usize,
    /// Whether the output passed the limit.
    pub truncated: bool,
    /// Whether the one line shown is only the start of a line (none fit whole).
    pub cut_line: bool,
}

/// Output being limited. See the module docs.
#[derive(Debug)]
pub struct Sink {
    keep: Keep,
    max: usize,
    spill: Spill,
    /// Head: the first `max + 1` bytes. Tail: at least the last `max + 1` bytes
    /// once the output passes `max`, and all of it before that.
    window: Vec<u8>,
    total: usize,
    newlines: usize,
    last: Option<u8>,
}

impl Sink {
    #[must_use]
    pub fn new(limits: &Limits, keep: Keep) -> Self {
        let spill = Spill::Pending {
            dir: limits.dir.clone(),
            cap: limits.max_overflow,
        };
        Self::with(limits.max_bytes, keep, spill)
    }

    /// A sink that keeps `max` bytes and saves nothing.
    #[must_use]
    pub fn unspilled(max: usize, keep: Keep) -> Self {
        Self::with(max, keep, Spill::Never)
    }

    fn with(max: usize, keep: Keep, spill: Spill) -> Self {
        Self {
            keep,
            max,
            spill,
            window: Vec::new(),
            total: 0,
            newlines: 0,
            last: None,
        }
    }

    /// Whether more than the limit has been pushed.
    #[must_use]
    pub fn is_over(&self) -> bool {
        self.total > self.max
    }

    /// Bytes pushed so far.
    #[must_use]
    pub fn total(&self) -> usize {
        self.total
    }

    /// Take more output. A failure to write the overflow file does not fail the push: the
    /// sink goes on without a file and its note says why.
    pub fn push(&mut self, chunk: &[u8]) {
        let Some(&last) = chunk.last() else {
            return;
        };
        if matches!(self.spill, Spill::Pending { .. }) && self.total + chunk.len() > self.max {
            // The window still holds everything pushed so far (it holds
            // `2 * max` bytes at least, and the total has not passed `max`).
            self.spill = match mem::replace(&mut self.spill, Spill::Never) {
                Spill::Pending { dir, cap } => self.open_spill(&dir, cap),
                other => other,
            };
        }
        if let Spill::Open(overflow) = &mut self.spill {
            let take = (overflow.cap - overflow.written).min(chunk.len());
            match overflow.out.write_all(&chunk[..take]) {
                Ok(()) => {
                    overflow.written += take;
                    overflow.capped |= take < chunk.len();
                }
                Err(e) => {
                    let saved = bytes_in(overflow.out.get_ref());
                    let reason = write_failed(&overflow.path, &e);
                    self.spill = Spill::Failed { reason, saved };
                }
            }
        }
        self.total += chunk.len();
        self.newlines += newlines_in(chunk);
        self.last = Some(last);
        self.keep_in_window(chunk);
    }

    fn keep_in_window(&mut self, chunk: &[u8]) {
        match self.keep {
            Keep::Head => {
                let room = (self.max + 1).saturating_sub(self.window.len());
                self.window
                    .extend_from_slice(&chunk[..room.min(chunk.len())]);
            }
            Keep::Tail => {
                self.window.extend_from_slice(chunk);
                if self.window.len() > 2 * self.max {
                    let drop = self.window.len() - (self.max + 1);
                    self.window.drain(..drop);
                }
            }
        }
    }

    /// The overflow file, holding what has been pushed so far; or why there is none.
    fn open_spill(&self, dir: &Path, cap: usize) -> Spill {
        let never_made = |reason| Spill::Failed { reason, saved: 0 };
        let dir_fd = match open_private_dir(dir) {
            Ok(fd) => fd,
            Err(reason) => return never_made(reason),
        };
        for _ in 0..ID_TRIES {
            let id: String = (0..ID_LEN)
                .map(|_| char::from(ID_CHARS[fastrand::usize(..ID_CHARS.len())]))
                .collect();
            let name = format!("{id}.out");
            let path = dir.join(&name);
            // Relative to the fd we checked, so a swapped path cannot redirect us.
            let flags = OFlag::O_WRONLY
                | OFlag::O_CREAT
                | OFlag::O_EXCL
                | OFlag::O_NOFOLLOW
                | OFlag::O_CLOEXEC;
            match openat(
                &dir_fd,
                name.as_str(),
                flags,
                Mode::from_bits_truncate(0o600),
            ) {
                Ok(fd) => {
                    let mut out = BufWriter::new(File::from(fd));
                    if let Err(e) = out.write_all(&self.window) {
                        return never_made(write_failed(&path, &e));
                    }
                    let written = self.window.len();
                    return Spill::Open(Overflow {
                        path,
                        out,
                        written,
                        cap,
                        capped: false,
                    });
                }
                Err(Errno::EEXIST) => {}
                Err(e) => {
                    return never_made(format!(
                        "the file {} could not be created: {e}",
                        path.display()
                    ));
                }
            }
        }
        never_made(format!(
            "no unused file name was found in {}",
            dir.display()
        ))
    }

    /// The text for the model: all of the output if it fit, else the kept end
    /// and the truncation note.
    ///
    #[must_use]
    pub fn finish(mut self) -> Finished {
        if !self.is_over() {
            let kept = self.window.as_slice();
            let text = self.compose(kept, None);
            return Finished {
                text,
                kept_lines: count_lines(kept),
                kept_bytes: kept.len(),
                truncated: false,
                cut_line: false,
            };
        }
        let spill = mem::replace(&mut self.spill, Spill::Never);
        let (kept, cut_line) = match self.keep {
            Keep::Head => self.kept_head(),
            Keep::Tail => self.kept_tail(),
        };
        let kept_lines = count_lines(kept);
        let note = match spill {
            Spill::Never => None,
            spill => Some(self.truncation_note(spill, kept_lines, cut_line)),
        };
        let text = self.compose(kept, note.as_deref());
        Finished {
            text,
            kept_lines,
            kept_bytes: kept.len(),
            truncated: true,
            cut_line,
        }
    }

    /// The note of an answer that was cut: what was omitted and where the whole is.
    fn truncation_note(&self, spill: Spill, kept_lines: usize, cut_line: bool) -> String {
        let total_lines = self.newlines + usize::from(self.last != Some(b'\n'));
        let omitted = total_lines.saturating_sub(kept_lines);
        let mut note = format!("[truncated: {}", lines(omitted));
        note.push_str(match self.keep {
            Keep::Head => " omitted after these",
            Keep::Tail => " omitted before these",
        });
        if cut_line {
            note.push_str(", and one long line cut short");
        }
        let spill = match spill {
            Spill::Open(Overflow {
                path,
                mut out,
                written,
                capped,
                cap,
            }) => match out.flush() {
                Ok(()) => Spill::Open(Overflow {
                    path,
                    out,
                    written,
                    cap,
                    capped,
                }),
                Err(e) => Spill::Failed {
                    reason: write_failed(&path, &e),
                    saved: bytes_in(out.get_ref()),
                },
            },
            other => other,
        };
        let _ = match spill {
            Spill::Open(Overflow {
                path,
                out,
                capped,
                cap,
                ..
            }) => {
                drop(out);
                if capped {
                    write!(
                        note,
                        "; the output is {} bytes but {} holds only the first {}]",
                        self.total,
                        path.display(),
                        size_text(cap)
                    )
                } else {
                    write!(
                        note,
                        "; full output ({} bytes) is in {}]",
                        self.total,
                        path.display()
                    )
                }
            }
            Spill::Failed { reason, saved } => {
                let after = if saved > 0 {
                    format!(" after {saved} bytes")
                } else {
                    String::new()
                };
                write!(
                    note,
                    "; the full output ({} bytes) was not saved{after}: {reason}]",
                    self.total
                )
            }
            Spill::Never | Spill::Pending { .. } => {
                unreachable!("a sink past its limit has a file or a reason")
            }
        };
        note
    }

    /// The kept bytes as text, with the truncation note and the invalid-UTF-8
    /// note on the side of the text that the omitted lines are on.
    fn compose(&self, kept: &[u8], truncation: Option<&str>) -> String {
        let text = String::from_utf8_lossy(kept);
        let lossy = std::str::from_utf8(kept).is_err().then_some(LOSSY_NOTE);
        let notes = truncation.into_iter().chain(lossy);
        let mut out = String::new();
        match self.keep {
            Keep::Head => {
                out.push_str(&text);
                for note in notes {
                    out.push('\n');
                    out.push_str(note);
                }
            }
            Keep::Tail => {
                for note in notes {
                    out.push_str(note);
                    out.push('\n');
                }
                out.push_str(&text);
            }
        }
        out
    }

    /// The kept bytes, and whether a line was cut because none fit whole.
    fn kept_head(&self) -> (&[u8], bool) {
        let w = &self.window;
        let fits = &w[..self.max];
        if w[self.max] == b'\n' {
            if !fits.is_empty() {
                return (fits, false);
            }
        } else if let Some(i) = fits.iter().rposition(|&b| b == b'\n')
            && i > 0
        {
            return (&fits[..i], false);
        }
        let mut end = self.max;
        while end > 0 && is_continuation(w[end]) {
            end -= 1;
        }
        (&w[..end], true)
    }

    fn kept_tail(&self) -> (&[u8], bool) {
        let w = &self.window;
        let start = w.len() - self.max;
        let fits = &w[start..];
        if w[start - 1] == b'\n' {
            return (fits, false);
        }
        if let Some(i) = fits.iter().position(|&b| b == b'\n')
            && i + 1 < fits.len()
        {
            return (&fits[i + 1..], false);
        }
        let skip = fits.iter().take_while(|&&b| is_continuation(b)).count();
        (&fits[skip..], true)
    }
}

/// Open `dir` (created `0700` if absent) as a directory fd, refusing a symlink,
/// and check with `fstat` on that fd that it is ours and private. Then drop what
/// is old in it. The error is the reason, for the note.
fn open_private_dir(dir: &Path) -> std::result::Result<OwnedFd, String> {
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
    if let Some(problem) = dir_problem(stat.st_uid, stat.st_mode) {
        return Err(format!("the overflow directory {shown} {problem}"));
    }
    remove_old_files(&fd);
    Ok(fd)
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
        let regular = SFlag::from_bits_truncate(stat.st_mode) & SFlag::S_IFMT == SFlag::S_IFREG;
        let age = now.saturating_sub(u64::try_from(stat.st_mtime).unwrap_or(0));
        if regular && age > KEEP_FILES.as_secs() {
            let _ = unlinkat(fd, name.as_c_str(), UnlinkatFlags::NoRemoveDir);
        }
    }
}

/// Whether `name` is one we generate: `ID_LEN` characters of `ID_CHARS`, then `.out`.
fn is_overflow_name(name: &[u8]) -> bool {
    name.strip_suffix(b".out")
        .is_some_and(|id| id.len() == ID_LEN && id.iter().all(|c| ID_CHARS.contains(c)))
}

/// What is wrong with a directory of this owner and mode, if anything.
fn dir_problem(uid: u32, mode: u32) -> Option<String> {
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

fn size_text(bytes: usize) -> String {
    if bytes.is_multiple_of(MIB) {
        format!("{} MiB", bytes / MIB)
    } else {
        format!("{bytes} bytes")
    }
}

#[allow(clippy::naive_bytecount)] // one pass over a chunk; not worth a dependency
fn newlines_in(bytes: &[u8]) -> usize {
    bytes.iter().filter(|&&b| b == b'\n').count()
}

fn is_continuation(byte: u8) -> bool {
    byte & 0xC0 == 0x80
}

/// What the file holds, as the file system has it (a failed write may have been partial).
fn bytes_in(file: &File) -> usize {
    file.metadata()
        .map_or(0, |m| usize::try_from(m.len()).unwrap_or(usize::MAX))
}

fn write_failed(path: &Path, e: &std::io::Error) -> String {
    format!("could not write the full output to {}: {e}", path.display())
}

/// Lines in `text`: its newlines, plus one for a last line without one.
#[must_use]
pub fn count_lines(text: &[u8]) -> usize {
    let newlines = newlines_in(text);
    newlines + usize::from(text.last().is_some_and(|&b| b != b'\n'))
}

fn lines(n: usize) -> String {
    if n == 1 {
        "1 line".to_owned()
    } else {
        format!("{n} lines")
    }
}

#[cfg(test)]
mod tests;
