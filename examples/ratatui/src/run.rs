//! One connection's worth of the screen: keys and the server's messages in, the model and the
//! screen out, calls made in tasks so that nothing waits on the network.

use ratatui::{
    DefaultTerminal,
    crossterm::event::{Event as Input, KeyCode, KeyEvent, KeyEventKind, KeyModifiers},
};
use tokio::{
    sync::mpsc::{UnboundedReceiver, UnboundedSender, unbounded_channel},
    time::Instant,
};

use crate::{
    ask,
    editor::Editor,
    link::{Connected, Incoming, Link},
    model::{Model, Need, PAGE as NODES},
    ui::{Asking, Screen},
    wire::{
        self, AnswerParams, ConfigureParams, Node, NodesParams, OptionsParams, ParseLineParams,
        ReopenParams, RunParams, SendParams, WalkParams, WatchParams, code,
    },
};

mod modes;
use modes::{Mode, Prompt};

/// What a reconnect keeps of the screen: the viewed cursor, the tree if it was open (whether the
/// highlight followed the cursor, and the node it was on), and a word about what was lost.
#[derive(Debug, Default)]
pub struct Restore {
    cursor: Option<u64>,
    tree: Option<(bool, Option<usize>)>,
    note: Option<String>,
}

/// How a connection's session ended.
#[derive(Debug)]
pub enum Ended {
    /// The person quit.
    Quit,
    /// The server cut this client off for falling behind; a fresh `hello` starts over, with
    /// what the screen can keep.
    CutOff(Restore),
    /// The server stopped on purpose (`end` with no error): leave, saying so.
    Finished(String),
    /// The session or the connection is over, for this reason.
    Stopped(String),
}

/// What tasks tell the loop.
enum Msg {
    Walked(Vec<Node>),
    /// A page of the tree's nodes.
    Paged(Vec<Node>),
    Reopened(u64),
    /// An extension's options, to fill in.
    OptionsOf(String, wire::Form),
    /// The extensions that have options, to pick from.
    Extensions(Vec<String>),
    /// `configure` answered: `Err` is the server's reason. `said` is what was set and where.
    Configured(Result<(), String>, String, u64),
    Notice(String),
    /// A line the session did not take, to put back in the editor.
    Restore(String),
    /// An answer the server refused.
    NotAnswered(String),
    Fatal(String),
}

const PAGE: usize = 10;

struct App<'a> {
    link: Link,
    tell: UnboundedSender<Msg>,
    model: Model,
    editor: &'a mut Editor,
    asking: Asking,
    scroll: usize,
    done: Option<Ended>,
    mode: Mode,
    prompt: Option<Prompt>,
    /// When the tree is next drawn again, if something it shows changed.
    due: Option<Instant>,
    /// Forms opened so far, to tell one from the next.
    forms: u64,
    /// Whether the tree asked for pages because a cursor was at a node not read.
    asked_for_missing: bool,
    /// What a reconnect keeps, until the new snapshot is whole.
    restore: Option<Restore>,
}

