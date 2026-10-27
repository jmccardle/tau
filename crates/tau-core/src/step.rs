//! One cursor's step: the phase loop of model.md §6 as a sans-IO machine.
//! `next_job` says what to do; the driver does it and `feed`s the result.

use crate::tree::{Author, Body, CursorId, Error, ItemData, ItemId, Next, Outcome, Session, calls};

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
    },
    Sample,
    /// Each call: `pre_tool`, the tool, `pre_result`, in the driver.
    RunCalls(Vec<RunCall>),
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

#[derive(Debug)]
pub struct Step {
    pub cursor: CursorId,
    pos: Pos,
    asked: bool,
    aborted: bool,
    committed: bool,
    started: u64,
    seen: u64,
    draft: Option<Draft>,
    error: Option<String>,
    ending: Ending,
    /// The running batch's calls in call order, each with its outcome once fed.
    batch: Vec<(String, Option<(Outcome, Author)>)>,
}

impl Step {
    /// A wake. `asked`: a frontend's `Session.step`, which runs phases with auto-step off.
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
            draft: None,
            error: None,
            ending: Ending::Done,
            batch: vec![],
        }
    }

    pub fn next_job(&mut self, s: &mut Session) -> Result<Job, Error> {
        loop {
            let on = !self.aborted && (self.asked || s.auto[&self.cursor]);
            let hooks = |point, draft| Job::Hooks {
                point,
                draft,
                ending: None,
            };
            return Ok(match self.pos {
                Pos::PreStep => hooks(Hook::PreStep, None),
                Pos::Input => match s
                    .items(self.cursor)
                    .find(|i| matches!(i.data, ItemData::Input { .. }))
                    .filter(|_| on)
                {
                    Some(item) => {
                        let ItemData::Input { client, text } = &item.data else {
                            unreachable!()
                        };
                        self.draft = Some(Draft {
                            body: Body::User(text.clone()),
                            delivers: vec![item.id],
                            by: Author::Frontend(client.clone()),
                        });
                        hooks(Hook::PreInput, self.draft.clone())
                    }
                    None => {
                        self.pos = Pos::PreSample;
                        continue;
                    }
                },
                Pos::PreSample if on && s.next(self.cursor) == Next::Sample => {
                    hooks(Hook::PreSample, None)
                }
                Pos::PreSample => {
                    self.pos = Pos::Tools;
                    continue;
                }
                Pos::Sample if self.draft.is_some() => {
                    self.pos = Pos::PreReply;
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
                Pos::Tools if on && s.next(self.cursor) == Next::RunTools => {
                    let run = self.start_batch(s)?;
                    self.pos = Pos::Calls;
                    Job::RunCalls(run)
                }
                Pos::Tools => {
                    self.pos = Pos::PostStep;
                    continue;
                }
                Pos::Calls => Job::Pending,
                Pos::PostStep => {
                    s.sync();
                    Job::Hooks {
                        point: Hook::PostStep,
                        draft: None,
                        ending: Some(self.ending.clone()),
                    }
                }
                Pos::Throttle => Job::Wait {
                    until: self.started + THROTTLE,
                },
                Pos::Rest => Job::Rest,
            });
        }
    }

    pub fn feed(&mut self, s: &mut Session, fed: Fed, now: u64) -> Result<(), Error> {
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
            (Pos::PreStep, Fed::Hooks { .. }) => self.pos = Pos::Input,
            (Pos::Input | Pos::PreReply, Fed::Hooks { draft, .. }) => {
                let d = draft.or(self.draft.take()).expect("a draft at a pre hook");
                s.write(self.cursor, d.body, d.delivers, d.by, now)?;
                self.committed = true;
                if self.pos == Pos::PreReply {
                    self.pos = Pos::PostReply;
                }
            }
            (Pos::PreSample, Fed::Hooks { draft, .. }) => {
                self.draft = draft;
                self.pos = Pos::Sample;
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
                self.pos = Pos::PreReply;
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
                None => self.pos = Pos::Tools,
            },
            (Pos::Calls, Fed::CallDone { call, outcome, by }) => {
                if let Some(slot) = self.batch.iter_mut().find(|c| c.0 == call) {
                    slot.1 = Some((outcome, by));
                }
                if self.batch.iter().all(|c| c.1.is_some()) {
                    self.join(s, now)?;
                }
            }
            (Pos::PostResult, Fed::Hooks { .. }) => self.pos = Pos::PostStep,
            (Pos::Throttle, Fed::Waited) => *self = Self::wake(s, self.cursor, false, now),
            (pos, fed) => panic!("{fed:?} fed at {pos:?}"),
        }
        Ok(())
    }

    /// Abort: auto-step off, no later effect. Returns true if the driver must
    /// cancel the effect in flight and feed what it got (partial reply, `Interrupted`s).
    pub fn abort(&mut self, s: &mut Session) -> bool {
        s.auto.insert(self.cursor, false);
        self.aborted = true;
        if self.ending == Ending::Done {
            self.ending = Ending::Aborted;
        }
        matches!(self.pos, Pos::Sample | Pos::Calls)
    }

    pub fn resting(&self) -> bool {
        self.pos == Pos::Rest
    }

    fn end(&mut self, s: &mut Session, ending: Ending) {
        s.auto.insert(self.cursor, false);
        if self.ending == Ending::Done {
            self.ending = ending;
        }
        self.pos = Pos::PostStep;
    }

    /// After `post_step`: sync what it wrote, then rest, throttle, or restart.
    fn after(&mut self, s: &mut Session, now: u64) {
        s.sync();
        let work = s.next(self.cursor) != Next::Wait
            || s.items(self.cursor)
                .any(|i| matches!(i.data, ItemData::Input { .. }));
        let changed = s.changes.get(&self.cursor).copied().unwrap_or(0) != self.seen;
        let again = self.ending == Ending::Done && ((s.auto[&self.cursor] && work) || changed);
        self.pos = match (again, self.committed) {
            (false, _) => Pos::Rest,
            (true, false) => Pos::Throttle,
            (true, true) => {
                *self = Self::wake(s, self.cursor, false, now);
                Pos::PreStep
            }
        };
    }

    fn start_batch(&mut self, s: &Session) -> Result<Vec<RunCall>, Error> {
        let run: Vec<RunCall> = calls(&s.node(s.at(self.cursor)?)?.body)
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

    /// The join: one `Results` node, in call order.
    fn join(&mut self, s: &mut Session, now: u64) -> Result<(), Error> {
        let outcomes = std::mem::take(&mut self.batch)
            .into_iter()
            .map(|(c, o)| {
                let (o, by) = o.expect("joined");
                (c, o, by)
            })
            .collect();
        s.write(
            self.cursor,
            Body::Results(outcomes),
            vec![],
            Author::Tools,
            now,
        )?;
        self.committed = true;
        self.pos = Pos::PostResult;
        Ok(())
    }
}
