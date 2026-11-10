//! The memo of a tokenizer: the token count of every piece of text it has counted, by the
//! piece's whole text, so the next count of a conversation that grew by one message tokenizes
//! only what is new. A hit can never be a different text (the key is the text, not a hash of
//! it), so a count read from here is the count a fresh tokenization gives.

use std::collections::HashMap;

/// The most key bytes kept. A 200k-token window is about 1 MB of text, so this is many requests'
/// worth; past it, the pieces the newest count did not touch are dropped.
const BUDGET: usize = 16 << 20;

#[derive(Debug)]
struct Slot {
    tokens: usize,
    /// The count call that last read or wrote this piece.
    round: u64,
}

/// One piece of text as the tokenizer sees it after splitting at added tokens and normalizing.
/// `first` is whether the piece starts the text: one pre-tokenizer (Metaspace, when it prepends
/// to the first word only) tokenizes the same piece differently there.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Piece {
    pub text: String,
    pub first: bool,
}

#[derive(Debug, Default)]
pub(crate) struct Memo {
    /// Counts of pieces that start the text, and of pieces that do not.
    slots: [HashMap<Box<str>, Slot>; 2],
    bytes: usize,
    round: u64,
    counted: u64,
    reused: u64,
}

impl Memo {
    /// Start a count: the pieces read and written until the next call belong to it.
    pub fn begin(&mut self) {
        self.round += 1;
    }

    pub fn get(&mut self, piece: &Piece) -> Option<usize> {
        let slot = self.slots[usize::from(piece.first)].get_mut(piece.text.as_str())?;
        slot.round = self.round;
        self.reused += 1;
        Some(slot.tokens)
    }

    pub fn put(&mut self, piece: Piece, tokens: usize) {
        self.counted += 1;
        let size = piece.text.len();
        let slot = Slot {
            tokens,
            round: self.round,
        };
        if self.slots[usize::from(piece.first)]
            .insert(piece.text.into_boxed_str(), slot)
            .is_none()
        {
            self.bytes += size;
        }
    }

    /// End a count: when the memo is over its budget, keep only what this count used, and
    /// nothing at all if that alone is over.
    pub fn end(&mut self) {
        if self.bytes <= BUDGET {
            return;
        }
        let round = self.round;
        for slots in &mut self.slots {
            slots.retain(|_, slot| slot.round == round);
        }
        self.bytes = self
            .slots
            .iter()
            .flat_map(HashMap::keys)
            .map(|key| key.len())
            .sum();
        if self.bytes > BUDGET {
            self.slots = Default::default();
            self.bytes = 0;
        }
    }

    /// How many pieces were tokenized, and how many counts were read back instead, so far.
    pub fn work(&self) -> (u64, u64) {
        (self.counted, self.reused)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn piece(text: &str, first: bool) -> Piece {
        Piece {
            text: text.to_owned(),
            first,
        }
    }

    #[test]
    fn a_piece_is_the_same_only_with_the_same_text_at_the_same_kind_of_place() {
        let mut memo = Memo::default();
        memo.begin();
        memo.put(piece("hello", false), 3);
        assert_eq!(memo.get(&piece("hello", false)), Some(3));
        assert_eq!(memo.get(&piece("hello", true)), None);
        assert_eq!(memo.get(&piece("hello ", false)), None);
        assert_eq!(memo.work(), (1, 1));
    }

    #[test]
    fn over_budget_only_the_newest_count_is_kept() {
        let mut memo = Memo::default();
        memo.begin();
        memo.put(piece(&"a".repeat(BUDGET), false), 1);
        memo.end();
        memo.begin();
        memo.put(piece("new", false), 1);
        memo.end();
        assert_eq!(memo.get(&piece("new", false)), Some(1));
        assert_eq!(memo.get(&piece(&"a".repeat(BUDGET), false)), None);
        memo.begin();
        memo.put(piece(&"b".repeat(BUDGET + 1), false), 1);
        memo.end();
        assert_eq!(memo.get(&piece("new", false)), None);
        assert_eq!(memo.bytes, 0);
    }
}
