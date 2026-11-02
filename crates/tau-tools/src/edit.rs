//! `edit`: an exact search and replace in one file, answered with a unified diff.
//!
//! One edit per call. The text to find must occur exactly once unless `replace_all` is
//! set: no occurrence, or several without `replace_all`, is an error that says how to fix
//! the call, and the file is not touched. The match is exact, bytes and all, with one
//! rule for line endings: in a file whose every line ends in CRLF, the `\n` in the model's
//! text stand for CRLF (the model cannot see the `\r`). Any other file is matched as it is.
//! The rest of the file, a missing final newline included, is kept byte for byte.
//!
//! Edits of one file wait for each other (a process-wide lock on the canonical path, held from
//! the read to the rename), so two in one step, which run in parallel, both land.
//!
//! The file is written the way `write` writes: a new file beside it, renamed over it, a
//! symlink followed, permission bits kept. A file that is not UTF-8 is refused.

use std::borrow::Cow;
use std::fmt::Write as _;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Condvar, Mutex, PoisonError};

use serde::Deserialize;
use serde_json::{Value, json};

use crate::diff::{Change, unified};
use crate::limit::{Keep, Sink};
use crate::write::{follow_symlinks, replace};
use crate::{Limits, Result, ToolError, parse_args, resolve};

/// The files being edited in this process, by canonical path: a step's tool calls run in
/// parallel, and two edits of one file would each read the old text and rename their own
/// result over the file, losing one change.
static EDITING: (Mutex<Vec<PathBuf>>, Condvar) = (Mutex::new(Vec::new()), Condvar::new());

/// Held while one file is edited, from its read to its rename.
struct Editing(PathBuf);

impl Editing {
    /// Wait until no other edit holds `path`, then hold it.
    fn of(path: PathBuf) -> Self {
        let (held, released) = &EDITING;
        let mut held = released
            .wait_while(held.lock().unwrap_or_else(PoisonError::into_inner), |h| {
                h.contains(&path)
            })
            .unwrap_or_else(PoisonError::into_inner);
        held.push(path.clone());
        Self(path)
    }
}

impl Drop for Editing {
    fn drop(&mut self) {
        let (held, released) = &EDITING;
        held.lock()
            .unwrap_or_else(PoisonError::into_inner)
            .retain(|p| *p != self.0);
        released.notify_all();
    }
}

/// Line numbers named in an ambiguity error before it says "and N more".
const LINES_SHOWN: usize = 5;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Args {
    path: String,
    old_string: String,
    new_string: String,
    replace_all: Option<bool>,
}

#[derive(Clone, Debug)]
pub struct Edit {
    cwd: PathBuf,
    limits: Limits,
}

impl Edit {
    #[must_use]
    pub fn new(cwd: PathBuf, limits: Limits) -> Self {
        Self { cwd, limits }
    }

    #[must_use]
    pub fn description(&self) -> String {
        format!(
            "Replace exact text in an existing file. `old_string` must match the file exactly, \
             whitespace and line breaks included, and occur once: if it occurs more often the \
             call fails and names the lines, so add the lines around the one to change, or set \
             `replace_all` to change every occurrence. One replacement per call. In a file whose \
             lines all end in CRLF, write line breaks as \\n and they are matched and written as \
             CRLF. The rest of the file is kept as it is, a missing final newline included. The \
             write is atomic, like `write`'s: a symlink is followed and permission bits are kept. \
             Answers a unified diff of the change; past {} bytes it is cut after the last whole \
             line that fits, and the full diff is saved to a file named in a note. Relative \
             paths are relative to the working directory, which is a default, not a sandbox: \
             absolute paths and `..` work.",
            self.limits.max_bytes
        )
    }

