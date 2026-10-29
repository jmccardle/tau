//! The session machine: every cursor's step, plus the calls a frontend makes
//! (model.md §5–§7). Frontend calls are inputs here, as jobs and results are
//! for a step (D11). Still sans-IO: the driver asks for each cursor's jobs.

use std::collections::BTreeMap;

use crate::step::{Fed, Job, Step};
use crate::transitions;
use crate::tree::{Author, Body, CursorId, Error, ItemData, ItemId, Move, NodeId, Session};

#[derive(Debug)]
pub struct Machine {
    pub session: Session,
    steps: BTreeMap<CursorId, Step>,
    /// Open questions per cursor; any open question locks the cursor.
    asking: BTreeMap<CursorId, u32>,
    /// A go-to on a busy cursor, applied once its aborted step rests.
    going: BTreeMap<CursorId, (NodeId, Author, u64)>,
    /// The last serial given to a cursor hook job; never reused, not persisted.
    serial: u64,
}

impl Machine {
    #[must_use]
    pub fn new(session: Session) -> Self {
        Self {
            session,
            steps: BTreeMap::new(),
            asking: BTreeMap::new(),
            going: BTreeMap::new(),
            serial: 0,
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

    /// Set a label on a cursor; a label already set elsewhere moves. Syncs on return
    /// (see `hook_set_label`).
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open.
    pub fn set_label(&mut self, label: &str, cursor: CursorId) -> Result<(), Error> {
        self.set_label_from(None, label, cursor)
    }

    /// `set_label` from the hook job `serial` on `hook`: no sync while that job is out.
    ///
    /// # Errors
    ///
    /// As `set_label`.
    pub fn hook_set_label(
        &mut self,
        hook: CursorId,
        serial: u64,
        label: &str,
        cursor: CursorId,
    ) -> Result<(), Error> {
        self.set_label_from(Some((hook, serial)), label, cursor)
    }

    fn set_label_from(
        &mut self,
        hook: Option<(CursorId, u64)>,
        label: &str,
        cursor: CursorId,
    ) -> Result<(), Error> {
        self.session.set_label(label, cursor)?;
        self.sync_unless_hook(hook);
        Ok(())
    }

    /// Remove a label. Syncs on return (see `hook_remove_label`).
    ///
    /// # Errors
    ///
    /// `NoSuchLabel` if `label` is not set.
    pub fn remove_label(&mut self, label: &str) -> Result<(), Error> {
        self.remove_label_from(None, label)
    }

    /// `remove_label` from the hook running on `hook`: no sync while that job is out.
    ///
    /// # Errors
    ///
    /// As `remove_label`.
    pub fn hook_remove_label(
        &mut self,
        hook: CursorId,
        serial: u64,
        label: &str,
    ) -> Result<(), Error> {
        self.remove_label_from(Some((hook, serial)), label)
    }

    fn remove_label_from(
        &mut self,
        hook: Option<(CursorId, u64)>,
        label: &str,
    ) -> Result<(), Error> {
        self.session.remove_label(label)?;
        self.sync_unless_hook(hook);
        Ok(())
    }

    /// Esc. Allowed while locked; the question stays open. Returns whether the
    /// driver must cancel the effect in flight.
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

    // Extension and driver calls.

    /// Open a cursor at `at`; opening wakes it (Q32). Syncs on return (the
    /// `hook_*` variant, called from a hook, rides that phase's sync instead, D11).
    ///
    /// # Errors
    ///
    /// `NoSuchNode` if `at` is not a node.
    pub fn open(&mut self, at: NodeId, by: Author, now: u64) -> Result<CursorId, Error> {
        self.open_from(None, at, by, now)
    }

    /// `open` from the hook running on `hook`: no sync while that job is out.
    ///
    /// # Errors
    ///
    /// As `open`.
    pub fn hook_open(
        &mut self,
        hook: CursorId,
        serial: u64,
        at: NodeId,
        by: Author,
        now: u64,
    ) -> Result<CursorId, Error> {
        self.open_from(Some((hook, serial)), at, by, now)
    }

    fn open_from(
        &mut self,
        hook: Option<(CursorId, u64)>,
        at: NodeId,
        by: Author,
        now: u64,
    ) -> Result<CursorId, Error> {
        let cursor = self.session.open(at, by, now)?;
        self.wake(cursor, false, now);
        self.sync_unless_hook(hook);
        Ok(cursor)
    }

    /// Add an item to `to`'s scratchpad; wakes `to`. Syncs on return (the
    /// `hook_*` variant, called from a hook, rides that phase's sync instead, D11).
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `to` is not open.
    pub fn add_item(&mut self, to: CursorId, data: ItemData, now: u64) -> Result<ItemId, Error> {
        self.add_item_from(None, to, data, now)
    }

    /// `add_item` from the hook running on `hook`: no sync while that job is out.
    ///
    /// # Errors
    ///
    /// As `add_item`.
    pub fn hook_add_item(
        &mut self,
        hook: CursorId,
        serial: u64,
        to: CursorId,
        data: ItemData,
        now: u64,
    ) -> Result<ItemId, Error> {
        self.add_item_from(Some((hook, serial)), to, data, now)
    }

    fn add_item_from(
        &mut self,
        hook: Option<(CursorId, u64)>,
        to: CursorId,
        data: ItemData,
        now: u64,
    ) -> Result<ItemId, Error> {
        self.session.at(to)?;
        let id = self.session.add_item(to, data);
        self.wake(to, false, now);
        self.sync_unless_hook(hook);
        Ok(id)
    }

    /// Remove an item from its cursor's scratchpad. Wakes nothing and counts as
    /// no change (only an added item does). Syncs on return.
    ///
    /// # Errors
    ///
    /// `NoSuchItem` if `id` is not in the scratchpad.
    pub fn remove_item(&mut self, id: ItemId) -> Result<(), Error> {
        self.remove_item_from(None, id)
    }

    /// `remove_item` from the hook running on `hook`: no sync while that job is out.
    ///
    /// # Errors
    ///
    /// As `remove_item`.
    pub fn hook_remove_item(
        &mut self,
        hook: CursorId,
        serial: u64,
        id: ItemId,
    ) -> Result<(), Error> {
        self.remove_item_from(Some((hook, serial)), id)
    }

    fn remove_item_from(&mut self, hook: Option<(CursorId, u64)>, id: ItemId) -> Result<(), Error> {
        if !self.session.scratchpad().items.iter().any(|i| i.id == id) {
            return Err(Error::NoSuchItem(id));
        }
        self.session.remove_item(id);
        self.sync_unless_hook(hook);
        Ok(())
    }

    /// Append a node under `parent` (none: a root) without moving any cursor
    /// (an extension's note). Syncs on return.
    ///
    /// # Errors
    ///
    /// As `Session::append`.
    pub fn append(
        &mut self,
        parent: Option<NodeId>,
        body: Body,
        by: Author,
        now: u64,
    ) -> Result<NodeId, Error> {
        self.append_from(None, parent, body, by, now)
    }

    /// `append` from the hook running on `hook`: no sync while that job is out.
    ///
    /// # Errors
    ///
    /// As `append`.
    pub fn hook_append(
        &mut self,
        hook: CursorId,
        serial: u64,
        parent: Option<NodeId>,
        body: Body,
        by: Author,
        now: u64,
    ) -> Result<NodeId, Error> {
        self.append_from(Some((hook, serial)), parent, body, by, now)
    }

    fn append_from(
        &mut self,
        hook: Option<(CursorId, u64)>,
        parent: Option<NodeId>,
        body: Body,
        by: Author,
        now: u64,
    ) -> Result<NodeId, Error> {
        let id = self.session.append(parent, by, body, vec![], now)?;
        self.sync_unless_hook(hook);
        Ok(id)
    }

    // A cursor hook's own-cursor calls (model.md section 7, Handles). Each is
    // allowed only while the cursor hook job with that `serial` is out for `cursor`; none checks
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
        serial: u64,
        to: NodeId,
        by: Author,
        now: u64,
    ) -> Result<(), Error> {
        self.in_hook(cursor, serial)?;
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
        serial: u64,
        body: Body,
        delivers: Vec<ItemId>,
        by: Author,
        now: u64,
    ) -> Result<NodeId, Error> {
        self.in_hook(cursor, serial)?;
        self.session.write(cursor, body, delivers, by, now)
    }

    /// A hook sets its own cursor's auto-step. Unlike a frontend's, it does not
    /// reset the `max_steps` count and needs no wake: the step is running.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor`, `NotInHook` as `hook_move`.
    pub fn hook_set_auto(&mut self, cursor: CursorId, serial: u64, on: bool) -> Result<(), Error> {
        self.in_hook(cursor, serial)?;
        self.session.auto.insert(cursor, on);
        Ok(())
    }

    /// A hook job is out for `cursor` and it is the one with this `serial`.
    fn in_hook(&self, cursor: CursorId, serial: u64) -> Result<(), Error> {
        self.session.at(cursor)?;
        if self
            .steps
            .get(&cursor)
            .is_some_and(|s| s.in_hook() && s.serial() == serial)
        {
            Ok(())
        } else {
            Err(Error::NotInHook(cursor))
        }
    }

    /// A hook asked a question on `cursor`: it is locked until answered.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open.
    pub fn ask(&mut self, cursor: CursorId) -> Result<(), Error> {
        self.session.at(cursor)?;
        *self.asking.entry(cursor).or_default() += 1; // rules: allow(entry): BTreeMap API
        Ok(())
    }

    /// A question on `cursor` was answered or declined.
    ///
    /// # Errors
    ///
    /// `NoSuchCursor` if `cursor` is not open.
    pub fn answered(&mut self, cursor: CursorId) -> Result<(), Error> {
        self.session.at(cursor)?;
        if let Some(n) = self.asking.get_mut(&cursor) {
            *n = n.saturating_sub(1);
        }
        Ok(())
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
        let mut job = step.next_job(&mut self.session)?;
        if let Job::Hooks { serial, .. } = &mut job {
            self.serial += 1;
            *serial = self.serial;
            step.set_serial(self.serial);
        }
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
    /// `NoSuchCursor` if `cursor` is not open; what the step's `feed` returns;
    /// `WrongFeed` if no step exists for `cursor`.
    pub fn feed(&mut self, cursor: CursorId, fed: Fed, now: u64) -> Result<(), Error> {
        let _span = tracing::info_span!("core.feed", cursor).entered();
        self.session.at(cursor)?;
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

    /// The sync a call owes on return. Only a call made from the cursor hook
    /// running on `hook` skips it: that phase's own sync covers the write. A
    /// frontend's or the driver's call (`None`) always syncs, whatever hook
    /// another cursor has out.
    fn sync_unless_hook(&mut self, hook: Option<(CursorId, u64)>) {
        let in_hook = hook.is_some_and(|(c, serial)| self.in_hook(c, serial).is_ok());
        if !in_hook {
            self.session.sync();
        }
    }

    /// Start a step unless one is running; a wake while busy joins it.
    fn wake(&mut self, cursor: CursorId, asked: bool, now: u64) {
        if !self.busy(cursor) {
            transitions::wake(asked);
            self.steps
                .insert(cursor, Step::wake(&self.session, cursor, asked, now));
        }
    }
}
