//! An image in a tool result (D30): what it is (`mime`, `sha256`, `width`, `height`), and the
//! bytes when this process has them. The bytes are never serialized and never compared: the log
//! and the wire carry the reference, and the store keeps the bytes beside the log.

use std::fmt;
use std::hash::{Hash, Hasher};
use std::sync::Arc;

use serde::{Deserialize, Serialize};

/// One image in a tool result (D30): its type, its identity and its size. The bytes are not in
/// this encoding: the session keeps them beside its log, named by `sha256`.
#[derive(Clone, Serialize, Deserialize)]
#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
pub struct Image {
    /// `image/png`, `image/jpeg`, `image/gif` or `image/webp`.
    pub mime: String,
    /// Lowercase hex of the SHA-256 of the bytes: the image's identity.
    pub sha256: String,
    /// Width in pixels.
    pub width: u32,
    /// Height in pixels.
    pub height: u32,
    /// The bytes this process holds, or `None` (a node loaded from a log). Never encoded, and
    /// not part of the value: two images are equal when the other four fields are.
    #[serde(skip)]
    #[cfg_attr(feature = "schema", schemars(skip))]
    pub data: Option<Arc<[u8]>>,
}

/// Bytes that are not a PNG, JPEG, GIF or WebP image whose size can be read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BadImage(pub String);

impl fmt::Display for BadImage {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for BadImage {}

impl Image {
    /// The image these bytes are: format and size from the header, identity from the hash.
    ///
    /// # Errors
    /// `BadImage` for bytes that are none of the four formats, a header too short to give the
    /// size, or a size of zero.
    pub fn from_bytes(data: impl Into<Arc<[u8]>>) -> Result<Self, BadImage> {
        let data: Arc<[u8]> = data.into();
        let (mime, width, height) = sniff(&data)?;
        Ok(Self {
            mime: mime.into(),
            sha256: sha256_hex(&data),
            width,
            height,
            data: Some(data),
        })
    }

    /// This image without its bytes: the reference the log holds.
    #[must_use]
    pub fn reference(&self) -> Self {
        Self {
            data: None,
            ..self.clone()
        }
    }

    /// The longer side, in pixels.
    #[must_use]
    pub fn longest_side(&self) -> u32 {
        self.width.max(self.height)
    }
}

impl PartialEq for Image {
    fn eq(&self, other: &Self) -> bool {
        self.mime == other.mime
            && self.sha256 == other.sha256
            && self.width == other.width
            && self.height == other.height
    }
}

impl Eq for Image {}

impl Hash for Image {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.mime.hash(state);
        self.sha256.hash(state);
        self.width.hash(state);
        self.height.hash(state);
    }
}

impl fmt::Debug for Image {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Image")
            .field("mime", &self.mime)
            .field("sha256", &self.sha256)
            .field("width", &self.width)
            .field("height", &self.height)
            .field("data", &self.data.as_ref().map(|d| d.len()))
            .finish()
    }
}

/// Lowercase hex of the SHA-256 of `data`.
#[must_use]
pub fn sha256_hex(data: &[u8]) -> String {
    use std::fmt::Write as _;
    let digest = super::sha256::digest(data);
    let mut out = String::with_capacity(64);
    for b in digest {
        let _ = write!(out, "{b:02x}");
    }
    out
}

/// Whether `data` starts as a PNG, JPEG, GIF or WebP file does (only the first bytes are read).
#[must_use]
pub fn is_image(data: &[u8]) -> bool {
    format(data).is_some()
}

fn format(data: &[u8]) -> Option<&'static str> {
    if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some("image/png")
    } else if data.starts_with(&[0xFF, 0xD8, 0xFF]) {
        Some("image/jpeg")
    } else if data.starts_with(b"GIF87a") || data.starts_with(b"GIF89a") {
        Some("image/gif")
    } else if data.len() >= 12 && &data[..4] == b"RIFF" && &data[8..12] == b"WEBP" {
        Some("image/webp")
    } else {
        None
    }
}

