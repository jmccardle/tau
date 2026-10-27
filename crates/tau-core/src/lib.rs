//! τ core: pure Rust, sans-IO. `tree` is the session tree, `step` one
//! cursor's step, `machine` the session's cursors and frontend calls.

pub mod machine;
pub mod scaffold;
pub mod step;
pub mod tree;
