//! `bash`: a shell command, with a timeout and a real kill.
//!
//! [`Bash::start`] spawns `bash -c` as a session leader (`setsid` in the child
//! before `exec`, so the session exists when `start` returns): no controlling
//! terminal, and its process group id and session id are its pid. Stdin is
//! closed; stdout and stderr share one pipe, so their order is the order the
//! command wrote them. [`BashRun::wait`] reads the output and returns the
//! call's answer.
//!
//! Ending a command. [`BashRun::kill`] (any thread, any time) and the timeout
//! both send SIGTERM to the process group and to every other process of the
//! session (see [`crate::session`]), then SIGKILL after a grace period.
//! Whenever the shell has exited (it is waited for without being reaped, so its
//! pid cannot be reused), the rest of its session is killed, and only then is
//! it reaped. A process that started a session of its own (`setsid`) is out of
//! reach; if it keeps the output pipe open, the pipe is drained for a short
//! grace period after the shell exits and then closed, and the answer says so.
//!
//! The kept end of the output is the tail. A nonzero exit is an `Err` whose
//! text is the output followed by the exit code.

use std::io::{PipeReader, Read as _};
use std::os::fd::AsFd;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::PathBuf;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::{Condvar, Mutex, PoisonError};
use std::time::{Duration, Instant};

use nix::errno::Errno;
use nix::poll::{PollFd, PollFlags, PollTimeout, poll};
use nix::sys::signal::Signal;
use nix::sys::wait::{Id, WaitPidFlag, waitid};
use nix::unistd::Pid;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::limit::{Keep, Sink};
use crate::session;
use crate::{Limits, Result, ToolError, lock, parse_args};

/// The timeout when the call gives none, in milliseconds.
pub const DEFAULT_TIMEOUT_MS: u64 = 30_000;
/// The longest timeout a call may ask for, in milliseconds.
pub const DEFAULT_MAX_TIMEOUT_MS: u64 = 600_000;
/// Time between SIGTERM and SIGKILL, in milliseconds.
pub const DEFAULT_KILL_GRACE_MS: u64 = 2_000;
/// How long the output is still read after the shell exited.
const DRAIN_GRACE: Duration = Duration::from_millis(250);
/// How often the reader looks at the clock while the pipe is quiet.
const POLL_MS: u16 = 50;

const ESCAPED: &str = "a process outside the command's group still holds the output open";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Args {
    command: String,
    timeout: Option<i64>,
}

/// Timeouts of the tool, in milliseconds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BashConfig {
    /// The timeout of a call that gives none.
    pub default_timeout_ms: u64,
    /// The longest timeout a call may ask for.
    pub max_timeout_ms: u64,
    /// Time between SIGTERM and SIGKILL.
    pub kill_grace_ms: u64,
}

impl Default for BashConfig {
    fn default() -> Self {
        Self {
            default_timeout_ms: DEFAULT_TIMEOUT_MS,
            max_timeout_ms: DEFAULT_MAX_TIMEOUT_MS,
            kill_grace_ms: DEFAULT_KILL_GRACE_MS,
        }
    }
}

#[derive(Clone, Debug)]
pub struct Bash {
    cwd: PathBuf,
    limits: Limits,
    config: BashConfig,
}

impl Bash {
    /// # Errors
    /// A zero timeout or grace, or a default timeout over the maximum.
    pub fn new(cwd: PathBuf, limits: Limits, config: BashConfig) -> Result<Self> {
        let BashConfig {
            default_timeout_ms,
            max_timeout_ms,
            kill_grace_ms,
        } = config;
        if default_timeout_ms == 0 || max_timeout_ms == 0 || kill_grace_ms == 0 {
            return Err("the timeouts and the kill grace must be at least 1 ms".into());
        }
        if default_timeout_ms > max_timeout_ms {
            return Err(format!(
                "the default timeout ({default_timeout_ms} ms) is over the maximum \
                 ({max_timeout_ms} ms)"
            )
            .into());
        }
        Ok(Self {
            cwd,
            limits,
            config,
        })
    }

