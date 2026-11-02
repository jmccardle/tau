//! A unified diff of a search and replace, from the replaced spans themselves.
//!
//! `edit` knows where each replacement was made in the old text and where its result
//! is in the new text, so the diff is built from those spans: no search for a longest
//! common subsequence. Each span grows to whole lines, spans that share a line are one
//! change, lines the two sides share at the edges of a change become context, and
//! changes whose context touches share a hunk, as `diff -U3` has it.

use std::fmt::Write as _;
use std::ops::Range;

/// Lines of context around a change.
const CONTEXT: usize = 3;

/// One replacement: the bytes it replaced in the old text and the bytes that took their place
/// in the new text.
pub(crate) struct Change {
    pub(crate) old: Range<usize>,
    pub(crate) new: Range<usize>,
}

/// The lines that changed in one place: `removed` from the old text at line index `at`, and
/// `added` in their place.
struct Chunk<'a> {
    at: usize,
    removed: Vec<&'a str>,
    added: Vec<&'a str>,
}

impl Chunk<'_> {
    fn end(&self) -> usize {
        self.at + self.removed.len()
    }
}

/// The diff of `old` into `new`, headed `--- name` and `+++ name`, without a final newline.
/// `changes` are in order, do not overlap, and are not empty on the old side.
pub(crate) fn unified(name: &str, old: &str, new: &str, changes: &[Change]) -> String {
    let lines: Vec<&str> = old.split_inclusive('\n').collect();
    let chunks = chunks(old, new, &lines, changes);
    let mut out = format!("--- {name}\n+++ {name}");
    let mut shift = 0_isize; // lines the new text is ahead of the old, before this hunk
    let mut rest = chunks.as_slice();
    while let Some(first) = rest.first() {
        let mut count = 1;
        while count < rest.len() && rest[count].at <= rest[count - 1].end() + 2 * CONTEXT {
            count += 1;
        }
        let (hunk, later) = rest.split_at(count);
        hunk_into(
            &mut out,
            &lines,
            hunk,
            first.at.saturating_sub(CONTEXT),
            shift,
        );
        shift += hunk
            .iter()
            .map(|c| c.added.len().cast_signed() - c.removed.len().cast_signed())
            .sum::<isize>();
        rest = later;
    }
    out
}

/// The changes as lines of the old text and of the new.
fn chunks<'a>(old: &'a str, new: &'a str, lines: &[&str], changes: &[Change]) -> Vec<Chunk<'a>> {
    let mut starts = Vec::with_capacity(lines.len());
    let mut at = 0;
    for line in lines {
        starts.push(at);
        at += line.len();
    }
    // Each change as the whole lines it touches, in both texts.
    let mut spans: Vec<(Range<usize>, Range<usize>)> = Vec::new();
    for change in changes {
        let first = line_start(old, change.old.start);
        // The text after the change is the same in both texts up to the next newline, so
        // one tail finishes the last line on both sides: unless each side already ends on
        // a line break.
        let tail = if old[..change.old.end].ends_with('\n') && new[..change.new.end].ends_with('\n')
        {
            0
        } else {
            old[change.old.end..]
                .find('\n')
                .map_or(old.len() - change.old.end, |i| i + 1)
        };
        let last = change.old.end + tail;
        let old_span = first..last;
        let new_span = change.new.start - (change.old.start - first)..change.new.end + tail;
        match spans.last_mut() {
            Some((before_old, before_new)) if first <= before_old.end => {
                before_old.end = last;
                before_new.end = new_span.end;
            }
            _ => spans.push((old_span, new_span)),
        }
    }
    spans
        .into_iter()
        .map(|(old_span, new_span)| {
            let at = starts.partition_point(|&s| s < old_span.start);
            let removed: Vec<&str> = old[old_span].split_inclusive('\n').collect();
            let added: Vec<&str> = new[new_span].split_inclusive('\n').collect();
            trimmed(at, removed, added)
        })
        .collect()
}

/// The chunk with the lines both sides start and end with left out.
fn trimmed<'a>(at: usize, mut removed: Vec<&'a str>, mut added: Vec<&'a str>) -> Chunk<'a> {
    let shared = removed
        .iter()
        .zip(&added)
        .take_while(|(a, b)| a == b)
        .count();
    removed.drain(..shared);
    added.drain(..shared);
    while removed.last().is_some_and(|r| added.last() == Some(r)) {
        removed.pop();
        added.pop();
    }
    Chunk {
        at: at + shared,
        removed,
        added,
    }
}

