//! Calibration (D26 section 6): what the `tokens` built-in learns about one
//! model from the proved spans its replies report, and what it predicts
//! from that. Pure functions over values: [`features`] of a body, [`fold`]
//! one observation into a [`Record`], [`predict`] a count from a record.
//! Nothing in core's own behavior calls this module.
//!
//! The model is linear. A node has a **role** (system, user, assistant,
//! calls, results; thinking is a role of its own because some servers are
//! sent a reply's thinking back and some are not) and its text is counted in
//! seven **classes** ([`CLASSES`]). A span's cost is one number for several
//! nodes, so every span is one row of [`DIM`] numbers: seven per role, then
//! how many nodes of each role it holds. The record keeps the sums a least
//! squares fit needs (`xtx`, `xty`), decayed by [`DECAY`] per span, never the
//! text.
//!
//! The fit is non-negative least squares pulled toward a **prior**: one
//! token per byte and nothing per word, which is the ceiling's assumption.
//! Where the spans say nothing (a class never seen, a role never seen) a
//! prediction is therefore the text's bytes, an over-count, and never zero.
//! Per-role integer constants are then fitted to what the weights leave.
//!
//! A prediction is answered only once the record is **ready**: [`WARMUP`]
//! spans folded since the last reset, then [`READY`] residuals of its own
//! predictions. Its bound is D26 section 6.4's, from those residuals.

mod nnls;

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::tree::{Body, Outcome, Part};

/// The record format this build writes and reads.
pub const VERSION: u32 = 1;
/// Roles: system, user, assistant, calls, results, thinking ([`ROLE_NAMES`]).
pub const ROLES: usize = 6;
/// Classes of a role's text: lowercase, uppercase, digits, other ASCII
/// (punctuation), whitespace, the bytes of non-ASCII characters, and words.
pub const CLASSES: usize = 7;
/// How many weights a fit has: [`CLASSES`] for each role.
pub const WEIGHTS: usize = ROLES * CLASSES;
/// The length of a feature vector: the weights' columns, then one count of
/// nodes for each role.
pub const DIM: usize = WEIGHTS + ROLES;
/// The roles' names, in the order of their columns.
pub const ROLE_NAMES: [&str; ROLES] = [
    "system",
    "user",
    "assistant",
    "calls",
    "results",
    "thinking",
];

/// What the stored sums are multiplied by for each span folded, so a slow
/// change is followed (D26 section 6.3).
pub const DECAY: f64 = 0.995;
/// What the stored sums keep at a reset (a jump, a changed marker).
pub const RESET_KEEPS: f64 = 0.05;
/// How many residuals a record keeps, newest last.
pub const WINDOW: usize = 200;
/// Spans folded after a reset before a prediction's residual is taken.
pub const WARMUP: u32 = 5;
/// Residuals a record needs before it answers a prediction.
pub const READY: usize = 5;
/// Below this many residuals the bound uses their largest, not their 95th
/// percentile.
pub const SMALL: usize = 20;
/// The CUSUM's slack: a residual this far from the usual one adds nothing.
pub const CUSUM_SLACK: f64 = 0.15;
/// The CUSUM's limit: past it, the model has jumped.
pub const CUSUM_LIMIT: f64 = 1.0;
/// The prior's strength, as the square of a text's length in characters:
/// the prior counts as one span of this many characters of each class.
pub const PRIOR_CHARS: f64 = 64.0;

const WORDS: usize = 6;
const SYSTEM: usize = 0;
const USER: usize = 1;
const ASSISTANT: usize = 2;
const CALLS: usize = 3;
const RESULTS: usize = 4;
const THINKING: usize = 5;

/// One row of the fit: for a node, or summed over a span's nodes.
#[derive(Clone, Debug, PartialEq)]
pub struct Features(pub Vec<f64>);

impl Features {
    /// A row of zeros: no node.
    #[must_use]
    pub fn zero() -> Self {
        Self(vec![0.0; DIM])
    }

    /// Whether the row holds no node that is sent.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.0[WEIGHTS..].iter().all(|n| *n == 0.0)
    }

    /// Adds another row to this one: a span is the sum of its nodes.
    pub fn add(&mut self, other: &Self) {
        for (a, b) in self.0.iter_mut().zip(&other.0) {
            *a += b;
        }
    }

    fn text(&mut self, role: usize, text: &str) {
        let row = &mut self.0[role * CLASSES..(role + 1) * CLASSES];
        let mut in_word = false;
        for c in text.chars() {
            row[class_of(c)] += bytes_of(c);
            let word = c.is_alphanumeric();
            if word && !in_word {
                row[WORDS] += 1.0;
            }
            in_word = word;
        }
    }

    fn node(&mut self, role: usize) {
        self.0[WEIGHTS + role] += 1.0;
    }
}

