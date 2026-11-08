//! Drawing: the screen from the model, the editor and the open question. Pure functions of what
//! they are given, so wrapping can be tested without a terminal.

use ratatui::Frame as Canvas; // rules: allow(frame): ratatui's own name for what one draw paints
use ratatui::{
    layout::{Constraint, Layout, Position, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::Paragraph,
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

/// What the open question draws: its lines, and where the answer field's cursor is in them
/// (row, column), if it has one.
struct Asked {
    lines: Vec<Line<'static>>,
    cursor: Option<(usize, usize)>,
}

/// `text` as lines at most `width` wide, each styled `look`.
fn wrapped(text: &str, width: usize, look: Style, out: &mut Vec<Line<'static>>) {
    out.extend(wrap(text, width).into_iter().map(|l| Line::styled(l, look)));
}

/// The lines of the open question, `width` columns wide: what it asks, then its choices or its
/// field.
fn question_lines(q: &OpenQuestion, asking: &Asking, width: usize, cap: usize) -> Asked {
    let shape = ask::shape(&q.schema);
    // The text may span lines (a permission question: the call, then the rule that matched).
    let mut text = q.text.lines();
    let label = format!("{} asks: ", q.extension);
    let mut out = Vec::new();
    let first = format!("{label}{}", text.next().unwrap_or_default());
    for (i, piece) in wrap(&first, width).into_iter().enumerate() {
        if i == 0 {
            let (head, rest) = piece.split_at(piece.len().min(label.len()));
            out.push(Line::from(vec![
                Span::styled(head.to_owned(), Style::new().fg(Color::Yellow).bold()),
                Span::raw(rest.to_owned()),
            ]));
        } else {
            out.push(Line::from(piece));
        }
    }
    for line in text {
        wrapped(&format!("  {line}"), width, Style::new(), &mut out);
    }
    let choices = shape.choices();
    let mut cursor = None;
    // The line to keep in view when the question is taller than `cap`.
    let mut focus = out.len();
    if choices.is_empty() {
        let (rows, at, _) = typed("answer: ", &asking.field, width, usize::MAX);
        cursor = Some((out.len() + at.0, at.1));
        focus = out.len() + at.0;
        for (i, row) in rows.into_iter().enumerate() {
            let head = if i == 0 { "answer: " } else { "" };
            out.push(Line::from(vec![
                Span::styled(head, Style::new().fg(Color::Yellow)),
                Span::raw(row),
            ]));
        }
    } else {
        for (i, choice) in choices.iter().enumerate() {
            let on = i == asking.picked;
            if on {
                focus = out.len();
            }
            let mark = if on { "> " } else { "  " };
            let look = if on {
                Style::new().add_modifier(Modifier::REVERSED)
            } else {
                Style::new()
            };
            wrapped(
                &format!("{mark}{}", ask::show(choice)),
                width,
                look,
                &mut out,
            );
        }
    }
    let hint = if choices.is_empty() {
        "Enter answers, Esc declines"
    } else {
        "Up/Down choose, Enter answers, Esc declines"
    };
    wrapped(hint, width, style(Kind::Dim), &mut out);
    if let Some(error) = &asking.error {
        wrapped(error, width, style(Kind::Failure), &mut out);
    }
    // Taller than `cap`: show the window holding the field or the picked choice, with the line
    // after it (the hint, usually).
    let first = window(out.len(), focus, 1, cap);
    let lines = out.into_iter().skip(first).take(cap).collect();
    let cursor = cursor.map(|(row, col)| (row - first, col));
    Asked { lines, cursor }
}

/// `field` laid out after a `prefix` on lines `width` cells wide, broken wherever the width falls
/// (a wide character that does not fit starts the next row; a combining one takes no room) so
/// the cursor's place is known: the rows (the first without the prefix), where the cursor is in
/// them (row, column), and the row the rows start at when more than `cap` would show. At most
/// `cap` rows are returned, ending at the cursor's row at the latest.
fn typed(
    prefix: &str,
    field: &Editor,
    width: usize,
    cap: usize,
) -> (Vec<String>, (usize, usize), usize) {
    let width = width.max(1);
    let used = prefix
        .chars()
        .map(|c| c.width().unwrap_or(0))
        .sum::<usize>()
        .min(width - 1);
    let text = field.text();
    let mut rows: Vec<String> = vec![String::new()];
    let (mut row, mut col) = (0, used);
    let mut cursor = (0, used);
    let mut count = 0;
    for (i, c) in text.chars().enumerate() {
        count = i + 1;
        let w = c.width().unwrap_or(0);
        if col > 0 && col + w > width {
            rows.push(String::new());
            row += 1;
            col = 0;
        }
        if i == field.at() {
            cursor = (row, col);
        }
        rows[row].push(c);
        col += w;
    }
    if field.at() >= count {
        // Past the last character on a full row, the cursor starts the next row.
        if col >= width {
            rows.push(String::new());
            row += 1;
            col = 0;
        }
        cursor = (row, col);
    }
    let first = window(rows.len(), cursor.0, 0, cap);
    let shown: Vec<String> = rows.into_iter().skip(first).take(cap).collect();
    (shown, (cursor.0 - first, cursor.1), first)
}

/// Where a window of `cap` of `total` lines starts so that line `focus` and the `after` lines
/// below it show, if they fit.
fn window(total: usize, focus: usize, after: usize, cap: usize) -> usize {
    (focus + after + 1)
        .saturating_sub(cap)
        .min(total.saturating_sub(cap))
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
/// field. Returns how far up the transcript can be scrolled at this size, for the caller to
/// keep the scroll within.
pub fn draw(f: &mut Canvas, screen: &Screen) -> usize {
    let area = f.area();
    let width = usize::from(area.width);
    let pending = lines(&screen.model.pending(), width);
    let asked = screen
        .question
        .map(|q| question_lines(q, screen.asking, width, usize::from(area.height / 2).max(1)));
    let tall = |n: usize| u16::try_from(n).unwrap_or(u16::MAX);
    // A long line being typed grows the editor upward, up to a quarter of the screen.
    let (typed_rows, cursor, first) = typed(
        "> ",
        screen.editor,
        width,
        usize::from(area.height / 4).max(1),
    );
    let [header, transcript, waiting, question, editor, bar] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Fill(1),
        Constraint::Length(tall(pending.len())),
        Constraint::Length(tall(asked.as_ref().map_or(0, |a| a.lines.len()))),
        Constraint::Length(tall(typed_rows.len())),
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
    let most = render_transcript(f, transcript, screen);
    f.render_widget(Paragraph::new(pending), waiting);
    if let Some(asked) = &asked {
        f.render_widget(Paragraph::new(asked.lines.clone()), question);
    }

    let prompt = Span::styled("> ", Style::new().fg(Color::Green).bold());
    let rows: Vec<Line> = typed_rows
        .into_iter()
        .enumerate()
        .map(|(i, row)| match (first, i) {
            (0, 0) => Line::from(vec![prompt.clone(), Span::raw(row)]),
            _ => Line::from(row),
        })
        .collect();
    f.render_widget(Paragraph::new(rows), editor);
    f.render_widget(status(screen), bar);

    let (x, y) = match asked.as_ref().and_then(|a| a.cursor) {
        Some((row, col)) => (question.x + to_u16(col), question.y + to_u16(row)),
        None => (editor.x + to_u16(cursor.1), editor.y + to_u16(cursor.0)),
    };
    f.set_cursor_position(Position::new(
        x.min(area.right().saturating_sub(1)),
        y.min(area.bottom().saturating_sub(1)),
    ));
    most
}

fn to_u16(n: usize) -> u16 {
    u16::try_from(n).unwrap_or(u16::MAX)
}

/// Draw the transcript scrolled up by `screen.scroll` lines, never past its top. Returns the
/// most it can be scrolled at this size.
fn render_transcript(f: &mut Canvas, area: Rect, screen: &Screen) -> usize {
    let all = lines(&screen.model.rows(), usize::from(area.width));
    let height = usize::from(area.height);
    let most = all.len().saturating_sub(height);
    let end = all.len() - screen.scroll.min(most);
    let start = end.saturating_sub(height);
    f.render_widget(Paragraph::new(all[start..end].to_vec()), area);
    most
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

    #[test]
    fn a_question_of_several_lines_shows_each_and_the_field_follows_them() {
        let q: OpenQuestion = serde_json::from_value(serde_json::json!({
            "id": 1, "to": null, "extension": "permissions",
            "text": "bash: git push\n(ask rule: bash)",
            "schema": {"type": "string", "enum": ["yes", "always", "no"]},
        }))
        .unwrap();
        let asked = question_lines(&q, &Asking::default(), 40, 10);
        let got: Vec<String> = asked
            .lines
            .iter()
            .map(|l| l.spans.iter().map(|s| s.content.as_ref()).collect())
            .collect();
        assert_eq!(got[0], "permissions asks: bash: git push");
        assert_eq!(got[1], "  (ask rule: bash)");
        assert_eq!(got[2], "> yes");
    }
}

#[cfg(test)]
mod screen_tests {
    use ratatui::{Terminal, backend::TestBackend}; // rules: allow(backend): ratatui API
    use serde_json::json;

    use super::*;

    const ME: &str = "tester@host:ratatui";

    fn hear(model: &mut Model, event: serde_json::Value) {
        model.hear(serde_json::from_value(event).unwrap());
    }

    /// A session whose one reply is `words` numbered words long.
    fn talked(words: usize) -> Model {
        let mut model = Model::new(ME);
        hear(
            &mut model,
            json!({"kind": "CursorView", "cursor": 0, "at": 2, "busy": false, "auto": false,
                   "questions": [], "items": [], "ending": "Done", "hook_error": null,
                   "model": "m", "shown": []}),
        );
        hear(&mut model, json!({"kind": "Labels", "labels": []}));
        let text = (0..words)
            .map(|i| format!("w{i:02}"))
            .collect::<Vec<_>>()
            .join(" ");
        let nodes = vec![
            json!({"id": 0, "parent": null, "time": 0, "by": {"Frontend": "t"},
                   "body": {"System": "sys"}, "delivers": []}),
            json!({"id": 1, "parent": 0, "time": 0, "by": {"Frontend": "t"},
                   "body": {"User": "hi"}, "delivers": []}),
            json!({"id": 2, "parent": 1, "time": 0, "by": {"Frontend": "t"},
                   "body": {"Assistant": {"parts": [{"Text": text}], "stop": "End",
                                          "finish_reason": null, "meta": null}},
                   "delivers": []}),
        ];
        hear(&mut model, json!({"kind": "Appended", "nodes": nodes}));
        model
    }

    /// What a draw left: the screen's lines, how far the transcript can scroll, the cursor.
    struct Shot {
        lines: Vec<String>,
        most: usize,
        cursor: (u16, u16),
    }

    /// The screen's lines after drawing `model` on a `width` by `height` terminal, and how far
    /// the transcript can scroll.
    fn drawn_with(
        model: &Model,
        (width, height): (u16, u16),
        scroll: usize,
        typing: &str,
        question: Option<&OpenQuestion>,
    ) -> Shot {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap(); // rules: allow(backend): ratatui API
        let mut editor = Editor::default();
        editor.set(typing);
        let asking = Asking::default();
        let mut most = 0;
        terminal
            .draw(|f| {
                most = draw(
                    f,
                    &Screen {
                        model,
                        editor: &editor,
                        asking: &asking,
                        question,
                        scroll,
                        session: "s",
                        cwd: "/",
                        client: ME,
                    },
                );
            })
            .unwrap();
        let buffer = terminal.backend().buffer(); // rules: allow(backend): ratatui API
        let lines = (0..height)
            .map(|y| (0..width).map(|x| buffer[(x, y)].symbol()).collect())
            .collect();
        let cursor = terminal.get_cursor_position().unwrap();
        Shot {
            lines,
            most,
            cursor: (cursor.x, cursor.y),
        }
    }

    fn drawn(model: &Model, size: (u16, u16), scroll: usize) -> Vec<String> {
        drawn_with(model, size, scroll, "", None).lines
    }

    fn widest(lines: &[String]) -> usize {
        lines
            .iter()
            .map(|l| l.trim_end().chars().count())
            .max()
            .unwrap()
    }

    #[test]
    fn transcript_text_fills_the_terminal_width() {
        let lines = drawn(&talked(60), (120, 24), 0);
        assert!(widest(&lines) > 100, "{lines:#?}");
    }

    #[test]
    fn a_long_question_wraps_to_the_width_instead_of_clipping() {
        let words = (0..40).map(|i| format!("q{i:02}")).collect::<Vec<_>>();
        let q: OpenQuestion = serde_json::from_value(json!({
            "id": 1, "to": null, "extension": "permissions",
            "text": format!("bash: {}", words.join(" ")),
            "schema": {"type": "string", "enum": ["yes", "no"]},
        }))
        .unwrap();
        let lines = drawn_with(&talked(1), (60, 24), 0, "", Some(&q)).lines;
        let screen = lines.join("\n");
        assert!(
            screen.contains("q39"),
            "the end of the question is shown: {screen}"
        );
        assert!(widest(&lines) <= 60);
    }

    #[test]
    fn a_long_line_being_typed_wraps_and_the_cursor_follows_it() {
        let typing = "x".repeat(70);
        let lines = drawn_with(&talked(1), (40, 12), 0, &typing, None).lines;
        let xs: usize = lines.iter().map(|l| l.matches('x').count()).sum();
        assert_eq!(xs, 70, "{lines:#?}");
        // "> " and 70 characters: the cursor is on the second row of 40 columns.
        assert_eq!(typed("> ", &editor_of(&typing), 40, 6).1, (1, 32));
    }

    fn editor_of(text: &str) -> Editor {
        let mut editor = Editor::default();
        editor.set(text);
        editor
    }

    #[test]
    fn a_terminal_that_grows_shows_the_whole_transcript_again() {
        let model = talked(60);
        let Shot {
            lines: small, most, ..
        } = drawn_with(&model, (40, 8), 0, "", None);
        assert!(most > 0 && !small.join("").contains("w00"), "{small:#?}");
        // Scrolled to the top, then the terminal grows to fit everything and the scroll that
        // no longer exists is not kept.
        let Shot {
            lines: big, most, ..
        } = drawn_with(&model, (40, 40), most, "", None);
        assert_eq!(most, 0);
        let big = big.join("\n");
        assert!(big.contains("w00") && big.contains("w59"), "{big}");
    }

    #[test]
    fn at_the_bottom_stays_at_the_bottom_when_the_terminal_resizes() {
        let model = talked(60);
        for size in [(40, 8), (40, 20), (100, 20), (30, 6)] {
            let lines = drawn(&model, size, 0);
            assert!(lines.join("").contains("w59"), "{size:?}: {lines:#?}");
        }
    }

    /// Where the cursor is (column, row) after `typing` is drawn on a 20 by 24 terminal.
    fn cursor_after(typing: &str) -> (u16, u16) {
        drawn_with(&talked(1), (20, 24), 0, typing, None).cursor
    }

    #[test]
    fn wide_characters_are_counted_by_their_cells_and_wrap_whole() {
        // "> " then 9 CJK characters fill 20 cells; the 30th lands on the fourth row.
        let cjk = "\u{4e2d}".repeat(30);
        let shot = drawn_with(&talked(1), (20, 24), 0, &cjk, None);
        assert_eq!(widest(&shot.lines), 20, "{:#?}", shot.lines);
        assert_eq!(shot.cursor, (2, 22), "{:#?}", shot.lines);
    }

    #[test]
    fn a_combining_mark_takes_no_room_and_emoji_take_two() {
        assert_eq!(cursor_after("ae\u{301}"), (4, 22));
        // "> " and 12 emoji of two cells on 20 columns: 9 fit the first row.
        let emoji = "\u{1f600}".repeat(12);
        assert_eq!(cursor_after(&emoji), (6, 22));
    }

    #[test]
    fn a_question_taller_than_the_screen_keeps_its_field_and_hint_in_view() {
        let words: Vec<String> = (0..400).map(|i| format!("q{i:03}")).collect();
        let q: OpenQuestion = serde_json::from_value(json!({
            "id": 1, "to": null, "extension": "ask", "text": words.join(" "),
            "schema": {"type": "string"},
        }))
        .unwrap();
        let shot = drawn_with(&talked(1), (40, 20), 0, "", Some(&q));
        let screen = shot.lines.join("\n");
        assert!(screen.contains("answer: "), "{screen}");
        assert!(screen.contains("Esc declines"), "{screen}");
        assert!(
            screen.contains("connecting") || screen.contains("idle"),
            "{screen}"
        );
        let row = shot
            .lines
            .iter()
            .position(|l| l.starts_with("answer: "))
            .unwrap();
        assert_eq!(usize::from(shot.cursor.1), row);
    }

    #[test]
    fn a_long_choice_list_keeps_the_picked_choice_in_view() {
        let choices: Vec<String> = (0..60).map(|i| format!("choice{i:02}")).collect();
        let q: OpenQuestion = serde_json::from_value(json!({
            "id": 1, "to": null, "extension": "ask", "text": "which?",
            "schema": {"type": "string", "enum": choices},
        }))
        .unwrap();
        let mut terminal = Terminal::new(TestBackend::new(40, 20)).unwrap(); // rules: allow(backend): ratatui API
        let model = talked(1);
        let (editor, asking) = (
            Editor::default(),
            Asking {
                picked: 50,
                ..Asking::default()
            },
        );
        terminal
            .draw(|f| {
                draw(
                    f,
                    &Screen {
                        model: &model,
                        editor: &editor,
                        asking: &asking,
                        question: Some(&q),
                        scroll: 0,
                        session: "s",
                        cwd: "/",
                        client: ME,
                    },
                );
            })
            .unwrap();
        let buffer = terminal.backend().buffer(); // rules: allow(backend): ratatui API
        let screen: String = (0..20)
            .map(|y| (0..40).map(|x| buffer[(x, y)].symbol()).collect::<String>())
            .collect::<Vec<_>>()
            .join("\n");
        assert!(screen.contains("> choice50"), "{screen}");
    }
}