/// Where the line holding byte `at` starts.
fn line_start(text: &str, at: usize) -> usize {
    text[..at].rfind('\n').map_or(0, |i| i + 1)
}

/// One hunk: `chunks` (close enough to share their context) from line `from` of the old text.
fn hunk_into(out: &mut String, lines: &[&str], chunks: &[Chunk], from: usize, shift: isize) {
    let mut body = String::new();
    let (mut old_count, mut new_count) = (0, 0);
    let mut at = from;
    for chunk in chunks {
        put_context(&mut body, &lines[at..chunk.at]);
        for line in &chunk.removed {
            put(&mut body, '-', line);
        }
        for line in &chunk.added {
            put(&mut body, '+', line);
        }
        old_count += chunk.at - at + chunk.removed.len();
        new_count += chunk.at - at + chunk.added.len();
        at = chunk.end();
    }
    let after = &lines[at..(at + CONTEXT).min(lines.len())];
    put_context(&mut body, after);
    old_count += after.len();
    new_count += after.len();
    let new_from = (from.cast_signed() + shift).cast_unsigned();
    let _ = write!(
        out,
        "\n@@ -{} +{} @@{body}",
        range(from, old_count),
        range(new_from, new_count)
    );
}

fn put_context(body: &mut String, lines: &[&str]) {
    for line in lines {
        put(body, ' ', line);
    }
}

/// `start,count` of a hunk header: 1-based, the line before when empty, no count of 1.
fn range(from: usize, count: usize) -> String {
    match count {
        0 => format!("{from},0"),
        1 => format!("{}", from + 1),
        _ => format!("{},{count}", from + 1),
    }
}