/// How many bytes a character is in UTF-8.
fn bytes_of(c: char) -> f64 {
    match c.len_utf8() {
        1 => 1.0,
        2 => 2.0,
        3 => 3.0,
        _ => 4.0,
    }
}

fn class_of(c: char) -> usize {
    match c {
        'a'..='z' => 0,
        'A'..='Z' => 1,
        '0'..='9' => 2,
        _ if c.is_ascii_whitespace() => 4,
        _ if c.is_ascii() => 3,
        _ => 5,
    }
}

/// The features of a node's body: its text by role and class, and one node
/// of its role. An assistant node with calls has the role `calls` for all it
/// says; thinking and replay data, in any reply, are the role `thinking`.
/// `Config` and `Custom` bodies are never sent and have no features.
#[must_use]
pub fn features(body: &Body) -> Features {
    let mut x = Features::zero();
    match body {
        Body::System(text) => {
            x.text(SYSTEM, text);
            x.node(SYSTEM);
        }
        Body::User(text) => {
            x.text(USER, text);
            x.node(USER);
        }
        Body::Assistant { parts, .. } => assistant(&mut x, parts),
        Body::Results(outcomes) => {
            for (call, outcome, _) in outcomes {
                x.text(RESULTS, call);
                x.text(RESULTS, outcome_text(outcome));
            }
            x.node(RESULTS);
        }
        Body::Config(_) | Body::Custom { .. } => {}
    }
    x
}

fn assistant(x: &mut Features, parts: &[Part]) {
    let role = if parts.iter().any(|p| matches!(p, Part::Call { .. })) {
        CALLS
    } else {
        ASSISTANT
    };
    let mut thought = false;
    for part in parts {
        let replay = match part {
            Part::Text { text, replay } => {
                x.text(role, text);
                replay
            }
            Part::Thinking { text, replay } => {
                x.text(THINKING, text);
                thought = true;
                replay
            }
            Part::Call {
                id,
                tool,
                args_raw,
                replay,
            } => {
                x.text(role, id);
                x.text(role, tool);
                x.text(role, args_raw);
                replay
            }
        };
        if let Some(replay) = replay {
            x.text(THINKING, replay.data());
            thought = true;
        }
    }
    x.node(role);
    if thought {
        x.node(THINKING);
    }
}

fn outcome_text(outcome: &Outcome) -> &str {
    match outcome {
        Outcome::Ok(text) | Outcome::Err(text) => text,
        Outcome::Interrupted => "Interrupted",
    }
}

/// Which model a record is about.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Key {
    pub provider: String,
    pub base_url: Option<String>,
    pub model: String,
}

/// The sums of the fit and what was fitted from them.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Heuristic {
    /// `XᵀX`, the upper triangle row by row ([`DIM`] by [`DIM`]), decayed.
    pub xtx: Vec<f64>,
    /// `Xᵀy`, decayed.
    pub xty: Vec<f64>,
    /// How many spans the sums are worth after decay.
    pub n: f64,
    /// The fitted weights: tokens per unit of each class, for each role.
    pub weights: Vec<f64>,
    /// The fitted integer constant of each role: tokens per node.
    pub per_message: Vec<u32>,
    /// The last [`WINDOW`] residuals, newest last: the signed relative error
    /// `(cost - predicted) / predicted` and what was predicted.
    pub residuals: Vec<(f64, f64)>,
    /// The two-sided CUSUM of the residuals about their usual value.
    pub cusum: (f64, f64),
    /// Spans folded since the last reset.
    pub since_reset: u32,
}

/// One model's calibration record (D26 section 6.5).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Record {
    pub version: u32,
    pub key: Key,
    /// The drift markers of the newest observation, by name.
    pub markers: BTreeMap<String, String>,
    /// The tokenizer the `tokens` extra identified; `null` without it. Kept
    /// as it was read.
    pub tokenizer: Option<serde_json::Value>,
    pub heuristic: Heuristic,
    /// Replies observed.
    pub observations: u64,
    /// Proved spans folded.
    pub spans: u64,
    /// When the newest observation was folded, as the caller wrote it.
    pub updated: String,
}

impl Record {
    /// A record that has seen nothing.
    #[must_use]
    pub fn new(key: Key) -> Self {
        let mut heuristic = Heuristic {
            xtx: vec![0.0; DIM * (DIM + 1) / 2],
            xty: vec![0.0; DIM],
            n: 0.0,
            weights: vec![],
            per_message: vec![],
            residuals: vec![],
            cusum: (0.0, 0.0),
            since_reset: 0,
        };
        heuristic.refit();
        Self {
            version: VERSION,
            key,
            markers: BTreeMap::new(),
            tokenizer: None,
            heuristic,
            observations: 0,
            spans: 0,
            updated: String::new(),
        }
    }

