//! Output shown as it arrives: the reader pushes bytes, an emitter thread hands them on.
//!
//! The reader never waits for the consumer. [`Stream::push`] appends to a bounded buffer
//! under a lock held for a copy; a separate thread ([`Stream::emit_until_closed`]) takes the
//! buffer and calls the consumer's function. A consumer that is slow stalls only that
//! thread: the buffer keeps its newest [`MAX_PENDING`] bytes and the older ones are dropped
//! (from a character boundary). The reader, and so the command, never wait on the consumer.
//! What does wait for it is whoever joins the emitter thread (the answer of `bash`): it waits
//! for a call already running and for the final flush, one more call.
//!
//! Coalescing is by time: at most one call per [`INTERVAL`], carrying everything that has
//! arrived since the last. The first bytes after a quiet spell go out at once. Text is cut
//! only between characters: a half-arrived character at the end (a lead byte and fewer
//! continuation bytes than it needs, in the last one to three bytes) waits for the rest of
//! it, or for the end, when it is shown as it is, lossily. Invalid bytes elsewhere are shown
//! at once, lossily, and do not hold up what follows.

use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

use crate::lock;

/// The shortest time between two emissions.
pub const INTERVAL: Duration = Duration::from_millis(100);
/// The most bytes held for the consumer; past it the oldest are dropped.
pub const MAX_PENDING: usize = 64 * 1024;

#[derive(Debug, Default)]
struct State {
    pending: Vec<u8>,
    closed: bool,
}

/// Bytes waiting for their consumer. See the module docs.
#[derive(Debug, Default)]
pub struct Stream {
    state: Mutex<State>,
    wake: Condvar,
}

impl Stream {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Add output. Never waits for the consumer.
    pub fn push(&self, chunk: &[u8]) {
        let mut state = lock(&self.state);
        state.pending.extend_from_slice(chunk);
        if state.pending.len() > MAX_PENDING {
            let excess = state.pending.len() - MAX_PENDING;
            state.pending.drain(..excess);
            // Not in the middle of a character: skip its remaining continuation bytes.
            let cut = state
                .pending
                .iter()
                .take(3)
                .take_while(|b| is_continuation(**b))
                .count();
            state.pending.drain(..cut);
        }
        drop(state);
        self.wake.notify_all();
    }

    /// No more output: the emitter shows what is left and returns.
    pub fn close(&self) {
        lock(&self.state).closed = true;
        self.wake.notify_all();
    }

    /// Call `emit` with the text that arrives, until [`Stream::close`]; then once more with
    /// what is left. Runs on a thread of its own. `emit` answers whether to go on: `false`
    /// (the consumer is gone, for good) ends this at once.
    pub fn emit_until_closed(&self, emit: &dyn Fn(String) -> bool) {
        let mut next_at = Instant::now();
        let mut state = lock(&self.state);
        loop {
            let now = Instant::now();
            if state.closed {
                let text =
                    String::from_utf8_lossy(&std::mem::take(&mut state.pending)).into_owned();
                drop(state);
                if !text.is_empty() {
                    emit(text);
                }
                return;
            }
            if state.pending.is_empty() {
                state = self
                    .wake
                    .wait(state)
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
            } else if now < next_at {
                state = self
                    .wake
                    .wait_timeout(state, next_at - now)
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .0;
            } else {
                let whole = whole_chars(&state.pending);
                if whole == 0 {
                    // Only the start of a character: wait for the rest (or the end).
                    state = self
                        .wake
                        .wait(state)
                        .unwrap_or_else(std::sync::PoisonError::into_inner);
                    continue;
                }
                let text: Vec<u8> = state.pending.drain(..whole).collect();
                drop(state);
                // Valid UTF-8 up to `whole`, or invalid bytes the command wrote: lossy either way.
                if !emit(String::from_utf8_lossy(&text).into_owned()) {
                    return;
                }
                next_at = Instant::now() + INTERVAL;
                state = lock(&self.state);
            }
        }
    }
}

fn is_continuation(byte: u8) -> bool {
    byte & 0xC0 == 0x80
}