    #[must_use]
    pub fn parameters() -> Value {
        json!({
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path of the file to edit (relative to the working directory, or absolute)"
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to find (not empty)"
                },
                "new_string": {
                    "type": "string",
                    "description": "The text to put in its place (may be empty, to delete)"
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence of old_string (default false: it must occur once)"
                }
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": false
        })
    }

    /// # Errors
    /// Bad arguments, a file that is missing, not a file or not UTF-8, text that is not found or
    /// is ambiguous, or any IO failure.
    pub fn run(&self, args_raw: &str) -> Result<String> {
        let args: Args = parse_args(args_raw)?;
        check(&args)?;
        let shown = args.path.as_str();
        let target = follow_symlinks(&resolve(&self.cwd, shown)).map_err(|e| failed(shown, &e))?;
        // A path that does not resolve is held by its own name; `load` then says why it fails.
        let _editing = Editing::of(fs::canonicalize(&target).unwrap_or_else(|_| target.clone()));
        let old = Self::load(&target, shown)?;
        let (find, with) = if all_crlf(&old) {
            (crlf(&args.old_string), crlf(&args.new_string))
        } else {
            (
                Cow::Borrowed(args.old_string.as_str()),
                Cow::Borrowed(args.new_string.as_str()),
            )
        };
        let found: Vec<usize> = old.match_indices(find.as_ref()).map(|(at, _)| at).collect();
        check_found(shown, &old, &found, args.replace_all.unwrap_or(false))?;
        let (new, changes) = substituted(&old, &found, &find, &with);
        let parent = target.parent().unwrap_or_else(|| Path::new("."));
        replace(&target, parent, new.as_bytes()).map_err(|e| failed(shown, &e))?;
        let n = changes.len();
        let mut sink = Sink::new(&self.limits, Keep::Head);
        let head = format!(
            "Replaced {n} occurrence{} in {shown}\n",
            if n == 1 { "" } else { "s" }
        );
        sink.push(head.as_bytes());
        sink.push(unified(shown, &old, &new, &changes).as_bytes());
        Ok(sink.finish().text)
    }

    /// The text of the file `shown` names, which is `target` once its symlinks are followed.
    fn load(target: &Path, shown: &str) -> Result<String> {
        let meta = fs::metadata(target).map_err(|e| match e.kind() {
            std::io::ErrorKind::NotFound => ToolError(format!(
                "file not found: {shown} (edit changes an existing file; write creates one)"
            )),
            _ => failed(shown, &e),
        })?;
        if meta.is_dir() {
            return Err(format!("{shown} is a directory, not a file").into());
        }
        if !meta.is_file() {
            return Err(format!("{shown} is not a regular file").into());
        }
        let bytes = fs::read(target).map_err(|e| failed(shown, &e))?;
        let text = String::from_utf8(bytes).map_err(|e| {
            ToolError(format!(
                "{shown} is not valid UTF-8 (the first bad byte is at offset {}); edit works on \
                 UTF-8 text",
                e.utf8_error().valid_up_to()
            ))
        })?;
        Ok(text)
    }
}

fn failed(shown: &str, e: &std::io::Error) -> ToolError {
    ToolError(format!("cannot edit {shown}: {e}"))
}

/// Whether the text found, at `found`, is one place, or several and `all` says to change them.
fn check_found(shown: &str, old: &str, found: &[usize], all: bool) -> Result<()> {
    if found.is_empty() {
        return Err(format!(
            "old_string not found in {shown}: it must match the file exactly, whitespace and \
             line breaks included (read the file again and copy the text)"
        )
        .into());
    }
    if found.len() > 1 && !all {
        return Err(ambiguous(shown, old, found));
    }
    Ok(())
}

fn check(args: &Args) -> Result<()> {
    if args.path.is_empty() {
        return Err("`path` must not be empty".into());
    }
    if args.old_string.is_empty() {
        return Err("`old_string` must not be empty".into());
    }
    if args.old_string == args.new_string {
        return Err(
            "`old_string` and `new_string` are the same: there is nothing to change".into(),
        );
    }
    Ok(())
}