    /// Whether a prediction is answered: enough residuals since the last
    /// reset.
    #[must_use]
    pub fn ready(&self) -> bool {
        self.heuristic.residuals.len() >= READY
    }

    /// The relative error the bound is made from (D26 section 6.4): the
    /// 95th percentile of the residuals' sizes, their largest while there
    /// are fewer than [`SMALL`]; `None` while not ready.
    #[must_use]
    pub fn error(&self) -> Option<f64> {
        if !self.ready() {
            return None;
        }
        let mut sizes: Vec<f64> = self.heuristic.residuals.iter().map(|r| r.0.abs()).collect();
        sizes.sort_by(f64::total_cmp);
        let n = sizes.len();
        let at = if n < SMALL {
            n - 1
        } else {
            (n * 95).div_ceil(100) - 1
        };
        Some(sizes[at])
    }

    /// Whether the shapes are those this build fits: a record read from a
    /// file is checked before it is used.
    #[must_use]
    pub fn well_formed(&self) -> bool {
        let h = &self.heuristic;
        h.xtx.len() == DIM * (DIM + 1) / 2
            && h.xty.len() == DIM
            && h.weights.len() == WEIGHTS
            && h.per_message.len() == ROLES
            && h.residuals.len() <= WINDOW
            && h.xtx
                .iter()
                .chain(&h.xty)
                .chain(&h.weights)
                .all(|v| v.is_finite())
            && h.residuals
                .iter()
                .all(|r| r.0.is_finite() && r.1.is_finite())
    }
}

fn tri(i: usize, j: usize) -> usize {
    let (i, j) = if i <= j { (i, j) } else { (j, i) };
    i * DIM - i * (i + 1) / 2 + j
}

impl Heuristic {
    fn raw(&self, x: &Features) -> f64 {
        let text: f64 = self.weights.iter().zip(&x.0).map(|(w, v)| w * v).sum();
        let nodes: f64 = self
            .per_message
            .iter()
            .zip(&x.0[WEIGHTS..])
            .map(|(c, n)| f64::from(*c) * n)
            .sum();
        text + nodes
    }

    fn scale(&mut self, by: f64) {
        for v in self.xtx.iter_mut().chain(self.xty.iter_mut()) {
            *v *= by;
        }
        self.n *= by;
    }

    fn reset(&mut self) {
        self.scale(RESET_KEEPS);
        self.residuals.clear();
        self.cusum = (0.0, 0.0);
        self.since_reset = 0;
    }

    /// Takes the residual of this span's prediction and says whether the
    /// residuals have jumped away from their usual value (their mean so
    /// far). The CUSUM runs only once there are [`SMALL`] residuals: a fit
    /// still settling is not a jump, and until then the bound is the largest
    /// residual, which covers it.
    #[allow(clippy::cast_precision_loss)]
    fn residual(&mut self, x: &Features, cost: f64) -> bool {
        let predicted = self.raw(x).max(1.0);
        let rel = (cost - predicted) / predicted;
        let settled = self.residuals.len() >= SMALL;
        let usual =
            self.residuals.iter().map(|r| r.0).sum::<f64>() / self.residuals.len().max(1) as f64;
        self.residuals.push((rel, predicted));
        if self.residuals.len() > WINDOW {
            self.residuals.remove(0);
        }
        if !settled {
            return false;
        }
        let off = rel - usual;
        self.cusum.0 = (self.cusum.0 + off - CUSUM_SLACK).max(0.0);
        self.cusum.1 = (self.cusum.1 - off - CUSUM_SLACK).max(0.0);
        self.cusum.0 > CUSUM_LIMIT || self.cusum.1 > CUSUM_LIMIT
    }

    fn add(&mut self, x: &Features, cost: f64) {
        self.scale(DECAY);
        self.n += 1.0;
        for i in 0..DIM {
            if x.0[i] == 0.0 {
                continue;
            }
            self.xty[i] += x.0[i] * cost;
            for j in i..DIM {
                self.xtx[tri(i, j)] += x.0[i] * x.0[j];
            }
        }
        self.since_reset += 1;
    }

