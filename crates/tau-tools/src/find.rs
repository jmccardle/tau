//! `find`: the paths under a directory whose name matches a glob, on `grep`'s walk.
//!
//! The walk is `grep`'s (`walk.rs`): parallel, honoring `.gitignore` in any tree and `.ignore`.
//! Unlike `grep`'s it lists hidden files and enters hidden directories (`.env`, `.github/`),
//! except `.git`. Directories are not listed and symlinks are listed, not followed.
//! Paths are shown relative to the searched directory, one per line, sorted, so the same
//! search gives the same answer. Memory is bounded as in `grep`: the walk stops once the paths
//! it holds reach the overflow cap.

use std::fmt::Write as _;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Mutex, PoisonError};

use serde::Deserialize;
use serde_json::{Value, json};

use crate::limit::{Keep, LOSSY_NOTE, Sink};
use crate::walk::{Exclude, Glob, Hidden, Item, clean, walk};
use crate::{Cancel, Limits, Result, lock, parse_args, resolve};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Args {
    pattern: String,
    path: Option<String>,
    /// Not in the model's schema: set by a hook (permissions) to leave files out of the walk.
    exclude: Option<Vec<String>>,
}

#[derive(Clone, Debug)]
pub struct Find {
    cwd: PathBuf,
    limits: Limits,
}

/// What every walker thread shares.
#[derive(Default)]
struct Found {
    paths: Mutex<Vec<String>>,
    unreadable: Mutex<Vec<String>>,
    /// Bytes of paths held, roughly; the walk stops at the cap.
    held: AtomicUsize,
    stopped: AtomicBool,
    lossy: AtomicBool,
}

impl Find {
    #[must_use]
    pub fn new(cwd: PathBuf, limits: Limits) -> Self {
        Self { cwd, limits }
    }

    #[must_use]
    pub fn description(&self) -> String {
        format!(
            "Find files by name with a glob (`*`, `?`, `[ab]`, `{{a,b}}`, `**`). A glob without a / \
             matches file names at any depth (`*.rs`); one with a / matches the path below the \
             searched directory (`src/**/*.rs`). Answers one path per line, relative to the \
             searched directory, sorted. Searches recursively, including hidden files and \
             directories (not .git), and skipping files ignored by .gitignore. Lists files and symlinks (not followed), not directories. \
             Output past {} bytes is cut after the last whole line that fits; the full list is \
             then saved to a file named in a note. Relative paths are relative to the working \
             directory, which is a default, not a sandbox: absolute paths and `..` work.",
            self.limits.max_bytes
        )
    }

