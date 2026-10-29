//! The session machine: every cursor's step, plus the calls a frontend makes
//! (model.md §5–§7). Frontend calls are inputs here, as jobs and results are
//! for a step (D11). Still sans-IO: the driver asks for each cursor's jobs.

use std::collections::BTreeMap;

use crate::step::{Fed, Job, Step};
use crate::tree::{Author, CursorId, Error, ItemData, ItemId, Move, NodeId, Session};

#[derive(Debug)]
pub struct Machine {
    pub session: Session,
    steps: BTreeMap<CursorId, Step>,
    /// Open questions per cursor; any open question locks the cursor.
    asking: BTreeMap<CursorId, u32>,
    /// A go-to on a busy cursor, applied once its aborted step rests.
    going: BTreeMap<CursorId, (NodeId, Author, u64)>,
}

impl Machine {
    #[must_use]
    pub fn new(session: Session) -> Self {
        Self {
            session,
            steps: BTreeMap::new(),
            asking: BTreeMap::new(),
            going: BTreeMap::new(),
        }
    }

    /// A machine for a session just opened from a store. Load is a wake
    /// (model.md §6): every cursor starts a step, which with auto-step off (the
    /// load's `reconcile`) runs only `pre_step` and `post_step`.
    #[must_use]
    pub fn loaded(session: Session, now: u64) -> Self {
        let mut m = Self::new(session);
        let cursors: Vec<CursorId> = m.session.auto.keys().copied().collect();
        for cursor in cursors {
            m.wake(cursor, false, now);
        }
        m
    }

    #[must_use]
    pub fn busy(&self, cursor: CursorId) -> bool {
        self.steps.get(&cursor).is_some_and(|s| !s.resting())
    }

    #[must_use]
    pub fn locked(&self, cursor: CursorId) -> bool {
        self.asking.get(&cursor).is_some_and(|n| *n > 0)
    }

    // Frontend calls. Each resets the cursor's `max_steps` count and syncs on return.

    /// Add an input item; wakes the cursor. Waits for the next input phase if busy.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open; `Locked` if `cursor` has an open question.
    pub fn input(
        &mut self,
        cursor: CursorId,
        client: String,
        text: &str,
        now: u64,
    ) -> Result<ItemId, Error> {
        self.frontend(cursor)?;
        let data = ItemData::Input {
            client,
            text: text.into(),
        };
        let id = self.add_item(cursor, data, now)?;
        self.session.sync();
        Ok(id)
    }

    /// Input plus auto-step on.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open; `Locked` if `cursor` has an open question.
    pub fn send(
        &mut self,
        cursor: CursorId,
        client: String,
        text: &str,
        now: u64,
    ) -> Result<ItemId, Error> {
        self.frontend(cursor)?;
        self.session.auto.insert(cursor, true);
        self.input(cursor, client, text, now)
    }

    /// `Session.step`: run the phases once with auto-step off.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open; `Locked` if `cursor` has an open question; `Busy` if a step is running.
    pub fn step(&mut self, cursor: CursorId, _client: String, now: u64) -> Result<(), Error> {
        self.frontend(cursor)?;
        if self.busy(cursor) {
            return Err(Error::Busy(cursor));
        }
        self.wake(cursor, true, now);
        self.session.sync();
        Ok(())
    }

    /// Go to `to`: auto-step off, then a wake. On a busy cursor the step is
    /// aborted and the move applies after its `post_step`; returns whether the
    /// driver must cancel the effect in flight.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open; `Locked` if `cursor` has an open question; `NoSuchNode` if `to` is not a node;
    /// `HookRunning` if a cursor hook job is out.
    pub fn go_to(
        &mut self,
        cursor: CursorId,
        to: NodeId,
        client: String,
        now: u64,
    ) -> Result<bool, Error> {
        self.frontend(cursor)?;
        self.session.node(to)?;
        let by = Author::Frontend(client);
        let cancel = if let Some(step) = self.steps.get_mut(&cursor).filter(|s| !s.resting()) {
            if step.in_hook() {
                return Err(Error::HookRunning(cursor));
            }
            self.going.insert(cursor, (to, by, now));
            step.abort(&mut self.session)
        } else {
            self.session.move_to(cursor, to, by, now)?;
            self.session.auto.insert(cursor, false);
            self.wake(cursor, false, now);
            false
        };
        self.session.sync();
        Ok(cancel)
    }

    /// Turn auto-step on or off; on wakes the cursor.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open.
    pub fn set_auto(
        &mut self,
        cursor: CursorId,
        on: bool,
        _client: String,
        now: u64,
    ) -> Result<(), Error> {
        self.session.at(cursor)?;
        self.session.taken.remove(&cursor);
        self.session.auto.insert(cursor, on);
        if on {
            self.wake(cursor, false, now);
        }
        self.session.sync();
        Ok(())
    }

