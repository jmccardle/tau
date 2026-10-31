use super::*;
use std::fs;

fn limits(max: usize, dir: &tempfile::TempDir) -> Limits {
    Limits::in_dir(max, dir.path().join("tau")).unwrap()
}

fn run<C: AsRef<[u8]>>(keep: Keep, max: usize, chunks: &[C]) -> (String, tempfile::TempDir) {
    let dir = tempfile::tempdir().unwrap();
    let mut sink = Sink::new(&limits(max, &dir), keep);
    for chunk in chunks {
        sink.push(chunk.as_ref());
    }
    (sink.finish().text, dir)
}

fn overflow_of(dir: &tempfile::TempDir) -> Vec<u8> {
    let mut files: Vec<_> = fs::read_dir(dir.path().join("tau")).unwrap().collect();
    assert_eq!(files.len(), 1);
    let path = files.pop().unwrap().unwrap().path();
    assert_eq!(path.extension().unwrap(), "out");
    assert_eq!(path.file_stem().unwrap().len(), ID_LEN);
    fs::read(path).unwrap()
}

#[test]
fn within_the_limit_nothing_is_written() {
    let (text, dir) = run(Keep::Head, 10, &[b"abc\n", b"def\n"][..]);
    assert_eq!(text, "abc\ndef\n");
    assert!(!dir.path().join("tau").exists());
}

#[test]
fn exactly_the_limit_is_not_truncated() {
    let (text, dir) = run(Keep::Tail, 8, &[b"abc\ndef\n"]);
    assert_eq!(text, "abc\ndef\n");
    assert!(!dir.path().join("tau").exists());
}

#[test]
fn head_keeps_whole_lines_and_counts_the_rest() {
    let (text, dir) = run(Keep::Head, 10, &[b"aaaa\nbbbb\ncccc\ndddd\n"]);
    assert!(
        text.starts_with("aaaa\nbbbb\n[truncated: 2 lines omitted after these;"),
        "{text}"
    );
    assert!(text.contains("full output (20 bytes) is in "));
    assert_eq!(overflow_of(&dir), b"aaaa\nbbbb\ncccc\ndddd\n");
}

#[test]
fn head_boundary_exactly_on_a_newline() {
    let (text, _dir) = run(Keep::Head, 9, &[b"aaaa\nbbbb\ncccc\n"]);
    assert!(
        text.starts_with("aaaa\nbbbb\n[truncated: 1 line omitted"),
        "{text}"
    );
}

#[test]
fn tail_keeps_whole_lines_and_counts_the_rest() {
    let (text, dir) = run(Keep::Tail, 10, &[b"aaaa\nbbbb\ncccc\ndddd\n"]);
    assert!(
        text.starts_with("[truncated: 2 lines omitted before these;"),
        "{text}"
    );
    assert!(text.ends_with("]\ncccc\ndddd\n"), "{text}");
    assert_eq!(overflow_of(&dir), b"aaaa\nbbbb\ncccc\ndddd\n");
}

#[test]
fn tail_with_a_last_line_lacking_a_newline() {
    let (text, _dir) = run(Keep::Tail, 10, &[b"aaaa\nbbbb\ncccc\ndddd"]);
    assert!(text.contains("2 lines omitted before"), "{text}");
    assert!(text.ends_with("]\ncccc\ndddd"), "{text}");
}

#[test]
fn chunks_may_split_lines_and_the_file_equals_the_whole() {
    let whole = b"one\ntwo\nthree\nfour\nfive\nsix\n";
    let parts: Vec<&[u8]> = whole.chunks(3).collect();
    for keep in [Keep::Head, Keep::Tail] {
        let (text, dir) = run(keep, 12, &parts);
        assert_eq!(overflow_of(&dir), whole);
        assert!(text.contains("full output (28 bytes)"), "{text}");
    }
}

#[test]
fn a_single_long_line_is_cut_on_a_character() {
    // 'é' is two bytes: a limit of 5 must not split the third one.
    let line = "ééééé\n".to_owned();
    for keep in [Keep::Head, Keep::Tail] {
        let (text, _dir) = run(keep, 5, &[line.as_bytes()]);
        assert!(text.contains("one long line cut short"), "{text}");
        assert!(std::str::from_utf8(text.as_bytes()).is_ok());
        assert!(!text.contains('\u{FFFD}'), "{text}");
    }
}

