//! Phase 0 probe: a struct that crosses the Rust/Python boundary.
//!
//! Not part of the model. It exists to prove the toolchain (owned data,
//! validation errors, deterministic hashing, proptest) and is deleted when
//! Phase 3 lands real types.

use std::fmt;

/// A value that round-trips through the Python bindings.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Probe {
    pub id: u64,
    pub label: String,
    pub parts: Vec<String>,
}

/// Why a [`Probe`] could not be constructed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProbeError {
    EmptyLabel,
}

impl fmt::Display for ProbeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyLabel => f.write_str("probe label must not be empty"),
        }
    }
}

impl std::error::Error for ProbeError {}

impl Probe {
    /// # Errors
    /// [`ProbeError::EmptyLabel`] when `label` is empty.
    pub fn new(id: u64, label: String, parts: Vec<String>) -> Result<Self, ProbeError> {
        if label.is_empty() {
            return Err(ProbeError::EmptyLabel);
        }
        Ok(Self { id, label, parts })
    }

    /// FNV-1a over every field, with length prefixes so that field
    /// boundaries are unambiguous. Stable across processes and platforms,
    /// unlike `std::hash`.
    #[must_use]
    pub fn digest(&self) -> u64 {
        let mut h = Fnv1a::new();
        h.write(&self.id.to_le_bytes());
        h.write_str(&self.label);
        h.write(&(self.parts.len() as u64).to_le_bytes());
        for part in &self.parts {
            h.write_str(part);
        }
        h.finish()
    }
}

struct Fnv1a(u64);

impl Fnv1a {
    const OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;

    fn new() -> Self {
        Self(Self::OFFSET)
    }

    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.0 ^= u64::from(b);
            self.0 = self.0.wrapping_mul(Self::PRIME);
        }
    }

    fn write_str(&mut self, s: &str) {
        self.write(&(s.len() as u64).to_le_bytes());
        self.write(s.as_bytes());
    }

    fn finish(&self) -> u64 {
        self.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn probe() -> impl Strategy<Value = Probe> {
        (any::<u64>(), ".+", prop::collection::vec(".*", 0..8))
            .prop_map(|(id, label, parts)| Probe { id, label, parts })
    }

    #[test]
    fn empty_label_is_rejected() {
        assert_eq!(
            Probe::new(1, String::new(), vec![]),
            Err(ProbeError::EmptyLabel)
        );
    }

    #[test]
    fn digest_is_pinned() {
        // Pinned against an independent FNV-1a in Python
        // (tests/scaffold/test_probe.py), so the two sides cannot drift.
        let p = Probe::new(7, "seven".into(), vec!["a".into(), "b".into()]).unwrap();
        assert_eq!(p.digest(), 0xf5c4_fe25_5f72_1547);
    }

    proptest! {
        #[test]
        fn new_preserves_fields(p in probe()) {
            let q = Probe::new(p.id, p.label.clone(), p.parts.clone()).unwrap();
            prop_assert_eq!(&p, &q);
            prop_assert_eq!(p.digest(), q.digest());
        }

        #[test]
        fn field_boundaries_change_digest(p in probe()) {
            // Moving the label/parts boundary must change the digest: "ab"+[]
            // and "a"+["b"] are different probes.
            prop_assume!(p.label.chars().count() > 1);
            let split = p.label.char_indices().nth(1).unwrap().0;
            let mut parts = vec![p.label[split..].to_string()];
            parts.extend(p.parts.iter().cloned());
            let q = Probe { id: p.id, label: p.label[..split].to_string(), parts };
            prop_assert_ne!(p.digest(), q.digest());
        }
    }
}