/// The MIME type, width and height read from an image's header.
///
/// # Errors
/// `BadImage` as for `Image::from_bytes`.
pub fn sniff(data: &[u8]) -> Result<(&'static str, u32, u32), BadImage> {
    let mime = format(data).ok_or_else(|| {
        BadImage("not a PNG, JPEG, GIF or WebP image (by its first bytes)".into())
    })?;
    let size = match mime {
        "image/png" => png(data),
        "image/jpeg" => jpeg(data),
        "image/gif" => gif(data),
        _ => webp(data),
    };
    match size {
        Some((w, h)) if w > 0 && h > 0 => Ok((mime, w, h)),
        Some((w, h)) => Err(BadImage(format!("an {mime} image of {w}x{h} pixels"))),
        None => Err(BadImage(format!(
            "an {mime} header that does not give the image's size (cut short or damaged)"
        ))),
    }
}

fn be16(d: &[u8], at: usize) -> Option<u32> {
    Some(u32::from(u16::from_be_bytes(
        d.get(at..at + 2)?.try_into().ok()?,
    )))
}

fn le16(d: &[u8], at: usize) -> Option<u32> {
    Some(u32::from(u16::from_le_bytes(
        d.get(at..at + 2)?.try_into().ok()?,
    )))
}

fn le24(d: &[u8], at: usize) -> Option<u32> {
    let b = d.get(at..at + 3)?;
    Some(u32::from(b[0]) | u32::from(b[1]) << 8 | u32::from(b[2]) << 16)
}

fn png(d: &[u8]) -> Option<(u32, u32)> {
    if d.get(12..16)? != b"IHDR" {
        return None;
    }
    let w = u32::from_be_bytes(d.get(16..20)?.try_into().ok()?);
    let h = u32::from_be_bytes(d.get(20..24)?.try_into().ok()?);
    Some((w, h))
}

fn gif(d: &[u8]) -> Option<(u32, u32)> {
    Some((le16(d, 6)?, le16(d, 8)?))
}

fn webp(d: &[u8]) -> Option<(u32, u32)> {
    match d.get(12..16)? {
        b"VP8 " => webp_lossy(d),
        b"VP8L" => webp_lossless(d),
        b"VP8X" => Some((1 + le24(d, 24)?, 1 + le24(d, 27)?)),
        _ => None,
    }
}

fn webp_lossy(d: &[u8]) -> Option<(u32, u32)> {
    if d.get(23..26)? != [0x9d, 0x01, 0x2a] {
        return None;
    }
    Some((le16(d, 26)? & 0x3FFF, le16(d, 28)? & 0x3FFF))
}

fn webp_lossless(d: &[u8]) -> Option<(u32, u32)> {
    if *d.get(20)? != 0x2f {
        return None;
    }
    let bits = u32::from_le_bytes(d.get(21..25)?.try_into().ok()?);
    Some((1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)))
}