    #[must_use]
    pub fn parameters() -> Value {
        json!({
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob for the files to list, relative to `path`, e.g. \"*.rs\" or \"src/**/*.rs\" (not an absolute path)"
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search (default: the working directory)"
                }
            },
            "required": ["pattern"],
            "additionalProperties": false
        })
    }

    /// # Errors
    /// Bad arguments or glob, a path that is missing or not a directory, or a cancel.
    pub fn run(&self, args_raw: &str, cancel: &Cancel) -> Result<String> {
        let args: Args = parse_args(args_raw)?;
        if args.pattern.is_empty() {
            return Err("`pattern` must not be empty".into());
        }
        let glob = Glob::new(&args.pattern)?;
        let given = args.path.as_deref().unwrap_or(".");
        let root = clean(&resolve(&self.cwd, given));
        if root.is_file() {
            return Err(format!("{given} is a file, not a directory").into());
        }
        if !root.is_dir() {
            return Err(format!("path not found: {given}").into());
        }
        let exclude = args.exclude.as_deref().map(Exclude::new).transpose()?;
        let found = Found::default();
        let cap = self.limits.max_overflow;
        walk(
            &root,
            Some(&glob),
            exclude.as_ref(),
            Hidden::Included,
            &|| cancel.is_cancelled() || found.stopped.load(Ordering::Relaxed),
            &|e| lock(&found.unreadable).push(e),
            || {
                |item: &Item| {
                    let below = item.path().strip_prefix(&root).unwrap_or(item.path());
                    if below.to_str().is_none() {
                        found.lossy.store(true, Ordering::Relaxed);
                    }
                    let shown = below.to_string_lossy().into_owned();
                    let cost = shown.len() + 1;
                    lock(&found.paths).push(shown);
                    if found.held.fetch_add(cost, Ordering::Relaxed) + cost >= cap {
                        found.stopped.store(true, Ordering::Relaxed);
                    }
                }
            },
        );
        cancel.check()?;
        let mut text = self.render(found, &args.pattern);
        if let Some(note) = exclude.as_ref().and_then(Exclude::note) {
            let _ = write!(text, "\n{note}");
        }
        Ok(text)
    }

    fn render(&self, found: Found, pattern: &str) -> String {
        let mut paths = found
            .paths
            .into_inner()
            .unwrap_or_else(PoisonError::into_inner);
        paths.sort();
        let mut text = if paths.is_empty() {
            format!("No files found matching \"{pattern}\"")
        } else {
            let mut sink = Sink::new(&self.limits, Keep::Head);
            sink.push(paths.join("\n").as_bytes());
            sink.finish().text
        };
        if found.lossy.into_inner() {
            let _ = write!(text, "\n{}", LOSSY_NOTE.replace("the output", "file names"));
        }
        if found.stopped.into_inner() {
            let _ = write!(
                text,
                "\n[search stopped: the paths reached {} bytes; there are more, and some \
                 directories were not searched]",
                self.limits.max_overflow
            );
        }
        let mut unreadable = found
            .unreadable
            .into_inner()
            .unwrap_or_else(PoisonError::into_inner);
        unreadable.sort();
        if let Some(example) = unreadable.first() {
            let _ = write!(
                text,
                "\n[{} paths could not be read; first: {example}]",
                unreadable.len()
            );
        }
        text
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn setup(max: usize) -> (Find, tempfile::TempDir) {
        let dir = tempfile::tempdir().unwrap();
        let limits = Limits::in_dir(max, dir.path().join(".out")).unwrap();
        (Find::new(dir.path().to_owned(), limits), dir)
    }

    fn call(tool: &Find, args: &Value) -> Result<String> {
        tool.run(&args.to_string(), &Cancel::new())
    }

    fn touch(dir: &tempfile::TempDir, names: &[&str]) {
        for name in names {
            let path = dir.path().join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, "x").unwrap();
        }
    }

    #[test]
    fn lists_sorted_paths_relative_to_the_searched_directory() {
        let (tool, dir) = setup(10_000);
        touch(&dir, &["b.rs", "a.rs", "a.txt", "src/deep/c.rs"]);
        let all = call(&tool, &json!({"pattern": "*.rs"})).unwrap();
        assert_eq!(all, "a.rs\nb.rs\nsrc/deep/c.rs");
        let below = call(&tool, &json!({"pattern": "*.rs", "path": "src"})).unwrap();
        assert_eq!(below, "deep/c.rs");
        let slashed = call(&tool, &json!({"pattern": "src/**/*.rs"})).unwrap();
        assert_eq!(slashed, "src/deep/c.rs");
    }

    #[test]
    fn exclude_leaves_files_out_and_says_so() {
        let (tool, dir) = setup(10_000);
        touch(&dir, &["a.rs", "secret/b.rs", "secret/{x}.rs"]);
        let root = dir.path().canonicalize().unwrap();
        let exclude = json!([format!("{}/secret/*", root.display())]);
        let out = call(&tool, &json!({"pattern": "*.rs", "exclude": exclude})).unwrap();
        assert_eq!(
            out,
            "a.rs\n[2 file(s) left out of this search: they match its `exclude` patterns]"
        );
        let braces = json!([format!("{}/secret/{{x}}.rs", root.display())]);
        let out = call(&tool, &json!({"pattern": "*.rs", "exclude": braces})).unwrap();
        assert!(
            out.contains("a.rs\nsecret/b.rs\n") || out.starts_with("a.rs\nsecret/b.rs"),
            "{out}"
        );
        assert!(out.contains("[1 file(s) left out"), "{out}");
    }

    #[test]
    fn ignore_rules_apply_without_a_git_repository() {
        let (tool, dir) = setup(10_000);
        fs::write(dir.path().join(".gitignore"), "gen.rs\nbuild/\n").unwrap();
        touch(&dir, &["gen.rs", "build/o.rs", "kept.rs"]);
        let out = call(&tool, &json!({"pattern": "*.rs"})).unwrap();
        assert_eq!(out, "kept.rs");
    }

    #[test]
    fn hidden_files_are_listed_but_not_the_git_directory() {
        let (tool, dir) = setup(10_000);
        fs::write(dir.path().join(".gitignore"), ".cache/\n").unwrap();
        touch(
            &dir,
            &[".env", ".github/ci.yml", ".cache/x", ".git/config", "a"],
        );
        let out = call(&tool, &json!({"pattern": "*"})).unwrap();
        assert_eq!(out, ".env\n.github/ci.yml\n.gitignore\na");
    }

    #[test]
    fn an_absolute_glob_is_an_error() {
        let (tool, _dir) = setup(10_000);
        let e = call(&tool, &json!({"pattern": "/etc/*.conf"})).unwrap_err();
        assert!(e.0.contains("relative to `path`"), "{e:?}");
    }

    #[test]
    fn no_match_is_an_answer() {
        let (tool, dir) = setup(10_000);
        touch(&dir, &["a.txt"]);
        let out = call(&tool, &json!({"pattern": "*.zzz"})).unwrap();
        assert_eq!(out, "No files found matching \"*.zzz\"");
    }

    #[test]
    fn errors_name_the_problem() {
        let (tool, dir) = setup(10_000);
        touch(&dir, &["f"]);
        let err = |args: Value| call(&tool, &args).unwrap_err().0;
        assert!(err(json!({"pattern": "[z-a"})).starts_with("invalid glob"));
        assert!(err(json!({"pattern": ""})).contains("must not be empty"));
        assert!(err(json!({"pattern": "*", "path": "nope"})).contains("path not found: nope"));
        assert!(err(json!({"pattern": "*", "path": "f"})).contains("f is a file, not a directory"));
        assert!(err(json!({})).contains("missing field `pattern`"));
    }

    #[test]
    fn a_long_list_keeps_the_head_and_saves_the_whole() {
        let (tool, dir) = setup(30);
        let names: Vec<String> = (0..50).map(|i| format!("f{i:02}.txt")).collect();
        touch(&dir, &names.iter().map(String::as_str).collect::<Vec<_>>());
        let out = call(&tool, &json!({"pattern": "*.txt"})).unwrap();
        assert!(
            out.starts_with("f00.txt\nf01.txt\nf02.txt\n[truncated: "),
            "{out}"
        );
        let spilled = fs::read_dir(dir.path().join(".out"))
            .unwrap()
            .next()
            .unwrap()
            .unwrap();
        let full = fs::read_to_string(spilled.path()).unwrap();
        assert_eq!(full.lines().count(), 50);
    }

    #[test]
    fn the_walk_stops_when_the_paths_reach_the_cap() {
        let dir = tempfile::tempdir().unwrap();
        let limits = Limits::new(50, 200, dir.path().join(".out")).unwrap();
        let tool = Find::new(dir.path().to_owned(), limits);
        let names: Vec<String> = (0..100).map(|i| format!("file{i:03}.txt")).collect();
        touch(&dir, &names.iter().map(String::as_str).collect::<Vec<_>>());
        let out = call(&tool, &json!({"pattern": "*.txt"})).unwrap();
        assert!(
            out.contains("[search stopped: the paths reached 200 bytes"),
            "{out}"
        );
    }

    #[test]
    fn a_cancelled_walk_is_an_error() {
        let (tool, dir) = setup(10_000);
        touch(&dir, &["a.txt"]);
        let cancel = Cancel::new();
        cancel.cancel();
        let err = tool
            .run(&json!({"pattern": "*"}).to_string(), &cancel)
            .unwrap_err();
        assert!(err.0.contains("cancelled"), "{err}");
    }
}