    /// Fits the weights, then the constants to what the weights leave.
    fn refit(&mut self) {
        let prior = PRIOR_CHARS * PRIOR_CHARS;
        let mut a = vec![0.0; WEIGHTS * WEIGHTS];
        let mut b = vec![0.0; WEIGHTS];
        for i in 0..WEIGHTS {
            for j in 0..WEIGHTS {
                a[i * WEIGHTS + j] = self.xtx[tri(i, j)];
            }
            a[i * WEIGHTS + i] += prior;
            // The prior: one token per byte of every class, nothing per word.
            b[i] = self.xty[i] + if i % CLASSES == WORDS { 0.0 } else { prior };
        }
        self.weights = nnls::solve(&a, &b, WEIGHTS);
        let mut c = vec![0.0; ROLES * ROLES];
        let mut r = vec![0.0; ROLES];
        for k in 0..ROLES {
            for l in 0..ROLES {
                c[k * ROLES + l] = self.xtx[tri(WEIGHTS + k, WEIGHTS + l)];
            }
            c[k * ROLES + k] += 1.0;
            let explained: f64 = (0..WEIGHTS)
                .map(|j| self.xtx[tri(WEIGHTS + k, j)] * self.weights[j])
                .sum();
            r[k] = self.xty[WEIGHTS + k] - explained;
        }
        self.per_message = nnls::solve(&c, &r, ROLES).into_iter().map(whole).collect();
    }
}

/// A non-negative finite number rounded to a whole one.
#[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
fn whole(v: f64) -> u32 {
    v.round().clamp(0.0, f64::from(u32::MAX)) as u32
}

/// One reply, as the `tokens` built-in saw it.
#[derive(Clone, Debug, PartialEq)]
pub struct Observation {
    /// The reply's drift markers, by name.
    pub markers: BTreeMap<String, String>,
    /// The newest proved span, if the reply proved one: the features of its
    /// nodes summed, and its cost.
    pub span: Option<(Features, u64)>,
    /// When, as text the record keeps.
    pub time: String,
}

/// What a fold found that the clients should be told.
#[derive(Clone, Debug, PartialEq)]
pub enum Event {
    /// A drift marker changed: the record was reset.
    Marker {
        name: String,
        was: String,
        now: String,
    },
    /// The residuals jumped: requests now cost this much more (or less),
    /// relative to what was predicted. The record was reset.
    Jump { by: f64 },
}

/// Folds one observation into a record. A marker that differs from the
/// record's resets it first; a marker the record or the observation lacks
/// says nothing. A span is predicted before it is added, once past the
/// warm-up, and the prediction's residual kept; a jump in the residuals
/// resets the record. Sums decay by [`DECAY`] per span.
#[must_use]
pub fn fold(mut record: Record, observation: &Observation) -> (Record, Vec<Event>) {
    let mut events = vec![];
    for (name, now) in &observation.markers {
        match record.markers.insert(name.clone(), now.clone()) {
            Some(was) if was != *now => events.push(Event::Marker {
                name: name.clone(),
                was,
                now: now.clone(),
            }),
            _ => {}
        }
    }
    if !events.is_empty() {
        record.heuristic.reset();
    }
    record.observations += 1;
    record.updated.clone_from(&observation.time);
    let Some((x, cost)) = &observation.span else {
        return (record, events);
    };
    if x.is_empty() || x.0.len() != DIM {
        return (record, events);
    }
    #[allow(clippy::cast_precision_loss)]
    let cost = *cost as f64;
    let h = &mut record.heuristic;
    if h.since_reset >= WARMUP && h.residual(x, cost) {
        let by = h.residuals.last().map_or(0.0, |r| r.0);
        events.push(Event::Jump { by });
        h.reset();
    }
    h.add(x, cost);
    h.refit();
    record.spans += 1;
    (record, events)
}

/// A heuristic count: `tokens`, and how many more it may be.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Estimate {
    pub tokens: u64,
    pub bound: u64,
}

/// The count the record predicts for a row of features, or `None` while the
/// record is not ready or the row holds no node. The bound is D26 section
/// 6.4's: `ceil(p * q * (1 + 2 / sqrt(n))) + 2`, with `q` the record's
/// [`Record::error`] and `n` its residuals.
#[must_use]
#[allow(
    clippy::cast_possible_truncation,
    clippy::cast_sign_loss,
    clippy::cast_precision_loss
)]
pub fn predict(record: &Record, x: &Features) -> Option<Estimate> {
    let q = record.error()?;
    if x.is_empty() || x.0.len() != DIM {
        return None;
    }
    let p = record.heuristic.raw(x).max(0.0).round();
    let n = record.heuristic.residuals.len() as f64;
    let bound = (p * q * (1.0 + 2.0 / n.sqrt())).ceil() + 2.0;
    Some(Estimate {
        tokens: p as u64,
        bound: bound as u64,
    })
}
