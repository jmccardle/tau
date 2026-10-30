//! `grep`: a regex over files, ripgrep's engine and ripgrep's walk.
//!
//! A directory is walked in parallel, honoring `.gitignore` (whether or not the
//! tree is a git repository), `.ignore`, and skipping hidden files, as `rg`
//! does. Files that look binary are skipped. Matches come out sorted by path
//! and line, so the same search gives the same answer. Paths are shown
//! relative to the tool's working directory.
//!
//! Memory is bounded: a matching line is cut at [`MAX_LINE`] bytes, and the
//! search stops once the matches it holds reach the overflow cap.

use std::fmt::Write as _;
use std::fs::File;
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Mutex, PoisonError};

use globset::{GlobBuilder, GlobMatcher};
use grep_regex::RegexMatcherBuilder;
use grep_searcher::{
    BinaryDetection, Searcher, SearcherBuilder, Sink as SearchSink, SinkFinish, SinkMatch,
};
use ignore::WalkBuilder;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::limit::{Keep, LOSSY_NOTE, Sink};
use crate::{Cancel, Limits, Result, ToolError, lock, parse_args, resolve};

/// The most bytes of one matching line that are shown.
pub const MAX_LINE: usize = 500;
/// The most memory the searcher may use for one line; a file with a longer
/// line is not searched (it lands in the could-not-search note).
const HEAP_LIMIT: usize = 16 * 1024 * 1024;
/// Names listed in a note before it says "and N more".
const NAMES_SHOWN: usize = 5;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Args {
    pattern: String,
    path: Option<String>,
    files: Option<Vec<String>>,
    ignore_case: Option<bool>,
    glob: Option<String>,
}

/// The lines of one file that matched.
struct Hits {
    path: String,
    lines: Vec<(u64, String)>,
}

/// A file whose search ended at a NUL byte.
struct Binary {
    path: String,
    offset: u64,
    had_matches: bool,
}

#[derive(Clone, Debug)]
pub struct Grep {
    cwd: PathBuf,
    limits: Limits,
}

impl Grep {
    #[must_use]
    pub fn new(cwd: PathBuf, limits: Limits) -> Self {
        Self { cwd, limits }
    }

    #[must_use]
    pub fn description(&self) -> String {
        format!(
            "Search files for a regular expression (Rust regex syntax). Answers one \
             `path:line:text` per matching line, paths relative to the working directory, sorted. \
             Searches a directory recursively, skipping hidden files, files ignored by \
             .gitignore, and binary files (counted in a note). Lines are cut at {MAX_LINE} bytes. \
             Output past {} bytes is cut after the last whole line that fits; the full result is \
             then saved to a file named in a note. Relative paths are relative to the working directory, which is a default, not a sandbox: absolute paths and `..` work.",
            self.limits.max_bytes
        )
    }