#[test]
fn multibyte_text_across_lines_is_never_split() {
    let text = "日本語\n".repeat(20);
    for keep in [Keep::Head, Keep::Tail] {
        for max in 1..40 {
            let (out, _dir) = run(keep, max, &[text.as_bytes()]);
            assert!(!out.contains('\u{FFFD}'), "max {max}: {out}");
        }
    }
}

#[test]
fn tail_keeps_the_end_when_the_window_has_been_trimmed() {
    let many: Vec<u8> = (0..1000)
        .flat_map(|i| format!("line {i}\n").into_bytes())
        .collect();
    let chunks: Vec<&[u8]> = many.chunks(7).collect();
    let (text, dir) = run(Keep::Tail, 50, &chunks);
    assert!(text.ends_with("line 999\n"), "{text}");
    assert_eq!(overflow_of(&dir), many);
    let kept = text.split_once("]\n").unwrap().1;
    assert!(kept.len() <= 50);
    let first: usize = kept
        .lines()
        .next()
        .unwrap()
        .strip_prefix("line ")
        .unwrap()
        .parse()
        .unwrap();
    assert!(
        text.contains(&format!("{first} lines omitted before")),
        "{text}"
    );
}

/// The note of an answer whose full output could not be saved: the kept text is
/// still there, and the reason is named.
fn unsaved_note(limits: &Limits) -> String {
    let mut sink = Sink::new(limits, Keep::Head);
    sink.push(b"too long for four");
    let text = sink.finish().text;
    assert!(text.starts_with("too"), "{text}");
    assert!(
        text.contains("the full output (17 bytes) was not saved"),
        "{text}"
    );
    text
}

#[test]
fn an_unusable_overflow_directory_costs_the_file_not_the_call() {
    let dir = tempfile::tempdir().unwrap();
    let blocker = dir.path().join("blocker");
    fs::write(&blocker, "x").unwrap();
    let text = unsaved_note(&Limits::in_dir(4, blocker.join("tau")).unwrap());
    assert!(text.contains("could not be created"), "{text}");
}

#[test]
fn a_zero_limit_is_refused() {
    assert!(Limits::new(0, 10, PathBuf::from("/x")).is_err());
}

#[test]
fn the_directory_is_private_and_the_file_is_0600() {
    use std::os::unix::fs::PermissionsExt;
    let (_text, dir) = run(Keep::Head, 5, &[b"too long for five"]);
    let tau = dir.path().join("tau");
    assert_eq!(
        fs::metadata(&tau).unwrap().permissions().mode() & 0o777,
        0o700
    );
    let file = fs::read_dir(&tau).unwrap().next().unwrap().unwrap().path();
    assert_eq!(
        fs::metadata(file).unwrap().permissions().mode() & 0o777,
        0o600
    );
}

#[test]
fn a_directory_open_to_others_is_refused_when_the_file_is_needed() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let open = dir.path().join("open");
    fs::create_dir(&open).unwrap();
    fs::set_permissions(&open, fs::Permissions::from_mode(0o755)).unwrap();
    // Building the limits is fine; the check is at the first spill.
    let text = unsaved_note(&Limits::in_dir(4, open.clone()).unwrap());
    assert!(text.contains("has mode 0755"), "{text}");
    assert_eq!(fs::read_dir(&open).unwrap().count(), 0, "no fallback file");
}

#[test]
fn a_symlink_as_the_directory_is_refused_when_the_file_is_needed() {
    let dir = tempfile::tempdir().unwrap();
    let real = dir.path().join("real");
    fs::create_dir(&real).unwrap();
    let link = dir.path().join("link");
    std::os::unix::fs::symlink(&real, &link).unwrap();
    let text = unsaved_note(&Limits::in_dir(4, link).unwrap());
    assert!(text.contains("is a symlink"), "{text}");
    assert_eq!(fs::read_dir(&real).unwrap().count(), 0);
}

#[test]
fn a_directory_of_another_user_is_refused_by_the_check_on_the_open_directory() {
    // `dir_problem` is what the open directory's fstat is judged by; a
    // squatter's directory differs from ours in the owner.
    let ours = Uid::effective().as_raw();
    assert!(dir_problem(ours, 0o40700).is_none());
    let foreign = dir_problem(ours + 1, 0o40700).unwrap();
    assert!(foreign.contains("is owned by uid"), "{foreign}");
}

