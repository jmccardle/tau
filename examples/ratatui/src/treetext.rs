//! What a tree row says: the type tag and text of a node, the summary of a folded turn or stub,
//! and the marks of the cursors at a row. Pure strings; `treeui` paints them.

use std::collections::BTreeMap;

use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::{
    model::prose,
    plan::{Facts, Kind, Row},
    wire::{Author, Body, Node, Outcome, Part},
};

const MIN_WIDTH: usize = 2;
/// Characters of a node's text a row keeps before the width cuts it.
const PREVIEW: usize = 200;
const STUB_GLYPH: &str = "├┄";
const OPEN_MARK: &str = "◀";
const CLOSED_MARK: &str = "○";
const DERIVED_MARK: &str = "⧉";
const COPY_MARK: &str = "≡";

/// The tag a row starts with, which colours it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tag {
    User,
    Assistant,
    Tool,
    System,
    Structural,
    Stub,
}

/// `text` cut to `width` cells, ending in `…` when anything was cut. With no room for a character
/// and the mark, `…` alone.
pub fn elide(text: &str, width: usize) -> String {
    if width < MIN_WIDTH {
        return "…".to_owned();
    }
    if text.width() <= width {
        return text.to_owned();
    }
    let mut out = String::new();
    let mut used = 0;
    for c in text.chars() {
        let w = c.width().unwrap_or(0);
        if used + w > width - 1 {
            break;
        }
        out.push(c);
        used += w;
    }
    out.push('…');
    out
}

fn author_name(by: &Author) -> String {
    match by {
        Author::Frontend(who) | Author::Extension(who) => who.clone(),
        Author::Provider => "model".to_owned(),
        Author::Tools => "tools".to_owned(),
        Author::Core => "core".to_owned(),
    }
}

/// What the planner needs of a node: its parent, whether it opens a turn, its calls, and the
/// characters the model reads of it (D24's estimate: text, thinking, call arguments and outcomes;
/// `Config` and `Custom` nodes never reach the model).
pub fn facts(node: &Node) -> Facts {
    let chars = match &node.body {
        Body::System(text) | Body::User(text) => text.chars().count(),
        Body::Assistant { parts, .. } => parts
            .iter()
            .map(|p| match p {
                Part::Text(t) | Part::Thinking(t) => prose(t).chars().count(),
                Part::Call { args_raw, .. } => args_raw.chars().count(),
            })
            .sum(),
        Body::Results(results) => results
            .iter()
            .map(|(_, outcome, _)| match outcome {
                Outcome::Ok(text) | Outcome::Err(text) => text.chars().count(),
                Outcome::Interrupted => 0,
            })
            .sum(),
        Body::Config(_) | Body::Custom { .. } => 0,
    };
    Facts {
        parent: node.parent.map(|p| p as usize),
        user: matches!(node.body, Body::User(_)),
        calls: match &node.body {
            Body::Assistant { parts, .. } => parts
                .iter()
                .filter(|p| matches!(p, Part::Call { .. }))
                .count(),
            _ => 0,
        },
        chars,
    }
}

fn squash(text: &str) -> String {
    text.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(PREVIEW)
        .collect()
}

/// What a `Config` node changed, in one line.
fn config_line(data: &str) -> String {
    let Ok(fields) = serde_json::from_str::<serde_json::Value>(data) else {
        return data.to_owned();
    };
    let mut parts = Vec::new();
    if let Some(model) = fields.get("model") {
        parts.push(format!(
            "model: {}",
            model.as_str().unwrap_or(&model.to_string())
        ));
    }
    if let Some(extensions) = fields.get("extensions").and_then(|e| e.as_object()) {
        for (name, options) in extensions {
            let set = options.as_object().into_iter().flatten();
            let set: Vec<String> = set
                .map(|(k, v)| format!("{k}={}", v.as_str().map_or(v.to_string(), str::to_owned)))
                .collect();
            parts.push(format!("{name}: {}", set.join(", ")));
        }
    }
    parts.join("  ")
}

/// A node's tag word and its one-line text.
pub fn describe(node: &Node) -> (Tag, &'static str, String) {
    match &node.body {
        Body::User(text) => (Tag::User, "user", squash(text)),
        Body::Assistant { parts, .. } => {
            let said: String = parts
                .iter()
                .filter_map(|p| match p {
                    Part::Text(t) => Some(prose(t)),
                    _ => None,
                })
                .collect();
            let calls: Vec<&str> = parts
                .iter()
                .filter_map(|p| match p {
                    Part::Call { tool, .. } => Some(tool.as_str()),
                    _ => None,
                })
                .collect();
            let said = squash(&said);
            let text = if said.is_empty() {
                format!("calls {}", calls.join(", "))
            } else {
                said
            };
            (Tag::Assistant, "assistant", text)
        }
        Body::Results(results) => (Tag::Tool, "tool", plural(results.len(), "result", None)),
        Body::System(_) => (Tag::System, "system", "system prompt".to_owned()),
        Body::Config(config) => (Tag::Structural, "config", config_line(&config.data)),
        Body::Custom { kind, .. } => (Tag::Structural, "custom", kind.clone()),
    }
}