    #[must_use]
    pub fn parameters() -> Value {
        json!({
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression to search for"},
                "path": {
                    "type": "string",
                    "description": "File or directory to search (default: the working directory)"
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files to search, searched as named even if ignored (overrides path)"
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "Match case-insensitively (default false)"
                },
                "glob": {
                    "type": "string",
                    "description": "Only search files of a directory whose name matches this glob, \
                                    e.g. \"*.rs\"; a glob with a / matches the path below the searched \
                                    directory. Ignore rules still apply."
                }
            },
            "required": ["pattern"],
            "additionalProperties": false
        })
    }

    /// # Errors
    /// Bad arguments or regex or glob, a path that is missing, a cancel, or a
    /// failure to write the overflow file.
    pub fn run(&self, args_raw: &str, cancel: &Cancel) -> Result<String> {
        let args: Args = parse_args(args_raw)?;
        if args.pattern.is_empty() {
            return Err("`pattern` must not be empty".into());
        }
        let matcher = RegexMatcherBuilder::new()
            .case_insensitive(args.ignore_case.unwrap_or(false))
            .build(&args.pattern)
            .map_err(|e| ToolError(format!("invalid regex: {e}")))?;
        let search = Search::new(matcher, &self.cwd, self.limits.max_overflow, cancel);
        let named = match &args.files {
            Some(files) if files.is_empty() => return Err("`files` must not be empty".into()),
            Some(files) => {
                if args.glob.is_some() {
                    return Err(
                        "`glob` filters a directory walk and cannot be used with `files`".into(),
                    );
                }
                self.search_named(&search, files)?;
                true
            }
            None => self.search_path(&search, &args)?,
        };
        cancel.check()?;
        self.render(search, named)
    }

    fn search_named(&self, search: &Search, files: &[String]) -> Result<()> {
        let mut searcher = searcher();
        for name in files {
            let path = clean(&resolve(&self.cwd, name));
            if !path.is_file() {
                return Err(format!("not a file: {name}").into());
            }
            search.file(&mut searcher, &path);
        }
        Ok(())
    }

    /// Search `path`; true if it named a single file.
    fn search_path(&self, search: &Search, args: &Args) -> Result<bool> {
        let given = args.path.as_deref().unwrap_or(".");
        let root = clean(&resolve(&self.cwd, given));
        if root.is_file() {
            if args.glob.is_some() {
                return Err("`glob` filters a directory walk, but `path` is a file".into());
            }
            search.file(&mut searcher(), &root);
            return Ok(true);
        }
        if !root.is_dir() {
            return Err(format!("path not found: {given}").into());
        }
        let glob = args.glob.as_deref().map(compile_glob).transpose()?;
        let mut walk = WalkBuilder::new(&root);
        // .gitignore applies in any tree, not only inside a git repository.
        walk.require_git(false);
        let (root, glob) = (&root, &glob);
        walk.build_parallel().run(|| {
            let mut searcher = searcher();
            Box::new(move |found| {
                if search.should_stop() {
                    return ignore::WalkState::Quit;
                }
                match found {
                    Ok(item) if item.file_type().is_some_and(|t| t.is_file()) => {
                        if glob_allows(glob.as_ref(), root, item.path()) {
                            search.file(&mut searcher, item.path());
                        }
                    }
                    Ok(_) => {}
                    Err(e) => lock(&search.unreadable).push(e.to_string()),
                }
                ignore::WalkState::Continue
            })
        });
        Ok(false)
    }

    fn render(&self, search: Search, named: bool) -> Result<String> {
        let mut all = into_data(search.found);
        let searched = search.searched.into_inner();
        all.sort_by(|a, b| a.path.cmp(&b.path));
        let mut sink = Sink::new(&self.limits, Keep::Head);
        let mut first = true;
        for hits in &all {
            for (number, text) in &hits.lines {
                let line = format!(
                    "{}{}:{number}:{text}",
                    if first { "" } else { "\n" },
                    hits.path
                );
                first = false;
                sink.push(line.as_bytes())?;
            }
        }
        let mut text = if first {
            format!("No matches found in {searched} file(s)")
        } else {
            sink.finish()?.text
        };
        let mut notes = Vec::new();
        let mut binary = into_data(search.binary);
        binary.sort_by(|a, b| a.path.cmp(&b.path));
        let skipped: Vec<&str> = binary
            .iter()
            .filter(|b| !b.had_matches)
            .map(|b| b.path.as_str())
            .collect();
        if !skipped.is_empty() {
            notes.push(if named {
                format!("[skipped binary files: {}]", names(&skipped))
            } else {
                format!("[skipped {} binary file(s)]", skipped.len())
            });
        }
        for b in binary.iter().filter(|b| b.had_matches).take(NAMES_SHOWN) {
            notes.push(format!(
                "[matching in {} stopped at a NUL byte at offset {}; the rest of the file was not searched]",
                b.path, b.offset
            ));
        }
        if search.lossy.into_inner() {
            notes.push(LOSSY_NOTE.replace("the output", "matching lines"));
        }
        if search.stopped.into_inner() {
            notes.push(format!(
                "[search stopped: the matches reached {} bytes; there are more, and some files were not searched]",
                self.limits.max_overflow
            ));
        }
        let mut unreadable = into_data(search.unreadable);
        unreadable.sort();
        if let Some(example) = unreadable.first() {
            notes.push(format!(
                "[{} paths could not be searched; first: {example}]",
                unreadable.len()
            ));
        }
        for note in notes {
            let _ = write!(text, "\n{note}");
        }
        Ok(text)
    }
}

