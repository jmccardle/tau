//! One cursor's step: the phase loop of model.md §6 as a sans-IO machine.
//! `next_job` says what to do; the driver does it and `answer`s the result.

use crate::transitions;
use crate::tree::{
    Author, Body, CursorId, Error, Item, ItemData, ItemId, Next, NodeId, Outcome, Session, calls,
};

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
    /// Each call: `pre_tool`, the tool, `pre_result`, in the driver. `node` is
    /// the `Assistant` node holding the batch, which `at` may have moved past
    /// (a `Custom` node written after the reply).
    RunCalls {
        node: NodeId,
        calls: Vec<RunCall>,
    },
    Pending,
    Rest,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Answer {
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
    Rest,
}

fn hooks(point: Hook, draft: Option<Draft>) -> Job {
    Job::Hooks {
        point,
        draft,
        ending: None,
    }
}

#[derive(Debug)]
pub struct Step {
    pub cursor: CursorId,
    pos: Pos,
    asked: bool,
    seen: u64,
    /// `Session::moves.len()` when this step woke.
    first_move: usize,
    /// A job is out: handed out by `next_job` and not yet answered. Which kind
    /// is `pos`'s; the batch stays out through `Pending` until its last call is answered.
    out: bool,
    draft: Option<Draft>,
    /// The input phase's work: the inputs pending when it began, oldest
    /// first, each offered once. `None` until the phase begins.
    inputs: Option<Vec<ItemId>>,
    error: Option<String>,
    ending: Ending,
    /// The running batch's calls in call order, each with its outcome once answered.
    batch: Vec<(String, Option<(Outcome, Author)>)>,
}

impl Step {
    /// A wake. `asked`: a frontend's `Session.step`, which runs phases with auto-step off.
    #[must_use]
    pub fn wake(s: &Session, cursor: CursorId, asked: bool) -> Self {
        let seen = s.changes.get(&cursor).copied().unwrap_or(0);
        Self {
            cursor,
            pos: Pos::PreStep,
            asked,
            seen,
            first_move: s.moves.len(),
            out: false,
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
        self.out = job != Job::Rest;
        Ok(job)
    }

    fn job(&mut self, s: &mut Session) -> Result<Job, Error> {
        loop {
            // An aborted step's ending is no longer `Done`: no later phase applies.
            let on = self.ending == Ending::Done && (self.asked || s.auto[&self.cursor]);
            // `None`: the position hopped on without a job.
            let job = match self.pos {
                Pos::PreStep | Pos::Input | Pos::PreSample | Pos::Sample | Pos::PreReply => {
                    self.job_before_reply(s, on)
                }
                _ => self.job_after_reply(s, on)?,
            };
            if let Some(job) = job {
                return Ok(job);
            }
        }
    }

    /// The jobs up to the reply's hooks.
    fn job_before_reply(&mut self, s: &Session, on: bool) -> Option<Job> {
        match self.pos {
            Pos::Input => self.input_job(s, on),
            Pos::PreSample if on && s.next(self.cursor) == Next::Sample => {
                Some(hooks(Hook::PreSample, None))
            }
            Pos::PreSample => {
                self.hop("sample does not apply", Pos::Tools);
                None
            }
            Pos::Sample if self.draft.is_some() => {
                self.hop("pre_sample draft answers the sample", Pos::PreReply);
                None
            }
            Pos::Sample => Some(Job::Sample),
            Pos::PreReply => Some(hooks(Hook::PreReply, self.draft.clone())),
            Pos::PreStep => Some(hooks(Hook::PreStep, None)),
            Pos::PostReply
            | Pos::PostResult
            | Pos::Tools
            | Pos::Calls
            | Pos::PostStep
            | Pos::Rest => unreachable!("job_at sends these to job_after_reply"),
        }
    }

    /// The jobs from the reply's post hooks on.
    fn job_after_reply(&mut self, s: &mut Session, on: bool) -> Result<Option<Job>, Error> {
        Ok(match self.pos {
            // Post hooks run on data that is on disk.
            Pos::PostReply => {
                s.sync();
                Some(hooks(Hook::PostReply, None))
            }
            Pos::PostResult => {
                s.sync();
                Some(hooks(Hook::PostResult, None))
            }
            Pos::Tools => self.tools_job(s, on)?,
            Pos::Calls => Some(Job::Pending),
            Pos::PostStep => {
                s.sync();
                Some(Job::Hooks {
                    point: Hook::PostStep,
                    draft: None,
                    ending: Some(self.ending.clone()),
                })
            }
            Pos::Rest => Some(Job::Rest),
            Pos::PreStep | Pos::Input | Pos::PreSample | Pos::Sample | Pos::PreReply => {
                unreachable!("job_at sends these to job_before_reply")
            }
        })
    }

    /// The input phase: offer the next input still on the pad, or hop on.
    fn input_job(&mut self, s: &Session, on: bool) -> Option<Job> {
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
            return None;
        };
        self.draft = Some(Draft {
            body: Body::User(text),
            delivers: vec![id],
            by: Author::Frontend(client),
        });
        Some(hooks(Hook::PreInput, self.draft.clone()))
    }

