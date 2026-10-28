//! Cost of the agent access pattern on the tree and the store: an agent goes
//! forward, gathering `walk(at)` for every sample, and now and then jumps back
//! (a go-to) or branches a new cursor off a recent node. Std only; run with
//! `cargo bench -p tau-store --bench tree -- <scenario>...` (no scenario: all).
//!
//! Drives the public `Machine` with a scripted provider and tools, as the
//! tests do. Sizes are in tokens at 4 bytes a token.
#![expect(
    clippy::cast_precision_loss,
    reason = "report figures: rates and megabytes, far below 2^52"
)]

use std::fmt::Write as _;
use std::path::Path;
use std::time::{Duration, Instant};

use tau_core::machine::Machine;
use tau_core::step::{Draft, Fed, Job};
use tau_core::tree::{Author, Body, CursorId, Node, NodeId, Outcome, Part, Session, Stop};
use tau_store::Store;

const TOKEN: usize = 4;

/// A turn: one input, `rounds` tool rounds (an assistant node with a call,
/// then its results), then a final assistant reply. Sizes in bytes.
#[derive(Clone, Copy)]
struct Shape {
    name: &'static str,
    user: usize,
    assistant: usize,
    results: usize,
    rounds: u32,
}

const CHATTY: Shape = Shape {
    name: "chatty",
    user: 400,
    assistant: 600,
    results: 1_000,
    rounds: 4,
};
const BULKY: Shape = Shape {
    name: "bulky",
    user: 400,
    assistant: 800,
    results: 16_000,
    rounds: 4,
};
const TINY: Shape = Shape {
    name: "tiny",
    user: 80,
    assistant: 120,
    results: 120,
    rounds: 8,
};

/// xorshift64: deterministic, no dependency.
fn rand(state: &mut u64) -> u64 {
    *state ^= *state << 13;
    *state ^= *state >> 7;
    *state ^= *state << 17;
    *state
}

fn text(len: usize) -> String {
    "lorem ipsum dolor sit amet "
        .chars()
        .cycle()
        .take(len)
        .collect()
}

fn body_bytes(b: &Body) -> usize {
    match b {
        Body::System(t) | Body::User(t) => t.len(),
        Body::Assistant { parts, .. } => parts
            .iter()
            .map(|p| match p {
                Part::Text(t) | Part::Thinking(t) => t.len(),
                Part::Call { args_raw, .. } => args_raw.len(),
            })
            .sum(),
        Body::Results(r) => r
            .iter()
            .map(|(_, o, _)| match o {
                Outcome::Ok(t) | Outcome::Err(t) => t.len(),
                Outcome::Interrupted => 0,
            })
            .sum(),
        Body::Config(c) => c.data.len(),
        Body::Custom { data, .. } => data.len(),
    }
}

fn tokens(walk: &[&Node]) -> usize {
    walk.iter().map(|n| body_bytes(&n.body)).sum::<usize>() / TOKEN
}

/// What the driver does with the context at each sample.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Read {
    /// `walk(at)` only.
    Walk,
    /// `walk(at)`, then the walk as JSON: a stand-in for building the request.
    Json,
}

#[derive(Default)]
struct Stats {
    /// Per sample: context tokens, walk time, JSON time.
    samples: Vec<(usize, Duration, Duration)>,
    /// Per `Store::save` that synced: its time.
    syncs: Vec<Duration>,
    /// `Store::save` calls that wrote more than one `Sync` (one fsync each).
    multi_sync: usize,
    /// Time in the machine: `next_job`, `feed`, `send`.
    core: Duration,
}

/// One machine, its store if any, the clock, and what was measured.
struct Rig {
    m: Machine,
    store: Option<Store>,
    now: u64,
    read: Read,
    stats: Stats,
    payloads: Payloads,
}

/// Bodies built once per shape and cloned per node.
struct Payloads {
    shape: Shape,
    user: String,
    assistant: String,
    results: String,
}

