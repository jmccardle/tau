//! The session machine: every cursor's step, plus the calls a frontend makes
//! (model.md §5–§7). Frontend calls are inputs here, as jobs and results are
//! for a step (D11). Still sans-IO: the driver asks for each cursor's jobs.

use std::collections::BTreeMap;

use crate::step::{Answer, Job, Step};
use crate::transitions;
use crate::tree::{Author, Body, Config, CursorId, Error, ItemData, ItemId, Move, NodeId, Session};

#[derive(Debug)]
pub struct Machine {
    pub session: Session,
    steps: BTreeMap<CursorId, Step>,
    /// A go-to on a busy cursor, applied once its aborted step rests.
    going: BTreeMap<CursorId, (NodeId, Author, u64)>,
}

impl Machine {
    #[must_use]
    pub fn new(session: Session) -> Self {
        Self {
            session,
            steps: BTreeMap::new(),
            going: BTreeMap::new(),
        }
    }

    /// A machine for a session just opened from a store. Load is a wake
    /// (model.md §6): every cursor starts a step, which with auto-step off (the
    /// load's `reconcile`) runs only `pre_step` and `post_step`.
    #[must_use]
    pub fn loaded(session: Session) -> Self {
        let mut m = Self::new(session);
        let cursors: Vec<CursorId> = m.session.auto.keys().copied().collect();
        for cursor in cursors {
            m.wake(cursor, false);
        }
        m
    }

    /// A step is running, or a go-to waits on one to rest: the cursor needs `next_job`.
    #[must_use]
    pub fn busy(&self, cursor: CursorId) -> bool {
        self.going.contains_key(&cursor) || self.steps.get(&cursor).is_some_and(|s| !s.resting())
    }

    // Frontend calls. Each syncs on return.

    /// Add an input item; wakes the cursor. Waits for the next input phase if busy.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open.
    pub fn input(&mut self, cursor: CursorId, client: String, text: &str) -> Result<ItemId, Error> {
        self.session.at(cursor)?;
        let data = ItemData::Input {
            client,
            text: text.into(),
        };
        let id = self.add_item(cursor, data, None)?;
        self.session.sync();
        Ok(id)
    }

    /// `Session.step`: run the phases once with auto-step off.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open; `Busy` if a step is running.
    pub fn step(&mut self, cursor: CursorId) -> Result<(), Error> {
        self.session.at(cursor)?;
        if self.busy(cursor) {
            return Err(Error::Busy(cursor));
        }
        self.wake(cursor, true);
        self.session.sync();
        Ok(())
    }

