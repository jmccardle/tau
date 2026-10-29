//! `write`: a whole file, atomically.
//!
//! Its answer is one short line, so the output limit does not apply to it.

use std::fs::{self, OpenOptions};
use std::io::Write as _;
use std::path::{Path, PathBuf};

use serde::Deserialize;
use serde_json::{Value, json};

use crate::limit::count_lines;
use crate::{Result, ToolError, parse_args, resolve};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Args {
    path: String,
    content: String,
}

#[derive(Clone, Debug)]
pub struct Write {
    cwd: PathBuf,
}

impl Write {
    #[must_use]
    pub fn new(cwd: PathBuf) -> Self {
        Self { cwd }
    }

    #[must_use]
    pub fn description() -> String {
        "Write a file with the given content, replacing it if it exists. Creates missing parent \
         directories. The write is atomic: the file is never seen half written."
            .to_owned()
    }

    #[must_use]
    pub fn parameters() -> Value {
        json!({
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path of the file to write (relative paths are relative to the working directory)"
                },
                "content": {
                    "type": "string",
                    "description": "The complete new content of the file"
                }
            },
            "required": ["path", "content"],
            "additionalProperties": false
        })
    }

    /// # Errors
    /// Bad arguments, a directory in the file's place, or any IO failure.
    pub fn run(&self, args_raw: &str) -> Result<String> {
        let args: Args = parse_args(args_raw)?;
        if args.path.is_empty() {
            return Err("`path` must not be empty".into());
        }
        let failed = |e: std::io::Error| ToolError(format!("cannot write {}: {e}", args.path));
        let mut target = resolve(&self.cwd, &args.path);
        if target.is_dir() {
            return Err(format!("{} is a directory, not a file", args.path).into());
        }
        if target.exists() {
            // Write through a symlink rather than replacing it.
            target = fs::canonicalize(&target).map_err(failed)?;
        }
        let parent = target.parent().unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent).map_err(failed)?;
        replace(&target, parent, args.content.as_bytes()).map_err(failed)?;
        Ok(format!(
            "Wrote {} lines ({} bytes) to {}",
            count_lines(args.content.as_bytes()),
            args.content.len(),
            args.path
        ))
    }
}

/// Write `bytes` to a new file beside `target`, then rename it over `target`.
fn replace(target: &Path, parent: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let name = target
        .file_name()
        .map_or_else(String::new, |n| n.to_string_lossy().into_owned());
    let (temp, mut file) = loop {
        let temp = parent.join(format!(".{name}.tau-{:08x}.tmp", fastrand::u32(..)));
        match OpenOptions::new().write(true).create_new(true).open(&temp) {
            Ok(file) => break (temp, file),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(e) => return Err(e),
        }
    };
    let result = (|| {
        file.write_all(bytes)?;
        file.sync_all()?;
        if let Ok(existing) = fs::metadata(target) {
            fs::set_permissions(&temp, existing.permissions())?;
        }
        fs::rename(&temp, target)
    })();
    if result.is_err() {
        // Best effort: the error being returned is the write's, not the cleanup's.
        let _ = fs::remove_file(&temp);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    fn call(dir: &tempfile::TempDir, args: &Value) -> Result<String> {
        Write::new(dir.path().to_owned()).run(&args.to_string())
    }

    #[test]
    fn writes_creating_parents_and_replaces_atomically() {
        let dir = tempfile::tempdir().unwrap();
        let out = call(&dir, &json!({"path": "a/b/c.txt", "content": "x\ny\n"})).unwrap();
        assert_eq!(out, "Wrote 2 lines (4 bytes) to a/b/c.txt");
        call(&dir, &json!({"path": "a/b/c.txt", "content": "z"})).unwrap();
        let path = dir.path().join("a/b/c.txt");
        assert_eq!(fs::read_to_string(&path).unwrap(), "z");
        let names: Vec<_> = fs::read_dir(path.parent().unwrap()).unwrap().collect();
        assert_eq!(names.len(), 1, "no temp file is left behind");
    }

    #[test]
    fn keeps_the_mode_of_the_file_it_replaces() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("run.sh");
        fs::write(&path, "old").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        call(&dir, &json!({"path": "run.sh", "content": "new"})).unwrap();
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o755
        );
    }

    #[test]
    fn a_directory_and_bad_arguments_are_errors() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join("d")).unwrap();
        let err = |args| call(&dir, &args).unwrap_err().0;
        assert!(err(json!({"path": "d", "content": ""})).contains("is a directory"));
        assert!(err(json!({"path": "a"})).contains("missing field `content`"));
        assert!(err(json!({"path": "", "content": ""})).contains("must not be empty"));
    }

    #[test]
    fn a_parent_that_is_a_file_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("f"), "x").unwrap();
        let err = call(&dir, &json!({"path": "f/g", "content": ""})).unwrap_err();
        assert!(err.0.starts_with("cannot write f/g"), "{err}");
    }
}