    #[must_use]
    pub fn description(&self) -> String {
        format!(
            "Run a command with `bash -c` in the working directory. Standard output and error \
             arrive interleaved. A nonzero exit is an error and its code ends the text. After \
             `timeout` milliseconds (default {}, at most {}) the command and everything it \
             started are terminated. Output past {} bytes keeps its last whole lines; the full \
             output is then saved to a file named in a note.",
            self.config.default_timeout_ms, self.config.max_timeout_ms, self.limits.max_bytes
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
                    "description": "Milliseconds before the command is terminated"
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
            None => self.config.default_timeout_ms,
            Some(ms) if ms < 1 => {
                return Err(format!("`timeout` must be at least 1 ms, not {ms}").into());
            }
            Some(ms) if ms.unsigned_abs() > self.config.max_timeout_ms => {
                return Err(format!(
                    "`timeout` of {ms} ms is over the maximum of {} ms",
                    self.config.max_timeout_ms
                )
                .into());
            }
            Some(ms) => ms.unsigned_abs(),
        };
        let (reader, writer) = std::io::pipe().map_err(spawn_failed)?;
        let child = {
            // The command holds the writer ends; they must be gone from here
            // before we read, or the pipe never ends.
            let mut command = Command::new("bash");
            command
                .arg("-c")
                .arg(&args.command)
                .current_dir(&self.cwd)
                .stdin(Stdio::null())
                .stdout(writer.try_clone().map_err(spawn_failed)?)
                .stderr(writer);
            // SAFETY: the closure runs in the forked child before `exec`, where
            // only async-signal-safe calls are allowed. `setsid(2)` is one, and
            // it touches no memory of ours: no allocation, no lock. The error
            // path builds an `io::Error` from the errno, which does not allocate.
            #[allow(unsafe_code)]
            unsafe {
                command.pre_exec(|| {
                    nix::unistd::setsid()
                        .map(drop)
                        .map_err(std::io::Error::from)
                });
            }
            command.spawn().map_err(|e| {
                ToolError(format!(
                    "could not start the command (does the working directory {} exist, and is \
                     `bash` installed?): {e}",
                    self.cwd.display()
                ))
            })?
        };
        let pgid = Pid::from_raw(i32::try_from(child.id()).map_err(spawn_failed)?);
        Ok(Running {
            pgid,
            child: Mutex::new(Some(child)),
            reader: Mutex::new(Some(reader)),
            ctl: Mutex::new(Ctl::default()),
            wake: Condvar::new(),
            timeout: Duration::from_millis(timeout_ms),
            grace: Duration::from_millis(self.config.kill_grace_ms),
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

/// What the threads of one `wait` share, under one lock.
// Flags of a state machine, each set once; a bitset would say less.
#[allow(clippy::struct_excessive_bools)]
#[derive(Debug, Default)]
struct Ctl {
    /// The shell has been reaped: its pid may be reused, so no more signals.
    reaped: bool,
    exited_at: Option<Instant>,
    status: Option<std::result::Result<ExitStatus, String>>,
    kill_requested: bool,
    timed_out: bool,
    term_sent: Option<Instant>,
    kill_sent: bool,
    escaped: bool,
    signal_error: Option<ToolError>,
}

#[derive(Debug)]
struct Running {
    pgid: Pid,
    child: Mutex<Option<Child>>,
    reader: Mutex<Option<PipeReader>>,
    ctl: Mutex<Ctl>,
    wake: Condvar,
    timeout: Duration,
    grace: Duration,
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

    /// Ask for the command and its process group to be terminated (SIGTERM,
    /// then SIGKILL after the grace period). Does not block; the `wait` in
    /// progress answers. Safe at any time, from any thread, any number of
    /// times; a no-op once the command has ended.
    pub fn kill(&self) {
        if let Inner::Running(running) = &self.0 {
            lock(&running.ctl).kill_requested = true;
            running.wake.notify_all();
        }
    }
}

/// Makes the thread that reaps the shell always end the wait, even by a panic:
/// without it `supervise` would wait for a reap that never comes.
struct ReapGuard<'a>(&'a Running);

impl Drop for ReapGuard<'_> {
    fn drop(&mut self) {
        let mut ctl = lock(&self.0.ctl);
        if !ctl.reaped {
            // Cleanup after a panic; the panic itself is reported by the join.
            let _ = session::kill_all(self.0.pgid);
            ctl.reaped = true;
            ctl.exited_at.get_or_insert_with(Instant::now);
            ctl.status = Some(Err("the thread that waits for the command panicked".into()));
        }
        drop(ctl);
        self.0.wake.notify_all();
    }
}

impl Drop for Running {
    fn drop(&mut self) {
        // A run that was never waited on must not leave its command running.
        // Cleanup: there is no caller left to tell about a failure.
        let child = self.child.get_mut().unwrap_or_else(PoisonError::into_inner);
        if let Some(mut child) = child.take() {
            let _ = session::kill_all(self.pgid);
            let _ = child.wait();
        }
    }
}

impl Running {
    /// Signal the session, unless the shell is gone (its pid may be someone else's).
    fn signal(&self, ctl: &mut Ctl, signal: Signal) {
        if ctl.reaped {
            return;
        }
        let sent = if signal == Signal::SIGKILL {
            session::kill_all(self.pgid)
        } else {
            session::signal_all(self.pgid, signal)
        };
        if let Err(e) = sent {
            ctl.signal_error.get_or_insert(e);
        }
    }

    fn wait(&self) -> Result<String> {
        let child = lock(&self.child).take();
        let reader = lock(&self.reader).take();
        let (Some(child), Some(reader)) = (child, reader) else {
            return Err("this command was already waited on".into());
        };
        let helpers_died = || ToolError("a helper thread of the bash tool panicked".into());
        let output = std::thread::scope(|scope| {
            let output_thread = scope.spawn(|| self.read_output(reader));
            let reap_thread = scope.spawn(|| self.reap(child));
            self.supervise();
            let reaped = reap_thread.join();
            let output = output_thread.join();
            reaped.map_err(|_| helpers_died())?;
            output.map_err(|_| helpers_died())?
        });
        let ctl = lock(&self.ctl);
        if let Some(e) = &ctl.signal_error {
            return Err(e.clone());
        }
        let text = output?;
        let status = match &ctl.status {
            Some(Ok(status)) => *status,
            Some(Err(e)) => return Err(ToolError(format!("could not wait for the command: {e}"))),
            None => return Err("the command was never reaped".into()),
        };
        self.answer(&ctl, &text, status)
    }

    /// Terminate the group when asked to or when the timeout passes: SIGTERM,
    /// then SIGKILL after the grace period. Returns when the shell is reaped.
    fn supervise(&self) {
        let deadline = Instant::now() + self.timeout;
        let mut ctl = lock(&self.ctl);
        while !ctl.reaped {
            let now = Instant::now();
            let next = match ctl.term_sent {
                None if ctl.kill_requested || now >= deadline => {
                    ctl.timed_out = !ctl.kill_requested;
                    self.signal(&mut ctl, Signal::SIGTERM);
                    ctl.term_sent = Some(now);
                    continue;
                }
                None => Some(deadline),
                Some(sent) if !ctl.kill_sent && now >= sent + self.grace => {
                    self.signal(&mut ctl, Signal::SIGKILL);
                    ctl.kill_sent = true;
                    continue;
                }
                Some(sent) if !ctl.kill_sent => Some(sent + self.grace),
                Some(_) => None,
            };
            ctl = match next {
                Some(at) => {
                    self.wake
                        .wait_timeout(ctl, at.saturating_duration_since(now))
                        .unwrap_or_else(PoisonError::into_inner)
                        .0
                }
                None => self.wake.wait(ctl).unwrap_or_else(PoisonError::into_inner),
            };
        }
    }

    /// Wait for the shell to exit without reaping it, kill what it left in the
    /// group, then reap it.
    fn reap(&self, mut child: Child) {
        let _guard = ReapGuard(self);
        let waited = loop {
            match waitid(
                Id::Pid(self.pgid),
                WaitPidFlag::WEXITED | WaitPidFlag::WNOWAIT,
            ) {
                Err(Errno::EINTR) => {}
                other => break other.map(|_| ()),
            }
        };
        let mut ctl = lock(&self.ctl);
        self.signal(&mut ctl, Signal::SIGKILL);
        let status = child.wait().map_err(|e| e.to_string());
        ctl.status = Some(match (waited, status) {
            (Err(e), _) => Err(e.to_string()),
            (Ok(()), status) => status,
        });
        ctl.reaped = true;
        ctl.exited_at = Some(Instant::now());
        drop(ctl);
        self.wake.notify_all();
    }

    /// Read the pipe to its end into a tail-keeping sink. Once the shell has
    /// exited, only for [`DRAIN_GRACE`] more: a process that left the group
    /// can hold the pipe open forever. A failure of the sink terminates the
    /// command and is the call's error.
    fn read_output(&self, mut reader: PipeReader) -> Result<String> {
        let mut sink = Sink::new(&self.limits, Keep::Tail);
        let mut failure = None;
        let mut buf = vec![0_u8; 64 * 1024];
        loop {
            if lock(&self.ctl)
                .exited_at
                .is_some_and(|at| at.elapsed() >= DRAIN_GRACE)
            {
                lock(&self.ctl).escaped = true;
                break;
            }
            let mut fds = [PollFd::new(reader.as_fd(), PollFlags::POLLIN)];
            match poll(&mut fds, PollTimeout::from(POLL_MS)) {
                Ok(0) | Err(Errno::EINTR) => continue,
                Ok(_) => {}
                Err(e) => {
                    failure.get_or_insert(ToolError(format!("could not poll the output: {e}")));
                    self.kill_for_failure();
                    break;
                }
            }
            match reader.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    if failure.is_none()
                        && let Err(e) = sink.push(&buf[..n])
                    {
                        failure = Some(e);
                        self.kill_for_failure();
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                Err(e) => {
                    failure.get_or_insert(ToolError(format!("could not read the output: {e}")));
                    self.kill_for_failure();
                    break;
                }
            }
        }
        match failure {
            Some(e) => Err(e),
            None => Ok(sink.finish()?.text),
        }
    }

    fn kill_for_failure(&self) {
        lock(&self.ctl).kill_requested = true;
        self.wake.notify_all();
    }

    fn answer(&self, ctl: &Ctl, text: &str, status: ExitStatus) -> Result<String> {
        let escaped = ctl.escaped.then_some(ESCAPED);
        let trailer = if ctl.timed_out {
            format!(
                "command timed out after {} ms and was terminated",
                self.timeout.as_millis()
            )
        } else if ctl.kill_requested {
            "command was aborted".to_owned()
        } else if let Some(signal) = status.signal() {
            format!("command killed by signal {signal}")
        } else {
            match status.code() {
                Some(0) => {
                    return Ok(match escaped {
                        Some(cause) if text.is_empty() => {
                            format!("[output may be cut off: {cause}]")
                        }
                        Some(cause) => format!("{text}\n[output may be cut off: {cause}]"),
                        None => text.to_owned(),
                    });
                }
                Some(code) => format!("command exited with code {code}"),
                None => "command ended without an exit status".to_owned(),
            }
        };
        let trailer = match escaped {
            Some(cause) => format!("{trailer}; output may be cut off: {cause}"),
            None => trailer,
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

    /// Starts `sleep 13` in a session of its own and returns once it is there (a
    /// bare `setsid ... &` races with the command's own end), saving its pid.
    const LEAVE: &str = "setsid sleep 13 & p=$!; until [ \"$(cut -d' ' -f6 /proc/$p/stat)\" = \"$p\" ]; \
                         do sleep 0.02; done; echo $p > pid;";

    /// Whether a live process has `marker` in its command line.
    fn running(marker: &str) -> bool {
        let want = marker.as_bytes();
        std::fs::read_dir("/proc").unwrap().any(|item| {
            let path = item.unwrap().path();
            let Ok(cmdline) = std::fs::read(path.join("cmdline")) else {
                return false;
            };
            let alive =
                std::fs::read_to_string(path.join("stat")).is_ok_and(|stat| !stat.contains(") Z "));
            alive && cmdline.windows(want.len()).any(|w| w == want)
        })
    }

    fn config(grace: u64) -> BashConfig {
        BashConfig {
            default_timeout_ms: 10_000,
            max_timeout_ms: 20_000,
            kill_grace_ms: grace,
        }
    }

    fn tool_with(max: usize, config: BashConfig) -> (Bash, tempfile::TempDir) {
        let dir = tempfile::tempdir().unwrap();
        let limits = Limits::in_dir(max, dir.path().join(".out")).unwrap();
        (
            Bash::new(dir.path().to_owned(), limits, config).unwrap(),
            dir,
        )
    }

    fn tool(max: usize) -> (Bash, tempfile::TempDir) {
        tool_with(max, config(500))
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
        let err = run(&tool, &json!({"command": "kill -HUP $$"})).unwrap_err();
        assert_eq!(err.0, "command killed by signal 1");
    }

    #[test]
    fn runs_in_the_working_directory_with_stdin_closed() {
        let (tool, dir) = tool(10_000);
        let out = run(&tool, &json!({"command": "pwd; cat | wc -c"})).unwrap();
        let expected = dir.path().canonicalize().unwrap();
        assert_eq!(out, format!("{}\n0\n", expected.display()));
    }

    #[test]
    fn the_shell_leads_its_own_session_without_a_terminal() {
        let (tool, _dir) = tool(10_000);
        let out = run(&tool, &json!({"command": "cat /proc/$$/stat"})).unwrap();
        // pid (comm) state ppid pgrp session tty_nr
        let after = out.rsplit_once(") ").unwrap().1;
        let fields: Vec<&str> = after.split_whitespace().collect();
        let pid = out.split(' ').next().unwrap();
        assert_eq!(fields[2], pid, "pgrp is the pid: {out}");
        assert_eq!(fields[3], pid, "session is the pid: {out}");
        assert_eq!(fields[4], "0", "no controlling terminal: {out}");
    }

    #[test]
    fn bad_arguments_are_errors() {
        let (tool, _dir) = tool(100);
        let err = |args: Value| run(&tool, &args).unwrap_err().0;
        assert!(err(json!({})).contains("missing field `command`"));
        assert!(err(json!({"command": "  "})).contains("must not be empty"));
        assert!(err(json!({"command": "true", "timeout": 0})).contains("at least 1 ms"));
        assert_eq!(
            err(json!({"command": "true", "timeout": 20_001})),
            "`timeout` of 20001 ms is over the maximum of 20000 ms"
        );
    }

    #[test]
    fn the_timeouts_are_checked_at_construction() {
        let limits = Limits::in_dir(10, std::env::temp_dir().join("tau-unused")).unwrap();
        let build = |c| Bash::new(PathBuf::from("/"), limits.clone(), c);
        assert!(build(config(0)).is_err());
        let over = BashConfig {
            default_timeout_ms: 5,
            max_timeout_ms: 4,
            kill_grace_ms: 1,
        };
        assert!(build(over).is_err());
    }

    #[test]
    fn a_missing_working_directory_is_an_error_not_a_panic() {
        let limits = Limits::in_dir(100, std::env::temp_dir().join("tau-test-unused")).unwrap();
        let tool = Bash::new(PathBuf::from("/nonexistent/dir"), limits, config(500)).unwrap();
        let err = run(&tool, &json!({"command": "true"})).unwrap_err();
        assert!(err.0.starts_with("could not start the command"), "{err}");
    }

    #[test]
    fn timeout_terminates_the_command_and_its_children() {
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
            "before\n\ncommand timed out after 300 ms and was terminated"
        );
        let pid: i32 = read_pid(&dir);
        assert!(gone(pid), "the backgrounded sleep survived");
    }

    #[test]
    fn a_command_that_ignores_sigterm_is_killed_after_the_grace() {
        let (tool, _dir) = tool_with(1000, config(300));
        let started = Instant::now();
        let err = run(
            &tool,
            &json!({"command": "trap '' TERM; echo ready; while :; do sleep 0.05; done", "timeout": 200}),
        )
        .unwrap_err();
        let elapsed = started.elapsed();
        assert!(err.0.contains("timed out after 200 ms"), "{err}");
        assert!(
            elapsed >= Duration::from_millis(450),
            "SIGKILL waited for the grace: {elapsed:?}"
        );
        assert!(elapsed < Duration::from_secs(5), "{elapsed:?}");
    }

    #[test]
    fn kill_aborts_a_running_command() {
        let (tool, _dir) = tool(10_000);
        let started = Instant::now();
        let handle = tool.start(&json!({"command": "sleep 30", "timeout": 20000}).to_string());
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
    fn a_process_outside_the_group_does_not_hold_the_call_hostage() {
        let (tool, dir) = tool(10_000);
        let started = Instant::now();
        let err = run(
            &tool,
            &json!({"command": format!("echo hi; {LEAVE} sleep 30"), "timeout": 500}),
        )
        .unwrap_err();
        let elapsed = started.elapsed();
        assert!(elapsed < Duration::from_secs(5), "{elapsed:?}: {err}");
        assert!(
            err.0.starts_with("hi\n\ncommand timed out after 500 ms"),
            "{err}"
        );
        assert!(err.0.contains(ESCAPED), "{err}");
        end_process(read_pid(&dir));
    }

    #[test]
    fn an_escaped_process_after_a_normal_exit_is_an_ok_with_a_note() {
        let (tool, dir) = tool(10_000);
        let started = Instant::now();
        let out = run(&tool, &json!({"command": format!("{LEAVE} echo done")})).unwrap();
        assert!(started.elapsed() < Duration::from_secs(5));
        assert_eq!(out, format!("done\n\n[output may be cut off: {ESCAPED}]"));
        end_process(read_pid(&dir));
    }

    #[test]
    fn cancelling_while_an_escaped_process_holds_the_pipe_returns() {
        let (tool, dir) = tool(10_000);
        let handle = tool.start(&json!({"command": format!("{LEAVE} sleep 30")}).to_string());
        let started = Instant::now();
        std::thread::scope(|scope| {
            let waiting = scope.spawn(|| handle.wait());
            std::thread::sleep(Duration::from_millis(300));
            handle.kill();
            let err = waiting.join().unwrap().unwrap_err();
            assert!(err.0.contains("command was aborted"), "{err}");
            assert!(err.0.contains(ESCAPED), "{err}");
        });
        assert!(started.elapsed() < Duration::from_secs(5));
        end_process(read_pid(&dir));
    }

    #[test]
    fn what_the_shell_leaves_running_is_killed_when_it_exits() {
        let (tool, dir) = tool(10_000);
        run(&tool, &json!({"command": "sleep 30 & echo $! > pid"})).unwrap();
        assert!(gone(read_pid(&dir)));
    }

    #[test]
    fn dropping_a_run_that_was_not_waited_on_kills_it() {
        let (tool, dir) = tool(10_000);
        let handle = tool.start(&json!({"command": "echo $$ > pid; sleep 30"}).to_string());
        let pid = loop {
            if let Ok(text) = std::fs::read_to_string(dir.path().join("pid"))
                && text.ends_with('\n')
            {
                break text.trim().parse::<i32>().unwrap();
            }
            std::thread::sleep(Duration::from_millis(10));
        };
        drop(handle);
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

    fn read_pid(dir: &tempfile::TempDir) -> i32 {
        std::fs::read_to_string(dir.path().join("pid"))
            .unwrap()
            .trim()
            .parse()
            .unwrap()
    }

    /// Stop a process the test deliberately let escape.
    fn end_process(pid: i32) {
        let _ = std::process::Command::new("kill")
            .arg(pid.to_string())
            .status();
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

    #[test]
    fn processes_that_moved_to_their_own_group_are_killed_with_the_session() {
        // timeout(1) puts itself and its child in a group of their own.
        let (tool, _dir) = tool(10_000);
        let out = run(
            &tool,
            &json!({"command": "timeout 30 sleep 31.25 & echo ok"}),
        )
        .unwrap();
        assert_eq!(out, "ok\n");
        assert!(
            !running("sleep\x0031.25"),
            "a process of the session survived"
        );
        let err = run(
            &tool,
            &json!({"command": "timeout 30 sleep 31.5; echo after", "timeout": 300}),
        )
        .unwrap_err();
        assert!(err.0.contains("timed out after 300 ms"), "{err}");
        assert!(
            !running("sleep\x0031.5"),
            "a process of the session survived"
        );
    }

    #[test]
    fn killing_right_after_start_works_every_time() {
        let (tool, _dir) = tool(10_000);
        let started = Instant::now();
        for _ in 0..20 {
            let handle = tool.start(&json!({"command": "sleep 30.75"}).to_string());
            handle.kill();
            let err = handle.wait().unwrap_err();
            assert_eq!(err.0, "command was aborted");
        }
        assert!(
            started.elapsed() < Duration::from_secs(20),
            "{:?}",
            started.elapsed()
        );
        assert!(!running("30.75"), "a command survived its kill");
    }

    #[test]
    fn dropping_right_after_start_is_prompt_and_kills() {
        let (tool, _dir) = tool(10_000);
        for _ in 0..20 {
            let handle = tool.start(&json!({"command": "sleep 30.5"}).to_string());
            let dropped = Instant::now();
            drop(handle);
            assert!(
                dropped.elapsed() < Duration::from_secs(1),
                "{:?}",
                dropped.elapsed()
            );
        }
        assert!(!running("30.5"), "a command survived its drop");
    }

    #[test]
    fn an_escaped_process_with_no_output_leaves_a_clean_note() {
        let (tool, dir) = tool(10_000);
        let out = run(&tool, &json!({"command": format!("{LEAVE} true")})).unwrap();
        assert_eq!(out, format!("[output may be cut off: {ESCAPED}]"));
        end_process(read_pid(&dir));
    }
}
