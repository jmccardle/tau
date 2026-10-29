//! `write`: a whole file, atomically.
//!
//! Its answer is one short line, so the output limit does not apply to it.
//!
//! The content goes to a new file beside the target, which is synced and then
//! renamed over it; the directory is synced after. A symlink as the target
//! (even a dangling one) is followed, so the link stays a link. What a rename
//! cannot keep is not kept: hard links to the old file keep the old content,
//! and the owner, group and ACLs of an existing file are not carried over. The
//! permission bits are.

use std::fs::{self, File, OpenOptions};
use std::io::Write as _;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

use serde::Deserialize;
use serde_json::{Value, json};

use crate::limit::count_lines;
use crate::{Result, ToolError, parse_args, resolve};

/// Symlinks followed before giving up.
const MAX_HOPS: usize = 40;

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
         directories. The write is atomic: the file is never seen half written. A symlink is \
         followed. Permission bits of a replaced file are kept; hard links, owner and ACLs are \
         not."
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
        let target = follow_symlinks(&resolve(&self.cwd, &args.path)).map_err(failed)?;
        if target.is_dir() {
            return Err(format!("{} is a directory, not a file", args.path).into());
        }
        let parent = target.parent().unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent).map_err(failed)?;
        replace(&target, parent, args.content.as_bytes()).map_err(failed)?;
        let lines = count_lines(args.content.as_bytes());
        Ok(format!(
            "Wrote {lines} {} ({} bytes) to {}",
            if lines == 1 { "line" } else { "lines" },
            args.content.len(),
            args.path
        ))
    }
}

/// `path` with a final symlink, dangling or not, replaced by what it points at.
fn follow_symlinks(path: &Path) -> std::io::Result<PathBuf> {
    let mut path = path.to_owned();
    for _ in 0..MAX_HOPS {
        match fs::symlink_metadata(&path) {
            Ok(meta) if meta.file_type().is_symlink() => {
                let link = fs::read_link(&path)?;
                path = path.parent().unwrap_or_else(|| Path::new(".")).join(link);
            }
            Ok(_) => return Ok(path),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(path),
            Err(e) => return Err(e),
        }
    }
    Err(std::io::Error::other("too many levels of symbolic links"))
}

/// Write `bytes` to a new file beside `target`, then rename it over `target`.
fn replace(target: &Path, parent: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let name = target
        .file_name()
        .map_or_else(String::new, |n| n.to_string_lossy().into_owned());
    let existing = fs::metadata(target).ok();
    let (temp, mut file) = loop {
        let temp = parent.join(format!(".{name}.tau-{:08x}.tmp", fastrand::u32(..)));
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        if existing.is_some() {
            options.mode(0o600); // widened to the old file's mode below, before any content
        }
        match options.open(&temp) {
            Ok(file) => break (temp, file),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(e) => return Err(e),
        }
    };
    let result = (|| {
        if let Some(existing) = &existing {
            file.set_permissions(existing.permissions())?;
        }
        file.write_all(bytes)?;
        file.sync_all()?;
        fs::rename(&temp, target)?;
        File::open(parent)?.sync_all()
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
    fn the_answer_pluralizes() {
        let dir = tempfile::tempdir().unwrap();
        let one = call(&dir, &json!({"path": "a", "content": "x"})).unwrap();
        assert_eq!(one, "Wrote 1 line (1 bytes) to a");
        let none = call(&dir, &json!({"path": "b", "content": ""})).unwrap();
        assert_eq!(none, "Wrote 0 lines (0 bytes) to b");
    }

    #[test]
    fn keeps_the_mode_of_the_file_it_replaces() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("run.sh");
        fs::write(&path, "old").unwrap();
        for mode in [0o755, 0o640, 0o444] {
            fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
            call(&dir, &json!({"path": "run.sh", "content": "new"})).unwrap();
            let kept = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
            assert_eq!(kept, mode);
        }
    }

    #[test]
    fn writes_through_a_symlink_even_a_dangling_one() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("real.txt"), "old").unwrap();
        std::os::unix::fs::symlink("real.txt", dir.path().join("link.txt")).unwrap();
        std::os::unix::fs::symlink("sub/target.txt", dir.path().join("dangling.txt")).unwrap();
        call(&dir, &json!({"path": "link.txt", "content": "new"})).unwrap();
        assert!(dir.path().join("link.txt").is_symlink());
        assert_eq!(
            fs::read_to_string(dir.path().join("real.txt")).unwrap(),
            "new"
        );
        call(&dir, &json!({"path": "dangling.txt", "content": "made"})).unwrap();
        assert!(dir.path().join("dangling.txt").is_symlink());
        assert_eq!(
            fs::read_to_string(dir.path().join("sub/target.txt")).unwrap(),
            "made"
        );
    }

    #[test]
    fn a_symlink_loop_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        std::os::unix::fs::symlink("b", dir.path().join("a")).unwrap();
        std::os::unix::fs::symlink("a", dir.path().join("b")).unwrap();
        let err = call(&dir, &json!({"path": "a", "content": ""})).unwrap_err();
        assert!(err.0.contains("too many levels"), "{err}");
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
