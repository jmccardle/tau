//! Output limiting: keep one end of the output, spill the whole of it to a file.
//!
//! A [`Sink`] takes the output in chunks. While the total stays within the
//! limit nothing is written anywhere. At the first byte past it, the output so
//! far and everything after goes to `<dir>/<id>.out` (up to the overflow cap),
//! and memory holds only the window the kept text comes from. [`Sink::finish`]
//! cuts that window on a line boundary and adds the truncation note.
//!
//! The overflow directory is private to us: created `0700`, and refused unless
//! it is a real directory (not a symlink) owned by the effective user and
//! closed to everyone else. Files are created `0600`, never overwritten.

use std::fmt::Write as _;
use std::fs::{self, DirBuilder, File};
use std::io::{BufWriter, Write};
use std::os::fd::OwnedFd;
use std::os::unix::fs::{DirBuilderExt, MetadataExt};
use std::path::{Path, PathBuf};

use nix::errno::Errno;
use nix::fcntl::{OFlag, open, openat};
use nix::sys::stat::{Mode, fstat};
use nix::unistd::Uid;

use crate::{Limits, Result, ToolError};

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

/// Added to an answer whose kept text had bytes that are not UTF-8.
pub const LOSSY_NOTE: &str = "[invalid UTF-8 in the output was replaced with U+FFFD]";

#[derive(Debug)]
struct Overflow {
    path: PathBuf,
    out: BufWriter<File>,
    written: usize,
    capped: bool,
}

/// The answer a [`Sink`] makes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Finished {
    /// The text for the model, with its notes.
    pub text: String,
    /// Lines of the output shown (a cut-short line counts as one).
    pub kept_lines: usize,
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
    cap: usize,
    dir: PathBuf,
    /// Head: the first `max + 1` bytes. Tail: at least the last `max + 1` bytes
    /// once the output passes `max`, and all of it before that.
    window: Vec<u8>,
    total: usize,
    newlines: usize,
    last: Option<u8>,
    overflow: Option<Overflow>,
}

impl Sink {
    #[must_use]
    pub fn new(limits: &Limits, keep: Keep) -> Self {
        Self {
            keep,
            max: limits.max_bytes,
            cap: limits.max_overflow,
            dir: limits.dir.clone(),
            window: Vec::new(),
            total: 0,
            newlines: 0,
            last: None,
            overflow: None,
        }
    }

    /// Take more output.
    ///
    /// # Errors
    /// The overflow file could not be created or written.
    pub fn push(&mut self, chunk: &[u8]) -> Result<()> {
        let Some(&last) = chunk.last() else {
            return Ok(());
        };
        if self.overflow.is_none() && self.total + chunk.len() > self.max {
            // The window still holds everything pushed so far (it holds
            // `2 * max` bytes at least, and the total has not passed `max`).
            self.overflow = Some(self.create_overflow()?);
        }
        if let Some(overflow) = &mut self.overflow {
            let take = (self.cap - overflow.written).min(chunk.len());
            overflow
                .out
                .write_all(&chunk[..take])
                .map_err(|e| write_failed(&overflow.path, &e))?;
            overflow.written += take;
            overflow.capped |= take < chunk.len();
        }
        self.total += chunk.len();
        self.newlines += newlines_in(chunk);
        self.last = Some(last);
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
        Ok(())
    }