    /// Go to `to`: auto-step off, then a wake. On a busy cursor the step is
    /// aborted (a hook that is running finishes, as for `abort`) and the move
    /// applies after its `post_step`; returns whether the driver must cancel
    /// the effect in flight.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open; `NoSuchNode` if `to` is not a node.
    pub fn go_to(
        &mut self,
        cursor: CursorId,
        to: NodeId,
        client: String,
        now: u64,
    ) -> Result<bool, Error> {
        self.session.at(cursor)?;
        self.session.tree.node(to)?;
        let by = Author::Frontend(client);
        let cancel = if self.busy(cursor) {
            self.going.insert(cursor, (to, by, now));
            self.steps
                .get_mut(&cursor)
                .is_some_and(|step| step.abort(&mut self.session))
        } else {
            self.session.move_to(cursor, to, by, now)?;
            self.session.auto.insert(cursor, false);
            self.wake(cursor, false);
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
    pub fn set_auto(&mut self, cursor: CursorId, on: bool) -> Result<(), Error> {
        self.session.at(cursor)?;
        self.session.auto.insert(cursor, on);
        if on {
            self.wake(cursor, false);
        }
        self.session.sync();
        Ok(())
    }

    /// Set a label on a cursor; a label already set elsewhere moves. `hook`:
    /// the cursor whose hook makes the call (see `open`).
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open.
    pub fn set_label(
        &mut self,
        label: &str,
        cursor: CursorId,
        hook: Option<CursorId>,
    ) -> Result<(), Error> {
        self.session.set_label(label, cursor)?;
        self.sync_unless_hook(hook);
        Ok(())
    }

    /// Remove a label. `hook` as for `open`.
    ///
    /// # Errors
    ///
    /// `NoSuchLabel` if `label` is not set.
    pub fn remove_label(&mut self, label: &str, hook: Option<CursorId>) -> Result<(), Error> {
        self.session.remove_label(label)?;
        self.sync_unless_hook(hook);
        Ok(())
    }

    /// Esc. Returns whether the driver must cancel the effect in flight.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open.
    pub fn abort(&mut self, cursor: CursorId) -> Result<bool, Error> {
        self.session.at(cursor)?;
        Ok(
            if let Some(step) = self.steps.get_mut(&cursor).filter(|s| !s.resting()) {
                step.abort(&mut self.session)
            } else {
                self.session.auto.insert(cursor, false);
                false
            },
        )
    }

    // Extension and driver calls. Each syncs on return, unless `hook` names the
    // cursor whose hook job is out and makes the call: that phase's own sync
    // covers it (D11). A frontend's or the driver's call passes `None`.

    /// Open a cursor at `at`; opening wakes it (Q32).
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `at` is not a node.
    pub fn open(
        &mut self,
        at: NodeId,
        by: Author,
        now: u64,
        hook: Option<CursorId>,
    ) -> Result<CursorId, Error> {
        let cursor = self.session.open(at, by, now)?;
        self.wake(cursor, false);
        self.sync_unless_hook(hook);
        Ok(cursor)
    }

    /// Add an item to `to`'s scratchpad; wakes `to`. `hook` as for `open`.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `to` is not open.
    pub fn add_item(
        &mut self,
        to: CursorId,
        data: ItemData,
        hook: Option<CursorId>,
    ) -> Result<ItemId, Error> {
        self.session.at(to)?;
        let id = self.session.add_item(to, data);
        self.wake(to, false);
        self.sync_unless_hook(hook);
        Ok(id)
    }

    /// Remove an item from its cursor's scratchpad. Wakes nothing and counts as
    /// no change (only an added item does). `hook` as for `open`.
    ///
    /// # Errors
    ///
    /// `NoSuchItem` if `id` is not in the scratchpad.
    pub fn remove_item(&mut self, id: ItemId, hook: Option<CursorId>) -> Result<(), Error> {
        if !self.session.scratchpad().items.iter().any(|i| i.id == id) {
            return Err(Error::NoSuchItem(id));
        }
        self.session.remove_item(id);
        self.sync_unless_hook(hook);
        Ok(())
    }

    /// Append a node under `parent` without moving any cursor (an extension's
    /// note). `hook` as for `open`.
    ///
    /// # Errors
    ///
    /// As `Session::append`.
    pub fn append(
        &mut self,
        parent: NodeId,
        body: Body,
        by: Author,
        now: u64,
        hook: Option<CursorId>,
    ) -> Result<NodeId, Error> {
        let id = self.session.append(parent, by, body, vec![], now)?;
        self.sync_unless_hook(hook);
        Ok(id)
    }

    /// Append a root `Config` (a detached tree) without moving any cursor.
    /// `hook` as for `open`.
    pub fn append_root(
        &mut self,
        config: Config,
        by: Author,
        now: u64,
        hook: Option<CursorId>,
    ) -> NodeId {
        let id = self.session.append_root(by, config, now);
        self.sync_unless_hook(hook);
        id
    }

    // A cursor hook's own-cursor calls (model.md section 7, Handles). Each is
    // allowed only while a cursor hook job is out for `cursor`; none checks
    // what the hook did (no hook edit limits), and none syncs, because the
    // phase's own sync covers them (D11).

    /// `cursor.move(to)`: applies at once. No record when already at `to`.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open; `NotInHook` if no cursor hook is
    /// out for it; `NoSuchNode` if `to` is not a node.
    pub fn hook_move(
        &mut self,
        cursor: CursorId,
        to: NodeId,
        by: Author,
        now: u64,
    ) -> Result<(), Error> {
        self.in_hook(cursor)?;
        self.session.move_to(cursor, to, by, now)
    }

    /// `cursor.write(body, delivers)`: append under `at` and move onto it.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor`, `NotInHook` as `hook_move`; else as `Session::write`.
    pub fn hook_write(
        &mut self,
        cursor: CursorId,
        body: Body,
        delivers: Vec<ItemId>,
        by: Author,
        now: u64,
    ) -> Result<NodeId, Error> {
        self.in_hook(cursor)?;
        self.session.write(cursor, body, delivers, by, now)
    }

    /// A hook sets its own cursor's auto-step. It needs no wake: the step is running.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor`, `NotInHook` as `hook_move`.
    pub fn hook_set_auto(&mut self, cursor: CursorId, on: bool) -> Result<(), Error> {
        self.in_hook(cursor)?;
        self.session.auto.insert(cursor, on);
        Ok(())
    }

    /// A cursor hook job is out for `cursor`.
    fn in_hook(&self, cursor: CursorId) -> Result<(), Error> {
        self.session.at(cursor)?;
        if self.steps.get(&cursor).is_some_and(Step::in_hook) {
            Ok(())
        } else {
            Err(Error::NotInHook(cursor))
        }
    }

    /// Where in `Session::moves` the cursor's current step began; the end of
    /// them when it has none.
    #[must_use]
    pub fn step_start(&self, cursor: CursorId) -> usize {
        self.steps
            .get(&cursor)
            .map_or(self.session.moves.len(), Step::first_move)
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
    /// `NoSuchCursor` if `cursor` is not open; what the step's `next_job`
    /// returns, or a waiting go-to's `move_to`.
    pub fn next_job(&mut self, cursor: CursorId) -> Result<Job, Error> {
        let _span = tracing::info_span!("core.next_job", cursor).entered();
        self.session.at(cursor)?;
        let Some(step) = self.steps.get_mut(&cursor) else {
            return Ok(Job::Rest);
        };
        let job = step.next_job(&mut self.session)?;
        match (job, self.going.remove(&cursor)) {
            (Job::Rest, Some((to, by, time))) => {
                self.session.move_to(cursor, to, by, time)?;
                self.session.auto.insert(cursor, false);
                self.session.sync();
                self.wake(cursor, false);
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

    /// Answer the job last handed out for `cursor`.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open; what the step's `answer` returns;
    /// `WrongAnswer` if no step exists for `cursor`.
    pub fn answer(&mut self, cursor: CursorId, answer: Answer, now: u64) -> Result<(), Error> {
        let _span = tracing::info_span!("core.answer", cursor).entered();
        self.session.at(cursor)?;
        let step = self
            .steps
            .get_mut(&cursor)
            .ok_or(Error::WrongAnswer(cursor))?;
        step.answer(&mut self.session, answer, now)
    }

    /// The sync a call owes on return. Only a call made from the cursor hook
    /// running on `hook` skips it: that phase's own sync covers the write. A
    /// frontend's or the driver's call (`None`) always syncs, whatever hook
    /// another cursor has out.
    fn sync_unless_hook(&mut self, hook: Option<CursorId>) {
        let in_hook = hook.is_some_and(|c| self.in_hook(c).is_ok());
        if !in_hook {
            self.session.sync();
        }
    }

    /// Start a step unless one is running; a wake while busy joins it.
    fn wake(&mut self, cursor: CursorId, asked: bool) {
        if !self.busy(cursor) {
            transitions::wake(asked);
            self.steps
                .insert(cursor, Step::wake(&self.session, cursor, asked));
        }
    }
}
