//! Finding the server: `--socket`, or `--session` through its serve record, or the one server
//! that serves this directory. `$TAU_HOME/serve/<session id>.json` is the record `tau serve`
//! writes (docs/guide/quickstart.md): `{socket, pid, session, cwd, ...}`.

use std::{
    env, fs,
    path::{Path, PathBuf},
};

use serde::Deserialize;

/// What a serve record says that this client needs.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct Record {
    pub socket: PathBuf,
    pub session: String,
    pub cwd: PathBuf,
    pub pid: i64,
}

/// Where the user's τ lives: `$TAU_HOME` (absolute, or `~/...`), else `~/.tau`.
pub fn tau_home(tau_home: Option<&str>, home: Option<&str>) -> Result<PathBuf, String> {
    let from_home = |rest: &str| -> Result<PathBuf, String> {
        let home = home.filter(|h| !h.is_empty()).ok_or(
            "HOME is not set, so ~ and ~/.tau have no meaning; set TAU_HOME to an absolute path",
        )?;
        Ok(Path::new(home).join(rest))
    };
    match tau_home.filter(|t| !t.is_empty()) {
        None => from_home(".tau"),
        Some("~") => from_home(""),
        Some(t) if t.starts_with("~/") => from_home(&t[2..]),
        Some(t) if Path::new(t).is_absolute() => Ok(PathBuf::from(t)),
        Some(t) => Err(format!("TAU_HOME must be an absolute path, not {t:?}")),
    }
}

/// The user's τ from the environment.
pub fn tau_home_from_env() -> Result<PathBuf, String> {
    tau_home(
        env::var("TAU_HOME").ok().as_deref(),
        env::var("HOME").ok().as_deref(),
    )
}

/// Every serve record in `<home>/serve`, by session id. A file that is not a record is an error:
/// it is named, never skipped.
pub fn records(home: &Path) -> Result<Vec<Record>, String> {
    let dir = home.join("serve");
    let read = match fs::read_dir(&dir) {
        Ok(read) => read,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(format!("cannot read {}: {e}", dir.display())),
    };
    let mut found = Vec::new();
    for item in read {
        let path = item
            .map_err(|e| format!("cannot read {}: {e}", dir.display()))?
            .path();
        if path.extension().is_none_or(|e| e != "json") {
            continue;
        }
        let text = fs::read_to_string(&path)
            .map_err(|e| format!("cannot read {}: {e}", path.display()))?;
        let record = serde_json::from_str(&text)
            .map_err(|e| format!("{} is not a serve record: {e}", path.display()))?;
        found.push(record);
    }
    found.sort_by(|a: &Record, b| a.session.cmp(&b.session));
    Ok(found)
}

const NONE_RUNNING: &str = "start one with `tau serve -d` (this client does not start a server)";

