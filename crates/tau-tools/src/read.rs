//! `read`: a text file, from a line, for a number of lines.
//!
//! The file is scanned in buffer-sized pieces; no line is ever held whole, so a
//! file of one enormous line costs the output window, not the file.

use std::fmt::Write as _;
use std::fs::{self, File};
use std::io::{BufRead, BufReader};
use std::path::PathBuf;

use serde::Deserialize;
use serde_json::{Value, json};

use crate::limit::{Keep, Sink};
use crate::{Cancel, Limits, Result, ToolError, parse_args, resolve};

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
             in a note, and a last line gives the offset that continues. Images and other \
             binary files cannot be read.",
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
    /// Bad arguments, a missing, unreadable, binary or irregular file, an
    /// offset past the end, a cancel, or a failure to write the overflow file.
    pub fn run(&self, args_raw: &str, cancel: &Cancel) -> Result<String> {
        let args: Args = parse_args(args_raw)?;
        let offset = positive("offset", args.offset)?.unwrap_or(1);
        let limit = positive("limit", args.limit)?.unwrap_or(DEFAULT_LINES);
        let path = resolve(&self.cwd, &args.path);
        let io = |e: std::io::Error| ToolError(format!("cannot read {}: {e}", args.path));
        let meta = fs::metadata(&path).map_err(|e| match e.kind() {
            std::io::ErrorKind::NotFound => ToolError(format!("file not found: {}", args.path)),
            _ => io(e),
        })?;
        if meta.is_dir() {
            return Err(format!("{} is a directory, not a file", args.path).into());
        }
        if !meta.is_file() {
            return Err(format!(
                "{} is not a regular file (a pipe, device or socket); read cannot show it",
                args.path
            )
            .into());
        }
        let mut reader = BufReader::with_capacity(64 * 1024, File::open(&path).map_err(io)?);
        if reader.fill_buf().map_err(io)?.contains(&0) {
            return Err(binary(&args.path));
        }
        let past_end = || {
            ToolError(format!(
                "offset {offset} is past the end of {}, which has fewer lines",
                args.path
            ))
        };
        if !skip_lines(&mut reader, offset - 1, cancel)? {
            return Err(past_end());
        }
        let mut sink = Sink::new(&self.limits, Keep::Head);
        let read = read_lines(&mut reader, limit, &mut sink, cancel, &args.path)?;
        if offset > 1 && read == 0 {
            return Err(past_end());
        }
        let more = read == limit && !reader.fill_buf().map_err(io)?.is_empty();
        let finished = sink.finish()?;
        let mut text = finished.text;
        if finished.truncated || more {
            let shown = if finished.truncated {
                u64::try_from(finished.kept_lines).unwrap_or(u64::MAX)
            } else {
                read
            }
            .max(1);
            let _ = write!(
                text,
                "\n[showing lines {offset}-{}; offset={} continues]",
                offset + shown - 1,
                offset + shown
            );
        }
        Ok(text)
    }
}

/// Pass `n` lines; false if the file ended first.
fn skip_lines(reader: &mut BufReader<File>, n: u64, cancel: &Cancel) -> Result<bool> {
    let io = |e: std::io::Error| ToolError(format!("cannot read the file: {e}"));
    let mut seen = 0;
    let mut mid_line = false;
    while seen < n {
        cancel.check()?;
        let buf = reader.fill_buf().map_err(io)?;
        if buf.is_empty() {
            seen += u64::from(mid_line);
            break;
        }
        if let Some(i) = buf.iter().position(|&b| b == b'\n') {
            reader.consume(i + 1);
            seen += 1;
            mid_line = false;
        } else {
            let all = buf.len();
            reader.consume(all);
            mid_line = true;
        }
    }
    Ok(seen >= n)
}

