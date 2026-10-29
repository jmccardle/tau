//! `bash`: a shell command, with a timeout and a real kill.
//!
//! [`Bash::start`] spawns `bash -c` as the leader of its own process group,
//! with stdin closed and stdout and stderr on one pipe (so their order is the
//! order the command wrote them). [`BashRun::wait`] reads the output and
//! returns the call's answer; [`BashRun::kill`], callable from any thread while
//! `wait` runs, sends SIGKILL to the whole group, so the shell, its pipeline
//! and anything it backgrounded die together. The same kill ends a command
//! that outlives its timeout. When the shell exits, whatever it left running
//! in the group is killed too.
//!
//! The kept end of the output is the tail. A nonzero exit is an `Err` whose
//! text is the output followed by the exit code.

use std::io::{PipeReader, Read as _};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::PathBuf;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Condvar, Mutex};
use std::time::Duration;

use nix::errno::Errno;
use nix::sys::signal::{Signal, killpg};
use nix::unistd::Pid;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::limit::{Keep, Sink};
use crate::{Limits, Result, ToolError, parse_args};

/// The timeout when the call gives none, in milliseconds.
pub const DEFAULT_TIMEOUT_MS: u64 = 30_000;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Args {
    command: String,
    timeout: Option<i64>,
}

#[derive(Clone, Debug)]
pub struct Bash {
    cwd: PathBuf,
    limits: Limits,
    default_timeout_ms: u64,
}

impl Bash {
    /// # Errors
    /// A default timeout of zero.
    pub fn new(cwd: PathBuf, limits: Limits, default_timeout_ms: u64) -> Result<Self> {
        if default_timeout_ms == 0 {
            return Err("the default timeout must be at least 1 ms".into());
        }
        Ok(Self {
            cwd,
            limits,
            default_timeout_ms,
        })
    }

    #[must_use]
    pub fn description(&self) -> String {
        format!(
            "Run a command with `bash -c` in the working directory. Standard output and error \
             arrive interleaved. A nonzero exit is an error and its code ends the text. The \
             command is killed, with everything it started, after `timeout` milliseconds \
             (default {}). Output past {} bytes keeps its last whole lines; the full output is \
             then saved to a file named in a note.",
            self.default_timeout_ms, self.limits.max_bytes
        )
    }

    #[must_use]
    pub fn parameters() -> Value {
        json!({
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run"},
                "timeout": {
                    "type": "integer",
                    "description": "Milliseconds before the command is killed (default 30000)"
                }
            },
            "required": ["command"],
            "additionalProperties": false
        })
    }

    /// Spawn the command. Never fails: a bad call or a command that could not
    /// be started is a [`BashRun`] whose `wait` answers the `Err`.
    #[must_use]
    pub fn start(&self, args_raw: &str) -> BashRun {
        match self.spawn(args_raw) {
            Ok(running) => BashRun(Inner::Running(running)),
            Err(e) => BashRun(Inner::Failed(e)),
        }
    }

    fn spawn(&self, args_raw: &str) -> Result<Running> {
        let args: Args = parse_args(args_raw)?;
        if args.command.trim().is_empty() {
            return Err("`command` must not be empty".into());
        }
        let timeout_ms = match args.timeout {
            None => self.default_timeout_ms,
            Some(ms) if ms >= 1 => ms.unsigned_abs(),
            Some(ms) => return Err(format!("`timeout` must be at least 1 ms, not {ms}").into()),
        };
        let (reader, writer) = std::io::pipe().map_err(spawn_failed)?;
        let child = {
            // The command holds the writer ends; it must be gone before we read.
            let mut command = Command::new("bash");
            command
                .arg("-c")
                .arg(&args.command)
                .current_dir(&self.cwd)
                .stdin(Stdio::null())
                .stdout(writer.try_clone().map_err(spawn_failed)?)
                .stderr(writer)
                .process_group(0);
            command.spawn().map_err(spawn_failed)?
        };
        let pgid = Pid::from_raw(i32::try_from(child.id()).map_err(spawn_failed)?);
        Ok(Running {
            pgid,
            child: Mutex::new(Some(child)),
            reader: Mutex::new(Some(reader)),
            alive: Mutex::new(true),
            killed: AtomicBool::new(false),
            timed_out: AtomicBool::new(false),
            timeout: Duration::from_millis(timeout_ms),
            limits: self.limits.clone(),
        })
    }
}

fn spawn_failed(e: impl std::fmt::Display) -> ToolError {
    ToolError(format!("could not start the command: {e}"))
}

/// A started command: wait for it, or kill it. See the module docs.
#[derive(Debug)]
pub struct BashRun(Inner);