/// What the detail pane shows of a node: who wrote it and all of its text.
pub fn full_text(node: &Node) -> String {
    let (_, word, short) = describe(node);
    let head = format!(
        "{word}  ·  node {}  ·  by {}\n\n",
        node.id,
        author_name(&node.by)
    );
    let body = match &node.body {
        Body::User(text) | Body::System(text) => text.clone(),
        Body::Assistant { parts, .. } => parts
            .iter()
            .filter_map(|p| match p {
                Part::Text(t) => Some(prose(t)),
                Part::Call { tool, args_raw, .. } => Some(format!("{tool}({args_raw})")),
                Part::Thinking(_) => None,
            })
            .collect::<Vec<_>>()
            .join("\n"),
        Body::Results(results) => results
            .iter()
            .map(|(call, outcome, _)| {
                let shown = match outcome {
                    Outcome::Ok(value) => value.clone(),
                    Outcome::Err(error) => format!("error: {error}"),
                    Outcome::Interrupted => "interrupted before it finished".to_owned(),
                };
                format!("{call}: {shown}")
            })
            .collect::<Vec<_>>()
            .join("\n"),
        Body::Custom { data, .. } => data.clone(),
        Body::Config(_) => short,
    };
    head + &body
}

/// What the detail pane shows of `row`: a node whole, or what a stub holds and how it begins.
pub fn detail_text(row: &Row, node: &Node) -> String {
    if row.kind != Kind::Stub {
        return full_text(node);
    }
    let mut shown = vec![format!(
        "{} and {} on this branch",
        plural(row.size, "node", None),
        plural(row.turns, "turn", None)
    )];
    shown.extend(row.cursors.iter().map(|c| format!("cursor {c} is on it")));
    shown.extend(
        row.closed
            .iter()
            .map(|c| format!("cursor {c} (closed) was on it")),
    );
    format!("{}\n\nIt begins:\n\n{}", shown.join("\n"), full_text(node))
}

/// `1 node`, `3 nodes`; `many` is the plural when it is not the word and an s.
pub fn plural(count: usize, word: &str, many: Option<&str>) -> String {
    if count == 1 {
        format!("{count} {word}")
    } else {
        format!(
            "{count} {}",
            many.map_or_else(|| format!("{word}s"), str::to_owned)
        )
    }
}

/// What a cursor's mark shows besides its number.
#[derive(Clone, Copy, Debug, Default)]
pub struct Flags {
    pub at: usize,
    pub busy: bool,
    pub asking: bool,
}

/// The cursors a row can be marked with: each open cursor (`◀ 1 scout`, the viewed one
/// `◀ [0] main`, `…` while it works and `?` while a question is open) and each closed one, which
/// stays as its restart record (`○ 2 compaction:1`, drawn dimmed).
#[derive(Debug)]
pub struct Marks {
    open: BTreeMap<u64, Flags>,
    labels: Vec<(String, u64)>,
    viewed: u64,
    open_at: BTreeMap<usize, Vec<u64>>,
    closed_at: BTreeMap<usize, Vec<u64>>,
}

impl Marks {
    pub fn new(
        open: BTreeMap<u64, Flags>,
        gone_at: &BTreeMap<u64, usize>,
        labels: Vec<(String, u64)>,
        viewed: u64,
    ) -> Self {
        let mut open_at: BTreeMap<usize, Vec<u64>> = BTreeMap::new();
        let mut closed_at: BTreeMap<usize, Vec<u64>> = BTreeMap::new();
        for (cursor, flags) in &open {
            open_at.entry(flags.at).or_default().push(*cursor); // rules: allow(entry): BTreeMap API
        }
        for (cursor, at) in gone_at {
            closed_at.entry(*at).or_default().push(*cursor); // rules: allow(entry): BTreeMap API
        }
        Self {
            open,
            labels,
            viewed,
            open_at,
            closed_at,
        }
    }

    /// The open cursors at `node`.
    pub fn open_at(&self, node: usize) -> &[u64] {
        self.open_at.get(&node).map_or(&[], Vec::as_slice)
    }