/// Whether the text has line breaks and every one of them is a CRLF.
fn all_crlf(text: &str) -> bool {
    let bytes = text.as_bytes();
    let mut breaks = bytes.iter().enumerate().filter(|(_, b)| **b == b'\n');
    breaks.clone().next().is_some() && breaks.all(|(i, _)| i > 0 && bytes[i - 1] == b'\r')
}

/// `text` with every line break a CRLF.
fn crlf(text: &str) -> Cow<'_, str> {
    if text.contains('\n') {
        Cow::Owned(text.replace("\r\n", "\n").replace('\n', "\r\n"))
    } else {
        Cow::Borrowed(text)
    }
}

/// `old` with `find` at each offset of `at` replaced by `with`, and where each replacement is.
fn substituted(old: &str, at: &[usize], find: &str, with: &str) -> (String, Vec<Change>) {
    let mut new = String::with_capacity(old.len());
    let mut changes = Vec::with_capacity(at.len());
    let mut done = 0;
    for &start in at {
        new.push_str(&old[done..start]);
        let from = new.len();
        new.push_str(with);
        changes.push(Change {
            old: start..start + find.len(),
            new: from..new.len(),
        });
        done = start + find.len();
    }
    new.push_str(&old[done..]);
    (new, changes)
}

fn ambiguous(shown: &str, old: &str, found: &[usize]) -> ToolError {
    let mut line = 1;
    let mut seen = 0;
    let mut numbers = Vec::new();
    for &at in found.iter().take(LINES_SHOWN) {
        line += old[seen..at].matches('\n').count();
        seen = at;
        numbers.push(line.to_string());
    }
    let mut named = numbers.join(", ");
    if found.len() > LINES_SHOWN {
        let _ = write!(named, ", and {} more", found.len() - LINES_SHOWN);
    }
    ToolError(format!(
        "old_string occurs {} times in {shown} (lines {named}): add the lines around the one to \
         change to make it unique, or set replace_all to change every occurrence",
        found.len()
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn setup() -> (Edit, tempfile::TempDir) {
        let dir = tempfile::tempdir().unwrap();
        let limits = Limits::in_dir(10_000, dir.path().join(".out")).unwrap();
        (Edit::new(dir.path().to_owned(), limits), dir)
    }

    fn edit(tool: &Edit, path: &str, old: &str, new: &str) -> Result<String> {
        tool.run(&json!({"path": path, "old_string": old, "new_string": new}).to_string())
    }

    #[test]
    fn replaces_once_and_answers_the_diff() {
        let (tool, dir) = setup();
        fs::write(dir.path().join("a.txt"), "one\ntwo\nthree\n").unwrap();
        let out = edit(&tool, "a.txt", "two", "2").unwrap();
        assert_eq!(
            out,
            "Replaced 1 occurrence in a.txt\n--- a.txt\n+++ a.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+2\n three"
        );
        assert_eq!(
            fs::read_to_string(dir.path().join("a.txt")).unwrap(),
            "one\n2\nthree\n"
        );
    }

    #[test]
    fn not_found_and_ambiguous_change_nothing_and_say_how_to_fix_it() {
        let (tool, dir) = setup();
        fs::write(dir.path().join("a.txt"), "hit\nx\nhit\n").unwrap();
        let err = edit(&tool, "a.txt", "miss", "y").unwrap_err().0;
        assert!(err.starts_with("old_string not found in a.txt: "), "{err}");
        let err = edit(&tool, "a.txt", "hit", "y").unwrap_err().0;
        assert!(
            err.starts_with("old_string occurs 2 times in a.txt (lines 1, 3): "),
            "{err}"
        );
        assert!(err.contains("replace_all"), "{err}");
        assert_eq!(
            fs::read_to_string(dir.path().join("a.txt")).unwrap(),
            "hit\nx\nhit\n"
        );
    }

    #[test]
    fn replace_all_changes_every_occurrence() {
        let (tool, dir) = setup();
        fs::write(dir.path().join("a.txt"), "hit\nx\nhit\n").unwrap();
        let args =
            json!({"path": "a.txt", "old_string": "hit", "new_string": "y", "replace_all": true});
        let out = tool.run(&args.to_string()).unwrap();
        assert!(
            out.starts_with("Replaced 2 occurrences in a.txt\n"),
            "{out}"
        );
        assert_eq!(
            fs::read_to_string(dir.path().join("a.txt")).unwrap(),
            "y\nx\ny\n"
        );
    }

    #[test]
    fn crlf_is_kept_when_every_line_has_it_and_not_guessed_otherwise() {
        let (tool, dir) = setup();
        let path = dir.path().join("a.txt");
        fs::write(&path, b"a\r\nb\r\nc\r\n").unwrap();
        edit(&tool, "a.txt", "a\nb\n", "A\nB\nB2\n").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"A\r\nB\r\nB2\r\nc\r\n");
        fs::write(&path, b"a\r\nb\nc\r\n").unwrap();
        assert!(edit(&tool, "a.txt", "a\nb", "x").is_err());
        edit(&tool, "a.txt", "b\n", "B\n").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"a\r\nB\nc\r\n");
    }

    #[test]
    fn a_missing_final_newline_stays_missing() {
        let (tool, dir) = setup();
        let path = dir.path().join("a.txt");
        fs::write(&path, "a\nb").unwrap();
        edit(&tool, "a.txt", "b", "c").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"a\nc");
    }

    #[test]
    fn the_file_must_exist_be_a_file_and_be_utf8() {
        let (tool, dir) = setup();
        fs::create_dir(dir.path().join("d")).unwrap();
        fs::write(dir.path().join("bin"), b"caf\xe9").unwrap();
        let err = |path| edit(&tool, path, "a", "b").unwrap_err().0;
        assert!(err("nope").starts_with("file not found: nope"));
        assert_eq!(err("d"), "d is a directory, not a file");
        assert!(
            err("bin").starts_with("bin is not valid UTF-8 (the first bad byte is at offset 3)")
        );
        assert_eq!(fs::read(dir.path().join("bin")).unwrap(), b"caf\xe9");
    }

    #[test]
    fn bad_arguments_are_errors() {
        let (tool, _dir) = setup();
        let err = |args: Value| tool.run(&args.to_string()).unwrap_err().0;
        assert!(err(json!({"path": "", "old_string": "a", "new_string": "b"})).contains("`path`"));
        assert!(
            err(json!({"path": "p", "old_string": "", "new_string": "b"}))
                .contains("`old_string` must not be empty")
        );
        assert!(
            err(json!({"path": "p", "old_string": "a", "new_string": "a"})).contains("the same")
        );
        assert!(
            err(json!({"path": "p", "old_string": "a"})).contains("missing field `new_string`")
        );
    }

    #[test]
    fn many_occurrences_name_the_first_few_lines() {
        let (tool, dir) = setup();
        fs::write(dir.path().join("a.txt"), "hit\n".repeat(9)).unwrap();
        let err = edit(&tool, "a.txt", "hit", "y").unwrap_err().0;
        assert!(err.contains("(lines 1, 2, 3, 4, 5, and 4 more)"), "{err}");
    }

    #[test]
    fn a_long_diff_is_cut_and_saved() {
        let dir = tempfile::tempdir().unwrap();
        let limits = Limits::in_dir(100, dir.path().join(".out")).unwrap();
        let tool = Edit::new(dir.path().to_owned(), limits);
        fs::write(dir.path().join("a.txt"), "x\n".repeat(100)).unwrap();
        let args =
            json!({"path": "a.txt", "old_string": "x", "new_string": "y", "replace_all": true});
        let out = tool.run(&args.to_string()).unwrap();
        assert!(
            out.starts_with("Replaced 100 occurrences in a.txt\n--- a.txt"),
            "{out}"
        );
        assert!(out.contains("[truncated: "), "{out}");
        assert!(out.contains("omitted after these"), "{out}");
    }
}