/// How many leading bytes of `bytes` can be shown now: all of them, unless they end in an
/// half-arrived character (a lead byte followed by fewer continuation bytes than it needs),
/// which waits for the rest. Only the last three bytes can be that; invalid bytes anywhere
/// are shown, not waited for.
fn whole_chars(bytes: &[u8]) -> usize {
    let len = bytes.len();
    for back in 1..=len.min(3) {
        let at = len - back;
        let byte = bytes[at];
        if is_continuation(byte) {
            continue; // look further back for its lead
        }
        let needs = match byte {
            0xC2..=0xDF => 2,
            0xE0..=0xEF => 3,
            0xF0..=0xF4 => 4,
            _ => return len, // ASCII or not a lead: nothing is waiting
        };
        return if needs > back { at } else { len };
    }
    len
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, Ordering};

    fn collect(stream: &Arc<Stream>, feed: impl FnOnce(&Stream)) -> Vec<String> {
        let got = Mutex::new(Vec::new());
        std::thread::scope(|scope| {
            scope.spawn(|| {
                stream.emit_until_closed(&|s| {
                    lock(&got).push(s);
                    true
                });
            });
            feed(stream);
            stream.close();
        });
        got.into_inner().unwrap()
    }

    #[test]
    fn a_burst_is_one_emission_and_the_first_bytes_are_prompt() {
        let stream = Arc::new(Stream::new());
        let got = collect(&stream, |s| {
            for _ in 0..1000 {
                s.push(b"x");
            }
        });
        assert_eq!(got.concat(), "x".repeat(1000));
        assert!(got.len() <= 5, "{} emissions for 1000 pushes", got.len());
    }

    #[test]
    fn a_character_split_across_pushes_is_not_cut() {
        let stream = Arc::new(Stream::new());
        let bytes = "é".as_bytes();
        let got = collect(&stream, |s| {
            s.push(&bytes[..1]);
            std::thread::sleep(Duration::from_millis(30));
            s.push(&bytes[1..]);
        });
        assert_eq!(got, vec!["é".to_owned()]);
    }

    #[test]
    fn an_invalid_byte_does_not_hold_back_the_character_that_follows() {
        let stream = Arc::new(Stream::new());
        let got = collect(&stream, |s| {
            s.push(b"\xff\xc3");
            std::thread::sleep(Duration::from_millis(300));
            s.push(b"\xa9\n");
        });
        assert_eq!(got.concat(), "\u{FFFD}\u{e9}\n");
    }

    #[test]
    fn dropping_the_oldest_does_not_cut_a_character() {
        let (stream, entered) = (Stream::new(), AtomicBool::new(false));
        let go = AtomicBool::new(false);
        let got = Mutex::new(Vec::new());
        std::thread::scope(|scope| {
            scope.spawn(|| {
                stream.emit_until_closed(&|s| {
                    if !entered.swap(true, Ordering::SeqCst) {
                        while !go.load(Ordering::SeqCst) {
                            std::thread::sleep(Duration::from_millis(2));
                        }
                    }
                    lock(&got).push(s);
                    true
                });
            });
            stream.push(b"x");
            while !entered.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(2));
            }
            // Three-byte characters: the drop point lands inside one for most offsets.
            for _ in 0..(MAX_PENDING / 3 + 7) {
                stream.push("\u{20ac}".as_bytes());
            }
            stream.push(b"a"); // shifts the alignment of the front
            go.store(true, Ordering::SeqCst);
            stream.close();
        });
        let shown = got.into_inner().unwrap().concat();
        assert!(!shown.contains('\u{FFFD}'), "a character was cut in half");
    }

    #[test]
    fn a_consumer_that_answers_false_is_not_called_again() {
        let stream = Stream::new();
        let calls = std::sync::atomic::AtomicUsize::new(0);
        std::thread::scope(|scope| {
            scope.spawn(|| {
                stream.emit_until_closed(&|_| {
                    calls.fetch_add(1, Ordering::SeqCst);
                    false
                });
            });
            stream.push(b"one");
            std::thread::sleep(Duration::from_millis(250));
            stream.push(b"two");
            std::thread::sleep(Duration::from_millis(250));
            stream.close();
        });
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn a_slow_consumer_does_not_block_push_and_loses_the_oldest() {
        let stream = Stream::new();
        let (entered, released, pushes_done) = (
            AtomicBool::new(false),
            AtomicBool::new(false),
            AtomicBool::new(false),
        );
        let got = Mutex::new(Vec::new());
        let blocked_by_push = AtomicBool::new(false);
        std::thread::scope(|scope| {
            scope.spawn(|| {
                stream.emit_until_closed(&|s| {
                    if !entered.swap(true, Ordering::SeqCst) {
                        // The consumer is stuck until every push is done (or 10 s pass).
                        let waiting = Instant::now();
                        while !released.load(Ordering::SeqCst)
                            && waiting.elapsed() < Duration::from_secs(10)
                        {
                            std::thread::sleep(Duration::from_millis(2));
                        }
                        blocked_by_push
                            .store(!pushes_done.load(Ordering::SeqCst), Ordering::SeqCst);
                    }
                    lock(&got).push(s);
                    true
                });
            });
            stream.push(b"first");
            while !entered.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(2));
            }
            for _ in 0..(MAX_PENDING / 10 * 3) {
                stream.push(b"0123456789");
            }
            stream.push(b"last");
            pushes_done.store(true, Ordering::SeqCst);
            released.store(true, Ordering::SeqCst);
            stream.close();
        });
        assert!(
            !blocked_by_push.load(Ordering::SeqCst),
            "a push waited for the consumer"
        );
        let got = got.into_inner().unwrap();
        assert_eq!(got[0], "first");
        let rest = got[1..].concat();
        assert!(rest.len() <= MAX_PENDING && rest.ends_with("last"));
    }
}