/// Show the session `connected` until it ends. `keys` is the person's keyboard; `draft` is the
/// line being typed, which a reconnect keeps.
pub async fn session(
    term: &mut DefaultTerminal,
    connected: Connected,
    me: &str,
    draft: &mut Editor,
    keys: &mut UnboundedReceiver<Input>,
    restore: Restore,
) -> Result<Ended, String> {
    let Connected {
        link,
        mut incoming,
        welcome,
    } = connected;
    let (tell, mut told) = unbounded_channel();
    let mut model = Model::new(me);
    if let Some(note) = &restore.note {
        model.notify(note.clone());
    }
    let mut app = App {
        link,
        tell,
        model,
        editor: draft,
        asking: Asking::default(),
        scroll: 0,
        done: None,
        mode: Mode::Chat,
        prompt: None,
        due: None,
        forms: 0,
        asked_for_missing: false,
        restore: Some(restore),
    };
    loop {
        let mut most = None;
        term.draw(|f| {
            most = app.draw(
                f,
                &Screen {
                    model: &app.model,
                    editor: app.editor,
                    asking: &app.asking,
                    question: app.model.questions().first().copied(),
                    scroll: app.scroll,
                    session: &welcome.session,
                    cwd: &welcome.cwd,
                    client: me,
                },
            );
        })
        .map_err(|e| format!("cannot draw the screen: {e}"))?;
        // Whatever the size now, the scroll stays within the transcript: a terminal that grew
        // does not leave it scrolled past the top.
        if let Some(most) = most {
            app.scroll = app.scroll.min(most);
        }

        let due = app
            .due
            .unwrap_or_else(|| Instant::now() + std::time::Duration::from_secs(3600));
        tokio::select! {
            input = keys.recv() => match input {
                Some(input) => app.input(&input),
                None => return Ok(Ended::Stopped("the keyboard reader stopped".into())),
            },
            heard = incoming.recv() => match heard {
                Some(Incoming::Event(event)) => app.hear(event),
                Some(Incoming::End(end)) => return Ok(match ended(end) {
                    Ended::CutOff(_) => Ended::CutOff(app.keep()),
                    other => other,
                }),
                Some(Incoming::Lost(why)) => return Ok(Ended::Stopped(why)),
                None => return Ok(Ended::Stopped("the connection ended".into())),
            },
            Some(msg) = told.recv() => app.msg(msg),
            () = tokio::time::sleep_until(due), if app.due.is_some() => app.redraw_tree(),
        }
        if let Some(ended) = app.done.take() {
            return Ok(ended);
        }
        if let Some(why) = app.model.lost() {
            return Ok(Ended::Stopped(why.to_owned()));
        }
    }
}

/// The notification `end`: `CutOff` means start again; anything else is the reason to stop.
fn ended(end: wire::End) -> Ended {
    match end.error {
        None => Ended::Finished("the server stopped".into()),
        Some(error) if *error.code == code::CUT_OFF => Ended::CutOff(Restore::default()),
        Some(error) => Ended::Stopped(format!("the session failed: {}", error.message)),
    }
}

