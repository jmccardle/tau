//! The screens that take the keyboard from the chat: the tree browser, a form, the list of
//! extensions to configure, and the one-line questions they ask (a label, a go-to on a busy
//! cursor). Each is state in `Mode` or `Prompt`; its keys answer what is wanted, and this makes
//! the calls.

use ratatui::Frame as Canvas; // rules: allow(frame): ratatui's own name for what one draw paints
use ratatui::crossterm::event::{KeyCode, KeyEvent};

use super::{App, Ended, Msg};
use crate::{
    editor::Editor,
    form::{Done, Form, Landing, Pick, landing, show},
    formui,
    tree::{Act, Drawn, Tree},
    treeui,
    ui::{self, Screen},
    wire::{self, ConfigureParams, GoToParams, OpenParams, OptionsParams, SetLabelParams},
};

/// What has the keyboard besides the chat.
pub enum Mode {
    Chat,
    Tree(Box<Tree>),
    Form(Form),
    Pick(Pick),
}

/// A question on the last line, asked of the person while a screen is open.
pub enum Prompt {
    /// A label for this cursor.
    Label { cursor: u64, text: Editor },
    /// Going to `node` aborts the busy step of `cursor`: yes or no.
    Busy { cursor: u64, node: u64 },
}

const REDRAW_AFTER: std::time::Duration = std::time::Duration::from_millis(100);

