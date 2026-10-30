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
//! Progress. [`BashRun::wait_with`] also hands the output on as it arrives, through a
//! [`progress::Stream`]: the reader only copies bytes into a bounded buffer, and a thread of
//! its own calls the consumer, so a slow consumer never slows the command or the answer. The
//! saved answer is the same with or without it.
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
use crate::progress;
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
        self.wait_with(None)
    }

    /// As [`BashRun::wait`], calling `progress` with the output as it arrives (see
    /// [`progress`] for how it is coalesced), from a thread of its own, until the output
    /// ends; the last call returns before this does.
    ///
    /// # Errors
    /// As [`BashRun::wait`].
    pub fn wait_with(&self, progress: Option<&(dyn Fn(String) + Sync)>) -> Result<String> {
        match &self.0 {
            Inner::Failed(e) => Err(e.clone()),
            Inner::Running(running) => running.wait(progress),
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

/// Ends the progress stream however the reader ends, a panic included.
struct CloseOnDrop<'a>(&'a progress::Stream);

impl Drop for CloseOnDrop<'_> {
    fn drop(&mut self) {
        self.0.close();
    }
}

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

    fn wait(&self, on_progress: Option<&(dyn Fn(String) + Sync)>) -> Result<String> {
        let child = lock(&self.child).take();
        let reader = lock(&self.reader).take();
        let (Some(child), Some(reader)) = (child, reader) else {
            return Err("this command was already waited on".into());
        };
        let helpers_died = || ToolError("a helper thread of the bash tool panicked".into());
        let stream = progress::Stream::new();
        let output = std::thread::scope(|scope| {
            let shown = on_progress.map(|emit| scope.spawn(|| stream.emit_until_closed(emit)));
            let output_thread = scope.spawn(|| {
                let _close = CloseOnDrop(&stream);
                self.read_output(reader, on_progress.is_some().then_some(&stream))
            });
            let reap_thread = scope.spawn(|| self.reap(child));
            self.supervise();
            let reaped = reap_thread.join();
            let output = output_thread.join();
            if let Some(shown) = shown {
                shown.join().map_err(|_| helpers_died())?;
            }
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
    fn read_output(
        &self,
        mut reader: PipeReader,
        shown: Option<&progress::Stream>,
    ) -> Result<String> {
        let mut sink = Sink::new(&self.limits, Keep::Tail);
        let mut failure = None;
        let mut buf = vec![0_u8; 64 * 1024];
        loop {
            if self.drain_over() {
                break;
            }
            let mut fds = [PollFd::new(reader.as_fd(), PollFlags::POLLIN)];
            match poll(&mut fds, PollTimeout::from(POLL_MS)) {
                Ok(0) | Err(Errno::EINTR) => continue,
                Ok(_) => {}
                Err(e) => {
                    self.fail(&mut failure, format!("could not poll the output: {e}"));
                    break;
                }
            }
            match reader.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    if let Some(shown) = shown {
                        shown.push(&buf[..n]);
                    }
                    self.absorb(&mut sink, &mut failure, &buf[..n]);
                }
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                Err(e) => {
                    self.fail(&mut failure, format!("could not read the output: {e}"));
                    break;
                }
            }
        }
        match failure {
            Some(e) => Err(e),
            None => Ok(sink.finish()?.text),
        }
    }

    /// Whether the shell exited more than [`DRAIN_GRACE`] ago; if so, the pipe
    /// is held open by a process that left the group, and the answer says so.
    fn drain_over(&self) -> bool {
        let mut ctl = lock(&self.ctl);
        let over = ctl.exited_at.is_some_and(|at| at.elapsed() >= DRAIN_GRACE);
        ctl.escaped |= over;
        over
    }

    /// Push a chunk of output, unless the sink has already failed.
    fn absorb(&self, sink: &mut Sink, failure: &mut Option<ToolError>, chunk: &[u8]) {
        if failure.is_none()
            && let Err(e) = sink.push(chunk)
        {
            self.fail(failure, e.0);
        }
    }

    /// Keep the first failure and terminate the command.
    fn fail(&self, failure: &mut Option<ToolError>, message: String) {
        failure.get_or_insert(ToolError(message));
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
mod tests;
