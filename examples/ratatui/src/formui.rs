//! Drawing a form (or the list of extensions to configure) as a box over the chat.

use ratatui::Frame as Canvas; // rules: allow(frame): ratatui's own name for what one draw paints
use ratatui::{
    layout::{Constraint, Flex, Layout, Position, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Clear, Paragraph},
};

use crate::{
    form::{Field, Focus, Form, Pick, Widget, show},
    ui::wrap,
};

const HINT: &str = "Tab next  Space toggles  \u{2190}\u{2192} choose  Enter on Apply applies  \
                    Esc cancels";

/// A box of `width` by `height` in the middle of `area`, cleared.
fn centered(f: &mut Canvas, area: Rect, width: u16, height: u16, title: &str) -> Rect {
    let width = width.min(area.width);
    let height = height.min(area.height);
    let [row] = Layout::vertical([Constraint::Length(height)])
        .flex(Flex::Center)
        .areas(area);
    let [boxed] = Layout::horizontal([Constraint::Length(width)])
        .flex(Flex::Center)
        .areas(row);
    f.render_widget(Clear, boxed);
    let block = Block::bordered().title(format!(" {title} "));
    let inner = block.inner(boxed);
    f.render_widget(block, boxed);
    inner
}

fn dim() -> Style {
    Style::new().fg(Color::DarkGray)
}

fn widget_line(field: &Field, focused: bool) -> (Line<'static>, Option<u16>) {
    let mark = |on: bool, round: bool| match (on, round) {
        (true, true) => "(\u{2022})",
        (false, true) => "( )",
        (true, false) => "[x]",
        (false, false) => "[ ]",
    };
    match &field.widget {
        Widget::Text(editor) => {
            let text = editor.text();
            let shown = if text.is_empty() {
                Span::styled(field.placeholder(), dim())
            } else {
                Span::raw(text)
            };
            let at = u16::try_from(editor.at()).unwrap_or(u16::MAX);
            (
                Line::from(vec![Span::raw("  "), shown]),
                focused.then_some(2 + at),
            )
        }
        Widget::Switch(on) => (
            Line::from(format!(
                "  {} {}",
                mark(*on, false),
                if *on { "on" } else { "off" }
            )),
            None,
        ),
        Widget::Choice { options, picked } => {
            let spans = options.iter().enumerate().flat_map(|(i, o)| {
                [
                    Span::raw("  "),
                    Span::raw(format!("{} {}", mark(*picked == Some(i), true), show(o))),
                ]
            });
            (Line::from(spans.collect::<Vec<_>>()), None)
        }
        Widget::Multi { options, on, at } => {
            let spans = options.iter().zip(on).enumerate().flat_map(|(i, (o, on))| {
                let look = if focused && i == *at {
                    Style::new().add_modifier(Modifier::REVERSED)
                } else {
                    Style::new()
                };
                [
                    Span::raw("  "),
                    Span::styled(format!("{} {}", mark(*on, false), show(o)), look),
                ]
            });
            (Line::from(spans.collect::<Vec<_>>()), None)
        }
    }
}

/// Draw `form`; where the terminal's cursor goes, if the keyboard is in a text field.
pub fn draw_form(f: &mut Canvas, form: &Form) -> Option<Position> {
    let area = f.area();
    let wide = area.width;
    let tall = area.height.saturating_sub(2).max(8);
    let inner = centered(f, area, wide, tall, &form.title);
    let [body, hint] = Layout::vertical([Constraint::Fill(1), Constraint::Length(1)]).areas(inner);

    let mut lines: Vec<Line> = Vec::new();
    let mut seen_at = 0;
    let mut cursor = None;
    let focus = form.focus();
    for (i, field) in form.fields.iter().enumerate() {
        let focused = focus == Focus::Field(i);
        let head = format!(
            "{}{}{}{}",
            if focused { "> " } else { "  " },
            field.name,
            if field.required { "" } else { " (optional)" },
            field
                .description
                .as_ref()
                .map_or(String::new(), |d| format!(": {d}"))
        );
        let look = if focused {
            Style::new().add_modifier(Modifier::BOLD)
        } else {
            Style::new()
        };
        let across = usize::from(body.width);
        lines.extend(
            wrap(&head, across)
                .into_iter()
                .map(|l| Line::styled(l, look)),
        );
        let (line, at) = widget_line(field, focused);
        if focused {
            seen_at = lines.len();
            cursor = at.map(|x| (x, lines.len()));
        }
        lines.push(line);
        if let Some(error) = &field.error {
            lines.extend(
                wrap(&format!("  {error}"), across)
                    .into_iter()
                    .map(|l| Line::styled(l, Style::new().fg(Color::Red))),
            );
        }
    }
    lines.push(Line::default());
    let save = format!(
        "{} save for every session (your config file)",
        if form.save { "[x]" } else { "[ ]" }
    );
    let look = |on: bool| {
        if on {
            Style::new().add_modifier(Modifier::REVERSED)
        } else {
            Style::new()
        }
    };
    if focus == Focus::Save {
        seen_at = lines.len();
    }
    lines.push(Line::styled(save, look(focus == Focus::Save)));
    if matches!(focus, Focus::Apply | Focus::Cancel) {
        seen_at = lines.len();
    }
    lines.push(Line::from(vec![
        Span::styled("[ Apply ]", look(focus == Focus::Apply)),
        Span::raw("  "),
        Span::styled("[ Cancel ]", look(focus == Focus::Cancel)),
    ]));
    if let Some(error) = &form.error {
        let red = Style::new().fg(Color::Red);
        lines.extend(
            wrap(error, usize::from(body.width))
                .into_iter()
                .map(|l| Line::styled(l, red)),
        );
    }
    // Keep the focused part in view.
    let height = usize::from(body.height);
    let top = (seen_at + 2).saturating_sub(height);
    let shown: Vec<Line> = lines.into_iter().skip(top).take(height).collect();
    f.render_widget(Paragraph::new(shown), body);
    f.render_widget(Line::styled(HINT, dim()), hint);
    cursor.and_then(|(x, y)| {
        let row = y.checked_sub(top)?;
        let row = u16::try_from(row)
            .ok()
            .filter(|r| usize::from(*r) < height)?;
        Some(Position::new(body.x + x, body.y + row))
    })
}

/// Draw the list of extensions to configure.
pub fn draw_pick(f: &mut Canvas, pick: &Pick) {
    let area = f.area();
    let tall = u16::try_from(pick.items.len() + 3).unwrap_or(u16::MAX);
    let inner = centered(f, area, 50, tall, &pick.title);
    let lines: Vec<Line> = pick
        .items
        .iter()
        .enumerate()
        .map(|(i, item)| {
            if i == pick.at {
                Line::styled(
                    format!("> {item}"),
                    Style::new().add_modifier(Modifier::REVERSED),
                )
            } else {
                Line::from(format!("  {item}"))
            }
        })
        .chain([
            Line::default(),
            Line::styled("Up/Down choose, Enter picks, Esc cancels", dim()),
        ])
        .collect();
    f.render_widget(Paragraph::new(lines), inner);
}
