//! `grep`: a regex over files, ripgrep's engine and ripgrep's walk.
//!
//! A directory is walked in parallel, honoring `.gitignore` (whether or not the
//! tree is a git repository), `.ignore`, and skipping hidden files, as `rg`
//! does. Files that look binary are skipped. Matches come out sorted by path
//! and line, so the same search gives the same answer.

use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};

use grep_regex::RegexMatcherBuilder;
use grep_searcher::sinks::Lossy;
use grep_searcher::{BinaryDetection, Searcher, SearcherBuilder};
use ignore::WalkBuilder;
use ignore::overrides::OverrideBuilder;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::limit::{Keep, Sink};
use crate::{Limits, Result, ToolError, parse_args, resolve};

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
             `path:line:text` per matching line, sorted by path. Searches a directory \
             recursively, skipping hidden files, files ignored by .gitignore, and binary files. \
             Output past {} bytes is cut after the last whole line that fits; the full result \
             is then saved to a file named in a note.",
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
                    "description": "Only search files whose path matches this glob, e.g. \"*.rs\""
                }
            },
            "required": ["pattern"],
            "additionalProperties": false
        })
    }

    /// # Errors
    /// Bad arguments or regex or glob, a path that is missing, or a failure to
    /// write the overflow file.
    pub fn run(&self, args_raw: &str) -> Result<String> {
        let args: Args = parse_args(args_raw)?;
        if args.pattern.is_empty() {
            return Err("`pattern` must not be empty".into());
        }
        let matcher = RegexMatcherBuilder::new()
            .case_insensitive(args.ignore_case.unwrap_or(false))
            .build(&args.pattern)
            .map_err(|e| ToolError(format!("invalid regex: {e}")))?;
        let search = Search {
            matcher,
            searched: AtomicUsize::new(0),
            found: Mutex::new(Vec::new()),
            unreadable: Mutex::new(Vec::new()),
        };
        match &args.files {
            Some(files) if files.is_empty() => return Err("`files` must not be empty".into()),
            Some(files) => self.search_named(&search, files)?,
            None => self.search_path(&search, &args)?,
        }
        self.render(search)
    }

    fn search_named(&self, search: &Search, files: &[String]) -> Result<()> {
        let mut searcher = searcher();
        for name in files {
            let path = resolve(&self.cwd, name);
            if !path.is_file() {
                return Err(format!("not a file: {name}").into());
            }
            search.file(&mut searcher, &path, name.clone());
        }
        Ok(())
    }

    fn search_path(&self, search: &Search, args: &Args) -> Result<()> {
        let root = resolve(&self.cwd, args.path.as_deref().unwrap_or("."));
        if root.is_file() {
            let shown = args.path.clone().unwrap_or_default();
            search.file(&mut searcher(), &root, shown);
            return Ok(());
        }
        if !root.is_dir() {
            return Err(format!("path not found: {}", args.path.as_deref().unwrap_or(".")).into());
        }
        let mut walk = WalkBuilder::new(&root);
        // .gitignore applies in any tree, not only inside a git repository.
        walk.require_git(false);
        if let Some(glob) = &args.glob {
            let mut overrides = OverrideBuilder::new(&root);
            overrides
                .add(glob)
                .map_err(|e| ToolError(format!("invalid glob: {e}")))?;
            walk.overrides(
                overrides
                    .build()
                    .map_err(|e| ToolError(format!("invalid glob: {e}")))?,
            );
        }
        let root = &root;
        walk.build_parallel().run(|| {
            let mut searcher = searcher();
            Box::new(move |found| {
                match found {
                    Ok(item) if item.file_type().is_some_and(|t| t.is_file()) => {
                        let shown = relative(root, item.path());
                        search.file(&mut searcher, item.path(), shown);
                    }
                    Ok(_) => {}
                    Err(e) => search.unreadable.lock().unwrap().push(e.to_string()),
                }
                ignore::WalkState::Continue
            })
        });
        Ok(())
    }

    fn render(&self, search: Search) -> Result<String> {
        let mut all = search.found.into_inner().unwrap();
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
            sink.finish()?
        };
        let unreadable = search.unreadable.into_inner().unwrap();
        if let Some(example) = unreadable.first() {
            let _ = write!(
                text,
                "\n[{} paths could not be searched; first: {example}]",
                unreadable.len()
            );
        }
        Ok(text)
    }
}