#[test]
fn files_older_than_a_day_go_when_the_directory_is_opened() {
    let dir = tempfile::tempdir().unwrap();
    let tau = dir.path().join("tau");
    fs::create_dir(&tau).unwrap();
    fs::set_permissions(&tau, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
    let aged = |name: &str, age: Duration| {
        let file = File::create(tau.join(name)).unwrap();
        file.set_modified(SystemTime::now() - age).unwrap();
    };
    aged("old00000.out", Duration::from_secs(2 * 24 * 60 * 60));
    aged("new00000.out", Duration::from_secs(60 * 60));
    aged("other.txt", Duration::from_secs(2 * 24 * 60 * 60));
    aged("notes.out", Duration::from_secs(2 * 24 * 60 * 60));
    aged("Old00000.out", Duration::from_secs(2 * 24 * 60 * 60));
    let mut sink = Sink::new(&Limits::in_dir(4, tau.clone()).unwrap(), Keep::Head);
    sink.push(b"too long for four");
    drop(sink.finish());
    assert!(!tau.join("old00000.out").exists());
    assert!(tau.join("new00000.out").exists());
    assert!(tau.join("other.txt").exists(), "only .out files are ours");
    assert!(
        tau.join("notes.out").exists(),
        "only names we generate are ours"
    );
    assert!(
        tau.join("Old00000.out").exists(),
        "only names we generate are ours"
    );
}

#[test]
fn the_default_directory_follows_xdg_runtime_dir_or_the_temp_dir() {
    // Reads the environment of this test process; it sets nothing.
    let dir = Limits::default_dir();
    match std::env::var_os("XDG_RUNTIME_DIR").filter(|d| !d.is_empty()) {
        Some(runtime) => assert_eq!(dir, PathBuf::from(runtime).join("tau")),
        None => assert_eq!(
            dir,
            std::env::temp_dir().join(format!("tau-{}", Uid::effective()))
        ),
    }
}

#[test]
fn the_overflow_file_is_capped_but_counting_goes_on() {
    let dir = tempfile::tempdir().unwrap();
    let limits = Limits::new(10, 25, dir.path().join("tau")).unwrap();
    let mut sink = Sink::new(&limits, Keep::Head);
    let text = "line 0\nline 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\n"
        .to_owned();
    for chunk in text.as_bytes().chunks(7) {
        sink.push(chunk);
    }
    let finished = sink.finish();
    assert_eq!(overflow_of(&dir), &text.as_bytes()[..25]);
    assert!(
        finished
            .text
            .starts_with("line 0\n[truncated: 9 lines omitted after these;")
    );
    assert!(
        finished.text.contains("the output is 70 bytes but ")
            && finished.text.contains("holds only the first 25 bytes]"),
        "{}",
        finished.text
    );
}

#[test]
fn the_cap_note_speaks_in_mib_when_it_can() {
    let dir = tempfile::tempdir().unwrap();
    let limits = Limits::new(10, 2 * MIB, dir.path().join("tau")).unwrap();
    let mut sink = Sink::new(&limits, Keep::Tail);
    sink.push(&vec![b'x'; 3 * MIB]);
    assert!(sink.finish().text.contains("holds only the first 2 MiB]"));
}

#[test]
fn a_cap_below_the_limit_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    assert!(Limits::new(100, 99, dir.path().join("tau")).is_err());
}

#[test]
fn invalid_utf8_is_replaced_and_said_so_on_the_side_of_the_omission() {
    let (text, _dir) = run(Keep::Head, 100, &[b"caf\xe9\n"]);
    assert_eq!(text, format!("caf\u{FFFD}\n\n{LOSSY_NOTE}"));
    let (text, _dir) = run(Keep::Tail, 100, &[b"caf\xe9\n"]);
    assert_eq!(text, format!("{LOSSY_NOTE}\ncaf\u{FFFD}\n"));
}

#[test]
fn finish_reports_the_lines_kept() {
    let dir = tempfile::tempdir().unwrap();
    let mut sink = Sink::new(&limits(10, &dir), Keep::Head);
    sink.push(b"aaaa\nbbbb\ncccc\n");
    let finished = sink.finish();
    assert!(finished.truncated);
    assert_eq!(finished.kept_lines, 2);
}