impl Payloads {
    fn new(shape: Shape) -> Self {
        Self {
            shape,
            user: text(shape.user),
            assistant: text(shape.assistant),
            results: text(shape.results),
        }
    }
}

/// One scripted agent on one cursor.
struct Agent {
    cursor: CursorId,
    turns: u32,
    rounds: u32,
    calls: u64,
}

impl Agent {
    fn new(cursor: CursorId, turns: u32) -> Self {
        Self {
            cursor,
            turns,
            rounds: 0,
            calls: 0,
        }
    }

    /// One job. False once the agent's turns are done and its cursor rests.
    fn tick(&mut self, rig: &mut Rig) -> bool {
        let c = self.cursor;
        let start = Instant::now();
        let job = rig.m.next_job(c).unwrap();
        rig.stats.core += start.elapsed();
        rig.now += 1;
        let fed = match job {
            Job::Rest if self.turns == 0 => return false,
            Job::Rest => {
                self.turns -= 1;
                self.rounds = rig.payloads.shape.rounds;
                let start = Instant::now();
                rig.m
                    .send(c, "bench".into(), &rig.payloads.user, rig.now)
                    .unwrap();
                rig.stats.core += start.elapsed();
                vec![]
            }
            Job::Hooks { .. } => vec![Fed::Hooks {
                stop: None,
                draft: None,
            }],
            Job::Sample => {
                rig.sample(c);
                vec![self.reply(&rig.payloads)]
            }
            Job::RunCalls(run) => run
                .into_iter()
                .map(|call| Fed::CallDone {
                    call: call.id,
                    outcome: Outcome::Ok(rig.payloads.results.clone()),
                    by: Author::Tools,
                })
                .collect(),
            Job::Wait { .. } => vec![Fed::Waited],
            Job::Pending => unreachable!("every call is fed with its batch"),
        };
        let start = Instant::now();
        for f in fed {
            rig.m.feed(c, f, rig.now).unwrap();
        }
        rig.stats.core += start.elapsed();
        rig.save();
        true
    }

    fn reply(&mut self, p: &Payloads) -> Fed {
        let mut parts = vec![Part::Text(p.assistant.clone())];
        let stop = if self.rounds > 0 {
            self.rounds -= 1;
            self.calls += 1;
            parts.push(Part::Call {
                id: format!("c{}", self.calls),
                tool: "read".into(),
                args_raw: r#"{"path":"src/lib.rs"}"#.into(),
            });
            Stop::Tools
        } else {
            Stop::End
        };
        Fed::Sampled {
            reply: Some(Draft {
                body: Body::Assistant { parts, stop },
                delivers: vec![],
                by: Author::Provider,
            }),
            error: None,
        }
    }

    fn run(&mut self, rig: &mut Rig) {
        while self.tick(rig) {}
    }
}

impl Rig {
    fn new(shape: Shape, read: Read) -> (Self, CursorId) {
        let (session, cursor) = Session::create("{}", "You are a bench.", 0);
        let rig = Self {
            m: Machine::new(session),
            store: None,
            now: 0,
            read,
            stats: Stats::default(),
            payloads: Payloads::new(shape),
        };
        (rig, cursor)
    }

    fn sample(&mut self, c: CursorId) {
        let s = &self.m.session;
        let t = Instant::now();
        let walk = s.walk(s.at(c).unwrap());
        let walked = t.elapsed();
        let json = if self.read == Read::Json {
            let t = Instant::now();
            let bytes = serde_json::to_vec(&walk).unwrap();
            std::hint::black_box(bytes);
            t.elapsed()
        } else {
            Duration::ZERO
        };
        self.stats.samples.push((tokens(&walk), walked, json));
    }

    fn save(&mut self) {
        let Some(store) = &mut self.store else {
            return;
        };
        let syncs = self
            .m
            .session
            .outbox
            .iter()
            .filter(|r| matches!(r, tau_core::tree::Record::Sync { .. }))
            .count();
        let t = Instant::now();
        store.save(&mut self.m.session).unwrap();
        if syncs > 0 {
            self.stats.syncs.push(t.elapsed());
        }
        if syncs > 1 {
            self.stats.multi_sync += 1;
        }
    }