    /// Set a label on a cursor; a label already set elsewhere moves. Syncs on return.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open.
    pub fn set_label(&mut self, label: &str, cursor: CursorId) -> Result<(), Error> {
        self.session.set_label(label, cursor)?;
        self.session.sync();
        Ok(())
    }

    /// Remove a label. Syncs on return.
    ///
    /// # Errors
    ///
    /// `NoSuchLabel` if `label` is not set.
    pub fn remove_label(&mut self, label: &str) -> Result<(), Error> {
        self.session.remove_label(label)?;
        self.session.sync();
        Ok(())
    }

    /// Esc. Allowed while locked; the question stays open. Returns whether the
    /// driver must cancel the effect in flight.
    pub fn abort(&mut self, cursor: CursorId) -> bool {
        if let Some(step) = self.steps.get_mut(&cursor).filter(|s| !s.resting()) {
            step.abort(&mut self.session)
        } else {
            self.session.auto.insert(cursor, false);
            false
        }
    }

    // Extension and driver calls.

    /// Open a cursor at `at`; opening wakes it (Q32).
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `at` is not a node.
    pub fn open(&mut self, at: NodeId, by: Author, now: u64) -> Result<CursorId, Error> {
        let cursor = self.session.open(at, by, now)?;
        self.wake(cursor, false, now);
        Ok(cursor)
    }

    /// Add an item to `to`'s scratchpad; wakes `to`.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `to` is not open.
    pub fn add_item(&mut self, to: CursorId, data: ItemData, now: u64) -> Result<ItemId, Error> {
        self.session.at(to)?;
        let id = self.session.add_item(to, data);
        self.wake(to, false, now);
        Ok(id)
    }

    /// A hook asked a question on `cursor`: it is locked until answered.
    pub fn ask(&mut self, cursor: CursorId) {
        *self.asking.entry(cursor).or_default() += 1; // rules: allow(entry): BTreeMap API
    }

    /// A question on `cursor` was answered or declined.
    pub fn answered(&mut self, cursor: CursorId) {
        if let Some(n) = self.asking.get_mut(&cursor) {
            *n = n.saturating_sub(1);
        }
    }

    /// `cursor.moves(since_step=True)` for the cursor's current step.
    pub fn moves_since_step(&self, cursor: CursorId) -> impl Iterator<Item = &Move> {
        self.steps
            .get(&cursor)
            .into_iter()
            .flat_map(move |st| st.moves(&self.session))
    }

    // Driving.

    /// The cursor's next job; `Rest` when it has no step. A go-to waiting on an
    /// aborted step applies here, once that step rests, and wakes the cursor.
    ///
    /// # Errors
    ///
    /// What the step's `next_job` returns, or a waiting go-to's `move_to`.
    pub fn next_job(&mut self, cursor: CursorId) -> Result<Job, Error> {
        let Some(step) = self.steps.get_mut(&cursor) else {
            return Ok(Job::Rest);
        };
        let job = step.next_job(&mut self.session)?;
        match (job, self.going.remove(&cursor)) {
            (Job::Rest, Some((to, by, time))) => {
                self.session.move_to(cursor, to, by, time)?;
                self.session.auto.insert(cursor, false);
                self.session.sync();
                self.wake(cursor, false, time);
                self.next_job(cursor)
            }
            (job, going) => {
                if let Some(g) = going {
                    self.going.insert(cursor, g);
                }
                Ok(job)
            }
        }
    }

    /// Feed the result of the job last handed out for `cursor`.
    ///
    /// # Errors
    ///
    /// What the step's `feed` returns; `WrongFeed` if no step exists for `cursor`.
    pub fn feed(&mut self, cursor: CursorId, fed: Fed, now: u64) -> Result<(), Error> {
        let step = self
            .steps
            .get_mut(&cursor)
            .ok_or(Error::WrongFeed(cursor))?;
        step.feed(&mut self.session, fed, now)
    }

    /// A frontend call's checks: the cursor exists and is not locked; then
    /// its `max_steps` count resets.
    fn frontend(&mut self, cursor: CursorId) -> Result<(), Error> {
        self.session.at(cursor)?;
        if self.locked(cursor) {
            return Err(Error::Locked(cursor));
        }
        self.session.taken.remove(&cursor);
        Ok(())
    }

    /// Start a step unless one is running; a wake while busy joins it.
    fn wake(&mut self, cursor: CursorId, asked: bool, now: u64) {
        if !self.busy(cursor) {
            self.steps
                .insert(cursor, Step::wake(&self.session, cursor, asked, now));
        }
    }
}