/// The size from a JPEG's first SOF marker (`0xC0` to `0xCF`, but not `C4`, `C8` or `CC`).
fn jpeg(d: &[u8]) -> Option<(u32, u32)> {
    let mut i = 2;
    loop {
        if *d.get(i)? != 0xFF {
            return None;
        }
        while *d.get(i + 1)? == 0xFF {
            i += 1; // fill bytes
        }
        let marker = *d.get(i + 1)?;
        if matches!(marker, 0x01 | 0xD0..=0xD9) {
            i += 2; // a marker without a length
            continue;
        }
        let len = usize::try_from(be16(d, i + 2)?).ok()?;
        let size_marker = matches!(marker, 0xC0..=0xCF) && !matches!(marker, 0xC4 | 0xC8 | 0xCC);
        if size_marker {
            return Some((be16(d, i + 7)?, be16(d, i + 5)?));
        }
        i += 2 + len;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn png_header(w: u32, h: u32) -> Vec<u8> {
        let mut d = b"\x89PNG\r\n\x1a\n\0\0\0\x0dIHDR".to_vec();
        d.extend_from_slice(&w.to_be_bytes());
        d.extend_from_slice(&h.to_be_bytes());
        d.extend_from_slice(&[8, 2, 0, 0, 0]);
        d
    }

    #[test]
    fn png_size_and_identity() {
        let img = Image::from_bytes(png_header(640, 480)).unwrap();
        assert_eq!(
            (img.mime.as_str(), img.width, img.height),
            ("image/png", 640, 480)
        );
        assert_eq!(img.sha256.len(), 64);
        assert_eq!(img.reference(), img, "the bytes are not part of the value");
        assert!(img.reference().data.is_none());
    }

    #[test]
    fn gif_and_webp_sizes() {
        let mut gif = b"GIF89a".to_vec();
        gif.extend_from_slice(&[0x20, 0x01, 0x10, 0x00]);
        assert_eq!(sniff(&gif).unwrap(), ("image/gif", 288, 16));

        let mut vp8x = b"RIFF\0\0\0\0WEBPVP8X\x0a\0\0\0\0\0\0\0".to_vec();
        vp8x.extend_from_slice(&[99, 0, 0, 49, 0, 0]);
        assert_eq!(sniff(&vp8x).unwrap(), ("image/webp", 100, 50));

        let mut lossless = b"RIFF\0\0\0\0WEBPVP8L\0\0\0\0\x2f".to_vec();
        let bits: u32 = (10 - 1) | ((20 - 1) << 14);
        lossless.extend_from_slice(&bits.to_le_bytes());
        assert_eq!(sniff(&lossless).unwrap(), ("image/webp", 10, 20));

        let mut vp8 = b"RIFF\0\0\0\0WEBPVP8 \0\0\0\0\0\0\0\x9d\x01\x2a".to_vec();
        vp8.extend_from_slice(&[64, 0, 32, 0]);
        assert_eq!(sniff(&vp8).unwrap(), ("image/webp", 64, 32));
    }

    #[test]
    fn jpeg_size_from_its_sof_marker_after_other_segments() {
        let mut d = vec![0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x04, 0x00, 0x00];
        d.extend_from_slice(&[0xFF, 0xC2, 0x00, 0x11, 8, 0x01, 0x2C, 0x02, 0x58, 3]);
        assert_eq!(sniff(&d).unwrap(), ("image/jpeg", 600, 300));
    }

    #[test]
    fn what_is_not_an_image_is_refused_by_name() {
        assert!(sniff(b"hello").unwrap_err().0.contains("not a PNG"));
        assert!(
            sniff(b"\x89PNG\r\n\x1a\n")
                .unwrap_err()
                .0
                .contains("cut short")
        );
        assert!(
            sniff(&png_header(0, 5))
                .unwrap_err()
                .0
                .contains("0x5 pixels")
        );
        assert!(!is_image(b"GIF8"));
    }

    #[test]
    fn ok_without_images_encodes_as_before_and_images_travel_as_references() {
        use crate::tree::Outcome;
        let plain = Outcome::ok("text");
        assert_eq!(serde_json::to_string(&plain).unwrap(), r#"{"Ok":"text"}"#);
        let v1: Outcome = serde_json::from_str(r#"{"Ok":"text"}"#).unwrap();
        assert_eq!(v1, plain);

        let img = Image::from_bytes(png_header(2, 3)).unwrap();
        let with = Outcome::Ok {
            value: "seen".into(),
            images: vec![img.clone()],
        };
        let text = serde_json::to_string(&with).unwrap();
        assert_eq!(
            text,
            format!(
                r#"{{"Ok":{{"value":"seen","images":[{{"mime":"image/png","sha256":"{}","width":2,"height":3}}]}}}}"#,
                img.sha256
            ),
            "the bytes are never encoded"
        );
        let back: Outcome = serde_json::from_str(&text).unwrap();
        assert_eq!(back, with);
        let Outcome::Ok { images, .. } = back else {
            panic!()
        };
        assert!(images[0].data.is_none());
    }
}