fn names(paths: &[&str]) -> String {
    let mut out = paths
        .iter()
        .take(NAMES_SHOWN)
        .copied()
        .collect::<Vec<_>>()
        .join(", ");
    if paths.len() > NAMES_SHOWN {
        let _ = write!(out, ", and {} more", paths.len() - NAMES_SHOWN);
    }
    out
}

/// What every search thread shares.
struct Search<'a> {
    matcher: grep_regex::RegexMatcher,
    cwd: &'a Path,
    cap: usize,
    cancel: &'a Cancel,
    searched: AtomicUsize,
    /// Bytes of matches held, roughly; the search stops at `cap`.
    held: AtomicUsize,
    stopped: AtomicBool,
    lossy: AtomicBool,
    found: Mutex<Vec<Hits>>,
    binary: Mutex<Vec<Binary>>,
    unreadable: Mutex<Vec<String>>,
}

impl<'a> Search<'a> {
    fn new(
        matcher: grep_regex::RegexMatcher,
        cwd: &'a Path,
        cap: usize,
        cancel: &'a Cancel,
    ) -> Self {
        Self {
            matcher,
            cwd,
            cap,
            cancel,
            searched: AtomicUsize::new(0),
            held: AtomicUsize::new(0),
            stopped: AtomicBool::new(false),
            lossy: AtomicBool::new(false),
            found: Mutex::new(Vec::new()),
            binary: Mutex::new(Vec::new()),
            unreadable: Mutex::new(Vec::new()),
        }
    }

    fn should_stop(&self) -> bool {
        self.cancel.is_cancelled() || self.stopped.load(Ordering::Relaxed)
    }

    fn file(&self, searcher: &mut Searcher, path: &Path) {
        let shown = path
            .strip_prefix(self.cwd)
            .unwrap_or(path)
            .to_string_lossy()
            .into_owned();
        let mut collect = Collect {
            search: self,
            shown: &shown,
            lines: Vec::new(),
            binary_at: None,
        };
        let opened = File::open(path).map(|inner| CancelReader {
            inner,
            cancel: self.cancel,
        });
        let result =
            opened.and_then(|reader| searcher.search_reader(&self.matcher, reader, &mut collect));
        match result {
            Ok(()) => {
                self.searched.fetch_add(1, Ordering::Relaxed);
                let Collect {
                    lines, binary_at, ..
                } = collect;
                let had_matches = !lines.is_empty();
                if let Some(offset) = binary_at {
                    lock(&self.binary).push(Binary {
                        path: shown.clone(),
                        offset,
                        had_matches,
                    });
                }
                if had_matches {
                    lock(&self.found).push(Hits { path: shown, lines });
                }
            }
            Err(e) => lock(&self.unreadable).push(format!("{shown}: {e}")),
        }
    }
}

/// The matches of one file, as the searcher finds them.
struct Collect<'a, 'b> {
    search: &'a Search<'b>,
    shown: &'a str,
    lines: Vec<(u64, String)>,
    binary_at: Option<u64>,
}

impl SearchSink for Collect<'_, '_> {
    type Error = std::io::Error;

