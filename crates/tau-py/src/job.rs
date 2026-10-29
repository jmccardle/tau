//! What the driver is handed (`Job`) and what it feeds back (`Fed`), with the
//! types they carry. A driver reads jobs and builds feeds; nothing here decides.

use pyo3::prelude::*;
use tau_core::step;

use crate::values::{Author, Body, Outcome, debug_repr};

/// Python view of [`step::Hook`]: the seven cursor hook points.
#[pyclass(frozen, eq, eq_int, hash, from_py_object, module = "tau_next._core")]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Hook {
    PreStep,
    PreInput,
    PreSample,
    PreReply,
    PostReply,
    PostResult,
    PostStep,
}

impl From<step::Hook> for Hook {
    fn from(h: step::Hook) -> Self {
        match h {
            step::Hook::PreStep => Self::PreStep,
            step::Hook::PreInput => Self::PreInput,
            step::Hook::PreSample => Self::PreSample,
            step::Hook::PreReply => Self::PreReply,
            step::Hook::PostReply => Self::PostReply,
            step::Hook::PostResult => Self::PostResult,
            step::Hook::PostStep => Self::PostStep,
        }
    }
}

/// Python view of [`step::Ending`].
#[pyclass(frozen, eq, hash, from_py_object, module = "tau_next._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Ending {
    Done {},
    Halted { reason: String },
    Failed { error: String },
    Aborted {},
}

impl From<step::Ending> for Ending {
    fn from(e: step::Ending) -> Self {
        match e {
            step::Ending::Done => Self::Done {},
            step::Ending::Halted(reason) => Self::Halted { reason },
            step::Ending::Failed(error) => Self::Failed { error },
            step::Ending::Aborted => Self::Aborted {},
        }
    }
}

/// Python view of [`step::Draft`].
#[pyclass(frozen, eq, hash, get_all, from_py_object, module = "tau_next._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Draft {
    body: Body,
    delivers: Vec<u64>,
    by: Author,
}

#[pymethods]
impl Draft {
    #[new]
    fn new(body: Body, by: Author, delivers: Vec<u64>) -> Self {
        Self { body, delivers, by }
    }

    fn __repr__(&self) -> String {
        format!("{self:?}")
    }
}

debug_repr!(RunCall, Ending, Job, Fed);

impl From<step::Draft> for Draft {
    fn from(d: step::Draft) -> Self {
        Self {
            body: d.body.into(),
            delivers: d.delivers,
            by: d.by.into(),
        }
    }
}

impl From<Draft> for step::Draft {
    fn from(d: Draft) -> Self {
        Self {
            body: d.body.into(),
            delivers: d.delivers,
            by: d.by.into(),
        }
    }
}

/// Python view of [`step::RunCall`].
#[pyclass(frozen, eq, hash, get_all, from_py_object, module = "tau_next._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct RunCall {
    id: String,
    tool: String,
    args_raw: String,
}

impl From<step::RunCall> for RunCall {
    fn from(c: step::RunCall) -> Self {
        Self {
            id: c.id,
            tool: c.tool,
            args_raw: c.args_raw,
        }
    }
}

/// Python view of [`step::Job`].
#[pyclass(frozen, eq, hash, module = "tau_next._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Job {
    Hooks {
        point: Hook,
        draft: Option<Draft>,
        ending: Option<Ending>,
        serial: u64,
    },
    Sample {},
    RunCalls {
        node: u64,
        calls: Vec<RunCall>,
    },
    Pending {},
    Wait {
        until: u64,
    },
    Rest {},
}

impl From<step::Job> for Job {
    fn from(j: step::Job) -> Self {
        match j {
            step::Job::Hooks {
                point,
                draft,
                ending,
                serial,
            } => Self::Hooks {
                point: point.into(),
                draft: draft.map(Into::into),
                ending: ending.map(Into::into),
                serial,
            },
            step::Job::Sample => Self::Sample {},
            step::Job::RunCalls { node, calls } => Self::RunCalls {
                node,
                calls: calls.into_iter().map(Into::into).collect(),
            },
            step::Job::Pending => Self::Pending {},
            step::Job::Wait { until } => Self::Wait { until },
            step::Job::Rest => Self::Rest {},
        }
    }
}

/// Python view of [`step::Fed`].
#[pyclass(frozen, eq, hash, from_py_object, module = "tau_next._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Fed {
    #[pyo3(constructor = (stop = None, draft = None))]
    Hooks {
        stop: Option<String>,
        draft: Option<Draft>,
    },
    HookFailed {
        error: String,
    },
    Sampled {
        reply: Option<Draft>,
        error: Option<String>,
    },
    CallDone {
        call: String,
        outcome: Outcome,
        by: Author,
    },
    Waited {},
}

impl From<Fed> for step::Fed {
    fn from(f: Fed) -> Self {
        match f {
            Fed::Hooks { stop, draft } => Self::Hooks {
                stop,
                draft: draft.map(Into::into),
            },
            Fed::HookFailed { error } => Self::HookFailed(error),
            Fed::Sampled { reply, error } => Self::Sampled {
                reply: reply.map(Into::into),
                error,
            },
            Fed::CallDone { call, outcome, by } => Self::CallDone {
                call,
                outcome: outcome.into(),
                by: by.into(),
            },
            Fed::Waited {} => Self::Waited,
        }
    }
}