/// One line of the diff after a newline, with the marker git puts after a last line that has
/// none.
fn put(body: &mut String, sign: char, line: &str) {
    let text = line.strip_suffix('\n').unwrap_or(line);
    let text = text.strip_suffix('\r').unwrap_or(text);
    let _ = write!(body, "\n{sign}{text}");
    if !line.ends_with('\n') {
        body.push_str("\n\\ No newline at end of file");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The diff of replacing `find` with `with` in `old` everywhere.
    fn replace(old: &str, find: &str, with: &str) -> String {
        let mut new = String::new();
        let mut changes = Vec::new();
        let mut at = 0;
        for (start, _) in old.match_indices(find) {
            new.push_str(&old[at..start]);
            let from = new.len();
            new.push_str(with);
            changes.push(Change {
                old: start..start + find.len(),
                new: from..new.len(),
            });
            at = start + find.len();
        }
        new.push_str(&old[at..]);
        unified("f", old, &new, &changes)
    }

    #[test]
    fn a_changed_line_has_its_neighbours_as_context() {
        assert_eq!(
            replace("a\nb\nc\nd\ne\nf\ng\nh\n", "e\n", "E\n"),
            "--- f\n+++ f\n@@ -2,7 +2,7 @@\n b\n c\n d\n-e\n+E\n f\n g\n h"
        );
    }

    #[test]
    fn a_deleted_line_and_an_added_one_move_the_new_side() {
        assert_eq!(
            replace("a\nb\nc\n", "b\n", ""),
            "--- f\n+++ f\n@@ -1,3 +1,2 @@\n a\n-b\n c"
        );
        assert_eq!(
            replace("a\nc\n", "a\n", "a\nb\n"),
            "--- f\n+++ f\n@@ -1,2 +1,3 @@\n a\n+b\n c"
        );
    }

    #[test]
    fn deleting_every_line_leaves_a_hunk_with_no_new_lines() {
        assert_eq!(replace("a\n", "a\n", ""), "--- f\n+++ f\n@@ -1 +0,0 @@\n-a");
    }

    #[test]
    fn hunks_split_when_the_context_between_them_would_not_touch() {
        let old = format!("x\n{}x\n", "-\n".repeat(7));
        let out = replace(&old, "x", "y");
        assert_eq!(out.matches("\n@@ ").count(), 2, "{out}");
        let old = format!("x\n{}x\n", "-\n".repeat(6));
        let out = replace(&old, "x", "y");
        assert_eq!(out.matches("\n@@ ").count(), 1, "{out}");
    }

    #[test]
    fn the_second_hunk_counts_the_lines_the_first_added() {
        let old = format!("x\n{}x\n", "-\n".repeat(20));
        let out = replace(&old, "x", "y\ny");
        assert!(out.contains("@@ -1,4 +1,5 @@"), "{out}");
        assert!(out.contains("@@ -19,4 +20,5 @@"), "{out}");
    }

    #[test]
    fn a_last_line_without_a_newline_is_marked() {
        assert_eq!(
            replace("a\nb", "b", "c"),
            "--- f\n+++ f\n@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file"
        );
    }

    #[test]
    fn carriage_returns_are_not_shown() {
        assert_eq!(
            replace("a\r\nb\r\n", "b\r\n", "c\r\n"),
            "--- f\n+++ f\n@@ -1,2 +1,2 @@\n a\n-b\n+c"
        );
    }

    #[test]
    fn a_change_inside_a_line_changes_the_whole_line() {
        assert_eq!(
            replace("let x = 1;\n", "1", "2"),
            "--- f\n+++ f\n@@ -1 +1 @@\n-let x = 1;\n+let x = 2;"
        );
    }

    #[test]
    fn a_replacement_that_joins_the_next_line_takes_that_line_in() {
        assert_eq!(
            replace("a\nc\nd\n", "c\n", "X"),
            "--- f\n+++ f\n@@ -1,3 +1,2 @@\n a\n-c\n-d\n+Xd"
        );
        assert_eq!(
            replace("q y\na\nz", " y\na\n", ""),
            "--- f\n+++ f\n@@ -1,3 +1 @@\n-q y\n-a\n-z\n\\ No newline at end of file\n+qz\n\\ No newline at end of file"
        );
    }

    /// The old text with the diff's hunks applied, checking the hunk headers' counts.
    fn apply(old: &str, diff: &str) -> String {
        let old_lines: Vec<&str> = old.split_inclusive('\n').collect();
        let mut out = String::new();
        let mut at = 0; // next old line
        let mut lines = diff.lines().skip(2).peekable();
        while let Some(head) = lines.next() {
            let counts = |part: &str| {
                let part = part.trim_start_matches(['-', '+']);
                part.split_once(',').map_or_else(
                    || (part.parse::<usize>().unwrap(), 1),
                    |(a, b)| (a.parse::<usize>().unwrap(), b.parse::<usize>().unwrap()),
                )
            };
            let mut parts = head.split(' ').skip(1);
            let (start, old_count) = counts(parts.next().unwrap());
            let (_, new_count) = counts(parts.next().unwrap());
            let start = if old_count == 0 { start } else { start - 1 };
            for line in &old_lines[at..start] {
                out.push_str(line);
            }
            at = start;
            let (mut seen_old, mut seen_new) = (0, 0);
            let mut last = ' ';
            // A marker ends its side of the text: no line may follow it on that side.
            let (mut old_ended, mut new_ended) = (false, false);
            while let Some(line) = lines.next_if(|l| !l.starts_with("@@ ")) {
                if line.starts_with('\\') {
                    old_ended |= last != '+';
                    new_ended |= last != '-';
                    if last != '-' {
                        out.pop(); // the line before has no newline in the new text
                    }
                    continue;
                }
                let (sign, text) = line.split_at(1);
                last = sign.chars().next().unwrap();
                assert!(
                    (!old_ended || sign == "+") && (!new_ended || sign == "-"),
                    "a line after the end of the text\n{diff}"
                );
                if sign != "+" {
                    assert_eq!(old_lines[at].trim_end_matches('\n'), text, "{diff}");
                    at += 1;
                    seen_old += 1;
                }
                if sign != "-" {
                    out.push_str(text);
                    out.push('\n');
                    seen_new += 1;
                }
            }
            assert_eq!((seen_old, seen_new), (old_count, new_count), "{diff}");
        }
        for line in &old_lines[at..] {
            out.push_str(line);
        }
        out
    }

    #[test]
    fn applying_the_diff_to_the_old_text_gives_the_new() {
        let mut state = 0x2545_F491_4F6C_DD1D_u64;
        let mut next = |n: usize| {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            usize::try_from(state % u64::try_from(n).unwrap()).unwrap()
        };
        for _ in 0..5000 {
            let pieces = ["a", "b", "c", "ab", "", "x y"];
            let mut old = String::new();
            for _ in 0..next(30) {
                old.push_str(pieces[next(pieces.len())]);
                old.push('\n');
            }
            if next(3) == 0 {
                old.pop();
            }
            if old.is_empty() {
                continue;
            }
            let start = next(old.len());
            let end = (start + 1 + next(8)).min(old.len());
            let find = &old[start..end];
            let with: String = (0..next(6))
                .map(|_| ["a", "b", "\n", "Q"][next(4)])
                .collect();
            if find == with {
                continue;
            }
            let diff = replace(&old, find, &with);
            assert_eq!(
                apply(&old, &diff),
                old.replace(find, &with),
                "{old:?} {find:?} {with:?}\n{diff}"
            );
        }
    }
}
