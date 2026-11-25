use std::io::Cursor;

use image::{ImageFormat, Rgb, RgbImage};
use ratatui::{Terminal, backend::TestBackend}; // rules: allow(backend): ratatui API

use super::*;
use crate::{
    model::{Kind, Row},
    transcript,
};

/// A PNG of `width` by `height` in stripes ten pixels tall, red then white: each cell's upper
/// and lower halves differ, so halfblocks draws it with `▀` or `▄`.
fn striped(width: u32, height: u32) -> Vec<u8> {
    let image = RgbImage::from_fn(width, height, |_, y| {
        if (y / 10) % 2 == 0 {
            Rgb([220, 30, 30])
        } else {
            Rgb([255, 255, 255])
        }
    });
    let mut bytes = Cursor::new(Vec::new());
    DynamicImage::ImageRgb8(image)
        .write_to(&mut bytes, ImageFormat::Png)
        .unwrap();
    bytes.into_inner()
}

fn wired(sha256: &str, width: u32, height: u32) -> wire::Image {
    serde_json::from_value(serde_json::json!({
        "mime": "image/png", "sha256": sha256, "width": width, "height": height,
    }))
    .unwrap()
}

fn halfblocks() -> Images {
    Images::new(Some(Picker::halfblocks()))
}

/// `images` holding `sha256`, a striped picture of `width` by `height`.
fn holding(images: &mut Images, sha256: &str, (width, height): (u32, u32)) {
    let bytes = STANDARD.encode(striped(width, height));
    let most = images.most_pixels();
    images.arrived(sha256.into(), decode(&bytes, most));
}

#[test]
fn a_screenshot_is_a_thumbnail_and_a_small_image_is_not_grown() {
    // 10 by 20 pixel cells: 1920x1080 is 192x54 cells naturally; ROWS rows tall at most.
    let shot = fit((1920, 1080), (10, 20), 200).unwrap();
    assert_eq!(shot.height, ROWS);
    assert_eq!(shot.width, 29);
    assert_eq!(fit((40, 20), (10, 20), 200), Some(Size::new(4, 1)));
    // A narrow terminal makes it narrower still, never wider than there is room.
    assert!(fit((1920, 1080), (10, 20), 10).unwrap().width <= 10);
    assert_eq!(fit((0, 10), (10, 20), 80), None);
    // However wide the terminal, at most COLUMNS.
    let wide = halfblocks().size_of(&wired("a", 4000, 200), 500).unwrap();
    assert!(wide.width <= COLUMNS, "{wide:?}");
}

#[test]
fn bytes_decode_shrunk_and_bad_bytes_say_what_is_wrong() {
    let png = STANDARD.encode(striped(400, 300));
    let image = decode(&png, (100, 100)).unwrap();
    assert!(image.width() <= 100 && image.height() <= 100);
    let small = decode(&STANDARD.encode(striped(20, 20)), (100, 100)).unwrap();
    assert_eq!((small.width(), small.height()), (20, 20));
    let not_base64 = decode("not base64!", (100, 100)).unwrap_err();
    assert!(not_base64.contains("not base64"), "{not_base64}");
    let not_an_image = decode(&STANDARD.encode(b"hello"), (100, 100)).unwrap_err();
    assert!(not_an_image.contains("cannot decode"), "{not_an_image}");
}

#[test]
fn an_image_is_fetched_once_and_a_failure_is_kept_until_a_reconnect() {
    let mut images = halfblocks();
    let image = wired("a", 100, 40);
    assert!(matches!(images.look(&image, 80), Look::Fetching(_)));
    images.want("a");
    images.want("a");
    assert_eq!(images.take_wanted(), vec!["a".to_owned()]);
    // Being fetched: not wanted again.
    images.want("a");
    assert!(images.take_wanted().is_empty());
    images.arrived("a".into(), Err("cannot fetch it: NotFound".into()));
    assert_eq!(
        images.look(&image, 80),
        Look::Failed("cannot fetch it: NotFound".into())
    );
    images.want("a");
    assert!(
        images.take_wanted().is_empty(),
        "a failure is not fetched again"
    );
    images.reconnected();
    assert!(matches!(images.look(&image, 80), Look::Fetching(_)));
    images.want("a");
    assert_eq!(images.take_wanted(), vec!["a".to_owned()]);
}

#[test]
fn only_images_in_view_are_fetched() {
    let mut images = halfblocks();
    let image = wired("a", 200, 100);
    // Ten lines of text under the image, on five rows: it is out of view.
    let mut rows = rows(0, &image);
    rows.push(row("t", &"x\n".repeat(9), Kind::Other, vec![]));
    drawn(&rows, &mut images, (60, 5), 0);
    assert!(images.take_wanted().is_empty());
    drawn(&rows, &mut images, (60, 5), 100);
    assert_eq!(images.take_wanted(), vec!["a".to_owned()]);
}

#[test]
fn at_most_kept_images_are_held_and_the_one_used_longest_ago_goes() {
    let mut images = halfblocks();
    for i in 0..=KEPT {
        holding(&mut images, &format!("{i}"), (20, 20));
    }
    assert!(matches!(
        images.look(&wired("0", 20, 20), 80),
        Look::Fetching(_)
    ));
    assert!(matches!(
        images.look(&wired("1", 20, 20), 80),
        Look::Picture(_)
    ));
    assert!(matches!(
        images.look(&wired(&format!("{KEPT}"), 20, 20), 80),
        Look::Picture(_)
    ));
}

#[test]
fn with_pictures_off_an_image_is_its_line_and_nothing_is_fetched() {
    let mut images = Images::new(None);
    assert_eq!(images.look(&wired("a", 100, 40), 80), Look::Line);
    images.want("a");
    assert!(images.take_wanted().is_empty());
}

