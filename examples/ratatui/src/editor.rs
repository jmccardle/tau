//! The line being typed: text, a cursor, and the few keys a one-line editor needs.

use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

/// A single line of text with a cursor, counted in characters.
#[derive(Debug, Default, Clone)]
pub struct Editor {
    text: Vec<char>,
    at: usize,
}

impl Editor {
    pub fn text(&self) -> String {
        self.text.iter().collect()
    }

    pub fn is_empty(&self) -> bool {
        self.text.is_empty()
    }

    /// The cursor's column, in characters before it.
    pub fn at(&self) -> usize {
        self.at
    }

    pub fn insert(&mut self, c: char) {
        self.text.insert(self.at, c);
        self.at += 1;
    }

    pub fn backspace(&mut self) {
        if self.at > 0 {
            self.at -= 1;
            self.text.remove(self.at);
        }
    }

    pub fn delete(&mut self) {
        if self.at < self.text.len() {
            self.text.remove(self.at);
        }
    }

    pub fn left(&mut self) {
        self.at = self.at.saturating_sub(1);
    }

    pub fn right(&mut self) {
        self.at = (self.at + 1).min(self.text.len());
    }

    pub fn home(&mut self) {
        self.at = 0;
    }

    pub fn end(&mut self) {
        self.at = self.text.len();
    }

    /// Delete the word before the cursor (Ctrl+W).
    pub fn delete_word(&mut self) {
        while self.at > 0 && self.text[self.at - 1] == ' ' {
            self.backspace();
        }
        while self.at > 0 && self.text[self.at - 1] != ' ' {
            self.backspace();
        }
    }

    /// Delete everything before the cursor (Ctrl+U).
    pub fn delete_to_start(&mut self) {
        self.text.drain(..self.at);
        self.at = 0;
    }

    /// Replace the text, with the cursor at its end.
    pub fn set(&mut self, text: &str) {
        self.text = text.chars().collect();
        self.at = self.text.len();
    }

    /// The keys of a one-line editor, which the draft, an answer field and a form's text fields
    /// share. Whether `key` was one of them.
    pub fn handle(&mut self, key: KeyEvent) -> bool {
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        match key.code {
            KeyCode::Char('a') if ctrl => self.home(),
            KeyCode::Char('e') if ctrl => self.end(),
            KeyCode::Char('w') if ctrl => self.delete_word(),
            KeyCode::Char('u') if ctrl => self.delete_to_start(),
            KeyCode::Char(c) if !ctrl && !key.modifiers.contains(KeyModifiers::ALT) => {
                self.insert(c);
            }
            KeyCode::Backspace => self.backspace(),
            KeyCode::Delete => self.delete(),
            KeyCode::Left => self.left(),
            KeyCode::Right => self.right(),
            KeyCode::Home => self.home(),
            KeyCode::End => self.end(),
            _ => return false,
        }
        true
    }

    /// The line, leaving the editor empty.
    pub fn take(&mut self) -> String {
        self.at = 0;
        std::mem::take(&mut self.text).into_iter().collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn typed(s: &str) -> Editor {
        let mut e = Editor::default();
        s.chars().for_each(|c| e.insert(c));
        e
    }

    #[test]
    fn typing_inserts_at_the_cursor() {
        let mut e = typed("helo");
        e.left();
        e.insert('l');
        assert_eq!(e.text(), "hello");
        assert_eq!(e.at(), 4);
    }

    #[test]
    fn deleting_and_moving_stop_at_the_ends() {
        let mut e = typed("ab");
        e.right();
        e.delete();
        e.home();
        e.backspace();
        e.left();
        assert_eq!(e.text(), "ab");
        e.end();
        e.backspace();
        assert_eq!(e.text(), "a");
    }

    #[test]
    fn words_and_lines_go_before_the_cursor() {
        let mut e = typed("one two  ");
        e.delete_word();
        assert_eq!(e.text(), "one ");
        e.delete_to_start();
        assert!(e.is_empty());
    }

    #[test]
    fn take_empties_the_line() {
        let mut e = typed("é!");
        assert_eq!(e.take(), "é!");
        assert!(e.is_empty());
        assert_eq!(e.at(), 0);
    }
}