    fn matched(
        &mut self,
        _searcher: &Searcher,
        found: &SinkMatch<'_>,
    ) -> std::result::Result<bool, std::io::Error> {
        if self.search.should_stop() {
            return Ok(false);
        }
        let bytes = found.bytes();
        let bytes = bytes.strip_suffix(b"\n").unwrap_or(bytes);
        let bytes = bytes.strip_suffix(b"\r").unwrap_or(bytes);
        let (text, lossy) = cut_line(bytes);
        if lossy {
            self.search.lossy.store(true, Ordering::Relaxed);
        }
        let cost = text.len() + self.shown.len() + 12;
        self.lines.push((found.line_number().unwrap_or(0), text));
        if self.search.held.fetch_add(cost, Ordering::Relaxed) + cost >= self.search.cap {
            self.search.stopped.store(true, Ordering::Relaxed);
            return Ok(false);
        }
        Ok(true)
    }

    fn finish(
        &mut self,
        _searcher: &Searcher,
        finish: &SinkFinish,
    ) -> std::result::Result<(), std::io::Error> {
        self.binary_at = finish.binary_byte_offset();
        Ok(())
    }
}

/// `text` cut to [`MAX_LINE`] bytes on a character, with a marker if it was cut.
/// The line as text, cut to [`MAX_LINE`] bytes (on a character) with a marker if it
/// was longer, and whether bytes that are not UTF-8 were replaced. The cut is made
/// on the bytes, before decoding, so a huge line is never decoded whole.
fn cut_line(bytes: &[u8]) -> (String, bool) {
    let mut end = bytes.len().min(MAX_LINE);
    while end < bytes.len() && end > 0 && bytes[end] & 0xC0 == 0x80 {
        end -= 1;
    }
    let text = String::from_utf8_lossy(&bytes[..end]);
    let lossy = std::str::from_utf8(&bytes[..end]).is_err();
    if end == bytes.len() {
        (text.into_owned(), lossy)
    } else {
        (format!("{text} [+{} bytes]", bytes.len() - end), lossy)
    }
}

fn into_data<T>(mutex: Mutex<T>) -> T {
    mutex.into_inner().unwrap_or_else(PoisonError::into_inner)
}

/// A file that fails its reads once the call is cancelled, so that a search of
/// a big file with no matches stops, not only the walk between files.
struct CancelReader<'a> {
    inner: File,
    cancel: &'a Cancel,
}

impl Read for CancelReader<'_> {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        if self.cancel.is_cancelled() {
            return Err(std::io::Error::other("the call was cancelled"));
        }
        self.inner.read(buf)
    }
}

fn searcher() -> Searcher {
    SearcherBuilder::new()
        .heap_limit(Some(HEAP_LIMIT))
        .line_number(true)
        .binary_detection(BinaryDetection::quit(0))
        .build()
}

/// A glob and whether it has a `/` (so it is matched against the path below
/// the searched directory instead of the file name).
fn compile_glob(glob: &str) -> Result<(GlobMatcher, bool)> {
    let slashed = glob.contains('/');
    let matcher = GlobBuilder::new(glob.trim_start_matches("./"))
        .literal_separator(slashed)
        .build()
        .map_err(|e| ToolError(format!("invalid glob: {e}")))?
        .compile_matcher();
    Ok((matcher, slashed))
}

fn glob_allows(glob: Option<&(GlobMatcher, bool)>, root: &Path, path: &Path) -> bool {
    let Some((matcher, slashed)) = glob else {
        return true;
    };
    if *slashed {
        matcher.is_match(path.strip_prefix(root).unwrap_or(path))
    } else {
        path.file_name()
            .is_some_and(|name| matcher.is_match(Path::new(name)))
    }
}