/// The socket to dial. `socket` is `--socket`, `session` is `--session`; `home` is looked up
/// only if one of them needs it. With neither, the one served session whose directory is `cwd`.
///
/// A record whose server does not answer is stale (a crash left it) and does not count, as in
/// `tau`'s `live_server`: `alive` says whether the socket of a record answers, `Ok(false)` for
/// one that is gone or refuses, `Err` for anything that does not prove it dead. Records are not
/// deleted here: that is `tau`'s to do, and a client should not remove what a server that is
/// starting may be about to answer on. Several live servers are a usage error naming each, never
/// a guess.
pub async fn choose(
    socket: Option<&Path>,
    session: Option<&str>,
    home: &dyn Fn() -> Result<PathBuf, String>,
    cwd: &Path,
    alive: impl AsyncFn(&Path) -> Result<bool, String>,
) -> Result<PathBuf, String> {
    if let Some(socket) = socket {
        return Ok(socket.to_path_buf());
    }
    let home = home()?;
    let all = records(&home)?;
    if let Some(wanted) = session {
        let record = all
            .into_iter()
            .find(|r| r.session == wanted)
            .ok_or_else(|| {
                format!(
                    "no `tau serve` holds session {wanted} (no record {}); {NONE_RUNNING}, \
                 with --session {wanted}",
                    home.join("serve").join(format!("{wanted}.json")).display()
                )
            })?;
        return if alive(&record.socket).await? {
            Ok(record.socket)
        } else {
            Err(format!(
                "the record of session {wanted} is stale: nothing answers at {}; {NONE_RUNNING}, \
                 with --session {wanted}",
                record.socket.display()
            ))
        };
    }
    let elsewhere = all.iter().filter(|r| r.cwd != cwd).count();
    let mut here = Vec::new();
    let mut stale = 0;
    for record in all.into_iter().filter(|r| r.cwd == cwd) {
        if alive(&record.socket).await? {
            here.push(record);
        } else {
            stale += 1;
        }
    }
    match here.as_slice() {
        [one] => Ok(one.socket.clone()),
        [] => Err(format!(
            "no `tau serve` for {} ({elsewhere} for other directories, {stale} stale); \
             {NONE_RUNNING}, or name one with --session ID or --socket PATH",
            cwd.display(),
        )),
        many => {
            let named: Vec<String> = many
                .iter()
                .map(|r| {
                    format!(
                        "  --session {}   (pid {}, {})",
                        r.session,
                        r.pid,
                        r.socket.display()
                    )
                })
                .collect();
            Err(format!(
                "{} servers serve {}; say which:\n{}",
                many.len(),
                cwd.display(),
                named.join("\n")
            ))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn serve_dir(records: &[(&str, &str, &str)]) -> tempfile::TempDir {
        let home = tempfile::tempdir().unwrap();
        let dir = home.path().join("serve");
        fs::create_dir(&dir).unwrap();
        for (session, cwd, socket) in records {
            let text = format!(
                r#"{{"socket": "{socket}", "pid": 7, "session": "{session}", "cwd": "{cwd}",
                    "protocol": 1, "version": "x", "since": 0, "log": null}}"#
            );
            fs::write(dir.join(format!("{session}.json")), text).unwrap();
        }
        home
    }

    async fn all_alive(_: &Path) -> Result<bool, String> {
        Ok(true)
    }

    fn at(home: &tempfile::TempDir) -> impl Fn() -> Result<PathBuf, String> {
        let path = home.path().to_path_buf();
        move || Ok(path.clone())
    }

    #[test]
    fn tau_home_follows_the_environment_the_way_tau_does() {
        assert_eq!(
            tau_home(None, Some("/h")).unwrap(),
            PathBuf::from("/h/.tau")
        );
        assert_eq!(
            tau_home(Some("/t"), Some("/h")).unwrap(),
            PathBuf::from("/t")
        );
        assert_eq!(
            tau_home(Some("~/x"), Some("/h")).unwrap(),
            PathBuf::from("/h/x")
        );
        assert!(
            tau_home(Some("rel"), Some("/h"))
                .unwrap_err()
                .contains("absolute")
        );
        assert!(tau_home(None, None).unwrap_err().contains("HOME"));
    }

    #[tokio::test]
    async fn a_socket_flag_needs_no_record() {
        let got = choose(
            Some(Path::new("/s.sock")),
            None,
            &|| Err("unused".into()),
            Path::new("/w"),
            all_alive,
        )
        .await;
        assert_eq!(got.unwrap(), PathBuf::from("/s.sock"));
    }

    #[tokio::test]
    async fn a_session_is_found_through_its_record_whatever_its_directory() {
        let home = serve_dir(&[("a", "/other", "/a.sock"), ("b", "/w", "/b.sock")]);
        let got = choose(None, Some("a"), &at(&home), Path::new("/w"), all_alive).await;
        assert_eq!(got.unwrap(), PathBuf::from("/a.sock"));
        let missing = choose(None, Some("zzz"), &at(&home), Path::new("/w"), all_alive)
            .await
            .unwrap_err();
        assert!(
            missing.contains("zzz") && missing.contains("tau serve -d"),
            "{missing}"
        );
    }

    #[tokio::test]
    async fn with_neither_the_one_server_of_this_directory_is_taken() {
        let home = serve_dir(&[("a", "/other", "/a.sock"), ("b", "/w", "/b.sock")]);
        let got = choose(None, None, &at(&home), Path::new("/w"), all_alive)
            .await
            .unwrap();
        assert_eq!(got, PathBuf::from("/b.sock"));
    }

    #[tokio::test]
    async fn with_neither_and_no_server_here_the_error_names_tau_serve() {
        let home = serve_dir(&[("a", "/other", "/a.sock")]);
        let err = choose(None, None, &at(&home), Path::new("/w"), all_alive)
            .await
            .unwrap_err();
        assert!(err.contains("tau serve -d"), "{err}");
        let empty = tempfile::tempdir().unwrap();
        assert!(
            choose(None, None, &at(&empty), Path::new("/w"), all_alive)
                .await
                .unwrap_err()
                .contains("tau serve -d")
        );
    }

    #[tokio::test]
    async fn with_neither_and_two_servers_here_each_is_named_and_none_is_guessed() {
        let home = serve_dir(&[("a", "/w", "/a.sock"), ("b", "/w", "/b.sock")]);
        let err = choose(None, None, &at(&home), Path::new("/w"), all_alive)
            .await
            .unwrap_err();
        assert!(
            err.contains("--session a") && err.contains("--session b"),
            "{err}"
        );
    }

    #[test]
    fn a_file_that_is_not_a_record_is_named() {
        let home = serve_dir(&[]);
        fs::write(home.path().join("serve/bad.json"), "{}").unwrap();
        let err = records(home.path()).unwrap_err();
        assert!(err.contains("bad.json"), "{err}");
    }

    async fn only_b(socket: &Path) -> Result<bool, String> {
        Ok(socket != Path::new("/a.sock"))
    }

    #[tokio::test]
    async fn a_stale_record_does_not_make_the_choice_ambiguous() {
        let home = serve_dir(&[("a", "/w", "/a.sock"), ("b", "/w", "/b.sock")]);
        let got = choose(None, None, &at(&home), Path::new("/w"), only_b).await;
        assert_eq!(got.unwrap(), PathBuf::from("/b.sock"));
    }

    #[tokio::test]
    async fn with_only_stale_records_the_answer_is_no_server() {
        let home = serve_dir(&[("a", "/w", "/a.sock")]);
        let err = choose(None, None, &at(&home), Path::new("/w"), only_b)
            .await
            .unwrap_err();
        assert!(
            err.contains("tau serve -d") && err.contains("1 stale"),
            "{err}"
        );
        let named = choose(None, Some("a"), &at(&home), Path::new("/w"), only_b).await;
        let err = named.unwrap_err();
        assert!(
            err.contains("stale") && err.contains("tau serve -d"),
            "{err}"
        );
    }

    #[tokio::test]
    async fn a_socket_that_does_not_prove_dead_is_an_error_not_a_guess() {
        let home = serve_dir(&[("a", "/w", "/a.sock")]);
        let err = choose(None, None, &at(&home), Path::new("/w"), async |_: &Path| {
            Err::<bool, _>("did not answer hello".to_owned())
        })
        .await
        .unwrap_err();
        assert!(err.contains("did not answer hello"), "{err}");
    }
}
