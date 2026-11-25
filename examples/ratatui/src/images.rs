//! The pictures of the images in the transcript (D31): which graphics the terminal draws, the
//! images fetched with the call `image` and decoded, at most `KEPT` of them, and each drawn as a
//! thumbnail at most `ROWS` rows tall.
//!
//! Drawing is pure but for what it learns: which images it wanted and did not have (fetched after
//! the draw) and where it put each picture (a terminal that draws pixels over the cells, sixel or
//! iTerm2, is repainted whole when that changes, so no picture is left behind).

use std::collections::{HashMap, VecDeque};

use base64::{Engine, engine::general_purpose::STANDARD};
use image::DynamicImage;
use ratatui::{
    buffer::Buffer,
    layout::{Rect, Size},
    widgets::Widget,
};
use ratatui_image::{
    FilterType, Resize,
    picker::{Picker, ProtocolType},
    sliced::{SignedPosition, SlicedImage, SlicedProtocol},
};

use crate::wire;

/// The tallest a picture is, in rows: a thumbnail, so that a screenshot does not take the screen.
pub const ROWS: u16 = 8;
/// The widest a picture is, in columns, however wide the terminal.
pub const COLUMNS: u16 = 60;
/// How many decoded images are kept; the one used longest ago goes first.
pub const KEPT: usize = 32;

/// How the person asked for pictures (`--images`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, clap::ValueEnum)]
pub enum Choice {
    /// Ask the terminal which graphics it draws (kitty, sixel, iTerm2), else halfblocks.
    Auto,
    Kitty,
    Sixel,
    Iterm2,
    /// Coloured half blocks: any terminal, no query.
    Halfblocks,
    /// No pictures: one `[image WxH mime]` line per image, nothing fetched.
    Off,
}

/// What is known of one image.
enum Held {
    /// Asked for, not answered yet.
    Fetching,
    /// The image, shrunk to at most a thumbnail's pixels, and its picture at one size.
    Ready(DynamicImage, Option<(Size, SlicedProtocol)>),
    /// Why it cannot be shown: the fetch or the decode failed.
    Failed(String),
}

/// What the transcript says of an image besides its `[image WxH mime]` line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Look {
    /// Its picture is drawn under the line, in this many rows by columns.
    Picture(Size),
    /// Not here yet: the rows are kept for it.
    Fetching(Size),
    /// It cannot be drawn, and why.
    Failed(String),
    /// Pictures are off.
    Line,
}

/// One picture placed on the screen in a draw: the image, and the rectangle and first row of it
/// that showed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Placed {
    pub sha256: String,
    pub at: Rect,
    pub skip: u16,
}

pub struct Images {
    picker: Option<Picker>,
    held: HashMap<String, Held>,
    /// The `Ready` and `Failed` images, used longest ago first.
    used: VecDeque<String>,
    /// Images a draw wanted that are not held: fetched next.
    wanted: Vec<String>,
    /// Where the last draw put pictures, and where this one does.
    placed: (Vec<Placed>, Vec<Placed>),
    /// A picture could not be made in this draw: the next one shows why.
    failed_in_draw: bool,
}

/// What a draw leaves to do.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Redraw {
    No,
    /// Draw again: what a picture showed changed during the draw.
    Again,
    /// Clear the terminal and draw everything: pictures that are pixels over the cells moved.
    Whole,
}

impl Images {
    pub fn new(picker: Option<Picker>) -> Self {
        Self {
            picker,
            held: HashMap::new(),
            used: VecDeque::new(),
            wanted: Vec::new(),
            placed: (Vec::new(), Vec::new()),
            failed_in_draw: false,
        }
    }

    /// The picker for `choice`. `Auto` and the protocols named ask the terminal (for the size of
    /// its cells, and with `Auto` its graphics): call this with the screen taken and before
    /// anything reads the keyboard. A terminal that answers nothing gets halfblocks.
    pub fn pick(choice: Choice) -> Result<Option<Picker>, String> {
        let forced = match choice {
            Choice::Off => return Ok(None),
            Choice::Halfblocks => return Ok(Some(Picker::halfblocks())),
            Choice::Auto => None,
            Choice::Kitty => Some(ProtocolType::Kitty),
            Choice::Sixel => Some(ProtocolType::Sixel),
            Choice::Iterm2 => Some(ProtocolType::Iterm2),
        };
        let mut picker = Picker::from_query_stdio().map_err(|e| {
            format!("cannot ask the terminal which graphics it draws: {e} (--images halfblocks does not ask)")
        })?;
        if let Some(protocol) = forced {
            picker.set_protocol_type(protocol);
        }
        Ok(Some(picker))
    }

