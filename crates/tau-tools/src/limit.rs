//! Output limiting: keep one end of the output, spill the whole of it to a file.
//!
//! A [`Sink`] takes the output in chunks. While the total stays within the
//! limit nothing is written anywhere. At the first byte past it, the output so
//! far and everything after goes to `<dir>/<id>.out`, and memory holds only the
//! window the kept text comes from. [`Sink::finish`] cuts that window on a line
//! boundary and adds the truncation note.

use std::fmt::Write as _;
use std::fs::{self, File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::PathBuf;

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

#[derive(Debug)]
struct Overflow {
    path: PathBuf,
    out: BufWriter<File>,
}

/// Output being limited. See the module docs.
#[derive(Debug)]
pub struct Sink {
    keep: Keep,
    max: usize,
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
            overflow
                .out
                .write_all(chunk)
                .map_err(|e| write_failed(&overflow.path, &e))?;
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

    fn create_overflow(&mut self) -> Result<Overflow> {
        fs::create_dir_all(&self.dir).map_err(|e| {
            ToolError(format!(
                "output exceeds {} bytes and the directory for the full output could not be \
                 created ({}): {e}",
                self.max,
                self.dir.display()
            ))
        })?;
        for _ in 0..ID_TRIES {
            let id: String = (0..ID_LEN)
                .map(|_| char::from(ID_CHARS[fastrand::usize(..ID_CHARS.len())]))
                .collect();
            let path = self.dir.join(format!("{id}.out"));
            match OpenOptions::new().write(true).create_new(true).open(&path) {
                Ok(file) => {
                    let mut out = BufWriter::new(file);
                    out.write_all(&self.window)
                        .map_err(|e| write_failed(&path, &e))?;
                    return Ok(Overflow { path, out });
                }
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
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
    pub fn finish(mut self) -> Result<String> {
        let Some(overflow) = self.overflow.take() else {
            return Ok(String::from_utf8_lossy(&self.window).into_owned());
        };
        let Overflow { path, mut out } = overflow;
        out.flush().map_err(|e| write_failed(&path, &e))?;
        drop(out);
        let (kept, cut_line) = match self.keep {
            Keep::Head => self.kept_head(),
            Keep::Tail => self.kept_tail(),
        };
        let total_lines = self.newlines + usize::from(self.last != Some(b'\n'));
        let omitted = total_lines.saturating_sub(count_lines(kept));
        let kept = String::from_utf8_lossy(kept);
        let mut note = format!("[truncated: {}", lines(omitted));
        note.push_str(match self.keep {
            Keep::Head => " omitted after these",
            Keep::Tail => " omitted before these",
        });
        if cut_line {
            note.push_str(", and one long line cut short");
        }
        let _ = write!(
            note,
            "; full output ({} bytes) is in {}]",
            self.total,
            path.display()
        );
        Ok(match self.keep {
            Keep::Head => format!("{kept}\n{note}"),
            Keep::Tail => format!("{note}\n{kept}"),
        })
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

#[allow(clippy::naive_bytecount)] // one pass over a chunk; not worth a dependency
fn newlines_in(bytes: &[u8]) -> usize {
    bytes.iter().filter(|&&b| b == b'\n').count()
}

fn is_continuation(byte: u8) -> bool {
    byte & 0xC0 == 0x80
}

fn write_failed(path: &std::path::Path, e: &std::io::Error) -> ToolError {
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
        (sink.finish().unwrap(), dir)
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
        fs::write(&blocker, "x").unwrap();
        let limits = Limits::in_dir(4, blocker.join("tau")).unwrap();
        let mut sink = Sink::new(&limits, Keep::Head);
        let err = sink.push(b"too long for four").unwrap_err();
        assert!(err.0.contains("could not be created"), "{err}");
    }

    #[test]
    fn a_zero_limit_is_refused() {
        assert!(Limits::new(0).is_err());
    }
}
