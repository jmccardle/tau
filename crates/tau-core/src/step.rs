//! One cursor's step: the phase loop of model.md §6 as a sans-IO machine.
//! `next_job` says what to do; the driver does it and `feed`s the result.

use crate::transitions;
use crate::tree::{
    Author, Body, CursorId, Error, Item, ItemData, ItemId, Next, NodeId, Outcome, Session, calls,
};

pub const THROTTLE: u64 = 250;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Hook {
    PreStep,
    PreInput,
    PreSample,
    PreReply,
    PostReply,
    PostResult,
    PostStep,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Ending {
    Done,
    Halted(String),
    Failed(String),
    Aborted,
}

/// What a phase will commit. Pre hooks edit it; core writes it under `at`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Draft {
    pub body: Body,
    pub delivers: Vec<ItemId>,
    pub by: Author,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunCall {
    pub id: String,
    pub tool: String,
    pub args_raw: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Job {
    Hooks {
        point: Hook,
        draft: Option<Draft>,
        ending: Option<Ending>,
        /// Unique to this job, given by the `Machine` (0 from a bare `Step`): a
        /// hook's own-cursor calls carry it, and are refused once the job is fed.
        serial: u64,
    },
    Sample,
    /// Each call: `pre_tool`, the tool, `pre_result`, in the driver. `node` is
    /// the `Assistant` node holding the batch, which `at` may have moved past
    /// (a `Custom` node written after the reply).
    RunCalls {
        node: NodeId,
        calls: Vec<RunCall>,
    },
    Pending,
    Wait {
        until: u64,
    },
    Rest,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Fed {
    /// `draft`: the edited draft; from `pre_sample`, a reply that answers the sample.
    Hooks {
        stop: Option<String>,
        draft: Option<Draft>,
    },
    HookFailed(String),
    Sampled {
        reply: Option<Draft>,
        error: Option<String>,
    },
    /// `by`: `Tools`, or the extension whose `pre_tool` answered the call or
    /// whose `pre_result` last edited the outcome.
    CallDone {
        call: String,
        outcome: Outcome,
        by: Author,
    },
    Waited,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Pos {
    PreStep,
    Input,
    PreSample,
    Sample,
    PreReply,
    PostReply,
    Tools,
    Calls,
    PostResult,
    PostStep,
    Throttle,
    Rest,
}

/// The job last handed out and not yet answered: the only kind of feed the
/// step accepts.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Out {
    Nothing,
    Hook,
    Sample,
    /// The batch: stays out through `Pending` until its last call is fed.
    Calls,
    Wait,
}

#[derive(Debug)]
pub struct Step {
    pub cursor: CursorId,
    pos: Pos,
    asked: bool,
    aborted: bool,
    committed: bool,
    started: u64,
    seen: u64,
    /// `Session::moves.len()` when this step woke.
    first_move: usize,
    out: Out,
    /// The input item the draft in `Pos::Input` offers.
    offered: Option<ItemId>,
    /// The serial of the cursor hook job last handed out.
    serial: u64,
    draft: Option<Draft>,
    /// The input phase's work: the inputs pending when it began, oldest
    /// first, each offered once. `None` until the phase begins.
    inputs: Option<Vec<ItemId>>,
    error: Option<String>,
    ending: Ending,
    /// The running batch's calls in call order, each with its outcome once fed.
    batch: Vec<(String, Option<(Outcome, Author)>)>,
}

impl Step {
    /// A wake. `asked`: a frontend's `Session.step`, which runs phases with auto-step off.
    #[must_use]
    pub fn wake(s: &Session, cursor: CursorId, asked: bool, now: u64) -> Self {
        let seen = s.changes.get(&cursor).copied().unwrap_or(0);
        Self {
            cursor,
            pos: Pos::PreStep,
            asked,
            aborted: false,
            committed: false,
            started: now,
            seen,
            first_move: s.moves.len(),
            out: Out::Nothing,
            offered: None,
            serial: 0,
            draft: None,
            inputs: None,
            error: None,
            ending: Ending::Done,
            batch: vec![],
        }
    }

    /// The step's next job; `Rest` once it ends.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open.
    pub fn next_job(&mut self, s: &mut Session) -> Result<Job, Error> {
        let job = self.job(s)?;
        transitions::emit(self.pos, &job);
        match job {
            Job::Hooks { .. } => self.out = Out::Hook,
            Job::Sample => self.out = Out::Sample,
            Job::RunCalls { .. } => self.out = Out::Calls,
            Job::Wait { .. } => self.out = Out::Wait,
            Job::Pending => {}
            Job::Rest => self.out = Out::Nothing,
        }
        Ok(job)
    }

    fn job(&mut self, s: &mut Session) -> Result<Job, Error> {
        loop {
            let on = !self.aborted && (self.asked || s.auto[&self.cursor]);
            let hooks = |point, draft| Job::Hooks {
                point,
                draft,
                ending: None,
                serial: 0,
            };
            return Ok(match self.pos {
                Pos::PreStep => hooks(Hook::PreStep, None),
                Pos::Input => {
                    let cursor = self.cursor;
                    let queue = self.inputs.get_or_insert_with(|| {
                        let inputs = s
                            .items(cursor)
                            .filter(|i| matches!(i.data, ItemData::Input { .. }));
                        if on {
                            inputs.map(|i| i.id).collect()
                        } else {
                            vec![]
                        }
                    });
                    // An input a hook already delivered is gone from the pad.
                    let next = loop {
                        if queue.is_empty() {
                            break None;
                        }
                        let id = queue.remove(0);
                        if let Some(Item {
                            data: ItemData::Input { client, text },
                            ..
                        }) = s.items(cursor).find(|i| i.id == id)
                        {
                            break Some((id, client.clone(), text.clone()));
                        }
                    };
                    let Some((id, client, text)) = next else {
                        self.hop("no input to offer", Pos::PreSample);
                        continue;
                    };
                    self.offered = Some(id);
                    self.draft = Some(Draft {
                        body: Body::User(text),
                        delivers: vec![id],
                        by: Author::Frontend(client),
                    });
                    hooks(Hook::PreInput, self.draft.clone())
                }
                Pos::PreSample if on && s.next(self.cursor) == Next::Sample => {
                    hooks(Hook::PreSample, None)
                }
                Pos::PreSample => {
                    self.hop("sample does not apply", Pos::Tools);
                    continue;
                }
                Pos::Sample if self.draft.is_some() => {
                    self.hop("pre_sample draft answers the sample", Pos::PreReply);
                    continue;
                }
                Pos::Sample => Job::Sample,
                Pos::PreReply => hooks(Hook::PreReply, self.draft.clone()),
                // Post hooks run on data that is on disk.
                Pos::PostReply | Pos::PostResult => {
                    s.sync();
                    hooks(
                        if self.pos == Pos::PostReply {
                            Hook::PostReply
                        } else {
                            Hook::PostResult
                        },
                        None,
                    )
                }
                Pos::Tools => match s.deciding(self.cursor) {
                    Some((Next::RunTools, node)) if on => {
                        let calls = self.start_batch(s, node)?;
                        self.hop("tools apply", Pos::Calls);
                        Job::RunCalls { node, calls }
                    }
                    _ => {
                        self.hop("tools do not apply", Pos::PostStep);
                        continue;
                    }
                },
                Pos::Calls => Job::Pending,
                Pos::PostStep => {
                    s.sync();
                    Job::Hooks {
                        point: Hook::PostStep,
                        draft: None,
                        ending: Some(self.ending.clone()),
                        serial: 0,
                    }
                }
                Pos::Throttle => Job::Wait {
                    until: self.started + THROTTLE,
                },
                Pos::Rest => Job::Rest,
            });
        }
    }

    /// Feed the result of the job last handed out.
    ///
    /// # Errors
    ///
    /// What a commit's `write` or `append` returns (`NoSuchCursor`);
    /// `WrongFeed` if `fed` does not answer the job that is out (none, or one
    /// of another kind, a call not in the batch, or a call already answered), with
    /// nothing changed. Core checks no edit a hook made, only the tree's rules.
    pub fn feed(&mut self, s: &mut Session, fed: Fed, now: u64) -> Result<(), Error> {
        if !self.answers(&fed) {
            return Err(Error::WrongFeed(self.cursor));
        }
        transitions::fed(&fed);
        self.out = if self.out == Out::Calls && matches!(fed, Fed::CallDone { .. }) {
            Out::Calls
        } else {
            Out::Nothing
        };
        match (self.pos, fed) {
            (Pos::PostStep, Fed::Hooks { stop, .. }) => {
                if stop.is_some() {
                    s.auto.insert(self.cursor, false);
                }
                self.after(s, now);
            }
            (Pos::PostStep, Fed::HookFailed(_)) => self.after(s, now),
            (_, Fed::HookFailed(e)) => {
                self.draft = None;
                self.end(s, Ending::Failed(e));
            }
            (
                pos,
                Fed::Hooks {
                    stop: Some(reason), ..
                },
            ) => {
                if let (Pos::Input, Some(d)) = (pos, self.draft.take()) {
                    s.remove_item(d.delivers[0]);
                    if let Author::Frontend(client) = d.by {
                        s.notices.push((client, reason.clone()));
                    }
                }
                self.draft = None;
                self.end(s, Ending::Halted(reason));
            }
            (Pos::PreStep, Fed::Hooks { .. }) => self.go(Pos::Input),
            (Pos::Input | Pos::PreReply, Fed::Hooks { draft, .. }) => {
                let d = draft
                    .or(self.draft.take())
                    .ok_or(Error::WrongFeed(self.cursor))?;
                self.commit(s, d, now);
            }
            (Pos::PreSample, Fed::Hooks { draft, .. }) => {
                self.draft = draft;
                self.go(Pos::Sample);
            }
            (
                Pos::Sample,
                Fed::Sampled {
                    reply: Some(reply),
                    error,
                },
            ) => {
                self.draft = Some(reply);
                self.error = error;
                self.go(Pos::PreReply);
            }
            (Pos::Sample, Fed::Sampled { reply: None, error }) => match error {
                Some(e) => {
                    let at = s.at(self.cursor)?;
                    s.append(
                        Some(at),
                        Author::Core,
                        Body::Custom {
                            kind: "sample_error".into(),
                            data: e.clone(),
                        },
                        vec![],
                        now,
                    )?;
                    self.end(s, Ending::Failed(e));
                }
                None => self.end(s, Ending::Aborted),
            },
            (Pos::PostReply, Fed::Hooks { .. }) => match self.error.take() {
                Some(e) => self.end(s, Ending::Failed(e)),
                None => self.go(Pos::Tools),
            },
            (Pos::Calls, Fed::CallDone { call, outcome, by }) => {
                if let Some(slot) = self.batch.iter_mut().find(|c| c.0 == call) {
                    slot.1 = Some((outcome, by));
                }
                self.join(s, now)?;
                if self.pos == Pos::Calls {
                    self.go(Pos::Calls); // calls still out
                }
            }
            (Pos::PostResult, Fed::Hooks { .. }) => self.go(Pos::PostStep),
            (Pos::Throttle, Fed::Waited) => {
                transitions::go(Pos::Throttle, Pos::PreStep);
                *self = Self::wake(s, self.cursor, false, now);
            }
            _ => return Err(Error::WrongFeed(self.cursor)),
        }
        Ok(())
    }

    /// Whether `fed` answers the job that is out. Decided before anything changes.
    fn answers(&self, fed: &Fed) -> bool {
        match (self.out, self.pos, fed) {
            (Out::Hook, _, Fed::Hooks { .. } | Fed::HookFailed(_))
            | (Out::Sample, Pos::Sample, Fed::Sampled { .. })
            | (Out::Wait, Pos::Throttle, Fed::Waited) => true,
            (Out::Calls, Pos::Calls, Fed::CallDone { call, .. }) => {
                self.batch.iter().any(|c| c.0 == *call && c.1.is_none())
            }
            _ => false,
        }
    }

    /// Abort: auto-step off, no later effect. Returns true if the driver must
    /// cancel the effect in flight (or, in the throttle, its sleeper) and feed what it got (partial reply, `Interrupted`s).
    pub fn abort(&mut self, s: &mut Session) -> bool {
        s.auto.insert(self.cursor, false);
        self.aborted = true;
        if self.ending == Ending::Done {
            self.ending = Ending::Aborted;
        }
        let cancel = matches!(self.pos, Pos::Sample | Pos::Calls | Pos::Throttle);
        if self.pos == Pos::Throttle {
            transitions::event("abort");
            self.go(Pos::Rest); // the scheduled restart is cancelled
        }
        cancel
    }

    /// True while a cursor hook job is out: a frontend's go-to is refused.
    #[must_use]
    pub fn in_hook(&self) -> bool {
        self.out == Out::Hook
    }

    /// Write a phase's draft under `at`. Core validates the draft the hooks left: any
    /// refusal from `write` (it changes nothing when it refuses) fails the step, its
    /// reason the error's text.
    fn commit(&mut self, s: &mut Session, d: Draft, now: u64) {
        let mut delivers = d.delivers;
        if self.pos == Pos::Input && self.delivered_by_a_hook(s, &mut delivers) {
            // The hook's own write stood in for this commit: it counts as one.
            self.committed = true;
            transitions::event("input delivered by a hook");
            self.go(Pos::Input);
            return;
        }
        match s.write(self.cursor, d.body, delivers, d.by, now) {
            Ok(_) => {
                self.committed = true;
                if self.pos == Pos::PreReply {
                    self.go(Pos::PostReply);
                } else {
                    self.go(self.pos);
                }
            }
            Err(e) => {
                transitions::event("commit refused");
                self.draft = None;
                self.end(s, Ending::Failed(e.to_string()));
            }
        }
    }

    /// A hook may have delivered the offered input itself. Then that one id is
    /// taken out of `delivers` (every other id goes to `write`, which refuses one
    /// that is not pending); true when that leaves nothing to commit.
    fn delivered_by_a_hook(&mut self, s: &Session, delivers: &mut Vec<ItemId>) -> bool {
        let Some(id) = self.offered.take() else {
            return false;
        };
        if s.items(self.cursor).any(|i| i.id == id) {
            return false;
        }
        let was = delivers.len();
        delivers.retain(|d| *d != id);
        was != delivers.len() && delivers.is_empty()
    }

    /// A move inside `job` that no job or feed causes: logged under `why`.
    fn hop(&mut self, why: &str, to: Pos) {
        transitions::event(why);
        self.go(to);
    }

    /// Move to `to`, logging the change under the current event.
    fn go(&mut self, to: Pos) {
        transitions::go(self.pos, to);
        self.pos = to;
    }

    /// The machine numbers the hook job it just took from `next_job`.
    pub fn set_serial(&mut self, serial: u64) {
        self.serial = serial;
    }

    /// The serial of the cursor hook job last handed out (valid while `in_hook`).
    #[must_use]
    pub fn serial(&self) -> u64 {
        self.serial
    }

    #[must_use]
    pub fn resting(&self) -> bool {
        self.pos == Pos::Rest
    }

    /// `cursor.moves(since_step=True)`: this cursor's moves since the step woke.
    pub fn moves<'a>(&self, s: &'a Session) -> impl Iterator<Item = &'a crate::tree::Move> {
        s.moves_of(self.cursor, self.first_move)
    }

    fn end(&mut self, s: &mut Session, ending: Ending) {
        s.auto.insert(self.cursor, false);
        if self.ending == Ending::Done {
            self.ending = ending;
        }
        self.go(Pos::PostStep);
    }

    /// After `post_step`: sync what it wrote, then rest, throttle, or restart.
    fn after(&mut self, s: &mut Session, now: u64) {
        s.sync();
        if self.committed {
            let max = s
                .at(self.cursor)
                .and_then(|a| s.config_at(a))
                .ok()
                .and_then(|c| c.max_steps);
            let taken = s.taken.entry(self.cursor).or_default(); // rules: allow(entry): BTreeMap API
            *taken += 1;
            if max.is_some_and(|m| *taken >= m) {
                s.auto.insert(self.cursor, false);
            }
        }
        let work = s.next(self.cursor) != Next::Wait
            || s.items(self.cursor)
                .any(|i| matches!(i.data, ItemData::Input { .. }));
        let changed = s.changes.get(&self.cursor).copied().unwrap_or(0) != self.seen;
        let again = self.ending == Ending::Done && ((s.auto[&self.cursor] && work) || changed);
        let to = match (again, self.committed) {
            (false, _) => Pos::Rest,
            (true, false) => Pos::Throttle,
            (true, true) => Pos::PreStep,
        };
        transitions::ending(&self.ending);
        self.go(to);
        if to == Pos::PreStep {
            *self = Self::wake(s, self.cursor, false, now);
        }
    }

    fn start_batch(&mut self, s: &Session, node: NodeId) -> Result<Vec<RunCall>, Error> {
        let run: Vec<RunCall> = calls(&s.node(node)?.body)
            .into_iter()
            .map(|(id, tool, args_raw)| RunCall {
                id: id.into(),
                tool: tool.into(),
                args_raw: args_raw.into(),
            })
            .collect();
        self.batch = run.iter().map(|c| (c.id.clone(), None)).collect();
        Ok(run)
    }

    /// The join, once every call has its outcome: one `Results` node, in call order.
    fn join(&mut self, s: &mut Session, now: u64) -> Result<(), Error> {
        let Some(outcomes) = self
            .batch
            .iter()
            .map(|(c, o)| o.clone().map(|(o, by)| (c.clone(), o, by)))
            .collect::<Option<Vec<_>>>()
        else {
            return Ok(());
        };
        self.batch.clear();
        s.write(
            self.cursor,
            Body::Results(outcomes),
            vec![],
            Author::Tools,
            now,
        )?;
        self.committed = true;
        self.go(Pos::PostResult);
        Ok(())
    }
}