impl App<'_> {
    pub(super) fn draw(&self, f: &mut Canvas, screen: &Screen) {
        match &self.mode {
            Mode::Tree(_) if self.model.reading() => treeui::draw_reading(f, &self.model),
            Mode::Tree(tree) => treeui::draw(f, tree, &self.model),
            other => {
                ui::draw(f, screen);
                match other {
                    Mode::Form(form) => {
                        if let Some(at) = formui::draw_form(f, form) {
                            f.set_cursor_position(at);
                        }
                    }
                    Mode::Pick(pick) => formui::draw_pick(f, pick),
                    Mode::Chat | Mode::Tree(_) => {}
                }
            }
        }
        match &self.prompt {
            Some(Prompt::Label { cursor, text }) => treeui::draw_prompt(
                f,
                &format!("label for cursor {cursor} (it moves from any other): "),
                Some(text),
            ),
            Some(Prompt::Busy { cursor, .. }) => treeui::draw_prompt(
                f,
                &format!("cursor {cursor} is busy; going elsewhere aborts its step. Go? (y/n)"),
                None,
            ),
            None => {}
        }
    }

    /// What to keep of the screen if the server cuts this client off: the viewed cursor and the
    /// tree. An open form cannot be kept (its values are the old session's); the note says so.
    pub(super) fn keep(&self) -> super::Restore {
        let tree = match &self.mode {
            Mode::Tree(tree) => Some(tree.keep()),
            _ => None,
        };
        let note = match &self.mode {
            Mode::Form(form) => Some(format!(
                "cut off by the server: the {} form was closed; open it again",
                form.title
            )),
            _ => None,
        };
        super::Restore {
            cursor: self.model.cursor(),
            tree,
            note,
        }
    }

    /// After a reconnect, once the snapshot is whole: view the cursor that was viewed (if it is
    /// still open) and open the tree again where it was.
    pub(super) fn resume(&mut self) {
        if self.restore.is_none() || self.model.cursor().is_none() {
            return;
        }
        let Some(kept) = self.restore.take() else {
            return;
        };
        if let Some(cursor) = kept.cursor
            && self.model.cursor() != Some(cursor)
            && self.model.views().contains_key(&cursor)
        {
            let needs = self.model.attach(cursor);
            self.go(needs);
            self.follow_question();
        }
        if let Some((following, node)) = kept.tree {
            self.open_tree();
            if let Mode::Tree(tree) = &mut self.mode {
                tree.resume(following, node);
            }
        }
    }

    fn fatal(&mut self, why: String) {
        self.done = Some(Ended::Stopped(why));
    }

    // The tree

    pub(super) fn open_tree(&mut self) {
        let Some(cursor) = self.model.cursor() else {
            return;
        };
        let needs = self.model.begin_read();
        self.go(needs);
        self.mode = Mode::Tree(Box::new(Tree::new(cursor)));
        if !self.model.reading() {
            self.redraw_tree();
        }
    }

    /// Something the tree shows changed: draw it again soon, once for a burst.
    pub(super) fn tree_changed(&mut self) {
        if matches!(self.mode, Mode::Tree(_)) && !self.model.reading() && self.due.is_none() {
            self.due = Some(tokio::time::Instant::now() + REDRAW_AFTER);
        }
    }

    pub(super) fn redraw_tree(&mut self) {
        self.due = None;
        let Mode::Tree(tree) = &mut self.mode else {
            return;
        };
        match tree.redraw(&self.model) {
            Ok(Drawn::Done) => self.asked_for_missing = false,
            // A cursor is at a node this client has not read (after a reconnect, say): read the
            // nodes. If a whole read still leaves it missing, the server's answer is wrong.
            Ok(Drawn::Missing(cursor, node)) if self.model.reading() => {
                let _ = (cursor, node);
            }
            Ok(Drawn::Missing(cursor, node)) if self.asked_for_missing => self.fatal(format!(
                "cursor {cursor} is at node {node}, which the server does not return"
            )),
            Ok(Drawn::Missing(..)) => {
                self.asked_for_missing = true;
                let needs = self.model.begin_read();
                self.go(needs);
            }
            Err(why) => self.fatal(format!("the tree cannot be drawn: {why}")),
        }
    }

    /// A key while a screen other than the chat is open.
    pub(super) fn in_mode(&mut self, key: KeyEvent) {
        match std::mem::replace(&mut self.mode, Mode::Chat) {
            Mode::Chat => {}
            Mode::Tree(mut tree) => {
                tree.said = None;
                if self.model.reading() {
                    // Nothing to move on yet; Esc still leaves.
                    if key.code != KeyCode::Esc {
                        self.mode = Mode::Tree(tree);
                    }
                    return;
                }
                match tree.key(key, &self.model) {
                    Ok(act) => {
                        if let Some(tree) = self.tree_act(tree, act) {
                            self.mode = Mode::Tree(tree);
                        }
                    }
                    Err(why) => self.fatal(format!("the tree cannot be drawn: {why}")),
                }
            }
            Mode::Form(mut form) => match form.key(key) {
                Done::Open => self.mode = Mode::Form(form),
                Done::Cancel => {}
                Done::Submit(values, save) => self.apply(form, values, save),
            },
            Mode::Pick(mut pick) => match pick.key(key) {
                None => self.mode = Mode::Pick(pick),
                Some(None) => {}
                Some(Some(extension)) => self.fetch_options(extension),
            },
        }
    }

    /// Do what the tree was asked; the tree back if it stays open.
    fn tree_act(&mut self, mut tree: Box<Tree>, act: Act) -> Option<Box<Tree>> {
        match act {
            Act::Nothing => {}
            Act::Leave => return None,
            Act::Say(text) => tree.said = Some(text),
            Act::View(cursor) => {
                let needs = self.model.attach(cursor);
                self.go(needs);
                self.follow_question();
                self.scroll = 0;
                tree.view_changed();
                if let Err(why) = tree.redraw(&self.model) {
                    self.fatal(format!("the tree cannot be drawn: {why}"));
                }
            }
            Act::Label(cursor) => {
                self.prompt = Some(Prompt::Label {
                    cursor,
                    text: Editor::default(),
                });
            }
            Act::Open(node) => {
                self.open_at(node);
                return None;
            }
            Act::Go { cursor, node } => {
                if self.model.views().get(&cursor).is_some_and(|v| v.busy) {
                    self.prompt = Some(Prompt::Busy { cursor, node });
                } else {
                    self.go_to(cursor, node);
                    return None;
                }
            }
        }
        Some(tree)
    }

    /// A key while a one-line question is open.
    pub(super) fn prompting(&mut self, key: KeyEvent) {
        match self.prompt.take() {
            Some(Prompt::Label { cursor, mut text }) => match key.code {
                KeyCode::Esc => {}
                KeyCode::Enter if !text.text().trim().is_empty() => {
                    self.label(cursor, text.text().trim().to_owned());
                    self.mode = Mode::Chat;
                }
                _ => {
                    text.handle(key);
                    self.prompt = Some(Prompt::Label { cursor, text });
                }
            },
            Some(Prompt::Busy { cursor, node }) => match key.code {
                KeyCode::Char('y' | 'Y') => {
                    self.go_to(cursor, node);
                    self.mode = Mode::Chat;
                }
                KeyCode::Char('n' | 'N') | KeyCode::Esc => {}
                _ => self.prompt = Some(Prompt::Busy { cursor, node }),
            },
            None => {}
        }
    }

    // Calls made for the tree

    fn go_to(&self, cursor: u64, node: u64) {
        let (link, tell) = (self.link.clone(), self.tell.clone());
        tokio::spawn(async move {
            if let Err(e) = link.call(&GoToParams { cursor, to: node }).await {
                let _ = tell.send(Msg::Notice(format!("go to {node}: {e}")));
            }
        });
    }

    fn open_at(&self, node: u64) {
        let (link, tell) = (self.link.clone(), self.tell.clone());
        tokio::spawn(async move {
            let told = match link.call(&OpenParams { at: node }).await {
                Ok(cursor) => format!("opened cursor {} at node {node}", cursor.0),
                Err(e) => format!("open at {node}: {e}"),
            };
            let _ = tell.send(Msg::Notice(told));
        });
    }

    fn label(&self, cursor: u64, label: String) {
        let (link, tell) = (self.link.clone(), self.tell.clone());
        tokio::spawn(async move {
            if let Err(e) = link.call(&SetLabelParams { cursor, label }).await {
                let _ = tell.send(Msg::Notice(format!("label: {e}")));
            }
        });
    }

    // Config

    /// Fetch `extension`'s options, to be filled in as a form (`Msg::OptionsOf`).
    fn fetch_options(&self, extension: String) {
        let Some(cursor) = self.model.cursor() else {
            return;
        };
        let (link, tell) = (self.link.clone(), self.tell.clone());
        tokio::spawn(async move {
            let params = OptionsParams {
                cursor,
                extension: extension.clone(),
            };
            let told = match link.call(&params).await {
                Ok(form) => Msg::OptionsOf(extension, form.0),
                Err(e) => Msg::Notice(format!("/config {extension}: {e}")),
            };
            let _ = tell.send(told);
        });
    }

    pub(super) fn open_pick(&mut self, items: Vec<String>) {
        self.mode = Mode::Pick(Pick {
            title: "Configure which extension?".to_owned(),
            items,
            at: 0,
        });
    }

    /// The options of `extension` arrived: a form, or, for a schema the form cannot show, the
    /// reason and the way that works.
    pub(super) fn open_form(&mut self, extension: &str, options: &wire::Form) {
        self.forms += 1;
        match Form::new(self.forms, extension, &options.schema) {
            Ok(form) => self.mode = Mode::Form(form),
            Err(why) => self.model.notify(format!(
                "/config {extension}: {why}; set it with /config {extension} name=value"
            )),
        }
    }

    /// Apply was chosen: send what changed. The form stays until the server answers.
    fn apply(
        &mut self,
        form: Form,
        values: serde_json::Map<String, serde_json::Value>,
        save: bool,
    ) {
        if values.is_empty() && !save {
            self.model
                .notify(format!("/config {}: nothing changed", form.extension));
            return;
        }
        let Some(cursor) = self.model.cursor() else {
            return;
        };
        let shown: Vec<String> = values
            .iter()
            .map(|(k, v)| format!("{k}={}", show(v)))
            .collect();
        let said = match (values.is_empty(), save) {
            (true, _) => format!(
                "/config {}: saved for every session (no option changed on this branch)",
                form.extension
            ),
            (false, true) => format!(
                "/config {}: {} (for every session)",
                form.extension,
                shown.join(", ")
            ),
            (false, false) => format!(
                "/config {}: {} (on this branch)",
                form.extension,
                shown.join(", ")
            ),
        };
        let (extension, serial) = (form.extension.clone(), form.serial);
        self.mode = Mode::Form(form);
        let (link, tell) = (self.link.clone(), self.tell.clone());
        tokio::spawn(async move {
            let result = link
                .call(&ConfigureParams {
                    cursor,
                    extension,
                    values,
                    save: Some(save),
                })
                .await
                .map(|_| ())
                .map_err(|e| e.to_string());
            let _ = tell.send(Msg::Configured(result, said, serial));
        });
    }

    /// `configure` answered for the form `serial`. A refusal stays in that form and nothing is
    /// claimed saved; an answer for a form no longer open is a notice and touches no other.
    pub(super) fn configured(&mut self, result: Result<(), String>, said: String, serial: u64) {
        let open = match &self.mode {
            Mode::Form(form) => Some(form.serial),
            _ => None,
        };
        match (result, landing(open, serial)) {
            (Ok(()), Landing::Form) => {
                self.mode = Mode::Chat;
                self.model.notify(said);
            }
            (Ok(()), Landing::Notice) => self.model.notify(said),
            (Err(why), Landing::Form) => {
                if let Mode::Form(form) = &mut self.mode {
                    form.error = Some(why);
                }
            }
            (Err(why), Landing::Notice) => self.model.notify(format!("config refused: {why}")),
        }
    }
}