    /// The graphics the pictures are drawn with; `None` when pictures are off.
    pub fn protocol(&self) -> Option<ProtocolType> {
        self.picker.as_ref().map(Picker::protocol_type)
    }

    /// The rows by columns `image`'s picture takes when at most `columns` wide: its size in cells
    /// at the terminal's font, shrunk (never grown) to fit `columns` by `ROWS`. `None` when
    /// pictures are off, or for an image with no pixels.
    pub fn size_of(&self, image: &wire::Image, columns: u16) -> Option<Size> {
        let font = self.picker.as_ref()?.font_size();
        fit(
            (image.width, image.height),
            (font.width.max(1), font.height.max(1)),
            columns.min(COLUMNS),
        )
    }

    /// What the transcript shows of `image` when it may be `columns` wide.
    pub fn look(&self, image: &wire::Image, columns: u16) -> Look {
        let Some(size) = self.size_of(image, columns) else {
            return match self.picker {
                None => Look::Line,
                Some(_) => Look::Failed("the image has no pixels".to_owned()),
            };
        };
        match self.held.get(&image.sha256) {
            None | Some(Held::Fetching) => Look::Fetching(size),
            Some(Held::Failed(why)) => Look::Failed(why.clone()),
            Some(Held::Ready(..)) => Look::Picture(size),
        }
    }

    /// Note `sha256` to be fetched, if it is not held or being fetched.
    pub fn want(&mut self, sha256: &str) {
        if self.picker.is_some()
            && !self.held.contains_key(sha256)
            && !self.wanted.iter().any(|s| s == sha256)
        {
            self.wanted.push(sha256.to_owned());
        }
    }

    /// The images to fetch now; each is marked as being fetched.
    pub fn take_wanted(&mut self) -> Vec<String> {
        let wanted = std::mem::take(&mut self.wanted);
        for sha256 in &wanted {
            self.held.insert(sha256.clone(), Held::Fetching);
        }
        wanted
    }

    /// The image `sha256` arrived (decoded), or failed (why). The one used longest ago goes if
    /// more than `KEPT` are held.
    pub fn arrived(&mut self, sha256: String, got: Result<DynamicImage, String>) {
        let held = match got {
            Ok(image) => Held::Ready(image, None),
            Err(why) => Held::Failed(why),
        };
        self.used.retain(|s| *s != sha256);
        self.used.push_back(sha256.clone());
        self.held.insert(sha256, held);
        while self.used.len() > KEPT {
            if let Some(old) = self.used.pop_front() {
                self.held.remove(&old);
            }
        }
    }

    /// A new connection: what was being fetched on the old one will not arrive, and what failed
    /// may not fail again.
    pub fn reconnected(&mut self) {
        self.held.retain(|_, held| matches!(held, Held::Ready(..)));
        self.used.retain(|s| self.held.contains_key(s));
        self.wanted.clear();
    }