    fn open_text(&self, cursors: &[u64]) -> String {
        let mut out = String::new();
        for &cursor in cursors {
            let flags = self.open[&cursor];
            let number = if cursor == self.viewed {
                format!("[{cursor}]")
            } else {
                cursor.to_string()
            };
            out += &format!("  {OPEN_MARK} {number}");
            let names = self.labels.iter().filter(|(_, on)| *on == cursor);
            for (name, _) in names {
                out += &format!(" {name}");
            }
            let state = format!(
                "{}{}",
                if flags.busy { "…" } else { "" },
                if flags.asking { "?" } else { "" }
            );
            if !state.is_empty() {
                out += &format!(" {state}");
            }
        }
        out
    }

    fn closed_text(&self, cursors: &[u64]) -> String {
        let mut out = String::new();
        for &cursor in cursors {
            let names: Vec<&str> = self
                .labels
                .iter()
                .filter(|(_, on)| *on == cursor)
                .map(|(n, _)| n.as_str())
                .collect();
            out += &format!("  {CLOSED_MARK} {cursor}");
            if !names.is_empty() {
                out += &format!(" {}", names.join(" "));
            }
        }
        out
    }
}

/// A row's words. `body` may be cut to fit; `suffix` (the fold summary and the marks) never is;
/// `dim_at` is where in `suffix` the closed cursors' marks begin, in characters.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RowLabel {
    pub body: String,
    pub suffix: String,
    pub dim_at: Option<usize>,
    pub tag: Tag,
    /// Characters of `body` the tag colours.
    pub tag_size: usize,
}

/// What a folded turn hides: `  +14 · 6 calls · 1 branch`; empty for a turn of one node.
fn turn_summary(row: &Row) -> String {
    let parts = [
        (row.size > 0).then(|| format!("+{}", row.size)),
        (row.calls > 0).then(|| plural(row.calls, "call", None)),
        (row.branches > 0).then(|| plural(row.branches, "branch", Some("branches"))),
    ];
    let parts: Vec<String> = parts.into_iter().flatten().collect();
    if parts.is_empty() {
        String::new()
    } else {
        format!("  {}", parts.join(" · "))
    }
}

/// Where a node's content came from (D24 M1): `⧉` for one computed from another, `≡ (copy of 7)`
/// for a verbatim copy, nothing for the rest.
fn provenance_mark(node: &Node) -> String {
    if node.derived_from.is_some() {
        format!("  {DERIVED_MARK}")
    } else if let Some(source) = node.copied_from {
        format!("  {COPY_MARK} (copy of {source})")
    } else {
        String::new()
    }
}

/// The words of `row`: a node's tag and text with its fold summary and the cursors at it, or a
/// stub's counts and the cursors on its branch. A folded turn lists every cursor inside it, so
/// none is out of sight; an open one only those at its own node (the rest show below it).
pub fn row_label(row: &Row, node: &Node, marks: &Marks, folded: bool) -> RowLabel {
    let (body, tag, tag_size, summary, shown, closed);
    if row.kind == Kind::Stub {
        body = format!(
            "{STUB_GLYPH} {}, {}",
            plural(row.size, "node", None),
            plural(row.turns, "turn", None)
        );
        tag = Tag::Stub;
        tag_size = STUB_GLYPH.chars().count();
        summary = String::new();
        shown = row.cursors.clone();
        closed = row.closed.clone();
    } else {
        let (kind, word, text) = describe(node);
        body = format!("{word}: {text}");
        tag = kind;
        tag_size = word.chars().count() + 1;
        let whole = row.kind == Kind::Turn && folded;
        shown = if whole {
            row.cursors.clone()
        } else {
            marks.open_at(row.node).to_vec()
        };
        closed = if whole {
            row.closed.clone()
        } else {
            marks.closed_at.get(&row.node).cloned().unwrap_or_default()
        };
        summary = provenance_mark(node)
            + &if row.kind == Kind::Turn {
                turn_summary(row)
            } else {
                String::new()
            };
    }
    let before = summary + &marks.open_text(&shown);
    let after = marks.closed_text(&closed);
    RowLabel {
        body,
        dim_at: (!after.is_empty()).then(|| before.chars().count()),
        suffix: before + &after,
        tag,
        tag_size,
    }
}

/// `label` cut to `width` cells, and where the dimmed part begins in the result, in characters.
/// The body gives way first; if even the suffix does not fit, the whole is cut and nothing is
/// dimmed.
pub fn fit(label: &RowLabel, width: usize) -> (String, Option<usize>) {
    let room = width.saturating_sub(label.suffix.width());
    if room >= MIN_WIDTH && label.suffix.width() < width {
        let body = elide(&label.body, room);
        let cut = body.chars().count();
        return (body + &label.suffix, label.dim_at.map(|d| cut + d));
    }
    (
        elide(&format!("{}{}", label.body, label.suffix), width),
        None,
    )
}

#[cfg(test)]
mod tests;
