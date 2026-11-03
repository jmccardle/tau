//! A terminal chat client of `tau serve`, written in Rust against the wire schema. See README.md.

mod ask;
mod editor;
mod find;
mod link;
mod model;
mod run;
mod ui;
mod wire;

use std::{env, path::PathBuf, process::ExitCode, thread};

use clap::Parser;
use ratatui::{DefaultTerminal, crossterm::event};
use tokio::sync::mpsc::{UnboundedReceiver, unbounded_channel};

use editor::Editor;
use run::Ended;

/// A terminal chat client of `tau serve`.
///
/// Finds the server by --socket, or by --session through its record in $TAU_HOME/serve, or, with
/// neither, as the one `tau serve` whose session runs in this directory. Enter sends, Esc aborts
/// a busy cursor, Ctrl+C or Ctrl+D quits.
#[derive(Parser)]
#[command(version)]
struct Args {
    /// The server's Unix socket
    #[arg(long, value_name = "PATH", conflicts_with = "session")]
    socket: Option<PathBuf>,
    /// The id of a served session
    #[arg(long, value_name = "ID")]
    session: Option<String>,
    /// This client's id (default: user@host:ratatui)
    #[arg(long, value_name = "ID")]
    client: Option<String>,
}

fn client_id(given: Option<String>) -> Result<String, String> {
    if let Some(id) = given {
        return Ok(id);
    }
    let user = env::var("USER")
        .or_else(|_| env::var("LOGNAME"))
        .map_err(|_| "neither USER nor LOGNAME is set; give a client id with --client")?;
    let host = gethostname::gethostname();
    Ok(format!("{user}@{}:ratatui", host.to_string_lossy()))
}

/// The keyboard, read on a thread of its own: crossterm's reads block.
fn keyboard() -> UnboundedReceiver<event::Event> {
    let (send, keys) = unbounded_channel();
    thread::spawn(move || {
        while let Ok(input) = event::read() {
            if send.send(input).is_err() {
                break;
            }
        }
    });
    keys
}

async fn show(
    term: &mut DefaultTerminal,
    socket: &std::path::Path,
    me: &str,
    mut connected: link::Connected,
) -> Result<Option<String>, String> {
    let mut keys = keyboard();
    let mut draft = Editor::default();
    loop {
        match run::session(term, connected, me, &mut draft, &mut keys).await? {
            Ended::Quit => return Ok(None),
            Ended::Finished(why) => return Ok(Some(why)),
            Ended::Stopped(why) => return Err(why),
            Ended::CutOff => {
                connected = link::connect(socket, me)
                    .await
                    .map_err(|e| format!("cut off by the server, and could not reconnect: {e}"))?;
            }
        }
    }
}

/// Run until the person quits (`None`) or the server stops on purpose (`Some(why)`).
async fn start() -> Result<Option<String>, String> {
    let args = Args::parse();
    let me = client_id(args.client)?;
    let cwd = env::current_dir()
        .and_then(|d| d.canonicalize())
        .map_err(|e| format!("cannot read the current directory: {e}"))?;
    let socket = find::choose(
        args.socket.as_deref(),
        args.session.as_deref(),
        &find::tau_home_from_env,
        &cwd,
        async |socket: &std::path::Path| link::answers(socket).await,
    )
    .await?;
    // Before the screen is taken over, so that a refusal is printed on the plain terminal.
    let connected = link::connect(&socket, &me).await?;
    let mut term = ratatui::try_init().map_err(|e| {
        ratatui::restore(); // raw mode may be on already
        format!("cannot take over the terminal: {e}")
    })?;
    // ratatui's hook restores the terminal when anything panics. A panic in a task would then
    // leave the program running without that task, so after the restore the program ends.
    let restoring = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        restoring(info);
        std::process::exit(101);
    }));
    let shown = show(&mut term, &socket, &me, connected).await;
    ratatui::restore();
    shown
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    match start().await {
        Ok(None) => ExitCode::SUCCESS,
        Ok(Some(why)) => {
            eprintln!("tau-ratatui: {why}");
            ExitCode::SUCCESS
        }
        Err(why) => {
            eprintln!("tau-ratatui: {why}");
            ExitCode::FAILURE
        }
    }
}