/// What every search thread shares.
struct Search {
    matcher: grep_regex::RegexMatcher,
    searched: AtomicUsize,
    found: Mutex<Vec<Hits>>,
    unreadable: Mutex<Vec<String>>,
}

impl Search {
    fn file(&self, searcher: &mut Searcher, path: &Path, shown: String) {
        let mut lines = Vec::new();
        let sink = Lossy(|number, text: &str| {
            lines.push((number, text.trim_end_matches(['\n', '\r']).to_owned()));
            Ok(true)
        });
        match searcher.search_path(&self.matcher, path, sink) {
            Ok(()) => {
                self.searched.fetch_add(1, Ordering::Relaxed);
                if !lines.is_empty() {
                    self.found.lock().unwrap().push(Hits { path: shown, lines });
                }
            }
            Err(e) => self
                .unreadable
                .lock()
                .unwrap()
                .push(format!("{shown}: {e}")),
        }
    }
}

fn searcher() -> Searcher {
    SearcherBuilder::new()
        .line_number(true)
        .binary_detection(BinaryDetection::quit(0))
        .build()
}

fn relative(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .into_owned()
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
        tool.run(&args.to_string())
    }

    #[test]
    fn finds_sorted_lines_with_relative_paths() {
        let (tool, dir) = setup(10_000);
        fs::create_dir(dir.path().join("sub")).unwrap();
        fs::write(dir.path().join("b.txt"), "x\nneedle two\n").unwrap();
        fs::write(dir.path().join("sub/a.txt"), "needle one\r\n").unwrap();
        let out = call(&tool, &json!({"pattern": "needle"})).unwrap();
        assert_eq!(out, "b.txt:2:needle two\nsub/a.txt:1:needle one");
    }

    #[test]
    fn gitignore_hidden_and_binary_are_skipped_without_a_git_repo() {
        let (tool, dir) = setup(10_000);
        fs::write(dir.path().join(".gitignore"), "ignored.txt\nbuild/\n").unwrap();
        fs::create_dir(dir.path().join("build")).unwrap();
        fs::write(dir.path().join("build/x.txt"), "needle").unwrap();
        fs::write(dir.path().join("ignored.txt"), "needle").unwrap();
        fs::write(dir.path().join(".hidden"), "needle").unwrap();
        fs::write(dir.path().join("bin.dat"), b"needle\0\x01").unwrap();
        fs::write(dir.path().join("kept.txt"), "needle").unwrap();
        let out = call(&tool, &json!({"pattern": "needle"})).unwrap();
        assert_eq!(out, "kept.txt:1:needle");
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
    fn errors_name_the_problem() {
        let (tool, _dir) = setup(10_000);
        let err = |args: Value| call(&tool, &args).unwrap_err().0;
        assert!(err(json!({"pattern": "("})).starts_with("invalid regex"));
        assert!(err(json!({"pattern": ""})).contains("must not be empty"));
        assert!(err(json!({"pattern": "a", "path": "nope"})).contains("path not found: nope"));
        assert!(err(json!({"pattern": "a", "files": ["nope"]})).contains("not a file: nope"));
        assert!(err(json!({"pattern": "a", "glob": "[z-a"})).starts_with("invalid glob"));
        assert!(err(json!({})).contains("missing field `pattern`"));
    }

    #[test]
    fn long_results_keep_the_head_and_spill_the_whole() {
        let (tool, dir) = setup(30);
        let body = numbered("hit", 20);
        fs::write(dir.path().join("a.txt"), &body).unwrap();
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
}