#[derive(Debug)]
enum Inner {
    Failed(ToolError),
    Running(Running),
}

#[derive(Debug)]
struct Running {
    pgid: Pid,
    child: Mutex<Option<Child>>,
    reader: Mutex<Option<PipeReader>>,
    /// True until the shell has been reaped; the group id is not ours after.
    alive: Mutex<bool>,
    killed: AtomicBool,
    timed_out: AtomicBool,
    timeout: Duration,
    limits: Limits,
}

impl BashRun {
    /// Block until the command ends, is killed, or times out.
    ///
    /// # Errors
    /// The command failed, was killed, or timed out; or this run was already
    /// waited on.
    pub fn wait(&self) -> Result<String> {
        match &self.0 {
            Inner::Failed(e) => Err(e.clone()),
            Inner::Running(running) => running.wait(),
        }
    }

    /// Kill the command and its process group. Safe at any time, from any
    /// thread, any number of times; a no-op once the command has ended.
    pub fn kill(&self) {
        if let Inner::Running(running) = &self.0 {
            running.kill();
        }
    }
}

impl Running {
    fn kill(&self) {
        self.killed.store(true, Ordering::SeqCst);
        let alive = self.alive.lock().unwrap();
        if *alive {
            self.signal_group();
        }
    }

    fn signal_group(&self) {
        match killpg(self.pgid, Signal::SIGKILL) {
            // ESRCH: the group is already gone, which is what we wanted.
            Ok(()) | Err(Errno::ESRCH) => {}
            Err(e) => unreachable!("killpg of our own process group failed: {e}"),
        }
    }

    fn wait(&self) -> Result<String> {
        let child = self.child.lock().unwrap().take();
        let reader = self.reader.lock().unwrap().take();
        let (Some(mut child), Some(reader)) = (child, reader) else {
            return Err("this command was already waited on".into());
        };
        let done = (Mutex::new(false), Condvar::new());
        let (status, output) = std::thread::scope(|scope| {
            let output = scope.spawn(|| self.read_output(reader));
            scope.spawn(|| self.watch(&done));
            let status = child.wait();
            // The shell is reaped; what it left in the group dies with it.
            {
                let mut alive = self.alive.lock().unwrap();
                self.signal_group();
                *alive = false;
            }
            *done.0.lock().unwrap() = true;
            done.1.notify_all();
            (status, output.join().expect("the output reader panicked"))
        });
        let text = output?;
        let status =
            status.map_err(|e| ToolError(format!("could not wait for the command: {e}")))?;
        self.answer(&text, status)
    }

    /// Kill the group when the timeout passes before `done` is set.
    fn watch(&self, done: &(Mutex<bool>, Condvar)) {
        let (finished, wake) = done;
        let (_guard, result) = wake
            .wait_timeout_while(finished.lock().unwrap(), self.timeout, |finished| {
                !*finished
            })
            .unwrap();
        if result.timed_out() {
            self.timed_out.store(true, Ordering::SeqCst);
            self.kill();
        }
    }

    /// Read the pipe to its end into a tail-keeping sink. A failure of the
    /// sink kills the command and is the call's error.
    fn read_output(&self, mut reader: PipeReader) -> Result<String> {
        let mut sink = Sink::new(&self.limits, Keep::Tail);
        let mut failure = None;
        let mut buf = vec![0_u8; 64 * 1024];
        loop {
            match reader.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    if failure.is_none()
                        && let Err(e) = sink.push(&buf[..n])
                    {
                        failure = Some(e);
                        self.kill();
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                Err(e) => {
                    failure.get_or_insert(ToolError(format!("could not read the output: {e}")));
                    self.kill();
                    break;
                }
            }
        }
        match failure {
            Some(e) => Err(e),
            None => sink.finish(),
        }
    }