    fn tokens_at(&self, c: CursorId) -> usize {
        let s = &self.m.session;
        tokens(&s.walk(s.at(c).unwrap()))
    }

    /// Turns on `c` until its context reaches `target` tokens.
    fn grow(&mut self, c: CursorId, target: usize) {
        let mut agent = Agent::new(c, 0);
        while self.tokens_at(c) < target {
            agent.turns = 1;
            agent.run(self);
        }
    }

    /// The store, created over the session as it stands (one sync).
    fn persist(&mut self, dir: &Path) -> Duration {
        let t = Instant::now();
        self.store = Some(Store::create(dir, &mut self.m.session).unwrap());
        t.elapsed()
    }

    /// A random node among the last `recent` on `c`'s walk.
    fn recent(&self, c: CursorId, recent: usize, seed: &mut u64) -> NodeId {
        let s = &self.m.session;
        let walk = s.walk(s.at(c).unwrap());
        let from = walk.len().saturating_sub(recent).max(2);
        let span = (walk.len() - from) as u64;
        walk[from + usize::try_from(rand(seed) % span.max(1)).unwrap()].id
    }
}

/// Node ids are dense from 0.
fn node_count(s: &Session) -> u64 {
    let (mut lo, mut hi) = (0u64, 1u64 << 40);
    while lo < hi {
        let mid = lo + (hi - lo) / 2;
        if s.node(mid).is_ok() {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    lo
}

// Reporting.

fn us(d: Duration) -> String {
    let us = d.as_secs_f64() * 1e6;
    if us < 1_000.0 {
        format!("{us:.1} µs")
    } else if us < 1_000_000.0 {
        format!("{:.2} ms", us / 1e3)
    } else {
        format!("{:.2} s", us / 1e6)
    }
}

fn pct(mut v: Vec<Duration>, p: usize) -> Duration {
    if v.is_empty() {
        return Duration::ZERO;
    }
    v.sort();
    v[(v.len() - 1) * p / 100]
}

fn mean(v: &[Duration]) -> Duration {
    if v.is_empty() {
        return Duration::ZERO;
    }
    v.iter().sum::<Duration>() / u32::try_from(v.len()).unwrap()
}

fn rss_mb() -> f64 {
    let status = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    status
        .lines()
        .find_map(|l| l.strip_prefix("VmRSS:"))
        .and_then(|v| v.trim().trim_end_matches(" kB").parse::<f64>().ok())
        .map_or(0.0, |kb| kb / 1024.0)
}

fn log_mb(dir: &Path) -> f64 {
    std::fs::metadata(dir.join("log")).map_or(0.0, |m| m.len() as f64 / 1e6)
}

fn heading(title: &str) {
    println!("\n## {title}\n");
}

// Scenarios.

const BUCKETS: [usize; 6] = [10_000, 100_000, 250_000, 500_000, 750_000, 1_000_000];

/// Walk and request-build cost per sample as one cursor's context grows.
fn depth() {
    heading("depth: one agent going forward, walk(at) at every sample (memory only)");
    println!("| shape | context | nodes on walk | walk | walk as JSON | turn (core, no read) |");
    println!("|---|---|---|---|---|---|");
    for shape in [TINY, CHATTY, BULKY] {
        let (mut rig, c) = Rig::new(shape, Read::Json);
        let mut agent = Agent::new(c, 0);
        let mut last = 0;
        for target in BUCKETS {
            rig.stats = Stats::default();
            let mut turns = 0u32;
            while rig.tokens_at(c) < target {
                agent.turns = 1;
                agent.run(&mut rig);
                rig.m.session.outbox.clear(); // as a store would drain it
                turns += 1;
            }
            let s = &rig.stats.samples;
            let tail = &s[s.len().saturating_sub(10)..];
            let walk = mean(&tail.iter().map(|x| x.1).collect::<Vec<_>>());
            let json = mean(&tail.iter().map(|x| x.2).collect::<Vec<_>>());
            let core = rig.stats.core / turns.max(1);
            let at = rig.m.session.at(c).unwrap();
            let nodes = rig.m.session.walk(at).len();
            println!(
                "| {} | {}k→{}k | {nodes} | {} | {} | {} |",
                shape.name,
                last / 1000,
                target / 1000,
                us(walk),
                us(json),
                us(core),
            );
            last = target;
        }
    }
}

/// The same agent through the store: what a turn costs on disk, and what the
/// finished session costs to open.
fn store() {
    heading("store: one agent through the store, then reopen");
    println!(
        "| shape | context | create over first half | turns | turn | syncing saves per turn | save p50 | save p99 | log | open | RSS after open |"
    );
    println!("|---|---|---|---|---|---|---|---|---|---|---|");
    for shape in [CHATTY, BULKY] {
        for target in [100_000, 1_000_000] {
            let dir = tempfile::tempdir().unwrap();
            let (mut rig, c) = Rig::new(shape, Read::Walk);
            rig.grow(c, target / 2);
            let create = rig.persist(dir.path());
            rig.stats = Stats::default();
            let t = Instant::now();
            let mut agent = Agent::new(c, 0);
            let mut turns = 0u32;
            while rig.tokens_at(c) < target {
                agent.turns = 1;
                agent.run(&mut rig);
                turns += 1;
            }
            let turn = t.elapsed() / turns;
            let syncs = std::mem::take(&mut rig.stats.syncs);
            drop(rig);
            let t = Instant::now();
            let (store, session) = Store::open(dir.path()).unwrap();
            let open = t.elapsed();
            println!(
                "| {} | {}k (second half stored turn by turn) | {} | {turns} | {} | {:.1} | {} | {} | {:.1} MB | {} | {:.0} MB |",
                shape.name,
                target / 1000,
                us(create),
                us(turn),
                syncs.len() as f64 / f64::from(turns),
                us(pct(syncs.clone(), 50)),
                us(pct(syncs, 99)),
                log_mb(dir.path()),
                us(open),
                rss_mb(),
            );
            drop((store, session));
        }
    }
}

/// Go forward, and every `every` turns jump back to a random recent node:
/// a go-to on the same cursor (a branch), then keep going from there.
fn jump() {
    heading("jump: forward with a go-to to a random recent node every 5 turns (1M tokens, chatty)");
    let (mut rig, c) = Rig::new(CHATTY, Read::Walk);
    rig.grow(c, 1_000_000);
    let mut seed = 0x5eed;
    let mut agent = Agent::new(c, 0);
    let (mut go, mut first) = (vec![], vec![]);
    for _ in 0..40 {
        agent.turns = 5;
        agent.run(&mut rig);
        let to = rig.recent(c, 200, &mut seed);
        let t = Instant::now();
        rig.m.go_to(c, to, "bench".into(), rig.now).unwrap();
        go.push(t.elapsed());
        rig.stats.samples.clear();
        agent.turns = 1;
        agent.run(&mut rig);
        first.push(rig.stats.samples[0].1);
    }
    let nodes = rig.m.session.walk(rig.m.session.at(c).unwrap()).len();
    println!("| go-to | first walk after it | nodes on walk | nodes in session |");
    println!("|---|---|---|---|");
    println!(
        "| {} (p99 {}) | {} (p99 {}) | {nodes} | {} |",
        us(pct(go.clone(), 50)),
        us(pct(go, 99)),
        us(pct(first.clone(), 50)),
        us(pct(first, 99)),
        node_count(&rig.m.session),
    );
}

/// `k` agents, each its own session and store at ~1M tokens, on `k` threads,
/// running flat out (the provider answers at once) with a go-to back to a
/// random recent node every 4 turns.
fn parallel() {
    heading(
        "parallel: k sessions at 1M tokens (chatty), one thread each, flat out, go-to every 4 turns",
    );
    println!(
        "| k | prebuild (all) | turns/s (all) | turn p50 | save p50 | save p99 | sample read p99 (walk+JSON) | RSS |"
    );
    println!("|---|---|---|---|---|---|---|---|");
    let base = {
        let (mut rig, c) = Rig::new(CHATTY, Read::Walk);
        rig.grow(c, 1_000_000);
        rig.m.session
    };
    for k in [1usize, 2, 4, 8, 16, 32] {
        let dirs: Vec<_> = (0..k).map(|_| tempfile::tempdir().unwrap()).collect();
        let t = Instant::now();
        let mut rigs: Vec<Rig> = dirs
            .iter()
            .map(|d| {
                let (mut rig, _) = Rig::new(CHATTY, Read::Json);
                rig.m = Machine::new(base.clone());
                rig.persist(d.path());
                rig
            })
            .collect();
        let prebuild = t.elapsed();
        let rss = rss_mb();
        let t = Instant::now();
        let per: Vec<(Vec<Duration>, Stats)> = std::thread::scope(|scope| {
            let handles: Vec<_> = rigs
                .iter_mut()
                .enumerate()
                .map(|(i, rig)| scope.spawn(move || forward_and_jump(rig, 0, 24, i as u64 + 1)))
                .collect();
            handles.into_iter().map(|h| h.join().unwrap()).collect()
        });
        let wall = t.elapsed();
        let turns: usize = per.iter().map(|(t, _)| t.len()).sum();
        let all = |f: fn(&(Vec<Duration>, Stats)) -> Vec<Duration>| -> Vec<Duration> {
            per.iter().flat_map(f).collect()
        };
        let turn = all(|p| p.0.clone());
        let saves = all(|p| p.1.syncs.clone());
        let reads = all(|p| p.1.samples.iter().map(|s| s.1 + s.2).collect());
        println!(
            "| {k} | {} | {:.0} | {} | {} | {} | {} | {rss:.0} MB |",
            us(prebuild),
            turns as f64 / wall.as_secs_f64(),
            us(pct(turn, 50)),
            us(pct(saves.clone(), 50)),
            us(pct(saves, 99)),
            us(pct(reads, 99)),
        );
    }
}

/// `turns` turns on `c`, a go-to to a random recent node every 4. Per-turn times.
fn forward_and_jump(rig: &mut Rig, c: CursorId, turns: u32, seed: u64) -> (Vec<Duration>, Stats) {
    let mut seed = seed.wrapping_mul(0x9e37_79b9_7f4a_7c15) | 1;
    let mut agent = Agent::new(c, 0);
    let mut times = vec![];
    for i in 0..turns {
        if i % 4 == 3 {
            let to = rig.recent(c, 200, &mut seed);
            rig.m.go_to(c, to, "bench".into(), rig.now).unwrap();
            rig.save();
        }
        let t = Instant::now();
        agent.turns = 1;
        agent.run(rig);
        times.push(t.elapsed());
    }
    (times, std::mem::take(&mut rig.stats))
}

/// One session at 1M tokens; `k` cursors open on random recent nodes and run
/// interleaved job by job, as one driver would run them, each through the
/// one store. The log takes every branch.
fn cursors() {
    heading("cursors: k cursors branched off recent nodes of one 1M-token session, interleaved");
    println!(
        "| k | turns/s | save p50 | save p99 | saves with >1 Sync | walk p99 | log after | open after | nodes |"
    );
    println!("|---|---|---|---|---|---|---|---|---|");
    let base = {
        let (mut rig, c) = Rig::new(CHATTY, Read::Walk);
        rig.grow(c, 1_000_000);
        rig.m.session
    };
    for k in [1usize, 4, 16, 64] {
        let dir = tempfile::tempdir().unwrap();
        let (mut rig, _) = Rig::new(CHATTY, Read::Walk);
        rig.m = Machine::new(base.clone());
        rig.persist(dir.path());
        let mut seed = 0x00c0_ffee;
        let mut agents: Vec<Agent> = (0..k)
            .map(|_| {
                let at = rig.recent(0, 400, &mut seed);
                let c = rig.m.open(at, Author::Core, rig.now).unwrap();
                Agent::new(c, 8)
            })
            .collect();
        rig.m.session.sync();
        rig.save();
        rig.stats = Stats::default();
        let t = Instant::now();
        let mut active = agents.len();
        while active > 0 {
            active = agents
                .iter_mut()
                .map(|a| a.tick(&mut rig))
                .filter(|&on| on)
                .count();
        }
        let wall = t.elapsed();
        let walks: Vec<Duration> = rig.stats.samples.iter().map(|s| s.1).collect();
        let syncs = std::mem::take(&mut rig.stats.syncs);
        let multi = rig.stats.multi_sync;
        let nodes = node_count(&rig.m.session);
        drop(rig);
        let t = Instant::now();
        let (store, session) = Store::open(dir.path()).unwrap();
        let open = t.elapsed();
        println!(
            "| {k} | {:.0} | {} | {} | {multi} | {} | {:.1} MB | {} | {nodes} |",
            f64::from(8 * u32::try_from(k).unwrap()) / wall.as_secs_f64(),
            us(pct(syncs.clone(), 50)),
            us(pct(syncs, 99)),
            us(pct(walks, 99)),
            log_mb(dir.path()),
            us(open),
        );
        drop((store, session));
    }
}

/// Classify the walk, copy the kept nodes as a new line under the system
/// prompt, summarize that line into one node on top, move there, go on.
/// Repeated each time the context reaches 1M tokens.
fn compact() {
    heading("compact: at 1M tokens classify, copy, summarize, move; 5 cycles (chatty, stored)");
    println!(
        "| cycle | before | after | classify | copy (nodes) | save copy | summarize | pause total | next sample walk | log |"
    );
    println!("|---|---|---|---|---|---|---|---|---|---|");
    let dir = tempfile::tempdir().unwrap();
    let (mut rig, c) = Rig::new(CHATTY, Read::Walk);
    rig.grow(c, 1_000_000);
    rig.persist(dir.path());
    for cycle in 1..=5 {
        if cycle > 1 {
            rig.grow(c, 1_000_000);
        }
        let before = rig.tokens_at(c);
        let (kept, classify) = classify(&rig.m.session, c);
        let t = Instant::now();
        let tip = copy(&mut rig.m.session, &kept, rig.now);
        let copy_time = t.elapsed();
        rig.m.session.sync();
        let t = Instant::now();
        rig.save();
        let save = t.elapsed();
        let t = Instant::now();
        let summary = summarize(&rig.m.session, tip);
        let s = &mut rig.m.session;
        let top = s
            .append(
                Some(tip),
                Author::Extension("compact".into()),
                Body::User(summary),
                vec![],
                rig.now,
            )
            .unwrap();
        s.move_to(c, top, Author::Extension("compact".into()), rig.now)
            .unwrap();
        s.sync();
        rig.save();
        let summarize = t.elapsed();
        rig.stats.samples.clear();
        let mut agent = Agent::new(c, 1);
        agent.run(&mut rig);
        println!(
            "| {cycle} | {}k | {}k | {} | {} ({}) | {} | {} | {} | {} | {:.1} MB |",
            before / 1000,
            rig.tokens_at(c) / 1000,
            us(classify),
            us(copy_time),
            kept.len(),
            us(save),
            us(summarize),
            us(classify + copy_time + save + summarize),
            us(rig.stats.samples[0].1),
            log_mb(dir.path()),
        );
    }
    drop(rig);
    let t = Instant::now();
    let (_store, session) = Store::open(dir.path()).unwrap();
    println!(
        "\nOpen after 5 cycles: {} for {} nodes; RSS {:.0} MB.",
        us(t.elapsed()),
        node_count(&session),
        rss_mb()
    );
}

/// A mock classifier: reads every byte of every node on the walk, keeps the
/// prefix, the newest quarter, and one older tool round in ten. A tool round
/// is kept whole: its assistant node with its results.
fn classify(s: &Session, c: CursorId) -> (Vec<Body>, Duration) {
    let start = Instant::now();
    let walk = s.walk(s.at(c).unwrap());
    let recent = walk.len() * 3 / 4;
    let mut kept = vec![];
    let mut i = 2; // past Config and System
    while i < walk.len() {
        let node = walk[i];
        let round = matches!(
            &node.body,
            Body::Assistant {
                stop: Stop::Tools,
                ..
            }
        );
        let len = if round { 2 } else { 1 };
        let score = std::hint::black_box(byte_score(&node.body));
        if i >= recent || (round && score.is_multiple_of(10)) || matches!(node.body, Body::User(_))
        {
            kept.extend(
                walk[i..(i + len).min(walk.len())]
                    .iter()
                    .map(|n| n.body.clone()),
            );
        }
        i += len;
    }
    (kept, start.elapsed())
}

fn byte_score(b: &Body) -> u64 {
    let json = serde_json::to_vec(b).unwrap();
    json.iter()
        .fold(0u64, |h, x| h.rotate_left(5) ^ u64::from(*x))
}

/// The kept bodies as a new line under the system prompt (node 1). Its tip.
fn copy(s: &mut Session, kept: &[Body], now: u64) -> NodeId {
    let by = Author::Extension("compact".into());
    kept.iter().fold(1, |parent, body| {
        s.append(Some(parent), by.clone(), body.clone(), vec![], now)
            .unwrap()
    })
}

/// A mock summary of the walk at `tip`: reads it all as JSON, returns 8 KB.
fn summarize(s: &Session, tip: NodeId) -> String {
    let json = serde_json::to_vec(&s.walk(tip)).unwrap();
    std::hint::black_box(json.len());
    text(8_000)
}

/// Point lookups on a big, broad session: a node by id, `config_at`, `lca`.
fn lookup() {
    heading("lookup: a node by id, config_at, lca (a 1M-token session with 64 branches)");
    let (mut rig, _) = Rig::new(TINY, Read::Walk);
    rig.grow(0, 1_000_000);
    let mut seed = 0xfeed;
    let mut tips = vec![];
    for _ in 0..64 {
        let at = rig.recent(0, 2_000, &mut seed);
        let c = rig.m.open(at, Author::Core, rig.now).unwrap();
        Agent::new(c, 2).run(&mut rig);
        tips.push(rig.m.session.at(c).unwrap());
    }
    let s = &rig.m.session;
    let total = node_count(s);
    let n = s.walk(tips[0]).len() as u64;
    let time = |f: &mut dyn FnMut(u64)| {
        let t = Instant::now();
        for i in 0..10_000u64 {
            f(i);
        }
        t.elapsed() / 10_000
    };
    let mut out = String::new();
    let by_id = time(&mut |i| drop(std::hint::black_box(s.node(i * 7919 % n))));
    let config = time(&mut |i| drop(s.config_at(tips[(i % 64) as usize])));
    let lca = time(&mut |i| drop(s.lca(tips[(i % 64) as usize], tips[(i * 13 % 64) as usize])));
    writeln!(
        out,
        "| node(id) | config_at(tip) | lca(tip, tip) | depth | nodes |"
    )
    .unwrap();
    writeln!(out, "|---|---|---|---|---|").unwrap();
    writeln!(
        out,
        "| {} | {} | {} | {n} | {total} |",
        us(by_id),
        us(config),
        us(lca)
    )
    .unwrap();
    print!("{out}");
}

fn main() {
    let args: Vec<String> = std::env::args()
        .skip(1)
        .filter(|a| !a.starts_with('-'))
        .collect();
    let all: [(&str, fn()); 7] = [
        ("depth", depth),
        ("lookup", lookup),
        ("jump", jump),
        ("store", store),
        ("compact", compact),
        ("cursors", cursors),
        ("parallel", parallel),
    ];
    println!(
        "# tau tree bench ({} cpus)",
        std::thread::available_parallelism().map_or(0, usize::from)
    );
    for (name, run) in all {
        if args.is_empty() || args.iter().any(|a| a == name) {
            run();
        }
    }
}