/// Push up to `limit` lines into `sink`; the number of lines pushed.
fn read_lines(
    reader: &mut BufReader<File>,
    limit: u64,
    sink: &mut Sink,
    cancel: &Cancel,
    shown: &str,
) -> Result<u64> {
    let io = |e: std::io::Error| ToolError(format!("cannot read {shown}: {e}"));
    let mut read = 0;
    let mut mid_line = false;
    while read < limit {
        cancel.check()?;
        let buf = reader.fill_buf().map_err(io)?;
        if buf.is_empty() {
            read += u64::from(mid_line);
            break;
        }
        let (take, ends) = match buf.iter().position(|&b| b == b'\n') {
            Some(i) => (i + 1, true),
            None => (buf.len(), false),
        };
        if buf[..take].contains(&0) {
            return Err(binary(shown));
        }
        sink.push(&buf[..take])?;
        reader.consume(take);
        if ends {
            read += 1;
        }
        mid_line = !ends;
    }
    Ok(read)
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

    fn tool(max: usize) -> (Read, tempfile::TempDir) {
        let dir = tempfile::tempdir().unwrap();
        let limits = Limits::in_dir(max, dir.path().join("out")).unwrap();
        (Read::new(dir.path().to_owned(), limits), dir)
    }

    fn call(tool: &Read, args: &Value) -> Result<String> {
        tool.run(&args.to_string(), &Cancel::new())
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
    fn a_last_line_without_a_newline_counts() {
        let (tool, dir) = tool(1000);
        fs::write(dir.path().join("a.txt"), "1\n2\n3").unwrap();
        let out = call(&tool, &json!({"path": "a.txt", "offset": 3})).unwrap();
        assert_eq!(out, "3");
        let out = call(&tool, &json!({"path": "a.txt", "limit": 3})).unwrap();
        assert_eq!(out, "1\n2\n3");
    }

    #[test]
    fn offset_and_limit_select_lines_and_say_how_to_go_on() {
        let (tool, dir) = tool(1000);
        fs::write(dir.path().join("a.txt"), "1\n2\n3\n4\n5\n").unwrap();
        let out = call(&tool, &json!({"path": "a.txt", "offset": 2, "limit": 2})).unwrap();
        assert_eq!(out, "2\n3\n\n[showing lines 2-3; offset=4 continues]");
        let out = call(&tool, &json!({"path": "a.txt", "offset": 4, "limit": 2})).unwrap();
        assert_eq!(out, "4\n5\n");
    }

    #[test]
    fn errors_name_the_problem() {
        let (tool, dir) = tool(1000);
        fs::create_dir(dir.path().join("d")).unwrap();
        fs::write(dir.path().join("b.bin"), b"ab\0cd").unwrap();
        fs::write(
            dir.path().join("late.bin"),
            [b"x\n".repeat(100), b"\0".to_vec()].concat(),
        )
        .unwrap();
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
    fn a_nul_after_the_first_buffer_is_still_binary() {
        let (tool, dir) = tool(1_000_000);
        let mut bytes = b"x\n".repeat(100_000);
        bytes.extend_from_slice(b"y\0\n");
        fs::write(dir.path().join("late.bin"), bytes).unwrap();
        let err = call(&tool, &json!({"path": "late.bin", "limit": 200_000})).unwrap_err();
        assert!(err.0.contains("binary file"), "{err}");
    }

    #[test]
    fn a_pipe_is_refused_not_opened() {
        let (tool, dir) = tool(1000);
        let fifo = dir.path().join("pipe");
        let made = std::process::Command::new("mkfifo")
            .arg(&fifo)
            .status()
            .unwrap();
        assert!(made.success());
        let err = call(&tool, &json!({"path": "pipe"})).unwrap_err();
        assert!(err.0.contains("not a regular file"), "{err}");
    }

    #[test]
    fn a_cancelled_read_stops_with_an_error() {
        let (tool, dir) = tool(1000);
        fs::write(dir.path().join("a.txt"), "1\n").unwrap();
        let cancel = Cancel::new();
        cancel.cancel();
        let err = tool
            .run(&json!({"path": "a.txt"}).to_string(), &cancel)
            .unwrap_err();
        assert!(err.0.contains("cancelled"), "{err}");
    }

    #[test]
    fn long_files_are_cut_to_the_head_and_the_hint_continues_after_the_kept_lines() {
        let (tool, dir) = tool(20);
        let body =
            "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\n";
        fs::write(dir.path().join("a.txt"), body).unwrap();
        let out = call(&tool, &json!({"path": "a.txt"})).unwrap();
        assert!(
            out.starts_with("line 1\nline 2\nline 3\n[truncated: 7 lines omitted after these;"),
            "{out}"
        );
        assert!(
            out.ends_with("\n[showing lines 1-3; offset=4 continues]"),
            "{out}"
        );
        let spilled = fs::read_dir(dir.path().join("out"))
            .unwrap()
            .next()
            .unwrap()
            .unwrap();
        assert_eq!(fs::read_to_string(spilled.path()).unwrap(), body);
        let out = call(&tool, &json!({"path": "a.txt", "offset": 4})).unwrap();
        assert!(out.starts_with("line 4\nline 5\nline 6\n"), "{out}");
        assert!(
            out.ends_with("[showing lines 4-6; offset=7 continues]"),
            "{out}"
        );
    }

    #[test]
    fn invalid_utf8_is_replaced_and_said_so() {
        let (tool, dir) = tool(1000);
        fs::write(dir.path().join("l.txt"), b"caf\xe9\n").unwrap();
        let out = call(&tool, &json!({"path": "l.txt"})).unwrap();
        assert_eq!(out, format!("caf\u{FFFD}\n\n{}", crate::limit::LOSSY_NOTE));
    }
}