    /// Draw `sha256`'s picture, `size` in cells, with its top-left at `position` relative to
    /// `area` (above the area when negative: only the rows inside it are drawn). The picture is
    /// made for `size` once, and again only when the size changes (a resize). A picture that
    /// cannot be made fails its image, and the next draw says why on its line.
    pub fn draw(
        &mut self,
        sha256: &str,
        size: Size,
        area: Rect,
        position: SignedPosition,
        buf: &mut Buffer,
    ) {
        let Some(picker) = &self.picker else {
            return;
        };
        let Some(held) = self.held.get_mut(sha256) else {
            return;
        };
        let Held::Ready(image, made) = held else {
            return;
        };
        if made.as_ref().is_none_or(|(at, _)| *at != size) {
            match SlicedProtocol::new_with_resize(
                picker,
                image.clone(),
                size,
                Resize::Fit(Some(FilterType::Triangle)),
            ) {
                Ok(protocol) => *made = Some((size, protocol)),
                Err(e) => {
                    *held = Held::Failed(format!("cannot draw it: {e}"));
                    self.failed_in_draw = true;
                    return;
                }
            }
        }
        let Some((_, protocol)) = made else {
            return;
        };
        SlicedImage::new(protocol, position).render(area, buf);
        let top = i32::from(position.y);
        let shown_top = top.max(0);
        let skip = u16::try_from(shown_top - top).unwrap_or(u16::MAX);
        let x = area.x + u16::try_from(position.x.max(0)).unwrap_or(0);
        let y = area.y + u16::try_from(shown_top).unwrap_or(0);
        let bottom = (top + i32::from(size.height)).min(i32::from(area.height));
        let height = u16::try_from(bottom - shown_top).unwrap_or(0);
        self.placed.1.push(Placed {
            sha256: sha256.to_owned(),
            at: Rect::new(x, y, size.width, height),
            skip,
        });
        self.touch(sha256);
    }

    fn touch(&mut self, sha256: &str) {
        if self.used.back().is_some_and(|s| s == sha256) {
            return;
        }
        self.used.retain(|s| s != sha256);
        self.used.push_back(sha256.to_owned());
    }

    /// A draw is over: what is left to do. `Whole` when pictures moved, came or went on a
    /// terminal whose pictures are pixels over the cells (sixel, iTerm2), which writing the cells
    /// again may not clear: the screen is repainted whole. Kitty's pictures are characters and
    /// halfblocks are text, so for them the cells written are enough. `Again` when a picture
    /// failed while drawing, to show why.
    pub fn drawn(&mut self) -> Redraw {
        let (last, now) = &mut self.placed;
        let moved = last != now;
        *last = std::mem::take(now);
        let pixels = matches!(
            self.protocol(),
            Some(ProtocolType::Sixel | ProtocolType::Iterm2)
        );
        if moved && pixels {
            Redraw::Whole
        } else if std::mem::take(&mut self.failed_in_draw) {
            Redraw::Again
        } else {
            Redraw::No
        }
    }

    /// The most pixels a picture uses, for shrinking an image as it is decoded: `COLUMNS` by
    /// `ROWS` cells at the terminal's font.
    pub fn most_pixels(&self) -> (u32, u32) {
        let font = self
            .picker
            .as_ref()
            .map_or((10, 20), |p| (p.font_size().width, p.font_size().height));
        (
            u32::from(COLUMNS) * u32::from(font.0.max(1)),
            u32::from(ROWS) * u32::from(font.1.max(1)),
        )
    }
}

/// An image of `pixels` (width, height) on cells of `font` pixels, in cells, shrunk to fit
/// `columns` by `ROWS` and never grown; at least one cell each way. `None` for no pixels.
pub fn fit(pixels: (u32, u32), font: (u16, u16), columns: u16) -> Option<Size> {
    let (w, h) = pixels;
    if w == 0 || h == 0 || columns == 0 {
        return None;
    }
    let (fw, fh) = (f64::from(font.0), f64::from(font.1));
    let natural = (f64::from(w) / fw, f64::from(h) / fh);
    let scale = (f64::from(columns) / natural.0)
        .min(f64::from(ROWS) / natural.1)
        .min(1.0);
    let cells = |n: f64, most: u16| {
        // Bounded by `most` (a u16) before the cast, so it cannot truncate.
        #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
        let n = (n * scale).ceil().clamp(1.0, f64::from(most)) as u16;
        n
    };
    Some(Size::new(cells(natural.0, columns), cells(natural.1, ROWS)))
}

/// The bytes `image` answered (base64), decoded and shrunk to at most `most` pixels. `Err` says
/// what was wrong with them.
pub fn decode(base64: &str, most: (u32, u32)) -> Result<DynamicImage, String> {
    let bytes = STANDARD
        .decode(base64)
        .map_err(|e| format!("the server's bytes are not base64: {e}"))?;
    let image = image::load_from_memory(&bytes).map_err(|e| format!("cannot decode it: {e}"))?;
    Ok(if image.width() > most.0 || image.height() > most.1 {
        image.resize(most.0, most.1, FilterType::Triangle)
    } else {
        image
    })
}

#[cfg(test)]
mod tests;
