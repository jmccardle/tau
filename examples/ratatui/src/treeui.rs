//! Drawing the tree browser: the open rows that are on screen, given their words now; the detail
//! pane; the title and the keys.

use ratatui::Frame as Canvas; // rules: allow(frame): ratatui's own name for what one draw paints
use ratatui::{
    layout::{Constraint, Layout, Position, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph},
};

use crate::{
    editor::Editor,
    model::Model,
    plan::Kind,
    tree::{Flat, Tree},
    treetext::{self, Marks, Tag},
    ui::wrap,
};

const HELP: &str = "Enter go to  v view  o open a cursor here  l label  [ ] cursors  f follow  p source  \
                    \u{2190}\u{2192} fold  Esc leave";
/// Below this many columns the detail pane goes under the tree, and below `SHORT` rows it goes.
const WIDE: u16 = 100;
const SHORT: u16 = 20;

fn tag_style(tag: Tag) -> Style {
    match tag {
        Tag::User => Style::new().fg(Color::Green),
        Tag::Assistant => Style::new().fg(Color::Blue),
        Tag::Tool => Style::new().fg(Color::Gray),
        Tag::System | Tag::Structural => Style::new().fg(Color::DarkGray),
        Tag::Stub => Style::new().fg(Color::Yellow),
    }
}

/// One row as a line `width` cells wide: guide and fold mark, the tag in its colour, the text,
/// the fold summary and cursor marks, closed cursors dimmed.
fn row_line(
    flat: &Flat,
    model: &Model,
    marks: &Marks,
    on_path: bool,
    width: usize,
    highlighted: bool,
) -> Line<'static> {
    let guide = "  ".repeat(flat.depth);
    let fold = match (flat.row.expandable, flat.open) {
        (false, _) => "  ",
        (true, true) => "\u{25be} ",
        (true, false) => "\u{25b8} ",
    };
    let prefix = format!("{guide}{fold}");
    let node = model
        .node(flat.row.node as u64)
        .expect("the tree holds its nodes");
    let label = treetext::row_label(&flat.row, node, marks, !flat.open);
    let (text, dim_at) = treetext::fit(&label, width.saturating_sub(prefix.chars().count()));
    let chars: Vec<char> = text.chars().collect();
    let tag_end = label.tag_size.min(chars.len());
    let dim_from = dim_at.unwrap_or(chars.len()).clamp(tag_end, chars.len());
    let part = |from: usize, to: usize| chars[from..to].iter().collect::<String>();
    let gutter = if on_path {
        Style::new().fg(Color::Cyan)
    } else {
        Style::new()
    };
    let mut line = Line::from(vec![
        Span::styled(prefix, gutter),
        Span::styled(part(0, tag_end), tag_style(label.tag)),
        Span::raw(part(tag_end, dim_from)),
        Span::styled(
            part(dim_from, chars.len()),
            Style::new().fg(Color::DarkGray),
        ),
    ]);
    if highlighted {
        line = line.patch_style(Style::new().add_modifier(Modifier::REVERSED));
    }
    line
}

fn draw_rows(f: &mut Canvas, area: Rect, tree: &Tree, model: &Model) {
    let marks = tree.marks_of(model);
    let path = tree.path();
    let window = tree.window(usize::from(area.height));
    let at = tree.highlight();
    let lines: Vec<Line> = tree.flat()[window.clone()]
        .iter()
        .zip(window)
        .map(|(flat, i)| {
            let on_path = path.contains(&flat.row.node) && flat.row.kind != Kind::Stub;
            row_line(
                flat,
                model,
                &marks,
                on_path,
                usize::from(area.width),
                i == at,
            )
        })
        .collect();
    f.render_widget(Paragraph::new(lines), area);
}

fn draw_detail(f: &mut Canvas, area: Rect, tree: &Tree, model: &Model) {
    let text = match tree.highlighted() {
        Some(flat) => {
            let node = model
                .node(flat.row.node as u64)
                .expect("the tree holds its nodes");
            tree.walk_line() + &treetext::detail_text(&flat.row, node)
        }
        None => String::new(),
    };
    let block = Block::new().borders(Borders::LEFT | Borders::TOP);
    let inner = block.inner(area);
    let lines: Vec<Line> = wrap(&text, usize::from(inner.width))
        .into_iter()
        .take(usize::from(inner.height))
        .map(Line::from)
        .collect();
    f.render_widget(block, area);
    f.render_widget(Paragraph::new(lines), inner);
}

/// Draw the whole screen: the tree browser, in place of the chat.
pub fn draw(f: &mut Canvas, tree: &Tree, model: &Model) {
    let area = f.area();
    let [title, body, help] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Fill(1),
        Constraint::Length(1),
    ])
    .areas(area);
    f.render_widget(
        Line::styled(tree.title(), Style::new().add_modifier(Modifier::REVERSED)),
        title,
    );
    // The status line is not on this screen; a provider waiting to retry is still said.
    let help_line = match model.retrying() {
        Some(waiting) => Line::styled(
            format!("{waiting}  |  {HELP}"),
            Style::new().fg(Color::Yellow),
        ),
        None => Line::styled(HELP, Style::new().fg(Color::DarkGray)),
    };
    f.render_widget(help_line, help);
    let (rows, detail) = if area.width >= WIDE {
        let [rows, detail] =
            Layout::horizontal([Constraint::Percentage(60), Constraint::Percentage(40)])
                .areas(body);
        (rows, Some(detail))
    } else if area.height >= SHORT {
        let [rows, detail] =
            Layout::vertical([Constraint::Fill(1), Constraint::Length(body.height / 3)])
                .areas(body);
        (rows, Some(detail))
    } else {
        (body, None)
    };
    draw_rows(f, rows, tree, model);
    if let Some(detail) = detail {
        draw_detail(f, detail, tree, model);
    }
}

/// While the tree is still being read.
pub fn draw_reading(f: &mut Canvas, model: &Model) {
    let area = f.area();
    f.render_widget(
        Line::styled(
            "The session's tree",
            Style::new().add_modifier(Modifier::REVERSED),
        ),
        Rect { height: 1, ..area },
    );
    f.render_widget(
        Line::from(format!("reading the tree: {} nodes so far", model.dense())),
        Rect {
            y: area.y + 1,
            height: 1,
            ..area
        },
    );
}

/// A question on the last line. `text` is the editor being typed in, if the question is typed.
pub fn draw_prompt(f: &mut Canvas, ask: &str, text: Option<&Editor>) {
    let area = f.area();
    let line = Rect {
        y: area.bottom().saturating_sub(1),
        height: 1,
        ..area
    };
    let typed = text.map(Editor::text).unwrap_or_default();
    f.render_widget(Clear, line);
    f.render_widget(
        Line::from(vec![
            Span::styled(ask.to_owned(), Style::new().fg(Color::Yellow).bold()),
            Span::raw(typed),
        ]),
        line,
    );
    if let Some(editor) = text {
        let x = line.x + u16::try_from(ask.chars().count() + editor.at()).unwrap_or(u16::MAX);
        f.set_cursor_position(Position::new(x.min(area.right().saturating_sub(1)), line.y));
    }
}
