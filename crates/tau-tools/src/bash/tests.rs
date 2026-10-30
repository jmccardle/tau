use super::*;

/// Starts `sleep 13` in a session of its own and returns once it is there (a
/// bare `setsid ... &` races with the command's own end), saving its pid.
const LEAVE: &str = "setsid sleep 13 & p=$!; until [ \"$(cut -d' ' -f6 /proc/$p/stat)\" = \"$p\" ]; \
                     do sleep 0.02; done; echo $p > pid;";

/// Whether a live process has `marker` in its command line.
/// A sleep duration, about `30 + tag` seconds, that no other process has: it ends in this
/// test process's pid, so suites running side by side (other worktrees, CI) cannot see each
/// other's sleeps. A `tag` for each place in a test that starts one.
fn unique_sleep(tag: u32) -> String {
    format!("{}.{}", 30 + tag, std::process::id())
}

/// The part of a command line that is exactly the argument `arg` (NUL-separated on both sides).
fn exactly(arg: &str) -> String {
    format!("\0{arg}\0")
}

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
    let (first, second) = (unique_sleep(1), unique_sleep(2));
    let out = run(
        &tool,
        &json!({"command": format!("timeout 30 sleep {first} & echo ok")}),
    )
    .unwrap();
    assert_eq!(out, "ok\n");
    assert!(
        !running(&exactly(&first)),
        "a process of the session survived"
    );
    let err = run(
        &tool,
        &json!({"command": format!("timeout 30 sleep {second}; echo after"), "timeout": 300}),
    )
    .unwrap_err();
    assert!(err.0.contains("timed out after 300 ms"), "{err}");
    assert!(
        !running(&exactly(&second)),
        "a process of the session survived"
    );
}

#[test]
fn killing_right_after_start_works_every_time() {
    let (tool, _dir) = tool(10_000);
    let started = Instant::now();
    let sleep = unique_sleep(3);
    for _ in 0..20 {
        let handle = tool.start(&json!({"command": format!("sleep {sleep}")}).to_string());
        handle.kill();
        let err = handle.wait().unwrap_err();
        assert_eq!(err.0, "command was aborted");
    }
    assert!(
        started.elapsed() < Duration::from_secs(20),
        "{:?}",
        started.elapsed()
    );
    assert!(!running(&exactly(&sleep)), "a command survived its kill");
}

#[test]
fn dropping_right_after_start_is_prompt_and_kills() {
    let (tool, _dir) = tool(10_000);
    let sleep = unique_sleep(4);
    for _ in 0..20 {
        let handle = tool.start(&json!({"command": format!("sleep {sleep}")}).to_string());
        let dropped = Instant::now();
        drop(handle);
        assert!(
            dropped.elapsed() < Duration::from_secs(1),
            "{:?}",
            dropped.elapsed()
        );
    }
    assert!(!running(&exactly(&sleep)), "a command survived its drop");
}

#[test]
fn an_escaped_process_with_no_output_leaves_a_clean_note() {
    let (tool, dir) = tool(10_000);
    let out = run(&tool, &json!({"command": format!("{LEAVE} true")})).unwrap();
    assert_eq!(out, format!("[output may be cut off: {ESCAPED}]"));
    end_process(read_pid(&dir));
}
