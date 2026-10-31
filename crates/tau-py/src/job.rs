//! What the driver is handed (`Job`) and what it answers with (`Answer`), with the
//! types they carry. A driver reads jobs and builds answers; nothing here decides.

use pyo3::prelude::*;
use tau_core::step;

use crate::values::{Author, Body, Outcome, Part, Stop, debug_repr};

/// Python view of [`step::Hook`]: the seven cursor hook points.
#[pyclass(frozen, eq, hash, from_py_object, module = "tau._core")]
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

#[pymethods]
impl Hook {
    /// The name an extension defines the hook by.
    #[getter]
    #[allow(clippy::trivially_copy_pass_by_ref)] // pyo3 cannot move `self` out of a Python object
    fn name(&self) -> &'static str {
        let point = match self {
            Self::PreStep => step::Hook::PreStep,
            Self::PreInput => step::Hook::PreInput,
            Self::PreSample => step::Hook::PreSample,
            Self::PreReply => step::Hook::PreReply,
            Self::PostReply => step::Hook::PostReply,
            Self::PostResult => step::Hook::PostResult,
            Self::PostStep => step::Hook::PostStep,
        };
        point.name()
    }
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
#[pyclass(frozen, eq, hash, from_py_object, module = "tau._core")]
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
#[pyclass(frozen, eq, hash, get_all, from_py_object, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Draft {
    body: Body,
    delivers: Vec<u64>,
    by: Author,
}

#[pymethods]
impl Draft {
    #[new]
    fn new(body: Body, delivers: Vec<u64>, by: Author) -> Self {
        Self { body, delivers, by }
    }

    fn __repr__(&self) -> String {
        format!("{self:?}")
    }
}

debug_repr!(RunCall, Ending, Job, Answer, SampleEnd);

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
#[pyclass(frozen, eq, hash, get_all, from_py_object, module = "tau._core")]
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
#[pyclass(frozen, eq, hash, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Job {
    Hooks {
        point: Hook,
        draft: Option<Draft>,
        node: Option<u64>,
        ending: Option<Ending>,
    },
    Sample {},
    RunCalls {
        node: u64,
        calls: Vec<RunCall>,
    },
    Pending {},
    Rest {},
}

impl From<step::Job> for Job {
    fn from(j: step::Job) -> Self {
        match j {
            step::Job::Hooks {
                point,
                draft,
                node,
                ending,
            } => Self::Hooks {
                point: point.into(),
                draft: draft.map(Into::into),
                node,
                ending: ending.map(Into::into),
            },
            step::Job::Sample => Self::Sample {},
            step::Job::RunCalls { node, calls } => Self::RunCalls {
                node,
                calls: calls.into_iter().map(Into::into).collect(),
            },
            step::Job::Pending => Self::Pending {},
            step::Job::Rest => Self::Rest {},
        }
    }
}

/// Python view of [`step::SampleEnd`]: how a sample ended.
#[pyclass(frozen, eq, hash, from_py_object, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum SampleEnd {
    #[pyo3(constructor = (stop, reason = None, meta = None))]
    Finished {
        stop: Stop,
        reason: Option<String>,
        meta: Option<String>,
    },
    #[pyo3(constructor = (error, meta = None))]
    Failed {
        error: String,
        meta: Option<String>,
    },
    Cancelled {},
}

impl From<SampleEnd> for step::SampleEnd {
    fn from(e: SampleEnd) -> Self {
        match e {
            SampleEnd::Finished { stop, reason, meta } => Self::Finished {
                stop: stop.into(),
                reason,
                meta,
            },
            SampleEnd::Failed { error, meta } => Self::Failed { error, meta },
            SampleEnd::Cancelled {} => Self::Cancelled,
        }
    }
}

/// Python view of [`step::Answer`].
#[pyclass(frozen, eq, hash, from_py_object, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Answer {
    #[pyo3(constructor = (stop = None, draft = None))]
    Hooks {
        stop: Option<String>,
        draft: Option<Draft>,
    },
    HookFailed {
        error: String,
    },
    Sampled {
        parts: Vec<Part>,
        end: SampleEnd,
    },
    CallDone {
        call: String,
        outcome: Outcome,
        by: Author,
    },
}

impl From<Answer> for step::Answer {
    fn from(f: Answer) -> Self {
        match f {
            Answer::Hooks { stop, draft } => Self::Hooks {
                stop,
                draft: draft.map(Into::into),
            },
            Answer::HookFailed { error } => Self::HookFailed(error),
            Answer::Sampled { parts, end } => Self::Sampled {
                parts: parts.into_iter().map(Into::into).collect(),
                end: end.into(),
            },
            Answer::CallDone { call, outcome, by } => Self::CallDone {
                call,
                outcome: outcome.into(),
                by: by.into(),
            },
        }
    }
}