impl App<'_> {
    fn hear(&mut self, event: wire::Event) {
        let changes_tree = matches!(
            event,
            wire::Event::Appended(_)
                | wire::Event::CursorView(_)
                | wire::Event::CursorGone(_)
                | wire::Event::Labels(_)
        );
        let needs = self.model.hear(event);
        self.go(needs);
        self.follow_question();
        self.resume();
        if changes_tree {
            self.tree_changed();
        }
    }

    /// The keyboard is in the first question this client may answer, if there is one.
    fn follow_question(&mut self) {
        let now = self.model.questions().first().map(|q| q.id);
        if now != self.asking.id {
            self.asking = Asking {
                id: now,
                ..Asking::default()
            };
        }
    }

    fn msg(&mut self, msg: Msg) {
        match msg {
            Msg::Walked(nodes) => {
                let needs = self.model.walked(nodes);
                self.go(needs);
            }
            Msg::Paged(nodes) => {
                let needs = self.model.paged(&nodes);
                self.go(needs);
                if !self.model.reading() {
                    self.redraw_tree();
                }
            }
            Msg::Reopened(cursor) => {
                let needs = self.model.attach(cursor);
                self.go(needs);
                self.follow_question();
            }
            Msg::OptionsOf(extension, form) => self.open_form(&extension, &form),
            Msg::Extensions(names) => self.open_pick(names),
            Msg::Configured(result, said, serial) => self.configured(result, said, serial),
            Msg::Notice(text) => self.model.notify(text),
            Msg::Restore(line) => {
                if self.editor.is_empty() {
                    self.editor.set(&line);
                }
            }
            Msg::NotAnswered(why) => self.asking.error = Some(why),
            Msg::Fatal(why) => self.done = Some(Ended::Stopped(why)),
        }
    }

    /// Make what the model needs happen. A call that cannot be made ends the session: the picture
    /// would be wrong.
    fn go(&self, needs: Vec<Need>) {
        for need in needs {
            let (link, tell) = (self.link.clone(), self.tell.clone());
            tokio::spawn(async move {
                let told = match need {
                    Need::Watch(cursor) => link
                        .call(&WatchParams {
                            cursors: vec![cursor],
                        })
                        .await
                        .map(|_| None),
                    Need::Walk(id) => link
                        .call(&WalkParams { id })
                        .await
                        .map(|n| Some(Msg::Walked(n.0))),
                    Need::Reopen(cursor) => link
                        .call(&ReopenParams { cursor })
                        .await
                        .map(|id| Some(Msg::Reopened(id.0))),
                    Need::Page(start) => link
                        .call(&NodesParams {
                            start,
                            limit: NODES,
                        })
                        .await
                        .map(|n| Some(Msg::Paged(n.0))),
                };
                match told {
                    Ok(Some(msg)) => drop(tell.send(msg)),
                    Ok(None) => {}
                    Err(e) => drop(tell.send(Msg::Fatal(format!("{need:?} failed: {e}")))),
                }
            });
        }
    }

    // Keys

    fn input(&mut self, input: &Input) {
        let Input::Key(key) = input else { return }; // a resize only redraws
        if key.kind == KeyEventKind::Release {
            return;
        }
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        match key.code {
            KeyCode::Char('c' | 'd') if ctrl => self.done = Some(Ended::Quit),
            _ if self.prompt.is_some() => self.prompting(*key),
            _ if !matches!(self.mode, Mode::Chat) => self.in_mode(*key),
            KeyCode::Char('g') if ctrl => self.open_tree(),
            _ if self.asking.id.is_some() => self.answering(*key),
            _ => self.typing(*key),
        }
    }

    fn typing(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Enter => self.submit(),
            KeyCode::Esc => {
                if self.model.view().is_some_and(|v| v.busy) {
                    self.abort();
                }
            }
            KeyCode::PageUp => self.scroll += PAGE,
            KeyCode::PageDown => self.scroll = self.scroll.saturating_sub(PAGE),
            _ => {
                self.editor.handle(key);
            }
        }
    }

    fn abort(&self) {
        let Some(cursor) = self.model.cursor() else {
            return;
        };
        let (link, tell) = (self.link.clone(), self.tell.clone());
        tokio::spawn(async move {
            if let Err(e) = link.call(&wire::AbortParams { cursor }).await {
                let _ = tell.send(Msg::Notice(format!("abort: {e}")));
            }
        });
    }

    fn submit(&mut self) {
        let line = self.editor.take();
        let Some(cursor) = self.model.cursor() else {
            self.editor.set(&line);
            return;
        };
        if line.trim().is_empty() {
            return;
        }
        self.scroll = 0;
        let (link, tell) = (self.link.clone(), self.tell.clone());
        tokio::spawn(async move {
            if let Err(why) = submit_line(&link, cursor, &line, &tell).await {
                let _ = tell.send(Msg::Notice(why));
                let _ = tell.send(Msg::Restore(line));
            }
        });
    }

    fn answering(&mut self, key: KeyEvent) {
        let Some(question) = self
            .model
            .questions()
            .first()
            .map(|q| (q.id, ask::shape(&q.schema)))
        else {
            return;
        };
        let (id, shape) = question;
        let choices = shape.choices();
        match key.code {
            KeyCode::Esc => self.answer(id, serde_json::Value::Null),
            KeyCode::Up if !choices.is_empty() => {
                self.asking.picked = self.asking.picked.saturating_sub(1);
            }
            KeyCode::Down if !choices.is_empty() => {
                self.asking.picked = (self.asking.picked + 1).min(choices.len() - 1);
            }
            KeyCode::Enter => {
                let value = match choices.get(self.asking.picked) {
                    Some(choice) => Ok(choice.clone()),
                    None => shape.parse(&self.asking.field.text()),
                };
                match value {
                    Ok(value) => self.answer(id, value),
                    Err(why) => self.asking.error = Some(why),
                }
            }
            _ if choices.is_empty() => {
                self.asking.error = None;
                self.asking.field.handle(key);
            }
            _ => {}
        }
    }

    fn answer(&self, question: u64, value: serde_json::Value) {
        let (link, tell) = (self.link.clone(), self.tell.clone());
        tokio::spawn(async move {
            if let Err(e) = link.call(&AnswerParams { question, value }).await {
                let _ = tell.send(Msg::NotAnswered(e.to_string()));
            }
        });
    }
}