/// `path` without `.` components, so that it can be shown relative to the
/// working directory.
fn clean(path: &Path) -> PathBuf {
    path.components()
        .filter(|c| *c != Component::CurDir)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn setup(max: usize) -> (Grep, tempfile::TempDir) {
        let dir = tempfile::tempdir().unwrap();
        let limits = Limits::in_dir(max, dir.path().join(".out")).unwrap();
        (Grep::new(dir.path().to_owned(), limits), dir)
    }

    fn numbered(word: &str, n: usize) -> String {
        (1..=n).fold(String::new(), |mut s, i| {
            let _ = writeln!(s, "{word} {i}");
            s
        })
    }

    fn call(tool: &Grep, args: &Value) -> Result<String> {
        tool.run(&args.to_string(), &Cancel::new())
    }

    #[test]
    fn finds_sorted_lines_with_paths_relative_to_the_working_directory() {
        let (tool, dir) = setup(10_000);
        fs::create_dir(dir.path().join("sub")).unwrap();
        fs::write(dir.path().join("b.txt"), "x\nneedle two\n").unwrap();
        fs::write(dir.path().join("sub/a.txt"), "needle one\r\n").unwrap();
        let out = call(&tool, &json!({"pattern": "needle"})).unwrap();
        assert_eq!(out, "b.txt:2:needle two\nsub/a.txt:1:needle one");
        let out = call(&tool, &json!({"pattern": "needle", "path": "sub"})).unwrap();
        assert_eq!(out, "sub/a.txt:1:needle one");
        let out = call(&tool, &json!({"pattern": "needle", "path": "./sub/a.txt"})).unwrap();
        assert_eq!(out, "sub/a.txt:1:needle one");
    }

    #[test]
    fn gitignore_hidden_and_binary_are_skipped_without_a_git_repo() {
        let (tool, dir) = setup(10_000);
        fs::write(dir.path().join(".gitignore"), "ignored.txt\nbuild/\n").unwrap();
        fs::create_dir(dir.path().join("build")).unwrap();
        fs::write(dir.path().join("build/x.txt"), "needle").unwrap();
        fs::write(dir.path().join("ignored.txt"), "needle").unwrap();
        fs::write(dir.path().join(".hidden"), "needle").unwrap();
        fs::write(dir.path().join("bin.dat"), b"\0needle\x01").unwrap();
        fs::write(dir.path().join("kept.txt"), "needle").unwrap();
        let out = call(&tool, &json!({"pattern": "needle"})).unwrap();
        assert_eq!(out, "kept.txt:1:needle\n[skipped 1 binary file(s)]");
    }

    #[test]
    fn a_nul_after_matches_is_noted() {
        // Binary detection works a buffer at a time: a small file with a NUL is
        // skipped whole, a large one is searched up to the buffer with the NUL.
        let (tool, dir) = setup(10_000_000);
        let mut bytes = b"needle\n".repeat(50_000);
        bytes.extend_from_slice(b"\0needle\n");
        fs::write(dir.path().join("mixed.dat"), &bytes).unwrap();
        let out = call(&tool, &json!({"pattern": "needle", "path": "mixed.dat"})).unwrap();
        assert!(
            out.starts_with("mixed.dat:1:needle\nmixed.dat:2:needle\n"),
            "{}",
            &out[..100]
        );
        let note = out.lines().last().unwrap();
        assert!(
            note.starts_with("[matching in mixed.dat stopped at a NUL byte at offset 350000;"),
            "{note}"
        );
    }

    #[test]
    fn explicitly_named_binary_files_are_named() {
        let (tool, dir) = setup(10_000);
        fs::write(dir.path().join("a.dat"), b"\0needle").unwrap();
        let out = call(&tool, &json!({"pattern": "needle", "files": ["a.dat"]})).unwrap();
        assert_eq!(
            out,
            "No matches found in 1 file(s)\n[skipped binary files: a.dat]"
        );
    }

    #[test]
    fn no_match_says_how_many_files_were_searched() {
        let (tool, dir) = setup(10_000);
        fs::write(dir.path().join("a.txt"), "x").unwrap();
        fs::write(dir.path().join("b.txt"), "y").unwrap();
        let out = call(&tool, &json!({"pattern": "zzz"})).unwrap();
        assert_eq!(out, "No matches found in 2 file(s)");
    }

    #[test]
    fn options_ignore_case_glob_files_and_single_path() {
        let (tool, dir) = setup(10_000);
        fs::write(dir.path().join("a.rs"), "Needle\n").unwrap();
        fs::write(dir.path().join("a.txt"), "Needle\n").unwrap();
        let by = |args: Value| call(&tool, &args).unwrap();
        assert!(by(json!({"pattern": "needle"})).starts_with("No matches"));
        assert_eq!(
            by(json!({"pattern": "needle", "ignore_case": true, "glob": "*.rs"})),
            "a.rs:1:Needle"
        );
        assert_eq!(
            by(json!({"pattern": "Needle", "files": ["a.txt"]})),
            "a.txt:1:Needle"
        );
        assert_eq!(
            by(json!({"pattern": "Needle", "path": "a.rs"})),
            "a.rs:1:Needle"
        );
    }

    #[test]
    fn a_glob_filters_but_does_not_override_ignore_rules() {
        let (tool, dir) = setup(10_000);
        fs::write(dir.path().join(".gitignore"), "gen.rs\n").unwrap();
        fs::write(dir.path().join("gen.rs"), "needle\n").unwrap();
        fs::write(dir.path().join(".hid.rs"), "needle\n").unwrap();
        fs::write(dir.path().join("main.rs"), "needle\n").unwrap();
        fs::write(dir.path().join("main.txt"), "needle\n").unwrap();
        fs::create_dir_all(dir.path().join("src/deep")).unwrap();
        fs::write(dir.path().join("src/deep/lib.rs"), "needle\n").unwrap();
        let out = call(&tool, &json!({"pattern": "needle", "glob": "*.rs"})).unwrap();
        assert_eq!(out, "main.rs:1:needle\nsrc/deep/lib.rs:1:needle");
        let out = call(&tool, &json!({"pattern": "needle", "glob": "src/**/*.rs"})).unwrap();
        assert_eq!(out, "src/deep/lib.rs:1:needle");
    }

    #[test]
    fn errors_name_the_problem() {
        let (tool, dir) = setup(10_000);
        fs::write(dir.path().join("f"), "x").unwrap();
        let err = |args: Value| call(&tool, &args).unwrap_err().0;
        assert!(err(json!({"pattern": "("})).starts_with("invalid regex"));
        assert!(err(json!({"pattern": ""})).contains("must not be empty"));
        assert!(err(json!({"pattern": "a", "path": "nope"})).contains("path not found: nope"));
        assert!(err(json!({"pattern": "a", "files": ["nope"]})).contains("not a file: nope"));
        assert!(err(json!({"pattern": "a", "glob": "[z-a"})).starts_with("invalid glob"));
        assert!(
            err(json!({"pattern": "a", "glob": "*", "files": ["f"]})).contains("cannot be used")
        );
        assert!(err(json!({"pattern": "a", "glob": "*", "path": "f"})).contains("is a file"));
        assert!(err(json!({})).contains("missing field `pattern`"));
    }

    #[test]
    fn long_results_keep_the_head_and_spill_the_whole() {
        let (tool, dir) = setup(30);
        fs::write(dir.path().join("a.txt"), numbered("hit", 20)).unwrap();
        let out = call(&tool, &json!({"pattern": "hit"})).unwrap();
        assert!(
            out.starts_with(
                "a.txt:1:hit 1\na.txt:2:hit 2\n[truncated: 18 lines omitted after these;"
            ),
            "{out}"
        );
        let spilled = fs::read_dir(dir.path().join(".out"))
            .unwrap()
            .next()
            .unwrap()
            .unwrap();
        let full = fs::read_to_string(spilled.path()).unwrap();
        assert_eq!(full.lines().count(), 20);
        assert!(full.starts_with("a.txt:1:hit 1\n"));
    }

    #[test]
    fn a_long_line_is_cut_on_a_character_with_a_marker() {
        let (tool, dir) = setup(100_000);
        fs::write(
            dir.path().join("min.js"),
            format!("needle {}\n", "é".repeat(1000)),
        )
        .unwrap();
        let out = call(&tool, &json!({"pattern": "needle"})).unwrap();
        let line = out.lines().next().unwrap();
        assert!(line.starts_with("min.js:1:needle é"), "{line}");
        assert!(line.ends_with(" [+1508 bytes]"), "{line}");
        assert!(!line.contains('\u{FFFD}'));
    }

    #[test]
    fn invalid_utf8_in_a_match_is_replaced_and_said_so() {
        let (tool, dir) = setup(10_000);
        fs::write(dir.path().join("l.txt"), b"caf\xe9 needle\n").unwrap();
        let out = call(&tool, &json!({"pattern": "needle"})).unwrap();
        assert!(
            out.starts_with("l.txt:1:caf\u{FFFD} needle\n[invalid UTF-8 in matching lines"),
            "{out}"
        );
    }

    #[test]
    fn the_search_stops_when_the_matches_reach_the_cap() {
        let dir = tempfile::tempdir().unwrap();
        let limits = Limits::new(50, 1000, dir.path().join(".out")).unwrap();
        let tool = Grep::new(dir.path().to_owned(), limits);
        fs::write(dir.path().join("a.txt"), numbered("hit", 10_000)).unwrap();
        let out = call(&tool, &json!({"pattern": "hit"})).unwrap();
        assert!(
            out.contains("[search stopped: the matches reached 1000 bytes"),
            "{out}"
        );
    }

    #[test]
    fn a_cancelled_search_is_an_error() {
        let (tool, dir) = setup(10_000);
        fs::write(dir.path().join("a.txt"), "needle\n").unwrap();
        let cancel = Cancel::new();
        cancel.cancel();
        let err = tool
            .run(&json!({"pattern": "needle"}).to_string(), &cancel)
            .unwrap_err();
        assert!(err.0.contains("cancelled"), "{err}");
    }

    #[test]
    fn a_file_with_a_line_over_the_heap_limit_is_not_searched() {
        let (tool, dir) = setup(10_000);
        let mut huge = b"needle ".to_vec();
        huge.resize(HEAP_LIMIT + 4096, b'x');
        fs::write(dir.path().join("huge.txt"), huge).unwrap();
        fs::write(dir.path().join("ok.txt"), "needle\n").unwrap();
        let out = call(&tool, &json!({"pattern": "needle"})).unwrap();
        assert!(
            out.starts_with("ok.txt:1:needle\n[1 paths could not be searched; first: huge.txt:"),
            "{out}"
        );
    }

    #[test]
    fn a_read_after_the_cancel_fails_and_one_before_it_does_not() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("f"), "data").unwrap();
        let cancel = Cancel::new();
        let mut reader = CancelReader {
            inner: File::open(dir.path().join("f")).unwrap(),
            cancel: &cancel,
        };
        let mut buf = [0_u8; 2];
        assert_eq!(reader.read(&mut buf).unwrap(), 2);
        cancel.cancel();
        let err = reader.read(&mut buf).unwrap_err();
        assert!(err.to_string().contains("cancelled"), "{err}");
    }

    #[test]
    fn cutting_happens_on_bytes_and_flags_only_real_invalid_utf8() {
        let line = format!("a{}", "é".repeat(300)); // 601 bytes; byte 500 is inside a character
        let (text, lossy) = cut_line(line.as_bytes());
        assert!(!lossy, "a cut inside a character is not invalid input");
        assert!(text.ends_with(" [+102 bytes]"), "{text}");
        let (text, lossy) = cut_line(b"caf\xe9");
        assert!(lossy);
        assert_eq!(text, "caf\u{FFFD}");
    }
}