    fn answer(&self, text: &str, status: ExitStatus) -> Result<String> {
        let trailer = if self.timed_out.load(Ordering::SeqCst) {
            format!(
                "command timed out after {} ms and was killed",
                self.timeout.as_millis()
            )
        } else if self.killed.load(Ordering::SeqCst) {
            "command was aborted".to_owned()
        } else if let Some(signal) = status.signal() {
            format!("command killed by signal {signal}")
        } else {
            match status.code() {
                Some(0) => return Ok(text.to_owned()),
                Some(code) => format!("command exited with code {code}"),
                None => "command ended without an exit status".to_owned(),
            }
        };
        let text = text.trim_end_matches('\n');
        Err(ToolError(if text.is_empty() {
            trailer
        } else {
            format!("{text}\n\n{trailer}")
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Instant;

    fn tool(max: usize) -> (Bash, tempfile::TempDir) {
        let dir = tempfile::tempdir().unwrap();
        let limits = Limits::in_dir(max, dir.path().join(".out")).unwrap();
        (
            Bash::new(dir.path().to_owned(), limits, 10_000).unwrap(),
            dir,
        )
    }

    fn run(tool: &Bash, args: &Value) -> Result<String> {
        tool.start(&args.to_string()).wait()
    }

    #[test]
    fn output_and_exit_codes() {
        let (tool, _dir) = tool(10_000);
        assert_eq!(
            run(&tool, &json!({"command": "echo hi; echo oops >&2"})).unwrap(),
            "hi\noops\n"
        );
        let err = run(&tool, &json!({"command": "echo partial; exit 3"})).unwrap_err();
        assert_eq!(err.0, "partial\n\ncommand exited with code 3");
        let err = run(&tool, &json!({"command": "kill -TERM $$"})).unwrap_err();
        assert_eq!(err.0, "command killed by signal 15");
    }

    #[test]
    fn runs_in_the_working_directory_with_stdin_closed() {
        let (tool, dir) = tool(10_000);
        let out = run(&tool, &json!({"command": "pwd; cat | wc -c"})).unwrap();
        let expected = dir.path().canonicalize().unwrap();
        assert_eq!(out, format!("{}\n0\n", expected.display()));
    }

    #[test]
    fn bad_arguments_are_errors() {
        let (tool, _dir) = tool(100);
        let err = |args: Value| run(&tool, &args).unwrap_err().0;
        assert!(err(json!({})).contains("missing field `command`"));
        assert!(err(json!({"command": "  "})).contains("must not be empty"));
        assert!(err(json!({"command": "true", "timeout": 0})).contains("at least 1 ms"));
    }

    #[test]
    fn a_missing_working_directory_is_an_error_not_a_panic() {
        let limits = Limits::in_dir(100, std::env::temp_dir().join("tau-test-unused")).unwrap();
        let tool = Bash::new(PathBuf::from("/nonexistent/dir"), limits, 1000).unwrap();
        let err = run(&tool, &json!({"command": "true"})).unwrap_err();
        assert!(err.0.starts_with("could not start the command"), "{err}");
    }

    #[test]
    fn timeout_kills_the_command_and_its_children() {
        let (tool, dir) = tool(10_000);
        let started = Instant::now();
        let err = run(
            &tool,
            &json!({"command": "echo before; sleep 30 & echo $! > pid; wait", "timeout": 300}),
        )
        .unwrap_err();
        assert!(started.elapsed() < Duration::from_secs(10));
        assert_eq!(
            err.0,
            "before\n\ncommand timed out after 300 ms and was killed"
        );
        let pid: i32 = std::fs::read_to_string(dir.path().join("pid"))
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        assert!(gone(pid), "the backgrounded sleep survived");
    }

    #[test]
    fn kill_aborts_a_running_command() {
        let (tool, _dir) = tool(10_000);
        let started = Instant::now();
        let handle = tool.start(&json!({"command": "sleep 30", "timeout": 60000}).to_string());
        std::thread::scope(|scope| {
            let waiting = scope.spawn(|| handle.wait());
            std::thread::sleep(Duration::from_millis(200));
            handle.kill();
            let err = waiting.join().unwrap().unwrap_err();
            assert_eq!(err.0, "command was aborted");
        });
        assert!(started.elapsed() < Duration::from_secs(10));
        handle.kill(); // after the end: a no-op
    }

    #[test]
    fn what_the_shell_leaves_running_is_killed_when_it_exits() {
        let (tool, dir) = tool(10_000);
        run(&tool, &json!({"command": "sleep 30 & echo $! > pid"})).unwrap();
        let pid: i32 = std::fs::read_to_string(dir.path().join("pid"))
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        assert!(gone(pid));
    }

    #[test]
    fn long_output_keeps_the_tail_and_the_exit_code() {
        let (tool, _dir) = tool(40);
        let err = run(&tool, &json!({"command": "seq 1 100; exit 2"})).unwrap_err();
        assert!(err.0.starts_with("[truncated: "), "{err}");
        assert!(
            err.0.ends_with("98\n99\n100\n\ncommand exited with code 2"),
            "{err}"
        );
    }

    /// Whether the process ends within two seconds. A killed process whose parent
    /// is gone stays a zombie in /proc until init reaps it; a zombie is gone.
    fn gone(pid: i32) -> bool {
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let alive = match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
                Ok(stat) => !stat.contains(") Z "),
                Err(_) => false,
            };
            if !alive {
                return true;
            }
            if Instant::now() > deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}
