//! Drawing: the screen from the model, the editor and the open question. Pure functions of what
//! they are given, so wrapping can be tested without a terminal.

use ratatui::Frame as Canvas; // rules: allow(frame): ratatui's own name for what one draw paints
use ratatui::{
    layout::{Constraint, Layout, Position, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Paragraph, Wrap},
};
use unicode_width::UnicodeWidthChar;

use crate::{
    ask,
    editor::Editor,
    model::{Kind, Model, Row},
    wire::OpenQuestion,
};

/// The question the keyboard is in, and how it is being answered.
#[derive(Debug, Default)]
pub struct Asking {
    pub id: Option<u64>,
    pub picked: usize,
    pub field: Editor,
    pub error: Option<String>,
}

/// What `draw` needs besides the model.
pub struct Screen<'a> {
    pub model: &'a Model,
    pub editor: &'a Editor,
    pub asking: &'a Asking,
    pub question: Option<&'a OpenQuestion>,
    pub scroll: usize,
    pub session: &'a str,
    pub cwd: &'a str,
    pub client: &'a str,
}

fn style(kind: Kind) -> Style {
    match kind {
        Kind::Mine => Style::new().fg(Color::Green),
        Kind::Other => Style::new().fg(Color::Magenta),
        Kind::Assistant => Style::new(),
        Kind::Thinking => Style::new()
            .fg(Color::DarkGray)
            .add_modifier(Modifier::ITALIC),
        Kind::Call => Style::new().fg(Color::Cyan),
        Kind::Result => Style::new().fg(Color::Gray),
        Kind::Failure => Style::new().fg(Color::Red),
        Kind::Dim => Style::new().fg(Color::DarkGray),
    }
}

/// `text` broken into lines at most `width` columns wide: at newlines, else at the last space
/// that fits, else mid-word. Indentation is kept.
pub fn wrap(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut out = Vec::new();
    for paragraph in text.split('\n') {
        let mut line = String::new();
        let mut used = 0;
        for c in paragraph.chars() {
            let w = c.width().unwrap_or(0);
            if used + w > width {
                match line.rfind(' ').filter(|&at| at > 0) {
                    Some(at) => {
                        let rest = line[at + 1..].to_owned();
                        line.truncate(at);
                        out.push(std::mem::take(&mut line));
                        used = rest.chars().map(|r| r.width().unwrap_or(0)).sum();
                        line = rest;
                    }
                    None => {
                        out.push(std::mem::take(&mut line));
                        used = 0;
                    }
                }
            }
            line.push(c);
            used += w;
        }
        out.push(line);
    }
    out
}

/// The rows as screen lines `width` wide: the label in bold on a row's first line, the rest
/// indented under the body.
pub fn lines(rows: &[Row], width: usize) -> Vec<Line<'static>> {
    let mut out = Vec::new();
    for row in rows {
        let label = format!("{}: ", row.label);
        let indent = " ".repeat(label.chars().count().min(width.saturating_sub(1)));
        let body_width = width.saturating_sub(indent.len()).max(1);
        for (i, text) in wrap(&row.body, body_width).into_iter().enumerate() {
            let head = if i == 0 {
                Span::styled(label.clone(), style(row.kind).add_modifier(Modifier::BOLD))
            } else {
                Span::raw(indent.clone())
            };
            out.push(Line::from(vec![head, Span::styled(text, style(row.kind))]));
        }
    }
    out
}

/// The lines of the open question: what it asks, then its choices or its field.
fn question_lines(q: &OpenQuestion, asking: &Asking) -> Vec<Line<'static>> {
    let shape = ask::shape(&q.schema);
    let mut out = vec![Line::from(vec![
        Span::styled(
            format!("{} asks: ", q.extension),
            Style::new().fg(Color::Yellow).bold(),
        ),
        Span::raw(q.text.clone()),
    ])];
    let choices = shape.choices();
    if choices.is_empty() {
        out.push(Line::from(vec![
            Span::styled("answer: ", Style::new().fg(Color::Yellow)),
            Span::raw(asking.field.text()),
        ]));
    } else {
        for (i, choice) in choices.iter().enumerate() {
            let on = i == asking.picked;
            let mark = if on { "> " } else { "  " };
            let look = if on {
                Style::new().add_modifier(Modifier::REVERSED)
            } else {
                Style::new()
            };
            out.push(Line::styled(format!("{mark}{}", ask::show(choice)), look));
        }
    }
    let hint = if choices.is_empty() {
        "Enter answers, Esc declines"
    } else {
        "Up/Down choose, Enter answers, Esc declines"
    };
    out.push(Line::styled(hint, style(Kind::Dim)));
    if let Some(error) = &asking.error {
        out.push(Line::styled(error.clone(), style(Kind::Failure)));
    }
    out
}