/// Send `line` as a message, or run it if it is a command. `Err` is what to tell the person; the
/// line then goes back to the editor.
async fn submit_line(
    link: &Link,
    cursor: u64,
    line: &str,
    tell: &UnboundedSender<Msg>,
) -> Result<(), String> {
    if !line.starts_with('/') || line.trim() == "/" {
        return send(link, cursor, line).await;
    }
    let parsed = match link
        .call(&ParseLineParams {
            cursor,
            line: line.to_owned(),
        })
        .await
    {
        Ok(parsed) => parsed.0,
        Err(e) if e.code() == Some(code::UNKNOWN_COMMAND) => {
            let name = e.data().and_then(|d| d["name"].as_str().map(str::to_owned));
            let _ = tell.send(Msg::Notice(format!(
                "no command /{}: sent as text",
                name.unwrap_or_default()
            )));
            return send(link, cursor, line).await;
        }
        Err(e) => return Err(e.to_string()),
    };
    if let Some(extension) = &parsed.extension
        && parsed.args.is_empty()
    {
        return options_of(link, cursor, extension.clone(), tell).await;
    }
    if parsed.extension.is_none() && parsed.command.name == "config" && !parsed.missing.is_empty() {
        return pick_extension(&parsed.command, tell);
    }
    if !parsed.missing.is_empty() {
        return Err(format!(
            "/{} needs {}; this example has no forms (use `tau`, or type the arguments)",
            parsed.command.name,
            parsed.missing.join(", ")
        ));
    }
    let result = match parsed.extension {
        Some(extension) => {
            let values = parsed.args;
            let save = parsed.save;
            link.call(&ConfigureParams {
                cursor,
                extension,
                values,
                save,
            })
            .await
            .map(|_| None)
        }
        None => {
            // A command is called by `extension:name`; a form with no extension by its name.
            let command = match &parsed.command.extension {
                Some(extension) => format!("{extension}:{}", parsed.command.name),
                None => parsed.command.name.clone(),
            };
            link.call(&RunParams {
                cursor,
                command,
                args: parsed.args,
            })
            .await
            .map(|r| r.0)
        }
    };
    match result {
        Ok(Some(text)) => {
            let _ = tell.send(Msg::Notice(format!("{}: {text}", parsed.command.name)));
            Ok(())
        }
        Ok(None) => Ok(()),
        Err(e) => Err(e.to_string()),
    }
}

/// `/config` with no extension: the ones that have options (the `extension` parameter's `enum`),
/// to pick from.
fn pick_extension(config: &wire::Form, tell: &UnboundedSender<Msg>) -> Result<(), String> {
    let named = config
        .schema
        .get("properties")
        .and_then(|p| p.get("extension"))
        .and_then(|e| e.get("enum"))
        .and_then(serde_json::Value::as_array);
    let names: Vec<String> = named
        .into_iter()
        .flatten()
        .filter_map(|n| n.as_str().map(str::to_owned))
        .collect();
    if names.is_empty() {
        return Err("no extension has options to configure".to_owned());
    }
    let _ = tell.send(Msg::Extensions(names));
    Ok(())
}

/// Fetch `extension`'s options and hand them over to be filled in.
async fn options_of(
    link: &Link,
    cursor: u64,
    extension: String,
    tell: &UnboundedSender<Msg>,
) -> Result<(), String> {
    let form = link
        .call(&OptionsParams {
            cursor,
            extension: extension.clone(),
        })
        .await
        .map_err(|e| e.to_string())?;
    let _ = tell.send(Msg::OptionsOf(extension, form.0));
    Ok(())
}

async fn send(link: &Link, cursor: u64, text: &str) -> Result<(), String> {
    link.call(&SendParams {
        cursor,
        text: text.to_owned(),
    })
    .await
    .map(|_| ())
    .map_err(|e| e.to_string())
}
