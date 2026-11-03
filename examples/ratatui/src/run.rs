//! One connection's worth of the screen: keys and the server's messages in, the model and the
//! screen out, calls made in tasks so that nothing waits on the network.

use ratatui::{
    DefaultTerminal,
    crossterm::event::{Event as Input, KeyCode, KeyEvent, KeyEventKind, KeyModifiers},
};
use tokio::sync::mpsc::{UnboundedReceiver, UnboundedSender, unbounded_channel};

use crate::{
    ask,
    editor::Editor,
    link::{Connected, Incoming, Link},
    model::{Model, Need},
    ui::{self, Asking, Screen},
    wire::{
        self, AnswerParams, ConfigureParams, Node, ParseLineParams, ReopenParams, RunParams,
        SendParams, WalkParams, WatchParams, code,
    },
};

/// How a connection's session ended.
#[derive(Debug)]
pub enum Ended {
    /// The person quit.
    Quit,
    /// The server cut this client off for falling behind; a fresh `hello` starts over.
    CutOff,
    /// The server stopped on purpose (`end` with no error): leave, saying so.
    Finished(String),
    /// The session or the connection is over, for this reason.
    Stopped(String),
}

/// What tasks tell the loop.
enum Msg {
    Walked(Vec<Node>),
    Reopened(u64),
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
}

/// Show the session `connected` until it ends. `keys` is the person's keyboard; `draft` is the
/// line being typed, which a reconnect keeps.
pub async fn session(
    term: &mut DefaultTerminal,
    connected: Connected,
    me: &str,
    draft: &mut Editor,
    keys: &mut UnboundedReceiver<Input>,
) -> Result<Ended, String> {
    let Connected {
        link,
        mut incoming,
        welcome,
    } = connected;
    let (tell, mut told) = unbounded_channel();
    let mut app = App {
        link,
        tell,
        model: Model::new(me),
        editor: draft,
        asking: Asking::default(),
        scroll: 0,
        done: None,
    };
    loop {
        term.draw(|f| {
            ui::draw(
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

        tokio::select! {
            input = keys.recv() => match input {
                Some(input) => app.input(&input),
                None => return Ok(Ended::Stopped("the keyboard reader stopped".into())),
            },
            heard = incoming.recv() => match heard {
                Some(Incoming::Event(event)) => app.hear(event),
                Some(Incoming::End(end)) => return Ok(ended(end)),
                Some(Incoming::Lost(why)) => return Ok(Ended::Stopped(why)),
                None => return Ok(Ended::Stopped("the connection ended".into())),
            },
            Some(msg) = told.recv() => app.msg(msg),
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
        Some(error) if *error.code == code::CUT_OFF => Ended::CutOff,
        Some(error) => Ended::Stopped(format!("the session failed: {}", error.message)),
    }
}

impl App<'_> {
    fn hear(&mut self, event: wire::Event) {
        let needs = self.model.hear(event);
        self.go(needs);
        self.follow_question();
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
            Msg::Reopened(cursor) => {
                let needs = self.model.attach(cursor);
                self.go(needs);
                self.follow_question();
            }
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
            _ if self.asking.id.is_some() => self.answering(*key, ctrl),
            _ => self.typing(*key, ctrl),
        }
    }

    /// The keys of a one-line editor, which the draft and an answer field share. Whether `key`
    /// was one of them.
    fn edit(editor: &mut Editor, key: KeyEvent, ctrl: bool) -> bool {
        match key.code {
            KeyCode::Char('a') if ctrl => editor.home(),
            KeyCode::Char('e') if ctrl => editor.end(),
            KeyCode::Char('w') if ctrl => editor.delete_word(),
            KeyCode::Char('u') if ctrl => editor.delete_to_start(),
            KeyCode::Char(c) if !ctrl && !key.modifiers.contains(KeyModifiers::ALT) => {
                editor.insert(c);
            }
            KeyCode::Backspace => editor.backspace(),
            KeyCode::Delete => editor.delete(),
            KeyCode::Left => editor.left(),
            KeyCode::Right => editor.right(),
            KeyCode::Home => editor.home(),
            KeyCode::End => editor.end(),
            _ => return false,
        }
        true
    }

    fn typing(&mut self, key: KeyEvent, ctrl: bool) {
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
                Self::edit(self.editor, key, ctrl);
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

    fn answering(&mut self, key: KeyEvent, ctrl: bool) {
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
                Self::edit(&mut self.asking.field, key, ctrl);
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
    if !parsed.missing.is_empty() {
        return Err(format!(
            "/{} needs {}; this example has no forms (use `tau`, or type the arguments)",
            parsed.command.name,
            parsed.missing.join(", ")
        ));
    }
    let result = match parsed.extension {
        Some(extension) if parsed.args.is_empty() => {
            return Err(format!(
                "/config {extension} is a form; this example has none"
            ));
        }
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

async fn send(link: &Link, cursor: u64, text: &str) -> Result<(), String> {
    link.call(&SendParams {
        cursor,
        text: text.to_owned(),
    })
    .await
    .map(|_| ())
    .map_err(|e| e.to_string())
}