    /// The tools phase: start the batch the reply decided, or hop to `post_step`.
    fn tools_job(&mut self, s: &Session, on: bool) -> Result<Option<Job>, Error> {
        match s.deciding(self.cursor) {
            Some((Next::RunTools, node)) if on => {
                let calls: Vec<RunCall> = calls(&s.tree.node(node)?.body)
                    .into_iter()
                    .map(|(id, tool, args_raw)| RunCall {
                        id: id.into(),
                        tool: tool.into(),
                        args_raw: args_raw.into(),
                    })
                    .collect();
                self.batch = calls.iter().map(|c| (c.id.clone(), None)).collect();
                self.hop("tools apply", Pos::Calls);
                Ok(Some(Job::RunCalls { node, calls }))
            }
            _ => {
                self.hop("tools do not apply", Pos::PostStep);
                Ok(None)
            }
        }
    }

    /// Answer the job last handed out.
    ///
    /// # Errors
    ///
    /// What a commit's `write` or `append` returns (`NoSuchCursor`);
    /// `WrongAnswer` if `answer` does not answer the job that is out (none, or one
    /// of another kind, a call not in the batch, or a call already answered), with
    /// nothing changed. Core checks no edit a hook made, only the tree's rules.
    pub fn answer(&mut self, s: &mut Session, answer: Answer, now: u64) -> Result<(), Error> {
        if !self.accepts(&answer) {
            return Err(Error::WrongAnswer(self.cursor));
        }
        transitions::answered(&answer);
        self.out = false;
        match answer {
            Answer::HookFailed(e) => self.on_hook_failed(s, e),
            Answer::Hooks { stop, draft } => self.on_hooks(s, stop, draft, now),
            Answer::Sampled { reply, error } => self.on_sampled(s, reply, error, now)?,
            Answer::CallDone { call, outcome, by } => self.on_call(s, &call, outcome, by, now)?,
        }
        Ok(())
    }

    /// A hook raised. At `post_step` the step is ending anyway.
    fn on_hook_failed(&mut self, s: &mut Session, e: String) {
        if self.pos == Pos::PostStep {
            self.after(s);
        } else {
            self.draft = None;
            self.end(s, Ending::Failed(e));
        }
    }

    /// A hook returned. `accepts` admits it only at a hook position.
    fn on_hooks(&mut self, s: &mut Session, stop: Option<String>, draft: Option<Draft>, now: u64) {
        match (self.pos, stop) {
            (Pos::PostStep, stop) => {
                if stop.is_some() {
                    s.auto.insert(self.cursor, false);
                }
                self.after(s);
            }
            (_, Some(reason)) => self.on_hook_stop(s, reason),
            (Pos::PreStep, None) => self.go(Pos::Input),
            (Pos::Input | Pos::PreReply, None) => {
                self.draft = None;
                self.commit(s, draft, now);
            }
            (Pos::PreSample, None) => {
                self.draft = draft;
                self.go(Pos::Sample);
            }
            (Pos::PostReply, None) => match self.error.take() {
                Some(e) => self.end(s, Ending::Failed(e)),
                None => self.go(Pos::Tools),
            },
            (Pos::PostResult, None) => self.go(Pos::PostStep),
            (Pos::Sample | Pos::Tools | Pos::Calls | Pos::Rest, None) => {
                unreachable!("accepts admits Hooks only at a hook position")
            }
        }
    }

    /// A hook halted the step. At `pre_input` the offered item is removed and
    /// its client told why.
    fn on_hook_stop(&mut self, s: &mut Session, reason: String) {
        if let (Pos::Input, Some(d)) = (self.pos, self.draft.take()) {
            s.remove_item(d.delivers[0]);
            if let Author::Frontend(client) = d.by {
                s.notices.push((client, reason.clone()));
            }
        }
        self.draft = None;
        self.end(s, Ending::Halted(reason));
    }

