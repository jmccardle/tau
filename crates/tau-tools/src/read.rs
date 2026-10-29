//! `read`: a text file, from a line, for a number of lines.

use std::fmt::Write as _;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;

use serde::Deserialize;
use serde_json::{Value, json};

use crate::limit::{Keep, Sink};
use crate::{Limits, Result, ToolError, parse_args, resolve};

/// Lines read when the call gives no `limit`.
pub const DEFAULT_LINES: u64 = 4096;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Args {
    path: String,
    offset: Option<i64>,
    limit: Option<i64>,
}

#[derive(Clone, Debug)]
pub struct Read {
    cwd: PathBuf,
    limits: Limits,
}

impl Read {
    #[must_use]
    pub fn new(cwd: PathBuf, limits: Limits) -> Self {
        Self { cwd, limits }
    }

    #[must_use]
    pub fn description(&self) -> String {
        format!(
            "Read a text file. `offset` is the 1-based line to start at and `limit` the number \
             of lines (default {DEFAULT_LINES}). Output past {} bytes is cut after the last \
             whole line that fits; the full text of the lines read is then saved to a file named \
             in a note. Images and other binary files cannot be read.",
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
                    "description": "Path of the file to read (relative paths are relative to the working directory)"
                },
                "offset": {
                    "type": "integer",
                    "description": "Line to start reading from, 1-based (default 1)"
                },
                "limit": {
                    "type": "integer",
                    "description": format!("Maximum number of lines to read (default {DEFAULT_LINES})")
                }
            },
            "required": ["path"],
            "additionalProperties": false
        })
    }

    /// # Errors
    /// Bad arguments, a missing, unreadable or binary file, an offset past the
    /// end, or a failure to write the overflow file.
    pub fn run(&self, args_raw: &str) -> Result<String> {
        let args: Args = parse_args(args_raw)?;
        let offset = positive("offset", args.offset)?.unwrap_or(1);
        let limit = positive("limit", args.limit)?.unwrap_or(DEFAULT_LINES);
        let path = resolve(&self.cwd, &args.path);
        let io = |e: std::io::Error| ToolError(format!("cannot read {}: {e}", args.path));
        if path.is_dir() {
            return Err(format!("{} is a directory, not a file", args.path).into());
        }
        let file = File::open(&path).map_err(|e| match e.kind() {
            std::io::ErrorKind::NotFound => ToolError(format!("file not found: {}", args.path)),
            _ => io(e),
        })?;
        let mut reader = BufReader::new(file);
        if reader.fill_buf().map_err(io)?.contains(&0) {
            return Err(binary(&args.path));
        }
        let mut line = Vec::new();
        for _ in 1..offset {
            line.clear();
            if reader.read_until(b'\n', &mut line).map_err(io)? == 0 {
                return Err(format!(
                    "offset {offset} is past the end of {}, which has fewer lines",
                    args.path
                )
                .into());
            }
        }
        let mut sink = Sink::new(&self.limits, Keep::Head);
        let mut read = 0;
        while read < limit {
            line.clear();
            if reader.read_until(b'\n', &mut line).map_err(io)? == 0 {
                break;
            }
            if line.contains(&0) {
                return Err(binary(&args.path));
            }
            sink.push(&line)?;
            read += 1;
        }
        if offset > 1 && read == 0 {
            return Err(format!(
                "offset {offset} is past the end of {}, which has fewer lines",
                args.path
            )
            .into());
        }
        let more = read == limit && !reader.fill_buf().map_err(io)?.is_empty();
        let mut text = sink.finish()?;
        if more {
            let last = offset + read - 1;
            let _ = write!(
                text,
                "\n[lines {offset}-{last} were read; more follow, offset={} continues]",
                last + 1
            );
        }
        Ok(text)
    }
}

fn positive(name: &str, value: Option<i64>) -> Result<Option<u64>> {
    match value {
        None => Ok(None),
        Some(n) if n >= 1 => Ok(Some(n.unsigned_abs())),
        Some(n) => Err(format!("`{name}` must be at least 1, not {n}").into()),
    }
}

fn binary(path: &str) -> ToolError {
    ToolError(format!(
        "{path} is a binary file (it contains NUL bytes); read cannot show it"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn tool(max: usize) -> (Read, tempfile::TempDir) {
        let dir = tempfile::tempdir().unwrap();
        let limits = Limits::in_dir(max, dir.path().join("out")).unwrap();
        (Read::new(dir.path().to_owned(), limits), dir)
    }

    fn call(tool: &Read, args: &Value) -> Result<String> {
        tool.run(&args.to_string())
    }

    #[test]
    fn reads_a_whole_small_file_verbatim() {
        let (tool, dir) = tool(1000);
        fs::write(dir.path().join("a.txt"), "one\ntwo\n").unwrap();
        assert_eq!(
            call(&tool, &json!({"path": "a.txt"})).unwrap(),
            "one\ntwo\n"
        );
    }

    #[test]
    fn offset_and_limit_select_lines_and_say_how_to_go_on() {
        let (tool, dir) = tool(1000);
        fs::write(dir.path().join("a.txt"), "1\n2\n3\n4\n5\n").unwrap();
        let out = call(&tool, &json!({"path": "a.txt", "offset": 2, "limit": 2})).unwrap();
        assert_eq!(
            out,
            "2\n3\n\n[lines 2-3 were read; more follow, offset=4 continues]"
        );
        let out = call(&tool, &json!({"path": "a.txt", "offset": 4, "limit": 2})).unwrap();
        assert_eq!(out, "4\n5\n");
    }

    #[test]
    fn errors_name_the_problem() {
        let (tool, dir) = tool(1000);
        fs::create_dir(dir.path().join("d")).unwrap();
        fs::write(dir.path().join("b.bin"), b"ab\0cd").unwrap();
        fs::write(dir.path().join("a.txt"), "1\n").unwrap();
        let err = |args: Value| call(&tool, &args).unwrap_err().0;
        assert!(err(json!({"path": "nope"})).contains("file not found: nope"));
        assert!(err(json!({"path": "d"})).contains("is a directory"));
        assert!(err(json!({"path": "b.bin"})).contains("binary file"));
        assert!(err(json!({"path": "a.txt", "offset": 0})).contains("at least 1"));
        assert!(err(json!({"path": "a.txt", "offset": 9})).contains("past the end"));
        assert!(err(json!({})).contains("missing field `path`"));
        assert!(err(json!({"path": "a.txt", "bogus": 1})).contains("unknown field"));
    }

    #[test]
    fn long_files_are_cut_to_the_head_with_the_rest_on_disk() {
        let (tool, dir) = tool(20);
        let body =
            "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\n"
                .to_owned();
        fs::write(dir.path().join("a.txt"), &body).unwrap();
        let out = call(&tool, &json!({"path": "a.txt"})).unwrap();
        assert!(
            out.starts_with("line 1\nline 2\nline 3\n[truncated: 7 lines omitted after these;"),
            "{out}"
        );
        let spilled = fs::read_dir(dir.path().join("out"))
            .unwrap()
            .next()
            .unwrap()
            .unwrap();
        assert_eq!(fs::read_to_string(spilled.path()).unwrap(), body);
    }
}
