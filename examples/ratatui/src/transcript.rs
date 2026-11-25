//! The transcript laid out with its images (D30, D31): each image is its `[image WxH mime]` line
//! and, under it, the rows its picture takes. The rows are kept from the start, sized from the
//! image's pixels as the wire gives them, so nothing moves when the bytes arrive.

use ratatui::Frame as Canvas; // rules: allow(frame): ratatui's own name for what one draw paints
use ratatui::{
    layout::{Rect, Size},
    style::{Color, Style},
    text::{Line, Span},
    widgets::Paragraph,
};
use ratatui_image::sliced::SignedPosition;

use crate::{
    images::{Images, Look},
    model::{Kind, Row},
    ui::{row_lines, style},
    wire,
};

/// A picture's place in the laid-out lines: its first line, its column, its size.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Picture {
    pub line: usize,
    pub column: u16,
    pub size: Size,
    pub sha256: String,
}

/// The transcript's lines, and the pictures drawn over the blank lines kept for them.
#[derive(Debug, Default)]
pub struct Laid {
    pub lines: Vec<Line<'static>>,
    pub pictures: Vec<Picture>,
}

/// The rows as lines `width` wide, each image as its line and the rows of its picture.
pub fn lay_out(rows: &[Row], width: usize, images: &Images) -> Laid {
    let mut laid = Laid::default();
    for row in rows {
        let (lines, indent) = row_lines(row, width);
        laid.lines.extend(lines);
        let column = u16::try_from(indent.chars().count()).unwrap_or(u16::MAX);
        let room = u16::try_from(width)
            .unwrap_or(u16::MAX)
            .saturating_sub(column);
        for image in &row.images {
            let look = images.look(image, room);
            laid.lines.push(caption(image, &look, &indent, row.kind));
            if let Look::Picture(size) | Look::Fetching(size) = look {
                laid.pictures.push(Picture {
                    line: laid.lines.len(),
                    column,
                    size,
                    sha256: image.sha256.clone(),
                });
                let blank = Line::raw(indent.clone());
                laid.lines
                    .extend(std::iter::repeat_n(blank, usize::from(size.height)));
            }
        }
    }
    laid
}

/// An image's line: `[image WxH mime]`, and what is wrong if it cannot be drawn.
fn caption(image: &wire::Image, look: &Look, indent: &str, kind: Kind) -> Line<'static> {
    let mut spans = vec![
        Span::raw(indent.to_owned()),
        Span::styled(wire::caption(image), style(kind)),
    ];
    match look {
        Look::Fetching(_) => {
            spans.push(Span::styled(" fetching", Style::new().fg(Color::DarkGray)))
        }
        Look::Failed(why) => spans.push(Span::styled(
            format!(" cannot show: {why}"),
            style(Kind::Failure),
        )),
        Look::Picture(_) | Look::Line => {}
    }
    Line::from(spans)
}

/// Draw `laid` in `area`, scrolled up by `scroll` lines (never past its top), and, given
/// `images`, the pictures whose rows show, cut where the area cuts them. A picture whose line or rows show and that is
/// not held is noted in `images` to be fetched: images are fetched as they come into view.
/// Returns how far it can be scrolled at this size.
pub fn render(
    f: &mut Canvas,
    area: Rect,
    laid: &Laid,
    scroll: usize,
    images: Option<&mut Images>,
) -> usize {
    let height = usize::from(area.height);
    let most = laid.lines.len().saturating_sub(height);
    let end = laid.lines.len() - scroll.min(most);
    let start = end.saturating_sub(height);
    f.render_widget(Paragraph::new(laid.lines[start..end].to_vec()), area);
    let Some(images) = images else {
        return most;
    };
    for picture in &laid.pictures {
        let bottom = picture.line + usize::from(picture.size.height);
        // From its `[image ...]` line, the one above its rows.
        if bottom <= start || picture.line > end {
            continue;
        }
        images.want(&picture.sha256);
        // Within the area's height (a u16) of its top, or the picture would not show.
        let y = i16::try_from(
            isize::try_from(picture.line).unwrap_or(isize::MAX)
                - isize::try_from(start).unwrap_or(0),
        )
        .unwrap_or(i16::MIN);
        let x = i16::try_from(picture.column).unwrap_or(i16::MAX);
        images.draw(
            &picture.sha256,
            picture.size,
            area,
            SignedPosition { x, y },
            f.buffer_mut(),
        );
    }
    most
}