fn row(label: &str, body: &str, kind: Kind, images: Vec<wire::Image>) -> Row {
    Row {
        label: label.into(),
        body: body.into(),
        images,
        kind,
        group: None,
        folded: false,
    }
}

/// `before` lines of text, then a user's row with `image`, then a reply.
fn rows(before: usize, image: &wire::Image) -> Vec<Row> {
    let text = (0..before)
        .map(|i| format!("l{i:02}"))
        .collect::<Vec<_>>()
        .join("\n");
    vec![
        row("t", &text, Kind::Other, vec![]),
        row("you", "look", Kind::Mine, vec![image.clone()]),
        row("assistant", "seen", Kind::Assistant, vec![]),
    ]
}

/// The transcript drawn on `width` by `height`, scrolled up by `scroll`: its lines.
fn drawn(
    rows: &[Row],
    images: &mut Images,
    (width, height): (u16, u16),
    scroll: usize,
) -> Vec<String> {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap(); // rules: allow(backend): ratatui API
    terminal
        .draw(|f| {
            let laid = transcript::lay_out(rows, usize::from(width), &*images);
            transcript::render(f, f.area(), &laid, scroll, Some(images));
        })
        .unwrap();
    let buffer = terminal.backend().buffer(); // rules: allow(backend): ratatui API
    (0..height)
        .map(|y| (0..width).map(|x| buffer[(x, y)].symbol()).collect())
        .collect()
}

fn is_picture(line: &str) -> bool {
    line.contains('▀') || line.contains('▄')
}

#[test]
fn a_users_image_is_its_line_then_its_picture_under_the_text() {
    let mut images = halfblocks();
    let image = wired("a", 200, 100); // 20 by 5 cells
    let rows = rows(0, &image);
    // Not here yet: the line says so and the rows are kept, so nothing moves when it comes.
    let waiting = drawn(&rows, &mut images, (60, 12), 0);
    assert!(
        waiting[2].contains("[image 200x100 image/png] fetching"),
        "{waiting:#?}"
    );
    assert!(waiting[8].starts_with("assistant: seen"), "{waiting:#?}");
    assert_eq!(images.take_wanted(), vec!["a".to_owned()]);
    holding(&mut images, "a", (200, 100));
    let shown = drawn(&rows, &mut images, (60, 12), 0);
    assert!(shown[1].starts_with("you: look"), "{shown:#?}");
    assert_eq!(shown[2].trim_end(), "     [image 200x100 image/png]");
    for line in &shown[3..8] {
        assert!(is_picture(line), "{shown:#?}");
        assert!(line.starts_with("     "), "under the text: {shown:#?}");
    }
    assert!(shown[8].starts_with("assistant: seen"), "{shown:#?}");
    assert_eq!(images.drawn(), Redraw::No, "halfblocks are text");
}

#[test]
fn a_picture_scrolled_half_out_of_view_shows_only_its_rows_inside() {
    let mut images = halfblocks();
    let image = wired("a", 200, 100);
    let shown = rows(0, &image);
    holding(&mut images, "a", (200, 100));
    // 9 lines (an empty text row, you, caption, 5 picture rows, the reply) on 5 rows: the
    // bottom 5 show, the picture's last 4 rows and the reply.
    let bottom = drawn(&shown, &mut images, (60, 5), 0);
    assert_eq!(
        bottom.iter().filter(|l| is_picture(l)).count(),
        4,
        "{bottom:#?}"
    );
    assert!(bottom[4].starts_with("assistant: seen"), "{bottom:#?}");
    // Scrolled up by 2: the caption on top, then the picture's first 4 rows; no reply.
    let up = drawn(&shown, &mut images, (60, 5), 2);
    assert!(up[0].contains("[image 200x100"), "{up:#?}");
    assert_eq!(up.iter().filter(|l| is_picture(l)).count(), 4, "{up:#?}");
    // Scrolled to the top of a long text above it: no picture at all.
    let top = drawn(&rows(6, &image), &mut images, (60, 5), 100);
    assert!(top.iter().all(|l| !is_picture(l)), "{top:#?}");
}

#[test]
fn a_failure_is_shown_on_the_images_line() {
    let mut images = halfblocks();
    let image = wired("a", 200, 100);
    images.arrived(
        "a".into(),
        Err("cannot fetch it: NotFound: no image a".into()),
    );
    let shown = drawn(&rows(0, &image), &mut images, (90, 6), 0);
    let screen = shown.join("\n");
    assert!(
        shown[2].contains("[image 200x100 image/png] cannot show: cannot fetch it: NotFound"),
        "{screen}"
    );
    assert!(
        shown[3].starts_with("assistant: seen"),
        "no rows kept: {screen}"
    );
}

#[test]
fn pixels_over_the_cells_repaint_the_screen_whole_when_a_picture_moves() {
    let mut picker = Picker::halfblocks();
    picker.set_protocol_type(ProtocolType::Sixel);
    let mut images = Images::new(Some(picker));
    let image = wired("a", 200, 100);
    let rows = rows(4, &image);
    holding(&mut images, "a", (200, 100));
    // 12 lines on 8 rows: the picture shows at either scroll.
    drawn(&rows, &mut images, (60, 8), 0);
    assert_eq!(images.drawn(), Redraw::Whole, "a picture came");
    drawn(&rows, &mut images, (60, 8), 0);
    assert_eq!(images.drawn(), Redraw::No, "nothing moved");
    drawn(&rows, &mut images, (60, 8), 1);
    assert_eq!(images.drawn(), Redraw::Whole, "scrolled: it moved");
    drawn(&rows, &mut images, (60, 8), 1);
    assert_eq!(images.drawn(), Redraw::No);
}