    /// Open `self.dir` (created `0700` if absent) as a directory fd, refusing a
    /// symlink, and check with `fstat` on that fd that it is ours and private.
    fn open_private_dir(&self) -> Result<OwnedFd> {
        let shown = self.dir.display();
        DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&self.dir)
            .map_err(|e| {
                ToolError(format!(
                    "output exceeds {} bytes and the directory for the full output could not be \
                     created ({shown}): {e}",
                    self.max
                ))
            })?;
        let flags = OFlag::O_RDONLY | OFlag::O_DIRECTORY | OFlag::O_NOFOLLOW | OFlag::O_CLOEXEC;
        let fd = open(self.dir.as_path(), flags, Mode::empty()).map_err(|e| {
            let problem = match fs::symlink_metadata(&self.dir) {
                Ok(meta) if meta.file_type().is_symlink() => "is a symlink".to_owned(),
                Ok(meta) if !meta.is_dir() => "is not a directory".to_owned(),
                _ => format!("cannot be opened: {e}"),
            };
            self.dir_refused(&problem)
        })?;
        let stat = fstat(&fd).map_err(|e| {
            ToolError(format!(
                "cannot inspect the overflow directory {shown}: {e}"
            ))
        })?;
        match dir_problem(stat.st_uid, stat.st_mode) {
            Some(problem) => Err(self.dir_refused(&problem)),
            None => Ok(fd),
        }
    }

    fn dir_refused(&self, problem: &str) -> ToolError {
        ToolError(format!(
            "output exceeds {} bytes, but the overflow directory {} {problem}, so the full \
             output was not saved",
            self.max,
            self.dir.display()
        ))
    }

    fn create_overflow(&mut self) -> Result<Overflow> {
        let dir = self.open_private_dir()?;
        for _ in 0..ID_TRIES {
            let id: String = (0..ID_LEN)
                .map(|_| char::from(ID_CHARS[fastrand::usize(..ID_CHARS.len())]))
                .collect();
            let name = format!("{id}.out");
            let path = self.dir.join(&name);
            // Relative to the fd we checked, so a swapped path cannot redirect us.
            let flags = OFlag::O_WRONLY
                | OFlag::O_CREAT
                | OFlag::O_EXCL
                | OFlag::O_NOFOLLOW
                | OFlag::O_CLOEXEC;
            match openat(&dir, name.as_str(), flags, Mode::from_bits_truncate(0o600)) {
                Ok(fd) => {
                    let mut out = BufWriter::new(File::from(fd));
                    out.write_all(&self.window)
                        .map_err(|e| write_failed(&path, &e))?;
                    let written = self.window.len();
                    return Ok(Overflow {
                        path,
                        out,
                        written,
                        capped: false,
                    });
                }
                Err(Errno::EEXIST) => {}
                Err(e) => {
                    return Err(ToolError(format!(
                        "output exceeds {} bytes and the file for the full output could not be \
                         created ({}): {e}",
                        self.max,
                        path.display()
                    )));
                }
            }
        }
        Err(ToolError(format!(
            "output exceeds {} bytes and no unused file name was found in {}",
            self.max,
            self.dir.display()
        )))
    }

    /// The text for the model: all of the output if it fit, else the kept end
    /// and the truncation note.
    ///
    /// # Errors
    /// The overflow file could not be completed.
    pub fn finish(mut self) -> Result<Finished> {
        let Some(overflow) = self.overflow.take() else {
            let kept = self.window.as_slice();
            let text = self.compose(kept, None);
            return Ok(Finished {
                text,
                kept_lines: count_lines(kept),
                truncated: false,
                cut_line: false,
            });
        };
        let Overflow {
            path,
            mut out,
            capped,
            ..
        } = overflow;
        out.flush().map_err(|e| write_failed(&path, &e))?;
        drop(out);
        let (kept, cut_line) = match self.keep {
            Keep::Head => self.kept_head(),
            Keep::Tail => self.kept_tail(),
        };
        let total_lines = self.newlines + usize::from(self.last != Some(b'\n'));
        let kept_lines = count_lines(kept);
        let omitted = total_lines.saturating_sub(kept_lines);
        let mut note = format!("[truncated: {}", lines(omitted));
        note.push_str(match self.keep {
            Keep::Head => " omitted after these",
            Keep::Tail => " omitted before these",
        });
        if cut_line {
            note.push_str(", and one long line cut short");
        }
        let _ = if capped {
            write!(
                note,
                "; the output is {} bytes but {} holds only the first {}]",
                self.total,
                path.display(),
                size_text(self.cap)
            )
        } else {
            write!(
                note,
                "; full output ({} bytes) is in {}]",
                self.total,
                path.display()
            )
        };
        let text = self.compose(kept, Some(&note));
        Ok(Finished {
            text,
            kept_lines,
            truncated: true,
            cut_line,
        })
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

/// Check an overflow directory that exists already, by `lstat`: it must be a
/// directory (not a symlink), ours, and closed to others. One that does not
/// exist is fine; it is created `0700` when first needed.
///
/// # Errors
/// The directory is there and unfit, or cannot be inspected.
pub fn check_existing_dir(dir: &Path) -> Result<()> {
    let shown = dir.display();
    let meta = match fs::symlink_metadata(dir) {
        Ok(meta) => meta,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => {
            return Err(ToolError(format!(
                "cannot inspect the overflow directory {shown}: {e}"
            )));
        }
    };
    let problem = if meta.file_type().is_symlink() {
        Some("is a symlink".to_owned())
    } else if !meta.is_dir() {
        Some("is not a directory".to_owned())
    } else {
        dir_problem(meta.uid(), meta.mode())
    };
    match problem {
        Some(problem) => Err(ToolError(format!(
            "the overflow directory {shown} {problem}"
        ))),
        None => Ok(()),
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

fn write_failed(path: &Path, e: &std::io::Error) -> ToolError {
    ToolError(format!(
        "could not write the full output to {}: {e}",
        path.display()
    ))
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
mod tests {
    use super::*;

    fn limits(max: usize, dir: &tempfile::TempDir) -> Limits {
        Limits::in_dir(max, dir.path().join("tau")).unwrap()
    }

    fn run<C: AsRef<[u8]>>(keep: Keep, max: usize, chunks: &[C]) -> (String, tempfile::TempDir) {
        let dir = tempfile::tempdir().unwrap();
        let mut sink = Sink::new(&limits(max, &dir), keep);
        for chunk in chunks {
            sink.push(chunk.as_ref()).unwrap();
        }
        (sink.finish().unwrap().text, dir)
    }

    fn overflow_of(dir: &tempfile::TempDir) -> Vec<u8> {
        let mut files: Vec<_> = fs::read_dir(dir.path().join("tau")).unwrap().collect();
        assert_eq!(files.len(), 1);
        let path = files.pop().unwrap().unwrap().path();
        assert_eq!(path.extension().unwrap(), "out");
        assert_eq!(path.file_stem().unwrap().len(), ID_LEN);
        fs::read(path).unwrap()
    }

    #[test]
    fn within_the_limit_nothing_is_written() {
        let (text, dir) = run(Keep::Head, 10, &[b"abc\n", b"def\n"][..]);
        assert_eq!(text, "abc\ndef\n");
        assert!(!dir.path().join("tau").exists());
    }

    #[test]
    fn exactly_the_limit_is_not_truncated() {
        let (text, dir) = run(Keep::Tail, 8, &[b"abc\ndef\n"]);
        assert_eq!(text, "abc\ndef\n");
        assert!(!dir.path().join("tau").exists());
    }

    #[test]
    fn head_keeps_whole_lines_and_counts_the_rest() {
        let (text, dir) = run(Keep::Head, 10, &[b"aaaa\nbbbb\ncccc\ndddd\n"]);
        assert!(
            text.starts_with("aaaa\nbbbb\n[truncated: 2 lines omitted after these;"),
            "{text}"
        );
        assert!(text.contains("full output (20 bytes) is in "));
        assert_eq!(overflow_of(&dir), b"aaaa\nbbbb\ncccc\ndddd\n");
    }

    #[test]
    fn head_boundary_exactly_on_a_newline() {
        let (text, _dir) = run(Keep::Head, 9, &[b"aaaa\nbbbb\ncccc\n"]);
        assert!(
            text.starts_with("aaaa\nbbbb\n[truncated: 1 line omitted"),
            "{text}"
        );
    }

    #[test]
    fn tail_keeps_whole_lines_and_counts_the_rest() {
        let (text, dir) = run(Keep::Tail, 10, &[b"aaaa\nbbbb\ncccc\ndddd\n"]);
        assert!(
            text.starts_with("[truncated: 2 lines omitted before these;"),
            "{text}"
        );
        assert!(text.ends_with("]\ncccc\ndddd\n"), "{text}");
        assert_eq!(overflow_of(&dir), b"aaaa\nbbbb\ncccc\ndddd\n");
    }

    #[test]
    fn tail_with_a_last_line_lacking_a_newline() {
        let (text, _dir) = run(Keep::Tail, 10, &[b"aaaa\nbbbb\ncccc\ndddd"]);
        assert!(text.contains("2 lines omitted before"), "{text}");
        assert!(text.ends_with("]\ncccc\ndddd"), "{text}");
    }

    #[test]
    fn chunks_may_split_lines_and_the_file_equals_the_whole() {
        let whole = b"one\ntwo\nthree\nfour\nfive\nsix\n";
        let parts: Vec<&[u8]> = whole.chunks(3).collect();
        for keep in [Keep::Head, Keep::Tail] {
            let (text, dir) = run(keep, 12, &parts);
            assert_eq!(overflow_of(&dir), whole);
            assert!(text.contains("full output (28 bytes)"), "{text}");
        }
    }

    #[test]
    fn a_single_long_line_is_cut_on_a_character() {
        // 'é' is two bytes: a limit of 5 must not split the third one.
        let line = "ééééé\n".to_owned();
        for keep in [Keep::Head, Keep::Tail] {
            let (text, _dir) = run(keep, 5, &[line.as_bytes()]);
            assert!(text.contains("one long line cut short"), "{text}");
            assert!(std::str::from_utf8(text.as_bytes()).is_ok());
            assert!(!text.contains('\u{FFFD}'), "{text}");
        }
    }

    #[test]
    fn multibyte_text_across_lines_is_never_split() {
        let text = "日本語\n".repeat(20);
        for keep in [Keep::Head, Keep::Tail] {
            for max in 1..40 {
                let (out, _dir) = run(keep, max, &[text.as_bytes()]);
                assert!(!out.contains('\u{FFFD}'), "max {max}: {out}");
            }
        }
    }

    #[test]
    fn tail_keeps_the_end_when_the_window_has_been_trimmed() {
        let many: Vec<u8> = (0..1000)
            .flat_map(|i| format!("line {i}\n").into_bytes())
            .collect();
        let chunks: Vec<&[u8]> = many.chunks(7).collect();
        let (text, dir) = run(Keep::Tail, 50, &chunks);
        assert!(text.ends_with("line 999\n"), "{text}");
        assert_eq!(overflow_of(&dir), many);
        let kept = text.split_once("]\n").unwrap().1;
        assert!(kept.len() <= 50);
        let first: usize = kept
            .lines()
            .next()
            .unwrap()
            .strip_prefix("line ")
            .unwrap()
            .parse()
            .unwrap();
        assert!(
            text.contains(&format!("{first} lines omitted before")),
            "{text}"
        );
    }

    #[test]
    fn an_unwritable_overflow_directory_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let blocker = dir.path().join("blocker");
        // Fine when the tool is built; a file takes the directory's place after.
        let limits = Limits::in_dir(4, blocker.join("tau")).unwrap();
        fs::write(&blocker, "x").unwrap();
        let mut sink = Sink::new(&limits, Keep::Head);
        let err = sink.push(b"too long for four").unwrap_err();
        assert!(err.0.contains("could not be created"), "{err}");
    }

    #[test]
    fn a_zero_limit_is_refused() {
        assert!(Limits::new(0).is_err());
    }

    #[test]
    fn the_directory_is_private_and_the_file_is_0600() {
        use std::os::unix::fs::PermissionsExt;
        let (_text, dir) = run(Keep::Head, 5, &[b"too long for five"]);
        let tau = dir.path().join("tau");
        assert_eq!(
            fs::metadata(&tau).unwrap().permissions().mode() & 0o777,
            0o700
        );
        let file = fs::read_dir(&tau).unwrap().next().unwrap().unwrap().path();
        assert_eq!(
            fs::metadata(file).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }

    #[test]
    fn a_directory_open_to_others_is_refused_at_construction_and_at_use() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let open = dir.path().join("open");
        fs::create_dir(&open).unwrap();
        fs::set_permissions(&open, fs::Permissions::from_mode(0o755)).unwrap();
        let err = Limits::in_dir(4, open.clone()).unwrap_err();
        assert!(err.0.contains("has mode 0755"), "{err}");
        // Opened up after the tool was built:
        let later = dir.path().join("later");
        fs::create_dir(&later).unwrap();
        fs::set_permissions(&later, fs::Permissions::from_mode(0o700)).unwrap();
        let limits = Limits::in_dir(4, later.clone()).unwrap();
        fs::set_permissions(&later, fs::Permissions::from_mode(0o755)).unwrap();
        let mut sink = Sink::new(&limits, Keep::Head);
        let err = sink.push(b"too long for four").unwrap_err();
        assert!(err.0.contains("has mode 0755"), "{err}");
        assert_eq!(fs::read_dir(&later).unwrap().count(), 0, "no fallback file");
    }

    #[test]
    fn a_symlink_as_the_directory_is_refused_at_construction_and_at_use() {
        let dir = tempfile::tempdir().unwrap();
        let real = dir.path().join("real");
        fs::create_dir(&real).unwrap();
        let link = dir.path().join("link");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        let err = Limits::in_dir(4, link.clone()).unwrap_err();
        assert!(err.0.contains("is a symlink"), "{err}");
        // Swapped for a symlink after the tool was built:
        let swapped = dir.path().join("swapped");
        let limits = Limits::in_dir(4, swapped.clone()).unwrap();
        std::os::unix::fs::symlink(&real, &swapped).unwrap();
        let mut sink = Sink::new(&limits, Keep::Head);
        let err = sink.push(b"too long for four").unwrap_err();
        assert!(err.0.contains("is a symlink"), "{err}");
        assert_eq!(fs::read_dir(&real).unwrap().count(), 0);
    }

    #[test]
    fn the_overflow_file_is_capped_but_counting_goes_on() {
        let dir = tempfile::tempdir().unwrap();
        let limits = limits(10, &dir).with_max_overflow(25).unwrap();
        let mut sink = Sink::new(&limits, Keep::Head);
        let text =
            "line 0\nline 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\n"
                .to_owned();
        for chunk in text.as_bytes().chunks(7) {
            sink.push(chunk).unwrap();
        }
        let finished = sink.finish().unwrap();
        assert_eq!(overflow_of(&dir), &text.as_bytes()[..25]);
        assert!(
            finished
                .text
                .starts_with("line 0\n[truncated: 9 lines omitted after these;")
        );
        assert!(
            finished.text.contains("the output is 70 bytes but ")
                && finished.text.contains("holds only the first 25 bytes]"),
            "{}",
            finished.text
        );
    }

    #[test]
    fn the_cap_note_speaks_in_mib_when_it_can() {
        let dir = tempfile::tempdir().unwrap();
        let limits = limits(10, &dir).with_max_overflow(2 * MIB).unwrap();
        let mut sink = Sink::new(&limits, Keep::Tail);
        sink.push(&vec![b'x'; 3 * MIB]).unwrap();
        assert!(
            sink.finish()
                .unwrap()
                .text
                .contains("holds only the first 2 MiB]")
        );
    }

    #[test]
    fn a_cap_below_the_limit_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        assert!(limits(100, &dir).with_max_overflow(99).is_err());
    }

    #[test]
    fn invalid_utf8_is_replaced_and_said_so_on_the_side_of_the_omission() {
        let (text, _dir) = run(Keep::Head, 100, &[b"caf\xe9\n"]);
        assert_eq!(text, format!("caf\u{FFFD}\n\n{LOSSY_NOTE}"));
        let (text, _dir) = run(Keep::Tail, 100, &[b"caf\xe9\n"]);
        assert_eq!(text, format!("{LOSSY_NOTE}\ncaf\u{FFFD}\n"));
    }

    #[test]
    fn finish_reports_the_lines_kept() {
        let dir = tempfile::tempdir().unwrap();
        let mut sink = Sink::new(&limits(10, &dir), Keep::Head);
        sink.push(b"aaaa\nbbbb\ncccc\n").unwrap();
        let finished = sink.finish().unwrap();
        assert!(finished.truncated);
        assert_eq!(finished.kept_lines, 2);
    }
}