    /// The sample came back: a reply (maybe with an error), an error, or nothing (aborted).
    fn on_sampled(
        &mut self,
        s: &mut Session,
        reply: Option<Draft>,
        error: Option<String>,
        now: u64,
    ) -> Result<(), Error> {
        match (reply, error) {
            (Some(reply), error) => {
                self.draft = Some(reply);
                self.error = error;
                self.go(Pos::PreReply);
            }
            (None, Some(e)) => {
                let at = s.at(self.cursor)?;
                s.append(
                    at,
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
            (None, None) => self.end(s, Ending::Aborted),
        }
        Ok(())
    }

    /// One call of the batch finished; the last one joins the batch.
    fn on_call(
        &mut self,
        s: &mut Session,
        call: &str,
        outcome: Outcome,
        by: Author,
        now: u64,
    ) -> Result<(), Error> {
        if let Some(slot) = self.batch.iter_mut().find(|c| c.0 == call) {
            slot.1 = Some((outcome, by));
        }
        self.join(s, now)?;
        if self.pos == Pos::Calls {
            self.out = true;
            self.go(Pos::Calls); // calls still out
        }
        Ok(())
    }

    /// Whether `answer` answers the job that is out. Decided before anything changes.
    fn accepts(&self, answer: &Answer) -> bool {
        match (self.pos, answer) {
            (_, Answer::Hooks { .. } | Answer::HookFailed(_)) => self.in_hook(),
            (Pos::Sample, Answer::Sampled { .. }) => self.out,
            (Pos::Calls, Answer::CallDone { call, .. }) => {
                self.out && self.batch.iter().any(|c| c.0 == *call && c.1.is_none())
            }
            _ => false,
        }
    }

    /// Abort: auto-step off, no later effect. Returns true if the driver must
    /// cancel the effect in flight and answer what it got (partial reply, `Interrupted`s).
    pub fn abort(&mut self, s: &mut Session) -> bool {
        s.auto.insert(self.cursor, false);
        if self.ending == Ending::Done {
            self.ending = Ending::Aborted;
        }
        matches!(self.pos, Pos::Sample | Pos::Calls)
    }

    /// True while a cursor hook job is out: the hook may act on its own cursor.
    #[must_use]
    pub fn in_hook(&self) -> bool {
        self.out
            && matches!(
                self.pos,
                Pos::PreStep
                    | Pos::Input
                    | Pos::PreSample
                    | Pos::PreReply
                    | Pos::PostReply
                    | Pos::PostResult
                    | Pos::PostStep
            )
    }

    /// Write a phase's draft under `at`; `None`, the draft the hooks left, commits
    /// nothing. Core validates the draft the hooks left: any refusal from
    /// `write` (it changes nothing when it refuses) fails the step, its reason
    /// the error's text.
    fn commit(&mut self, s: &mut Session, draft: Option<Draft>, now: u64) {
        if let Some(d) = draft
            && let Err(e) = s.write(self.cursor, d.body, d.delivers, d.by, now)
        {
            transitions::event("commit refused");
            self.end(s, Ending::Failed(e.to_string()));
            return;
        }
        self.go(if self.pos == Pos::PreReply {
            Pos::PostReply
        } else {
            self.pos
        });
    }

    /// A move inside `job` that no job or answer causes: logged under `why`.
    fn hop(&mut self, why: &str, to: Pos) {
        transitions::event(why);
        self.go(to);
    }

    /// Move to `to`, logging the change under the current event.
    fn go(&mut self, to: Pos) {
        transitions::go(self.pos, to);
        self.pos = to;
    }

    #[must_use]
    pub fn resting(&self) -> bool {
        self.pos == Pos::Rest
    }

    /// Where in `Session::moves` this step began.
    #[must_use]
    pub fn first_move(&self) -> usize {
        self.first_move
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

    /// After `post_step`: sync what it wrote, then rest or restart. A step that
    /// ended `Done` with work left (auto-step on and `next != Wait`, or input
    /// pending) starts the next at once if its cursor moved. If it did not move,
    /// it would only try the same thing again: auto-step goes off and it rests.
    /// An item added since the step woke is news from outside and wakes a step
    /// regardless; that step answers to the same rule.
    fn after(&mut self, s: &mut Session) {
        s.sync();
        let work = s.next(self.cursor) != Next::Wait
            || s.items(self.cursor)
                .any(|i| matches!(i.data, ItemData::Input { .. }));
        let changed = s.changes.get(&self.cursor).copied().unwrap_or(0) != self.seen;
        let moved = self.moves(s).next().is_some();
        let idle = self.ending == Ending::Done && s.auto[&self.cursor] && work && !moved;
        if idle {
            s.auto.insert(self.cursor, false);
        }
        let again =
            self.ending == Ending::Done && (changed || (s.auto[&self.cursor] && work && moved));
        transitions::ending(&self.ending);
        if again {
            self.go(Pos::PreStep);
            *self = Self::wake(s, self.cursor, false);
        } else {
            self.go(Pos::Rest);
        }
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
        self.go(Pos::PostResult);
        Ok(())
    }
}