fn status(screen: &Screen) -> Line<'static> {
    let model = screen.model;
    let (state, look) = match model.view() {
        Some(v) if v.busy => ("busy", Style::new().fg(Color::Yellow).bold()),
        Some(_) => ("idle", Style::new().fg(Color::Green).bold()),
        None => ("connecting", Style::new().fg(Color::DarkGray)),
    };
    let mut spans = vec![Span::styled(state, look)];
    if let Some(v) = model.view() {
        spans.push(Span::raw(format!(
            " | cursor {} | {}",
            v.cursor,
            v.model.as_deref().unwrap_or("no model")
        )));
    }
    if let Some(waiting) = model.retrying() {
        spans.push(Span::styled(
            format!(" | {waiting}"),
            Style::new().fg(Color::Yellow),
        ));
    }
    if let Some(trouble) = model.trouble() {
        spans.push(Span::styled(format!(" | {trouble}"), style(Kind::Failure)));
    } else if let Some(notice) = model.notices().last() {
        spans.push(Span::styled(
            format!(" | {notice}"),
            Style::new().fg(Color::Yellow),
        ));
    }
    Line::from(spans)
}

/// Draw the whole screen. The terminal's cursor goes to the editor, or to the open question's
/// field.
pub fn draw(f: &mut Canvas, screen: &Screen) {
    let area = f.area();
    let width = usize::from(area.width);
    let pending = lines(&screen.model.pending(), width);
    let asked = screen
        .question
        .map(|q| question_lines(q, screen.asking))
        .unwrap_or_default();
    let tall = |lines: &[Line]| u16::try_from(lines.len()).unwrap_or(u16::MAX);
    let [header, transcript, waiting, question, editor, bar] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Fill(1),
        Constraint::Length(tall(&pending)),
        Constraint::Length(tall(&asked)),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .areas(area);

    f.render_widget(
        Line::styled(
            format!(
                "tau {} | {} | {}",
                screen.session, screen.cwd, screen.client
            ),
            Style::new().add_modifier(Modifier::REVERSED),
        ),
        header,
    );
    render_transcript(f, transcript, screen);
    f.render_widget(Paragraph::new(pending).wrap(Wrap { trim: false }), waiting);
    f.render_widget(Paragraph::new(asked), question);

    let prompt = Span::styled("> ", Style::new().fg(Color::Green).bold());
    f.render_widget(
        Line::from(vec![prompt, Span::raw(screen.editor.text())]),
        editor,
    );
    f.render_widget(status(screen), bar);

    let (field, rect) = match (screen.question, ask_has_field(screen)) {
        (Some(_), true) => (&screen.asking.field, field_rect(question)),
        _ => (
            screen.editor,
            Rect {
                x: editor.x + 2,
                ..editor
            },
        ),
    };
    let at = u16::try_from(field.at()).unwrap_or(u16::MAX);
    f.set_cursor_position(Position::new(
        rect.x
            .saturating_add(at)
            .min(area.right().saturating_sub(1)),
        rect.y,
    ));
}

fn ask_has_field(screen: &Screen) -> bool {
    screen
        .question
        .is_some_and(|q| ask::shape(&q.schema).choices().is_empty())
}

/// Where the answer field's text starts: the second line of the question box, after `answer: `.
fn field_rect(question: Rect) -> Rect {
    Rect {
        x: question.x + 8,
        y: question.y + 1,
        width: question.width.saturating_sub(8),
        height: 1,
    }
}

fn render_transcript(f: &mut Canvas, area: Rect, screen: &Screen) {
    let all = lines(&screen.model.rows(), usize::from(area.width));
    let height = usize::from(area.height);
    let end = all.len().saturating_sub(screen.scroll);
    let start = end.saturating_sub(height);
    f.render_widget(Paragraph::new(all[start..end].to_vec()), area);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn short_lines_stay_and_newlines_break() {
        assert_eq!(wrap("ab\ncd", 10), vec!["ab", "cd"]);
        assert_eq!(wrap("", 10), vec![""]);
    }

    #[test]
    fn long_lines_break_at_the_last_space_that_fits() {
        assert_eq!(wrap("one two three", 8), vec!["one two", "three"]);
    }

    #[test]
    fn a_word_longer_than_the_line_is_cut() {
        assert_eq!(wrap("abcdefgh", 3), vec!["abc", "def", "gh"]);
    }

    #[test]
    fn indentation_is_kept() {
        assert_eq!(wrap("    code", 20), vec!["    code"]);
    }

    #[test]
    fn rows_get_their_label_once_and_an_indent_after() {
        let rows = [Row {
            label: "you".into(),
            body: "aaa bbb".into(),
            kind: Kind::Mine,
        }];
        let got: Vec<String> = lines(&rows, 9)
            .iter()
            .map(|l| l.spans.iter().map(|s| s.content.as_ref()).collect())
            .collect();
        assert_eq!(got, vec!["you: aaa", "     bbb"]);
    }
}
